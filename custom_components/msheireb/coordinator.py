"""DataUpdateCoordinator + health/command tracking for Msheireb."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextvars
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .alerts import Alerts
from .api import (
    AUTH_FAILED,
    AUTH_OK,
    MsheirebApi,
    MsheirebAuthError,
    MsheirebConnectionError,
    MsheirebError,
    MsheirebNotFoundError,
)
from .const import (
    ALERT_AFTER,
    CMD_CONFIRMED,
    CMD_FAILED,
    CMD_NOT_CONFIRMED,
    CMD_SUPERSEDED,
    CMD_PENDING,
    CONF_MAX_RETRIES,
    CONF_SCAN_INTERVAL,
    CONFIRM_TIMEOUT,
    CONF_POWER_FAN_DELAY,
    CONF_POWER_SETTLE,
    DEFAULT_POWER_SETTLE,
    CONF_FAN_SETTLE,
    DEFAULT_FAN_SETTLE,
    DEFAULT_POWER_FAN_DELAY,
    FAN_VERIFY_DELAY,
    FAN_VERIFY_READS,
    POWER_CONFIRM_MAX,
    POWER_POLL_INTERVAL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_SCAN_INTERVAL,
    DEVICE_MAX_SETPOINT,
    DEVICE_MIN_SETPOINT,
    DOMAIN,
    FAN_ROLES,
    PULSE_VALUE,
    PULSES_BETWEEN_READS,
    REFRESH_AFTER_COMMAND,
    ROLE_POWER,
    ROLE_TEMP_DOWN,
    ROLE_TEMP_UP,
    TEMP_STEP,
)
from .models import HvacZone, parse_smart_home

_LOGGER = logging.getLogger(__name__)

KIND_TARGET = "target"
KIND_POWER = "power"
KIND_FAN = "fan"


@dataclass
class ContractData:
    contract: dict[str, Any]
    apartment_ip: str | None = None
    controller_connected: bool | None = None
    zones: dict[str, HvacZone] = field(default_factory=dict)
    lock: dict[str, Any] | None = None
    raw_smart_home: dict[str, Any] | None = None

    @property
    def contract_id(self) -> int:
        return int(self.contract["id"])

    @property
    def title(self) -> str:
        c = self.contract
        return str(c.get("unitId") or c.get("unitName") or c.get("externalId") or c["id"])


MsheirebData = dict[int, ContractData]


class SequenceSuperseded(Exception):
    """A newer action for the same room replaced the running sequence."""


@dataclass
class RoomSequence:
    """One background press/settle/verify sequence for a room (the latest action wins)."""

    zone_key: str
    task: asyncio.Task | None = None
    started: bool = False  # holds the apartment queue: only stopped at safe points (before a press)
    cancelled: bool = False


class _RecGuard:
    """Lets a running retry stop at the next press once its command was superseded."""

    def __init__(self, rec: CommandRecord) -> None:
        self.rec = rec

    @property
    def cancelled(self) -> bool:
        return self.rec.result == CMD_SUPERSEDED


# the sequence (or retry) the current task belongs to; checked before every press
_SEQ: contextvars.ContextVar[Any] = contextvars.ContextVar("msheireb_sequence", default=None)


@dataclass
class CommandRecord:
    zone_key: str
    room: str
    description: str
    expected: dict[str, Any]
    pulses: list[dict[str, Any]]
    started_at: datetime
    sent_monotonic: float
    confirm_timeout: float = CONFIRM_TIMEOUT
    kind: str | None = None
    value: Any = None
    spacing: float = 0.0
    contract_id: int | None = None
    first_monotonic: float | None = None
    retries: int = 0
    retrying: bool = False
    result: str = CMD_PENDING
    confirmed_after_s: float | None = None
    error: str | None = None
    fan_matches: int = 0  # consecutive reads (>= FAN_VERIFY_DELAY apart) showing the expected fan
    last_fan_press: float | None = None  # monotonic
    last_fan_match: float | None = None  # monotonic time of the last counted matching fan read

    def as_attributes(self) -> dict[str, Any]:
        return {
            "command": self.description,
            "expected": self.expected,
            "pulses_sent": len(self.pulses),
            "sent": [f"{p['label']} (sn {p['sn']})" for p in self.pulses][:30],
            "sent_at": self.started_at.isoformat(),
            "confirm_timeout_s": round(self.confirm_timeout, 1),
            "retries": self.retries,
            "confirmed_after_s": self.confirmed_after_s,
            "fan_verified_reads": self.fan_matches,
            "error": self.error,
        }


@dataclass
class Health:
    auth_status: str = AUTH_OK
    last_success: datetime | None = None
    last_error: str | None = None
    last_error_type: str | None = None
    last_error_at: datetime | None = None
    portal_reachable: bool | None = None
    unreachable_since: float | None = None  # monotonic
    consecutive_failures: int = 0
    commands_sent: int = 0
    commands_confirmed: int = 0
    commands_failed: int = 0
    command_retries: int = 0
    controller_down_since: dict[int, float] = field(default_factory=dict)  # monotonic
    commands: dict[str, CommandRecord] = field(default_factory=dict)  # zone_key -> last


class MsheirebCoordinator(DataUpdateCoordinator[MsheirebData]):
    """Polls smart-home + controller-status for every contract and tracks health."""

    config_entry: ConfigEntry
    wait_for_sequences = False  # tests only: climate service calls wait for their sequence

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: MsheirebApi) -> None:
        interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.api = api
        self.health = Health()
        self.alerts = Alerts(hass, entry)
        self._health_listeners: list[CALLBACK_TYPE] = []
        self._command_locks: dict[int, asyncio.Lock] = {}
        self._cancel_refresh: CALLBACK_TYPE | None = None
        self._cancel_confirm: dict[str, CALLBACK_TYPE] = {}
        self._last_fan_press: dict[str, float] = {}
        self._sequences: dict[str, RoomSequence] = {}
        self._offset_listeners: dict[str, list[Callable[[float, float], Awaitable[None]]]] = {}
        self.last_unlock: dict[int, dict[str, Any]] = {}  # contract_id -> {result, at, message}
        from .drift import DriftManager

        self.drift = DriftManager(hass, self)
        from .door import DoorManager

        self.door = DoorManager(self)

    # ---------------------------------------------------------------- health listeners
    @callback
    def async_add_health_listener(self, cb: CALLBACK_TYPE) -> Callable[[], None]:
        self._health_listeners.append(cb)

        def _remove() -> None:
            if cb in self._health_listeners:
                self._health_listeners.remove(cb)

        return _remove

    @callback
    def notify_health(self) -> None:
        for cb in list(self._health_listeners):
            cb()

    @callback
    def on_auth_status(self, status: str) -> None:
        """Called by the API client when its auth state changes."""
        self.health.auth_status = status
        self.notify_health()

    @property
    def token_expires(self) -> datetime | None:
        exp = getattr(self.api, "expires_at", None)
        return dt_util.utc_from_timestamp(exp) if exp else None

    @property
    def last_response_ms(self) -> float | None:
        return getattr(self.api, "last_response_ms", None)

    def command_lock(self, contract_id: int) -> asyncio.Lock:
        return self._command_locks.setdefault(contract_id, asyncio.Lock())

    # ---------------------------------------------------------------- polling
    async def _async_update_data(self) -> MsheirebData:
        try:
            contracts = await self.api.async_get_contracts()
            result: MsheirebData = {}
            for contract in contracts:
                cd = await self._fetch_contract(contract)
                if cd is not None:
                    result[cd.contract_id] = cd
            _LOGGER.debug(
                "Poll: %d contract(s); %s",
                len(result),
                ", ".join(
                    f"contract {cid}: {len((cd.raw_smart_home or {}).get('rooms') or [])} room(s), "
                    f"{len(cd.zones)} HVAC zone(s), controller={cd.controller_connected}"
                    for cid, cd in result.items()
                ) or "nothing to poll",
            )
            if not result:
                _LOGGER.warning("No Msheireb contracts found for this account; no room entities will be created")
            for cid, cd in result.items():
                if cd.raw_smart_home is not None and not cd.zones:
                    _LOGGER.warning(
                        "Contract %s returned %d room(s) but no HVAC devices were recognised (labels: %s)",
                        cid,
                        len(cd.raw_smart_home.get("rooms") or []),
                        sorted({c.get("label") for r in cd.raw_smart_home.get("rooms") or []
                                for d in r.get("devices") or [] for c in d.get("controls") or []})[:20],
                    )
        except MsheirebAuthError as err:
            self._record_failure(err, reachable=True)
            self.health.auth_status = AUTH_FAILED
            self.alerts.raise_reauth_issue()
            self.alerts.raise_(
                "auth",
                "Msheireb: login failed",
                "The Msheireb portal rejected the saved login and automatic re-login failed. "
                "Open **Settings → Devices & services → Msheireb Smart Home** and re-authenticate.",
            )
            self.notify_health()
            raise ConfigEntryAuthFailed(str(err)) from err
        except MsheirebConnectionError as err:
            self._record_failure(err, reachable=False)
            self.notify_health()
            raise UpdateFailed(str(err)) from err
        except MsheirebError as err:
            self._record_failure(err, reachable=True)
            self.notify_health()
            raise UpdateFailed(str(err)) from err
        self._record_success(result)
        self.notify_health()
        return result

    async def _fetch_contract(self, contract: dict[str, Any]) -> ContractData | None:
        cid = int(contract["id"])
        previous = (self.data or {}).get(cid)
        cd = ContractData(contract=contract)
        try:
            smart_home = await self.api.async_get_smart_home(cid)
        except MsheirebNotFoundError:
            _LOGGER.debug("Contract %s has no smart-home configuration", cid)
            return cd
        cd.raw_smart_home = smart_home
        cd.apartment_ip = (smart_home.get("apartment_ip") or "").strip() or None
        cd.zones = parse_smart_home(cid, smart_home)
        self._log_fan(cd.zones.values())
        if cd.apartment_ip:
            try:
                status = await self.api.async_get_controller_status(cd.apartment_ip)
                cd.controller_connected = str(status.get("status", "")).lower() == "connected"
            except MsheirebAuthError:
                raise
            except MsheirebError as err:
                _LOGGER.debug("controller-status failed for %s: %s", cid, err)
                cd.controller_connected = previous.controller_connected if previous else None
        try:
            cd.lock = await self.api.async_get_lock_status(cid)
        except MsheirebAuthError:
            raise
        except MsheirebError:
            cd.lock = previous.lock if previous else None
        return cd

    def _record_failure(self, err: Exception, reachable: bool) -> None:
        h = self.health
        h.consecutive_failures += 1
        h.last_error = str(err)[:250] or type(err).__name__
        h.last_error_type = type(err).__name__
        h.last_error_at = dt_util.utcnow()
        h.portal_reachable = reachable
        now = time.monotonic()
        if reachable:
            h.unreachable_since = None
            self.alerts.clear("portal")
        else:
            if h.unreachable_since is None:
                h.unreachable_since = now
            if now - h.unreachable_since >= ALERT_AFTER:
                mins = int((now - h.unreachable_since) // 60)
                self.alerts.raise_(
                    "portal",
                    "Msheireb: portal unreachable",
                    f"The Msheireb portal API has been unreachable for about {mins} min. "
                    f"Last error: {h.last_error}",
                )

    def _record_success(self, data: MsheirebData) -> None:
        h = self.health
        h.consecutive_failures = 0
        h.last_success = dt_util.utcnow()
        h.portal_reachable = True
        h.unreachable_since = None
        h.auth_status = getattr(self.api, "auth_status", AUTH_OK) or AUTH_OK
        if h.auth_status == AUTH_FAILED:
            h.auth_status = AUTH_OK
        self.alerts.clear("portal")
        self.alerts.clear("auth")
        self.alerts.clear_reauth_issue()
        now = time.monotonic()
        for cid, cd in data.items():
            key = f"controller_{cid}"
            if cd.apartment_ip and cd.controller_connected is False:
                since = h.controller_down_since.setdefault(cid, now)
                if now - since >= ALERT_AFTER:
                    self.alerts.raise_(
                        key,
                        f"Msheireb: apartment controller offline ({cd.title})",
                        f"The apartment controller for {cd.title} has been disconnected for about "
                        f"{int((now - since) // 60)} min. AC control from Home Assistant is unavailable.",
                    )
            else:
                h.controller_down_since.pop(cid, None)
                self.alerts.clear(key)
        self._evaluate_commands(data)
        self.drift.evaluate(data)
        self.drift.evaluate_override(data)

    # ---------------------------------------------------------------- commands
    async def async_read_zone(self, zone: HvacZone) -> HvacZone | None:
        """Fresh read of one zone (used between temperature pulses)."""
        smart_home = await self.api.async_get_smart_home(zone.contract_id)
        fresh = parse_smart_home(zone.contract_id, smart_home).get(zone.key)
        if fresh is not None:
            self._log_fan([fresh])
        if fresh is not None and self.data and zone.contract_id in self.data:
            self.data[zone.contract_id].zones[zone.key] = fresh
        return fresh

    @staticmethod
    def _log_fan(zones: Any) -> None:
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        for z in zones:
            raw = z.fan_readings(FAN_ROLES)
            mode = z.fan_mode(FAN_ROLES)
            _LOGGER.debug("%s: power=%s fan readings=%s -> fan_mode=%s%s", z.room_name, z.power, raw, mode,
                          " (AMBIGUOUS)" if mode is None and any(raw.values()) else "")

    async def async_pulse(self, zone: HvacZone, role: str) -> dict[str, Any]:
        """Send a PULSE for a role of a zone (sn discovered from labels)."""
        guard = _SEQ.get()
        if guard is not None and guard.cancelled:
            raise SequenceSuperseded  # safe point: a newer action for this room takes over
        control = zone.controls.get(role)
        if control is None:
            raise MsheirebError(f"{zone.room_name}: no control for {role}")
        cd = (self.data or {}).get(zone.contract_id)
        if cd is None or not cd.apartment_ip:
            raise MsheirebError("apartment controller IP unknown")
        await self.api.async_send_command(
            cd.apartment_ip, control.sn, control.type_io, control.type_code, PULSE_VALUE
        )
        self.health.commands_sent += 1
        return {"sn": control.sn, "label": control.label, "type_code": control.type_code}

    @callback
    def track_command(
        self,
        zone: HvacZone,
        description: str,
        expected: dict[str, Any],
        pulses: list[dict[str, Any]],
        error: str | None = None,
        started_monotonic: float | None = None,
        spacing: float = 0.0,
        kind: str | None = None,
        value: Any = None,
        retryable: bool = True,
        extra_wait: float = 0.0,
    ) -> CommandRecord:
        """Track a command. Timeout scales with the presses: pulses x spacing + CONFIRM_TIMEOUT,
        measured from the first press."""
        now = time.monotonic()
        rec = CommandRecord(
            zone_key=zone.key,
            room=zone.room_name,
            description=description,
            expected=expected,
            pulses=pulses,
            started_at=dt_util.utcnow(),
            sent_monotonic=started_monotonic if started_monotonic is not None else now,
            confirm_timeout=len(pulses) * max(0.0, spacing) + CONFIRM_TIMEOUT + extra_wait
            + (self.fan_verify_window if KIND_FAN in expected else 0.0),
            kind="multi" if retryable else None,
            value=None,
            spacing=spacing,
            contract_id=zone.contract_id,
        )
        rec.first_monotonic = rec.sent_monotonic
        self.health.commands[zone.key] = rec
        if error is not None:
            rec.result = CMD_FAILED
            rec.error = error
            self.health.commands_failed += 1
            self._alert_command(rec)
        else:
            self._schedule_confirm_check(rec)
        self.notify_health()
        return rec

    @callback
    def _schedule_verify_refresh(self, rec: CommandRecord, delay: float) -> None:
        key = f"verify_{rec.zone_key}"
        if key in self._cancel_confirm:
            return

        @callback
        def _fire(_now: Any) -> None:
            self._cancel_confirm.pop(key, None)
            self.hass.async_create_task(self.async_refresh())

        self._cancel_confirm[key] = async_call_later(self.hass, max(0.5, delay), _fire)

    @callback
    def _schedule_confirm_check(self, rec: CommandRecord) -> None:
        key = rec.zone_key
        if key in self._cancel_confirm:
            self._cancel_confirm.pop(key)()

        @callback
        def _timeout(_now: Any) -> None:
            self._cancel_confirm.pop(key, None)
            self.hass.async_create_task(self.async_refresh())

        remaining = max(0.0, rec.sent_monotonic + rec.confirm_timeout - time.monotonic())
        self._cancel_confirm[key] = async_call_later(self.hass, remaining + 1, _timeout)

    @property
    def pulse_interval(self) -> float:
        from .const import CONF_PULSE_INTERVAL, DEFAULT_PULSE_INTERVAL

        return float(self.config_entry.options.get(CONF_PULSE_INTERVAL, DEFAULT_PULSE_INTERVAL))

    @callback
    def async_set_desired(self, zone_key: str, values: dict[str, Any]) -> None:
        self.drift.set_desired(zone_key, values)

    # ---------------------------------------------------------------- target offset
    def offset(self, zone_key: str) -> float:
        return self.drift.offset(zone_key)

    def device_target(self, zone_key: str, target: float) -> float:
        """The setpoint driven on the AC for the HA-facing target (target + offset, clamped to the
        AC's physical limits when those are known)."""
        value = float(target) + self.offset(zone_key)
        if DEVICE_MIN_SETPOINT is not None:
            value = max(DEVICE_MIN_SETPOINT, value)
        if DEVICE_MAX_SETPOINT is not None:
            value = min(DEVICE_MAX_SETPOINT, value)
        return round(round(value / TEMP_STEP) * TEMP_STEP, 2)

    def from_device(self, zone_key: str, setpoint: float) -> float:
        """The HA-facing target for a setpoint read from the AC (setpoint - offset)."""
        return round(float(setpoint) - self.offset(zone_key), 2)

    def to_device(self, zone_key: str, expected: dict[str, Any]) -> dict[str, Any]:
        """Copy of an HA-facing expected state with the target converted to the AC setpoint."""
        out = dict(expected)
        if out.get(KIND_TARGET) is not None:
            out[KIND_TARGET] = self.device_target(zone_key, out[KIND_TARGET])
        return out

    def zone_matches_target(self, zone: HvacZone, expected: dict[str, Any]) -> bool:
        """Does the AC match an HA-facing expected state (target compared as target + offset)?"""
        return self._zone_matches(zone, self.to_device(zone.key, expected))

    @callback
    def async_add_offset_listener(
        self, zone_key: str, cb: Callable[[float, float], Awaitable[None]]
    ) -> Callable[[], None]:
        self._offset_listeners.setdefault(zone_key, []).append(cb)

        def _remove() -> None:
            listeners = self._offset_listeners.get(zone_key, [])
            if cb in listeners:
                listeners.remove(cb)

        return _remove

    async def async_set_offset(self, zone_key: str, value: float) -> float:
        """Store a room's target offset; the room's climate entity re-drives the AC if needed."""
        old = self.offset(zone_key)
        new = self.drift.set_offset(zone_key, value)
        if new != old:
            _LOGGER.debug("%s: target offset %+.1f -> %+.1f °C", zone_key, old, new)
            for cb in list(self._offset_listeners.get(zone_key, [])):
                await cb(old, new)
        self.notify_health()
        return new

    @property
    def max_retries(self) -> int:
        return int(self.config_entry.options.get(CONF_MAX_RETRIES, DEFAULT_MAX_RETRIES))

    # ---------------------------------------------------------------- execution
    async def _execute(
        self, zone: HvacZone, kind: str, value: Any, spacing: float, sent: list[dict[str, Any]]
    ) -> None:
        """Send only the pulses needed to move `zone` from its current state to `value`."""
        if kind == KIND_POWER:
            if zone.power is not value:  # never blindly toggle
                sent.append(await self.async_pulse(zone, ROLE_POWER))
            return
        if kind == KIND_FAN:
            if zone.fan_mode(FAN_ROLES) != value:
                sent.append(await self.async_pulse(zone, value))
            return
        if kind != KIND_TARGET:
            raise MsheirebError(f"unknown command kind {kind}")
        if zone.setpoint is None:
            raise MsheirebError(f"{zone.room_name}: current setpoint unknown")
        needed = round((value - zone.setpoint) / TEMP_STEP)
        if needed == 0:
            return
        role = ROLE_TEMP_UP if needed > 0 else ROLE_TEMP_DOWN
        cap = abs(needed)  # never send more pulses than needed for this attempt
        remaining = cap
        count = 0
        while remaining > 0 and count < cap:
            t_start = time.monotonic()
            sent.append(await self.async_pulse(zone, role))
            count += 1
            remaining -= 1
            if remaining <= 0:
                break
            # spacing measured start-to-start (request time counts towards it)
            await asyncio.sleep(max(0.0, spacing - (time.monotonic() - t_start)))
            if count % PULSES_BETWEEN_READS == 0:
                fresh = await self.async_read_zone(zone)
                if fresh is not None and fresh.setpoint is not None:
                    zone = fresh
                    left = round((value - fresh.setpoint) / TEMP_STEP)
                    if left == 0 or (left > 0) != (needed > 0):
                        break  # reached or overshot
                    remaining = min(abs(left), cap - count)

    async def _execute_all(
        self, zone: HvacZone, expected: dict[str, Any], spacing: float, sent: list[dict[str, Any]],
        fan_if_differs: bool = False,
    ) -> float:
        """Apply several targets in a safe order: power first, then setpoint, then fan.

        After a power press, the remaining presses wait until the reported power has changed
        (read every POWER_POLL_INTERVAL s, up to POWER_CONFIRM_MAX s) plus the 'Power -> fan
        delay'; if the power never changes, nothing else is pressed (confirm/retry handles it).
        Returns the seconds spent waiting (added to the confirmation window).
        """
        waited = 0.0
        if KIND_POWER in expected:
            before = len(sent)
            await self._execute(zone, KIND_POWER, expected[KIND_POWER], spacing, sent)
            if len(sent) > before:
                t0 = time.monotonic()
                fresh = await self._wait_for_power(zone, expected[KIND_POWER])
                if fresh is not None and fan_if_differs and KIND_FAN in expected and KIND_TARGET not in expected \
                        and fresh.fan_mode(FAN_ROLES) == expected[KIND_FAN]:
                    expected.pop(KIND_FAN)  # actual speed already right: no delay, no fan press
                    _LOGGER.debug("%s: fan already %s after power change; not pressing", zone.room_name,
                                  fresh.fan_mode(FAN_ROLES))
                    return time.monotonic() - t0
                if fresh is None:
                    _LOGGER.debug("%s: power did not change within %.0f s; nothing else pressed",
                                  zone.room_name, self.power_settle + POWER_CONFIRM_MAX)
                    return time.monotonic() - t0
                if not any(k in expected for k in (KIND_TARGET, KIND_FAN)):
                    return time.monotonic() - t0
                delay = self.power_fan_delay
                _LOGGER.debug("%s: power confirmed %s; waiting %.1f s before the next press",
                              zone.room_name, "on" if expected[KIND_POWER] else "off", delay)
                await asyncio.sleep(delay)
                zone = await self.async_read_zone(zone) or fresh
                waited = time.monotonic() - t0
        if fan_if_differs and KIND_FAN in expected and not sent and zone.fan_mode(FAN_ROLES) == expected[KIND_FAN]:
            expected.pop(KIND_FAN)
        for kind in (KIND_TARGET, KIND_FAN):
            if kind in expected:
                if sent and waited == 0.0:
                    await asyncio.sleep(max(0.0, spacing))  # keep presses spaced (AC misses fast presses)
                    zone = await self.async_read_zone(zone) or zone
                before = len(sent)
                await self._execute(zone, kind, expected[kind], spacing, sent)
                if kind == KIND_FAN and len(sent) > before:
                    self._last_fan_press[zone.key] = time.monotonic()
        return waited

    async def _wait_for_power(self, zone: HvacZone, want: bool) -> HvacZone | None:
        """Wait the power settle time, then read every POWER_POLL_INTERVAL s until the power matches."""
        settle = self.power_settle
        _LOGGER.debug("%s: power pressed; settling %.0f s before checking", zone.room_name, settle)
        await asyncio.sleep(settle)
        deadline = time.monotonic() + POWER_CONFIRM_MAX
        while True:
            fresh = await self.async_read_zone(zone)
            if fresh is not None and fresh.power is want:
                return fresh
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(POWER_POLL_INTERVAL)

    @property
    def power_settle(self) -> float:
        return float(self.config_entry.options.get(CONF_POWER_SETTLE, DEFAULT_POWER_SETTLE))

    @property
    def fan_settle(self) -> float:
        return float(self.config_entry.options.get(CONF_FAN_SETTLE, DEFAULT_FAN_SETTLE))

    @property
    def fan_verify_window(self) -> float:
        """Extra confirm time for a fan change: settle before the 1st read + gap to the 2nd."""
        return self.fan_settle + FAN_VERIFY_DELAY * max(1, FAN_VERIFY_READS - 1)

    @property
    def power_fan_delay(self) -> float:
        return float(self.config_entry.options.get(CONF_POWER_FAN_DELAY, DEFAULT_POWER_FAN_DELAY))

    async def async_command(
        self,
        zone: HvacZone,
        expected: dict[str, Any],
        description: str,
        spacing: float,
        set_desired: bool = True,
        fan_if_differs: bool = False,
    ) -> list[dict[str, Any]]:
        """Execute + track a command. Caller holds the contract command lock.

        `expected` is HA-facing: its target is driven on the AC as target + the room's offset."""
        if set_desired:
            self.async_set_desired(zone.key, expected)
        if expected.get(KIND_TARGET) is not None:
            dev = self.device_target(zone.key, expected[KIND_TARGET])
            if abs(dev - float(expected[KIND_TARGET])) > 0.01:
                description = f"{description} [AC setpoint {dev:.1f}, offset {self.offset(zone.key):+.1f}]"
        expected = self.to_device(zone.key, expected)
        sent: list[dict[str, Any]] = []
        started = time.monotonic()
        self._last_fan_press.pop(zone.key, None)
        try:
            expected = dict(expected)
            waited = await self._execute_all(zone, expected, spacing, sent, fan_if_differs)
        except MsheirebError as err:
            self.track_command(zone, description, dict(expected), sent, error=str(err),
                               started_monotonic=started, spacing=spacing)
            raise
        if sent:
            rec = self.track_command(zone, description, dict(expected), sent,
                                     started_monotonic=started, spacing=spacing, extra_wait=waited)
            rec.last_fan_press = self._last_fan_press.get(zone.key)
        return sent

    async def _async_retry(self, rec: CommandRecord) -> None:
        """Re-read the actual state and send only what is still needed."""
        _SEQ.set(_RecGuard(rec))
        try:
            async with self.command_lock(rec.contract_id or 0):
                if self.health.commands.get(rec.zone_key) is not rec or rec.result != CMD_PENDING:
                    return  # superseded by a newer command
                cd = (self.data or {}).get(rec.contract_id)
                zone = cd.zones.get(rec.zone_key) if cd else None
                if zone is None:
                    raise MsheirebError("zone no longer available")
                fresh = await self.async_read_zone(zone) or zone
                if self._zone_matches(fresh, rec.expected):
                    if KIND_FAN not in rec.expected or rec.fan_matches + 1 >= FAN_VERIFY_READS:
                        self._confirm(rec)
                        return
                    # matches now but not verified twice yet: verify again instead of re-pressing
                    rec.fan_matches = 1
                    rec.last_fan_match = time.monotonic()
                    rec.sent_monotonic = time.monotonic()
                    rec.confirm_timeout = CONFIRM_TIMEOUT + FAN_VERIFY_DELAY
                    self._schedule_verify_refresh(rec, FAN_VERIFY_DELAY)
                    self._schedule_confirm_check(rec)
                    return
                rec.retries += 1
                self.health.command_retries += 1
                new: list[dict[str, Any]] = []
                started = time.monotonic()
                waited = 0.0
                self._last_fan_press.pop(rec.zone_key, None)
                try:
                    waited = await self._execute_all(fresh, rec.expected, rec.spacing, new)
                finally:
                    rec.pulses.extend(new)
                    if rec.zone_key in self._last_fan_press:
                        rec.last_fan_press = self._last_fan_press[rec.zone_key]
                        rec.fan_matches = 0
                        rec.last_fan_match = None
                if not new:
                    # nothing to send, yet state differs (e.g. unknown setpoint): treat as final
                    self._fail(rec, None)
                    return
                rec.sent_monotonic = started
                rec.confirm_timeout = (len(new) * max(0.0, rec.spacing) + CONFIRM_TIMEOUT + waited
                                       + (self.fan_verify_window if KIND_FAN in rec.expected else 0.0))
                _LOGGER.debug("Retry %s for %s: sent %s", rec.retries, rec.room, [p["label"] for p in new])
                self._schedule_confirm_check(rec)
        except SequenceSuperseded:
            _LOGGER.debug("Retry for %s stopped: superseded by a newer action", rec.room)
        except MsheirebError as err:
            rec.result = CMD_FAILED
            rec.error = str(err)
            self.health.commands_failed += 1
            self._alert_command(rec)
        finally:
            rec.retrying = False
            self.notify_health()
        self.schedule_refresh_after_command()

    def _confirm(self, rec: CommandRecord) -> None:
        rec.result = CMD_CONFIRMED
        rec.confirmed_after_s = round(time.monotonic() - (rec.first_monotonic or rec.sent_monotonic), 1)
        self.health.commands_confirmed += 1
        self.alerts.clear(f"command_{rec.zone_key}")
        for key in (rec.zone_key, f"verify_{rec.zone_key}"):
            if key in self._cancel_confirm:
                self._cancel_confirm.pop(key)()

    def _fail(self, rec: CommandRecord, error: str | None) -> None:
        # don't let drift-restore immediately repeat a command that just failed
        self.drift.mark_handled(rec.zone_key)
        rec.result = CMD_NOT_CONFIRMED
        rec.error = error
        self.health.commands_failed += 1
        self._alert_command(rec)

    @staticmethod
    def _zone_matches(zone: HvacZone, expected: dict[str, Any]) -> bool:
        for name, value in expected.items():
            if name == "target":
                if zone.setpoint is None or abs(zone.setpoint - value) > 0.01:
                    return False
            elif name == "power":
                if zone.power is not value:
                    return False
            elif name == "fan":
                if zone.fan_mode(FAN_ROLES) != value:
                    return False
        return True

    def _evaluate_commands(self, data: MsheirebData) -> None:
        now = time.monotonic()
        zones = {k: z for cd in data.values() for k, z in cd.zones.items()}
        for key, rec in self.health.commands.items():
            if rec.result != CMD_PENDING or rec.retrying:
                continue
            zone = zones.get(key)
            if zone is not None and self._zone_matches(zone, rec.expected):
                if KIND_FAN not in rec.expected:
                    self._confirm(rec)
                    continue
                # fan: the first counted read is taken >= the fan settle time after the press, the
                # next one >= FAN_VERIFY_DELAY after that (a controller can echo a press the AC then
                # drops, e.g. while starting); earlier reads are ignored, not counted
                settle = self.fan_settle
                if rec.last_fan_press is not None and now - rec.last_fan_press < settle - 0.5:
                    self._schedule_verify_refresh(rec, settle - (now - rec.last_fan_press))
                    continue
                if rec.fan_matches and rec.last_fan_match is not None \
                        and now - rec.last_fan_match < FAN_VERIFY_DELAY - 0.5:
                    self._schedule_verify_refresh(rec, FAN_VERIFY_DELAY - (now - rec.last_fan_match))
                    continue
                rec.fan_matches += 1
                rec.last_fan_match = now
                if rec.fan_matches >= FAN_VERIFY_READS:
                    self._confirm(rec)
                else:
                    self._schedule_verify_refresh(rec, FAN_VERIFY_DELAY)
                continue
            if zone is not None and KIND_FAN in rec.expected:
                rec.fan_matches = 0
                rec.last_fan_match = None
            if now - rec.sent_monotonic >= rec.confirm_timeout:
                if rec.kind and rec.retries < self.max_retries:
                    rec.retrying = True
                    self.config_entry.async_create_task(
                        self.hass, self._async_retry(rec), f"{DOMAIN}_retry_{key}"
                    )
                else:
                    self._fail(rec, None)

    def _alert_command(self, rec: CommandRecord) -> None:
        detail = (
            f"error: {rec.error}"
            if rec.error
            else f"not confirmed within {int(rec.confirm_timeout)} s after {rec.retries} retr{'y' if rec.retries == 1 else 'ies'}"
        )
        self.alerts.raise_(
            f"command_{rec.zone_key}",
            f"Msheireb: AC command not applied ({rec.room})",
            f"`{rec.description}` for **{rec.room}** was {detail}. "
            f"Expected {rec.expected}; the AC may not have registered every press.",
        )

    # ---------------------------------------------------------------- room sequences
    def room_busy(self, zone_key: str) -> bool:
        """A sequence (incl. debounce / waiting in the queue) or its confirm/retry is in flight."""
        seq = self._sequences.get(zone_key)
        if seq is not None and seq.task is not None and not seq.task.done():
            return True
        rec = self.health.commands.get(zone_key)
        return rec is not None and (rec.result == CMD_PENDING or rec.retrying)

    @callback
    def supersede(self, zone_key: str) -> bool:
        """Stop the room's running sequence/command in favour of a newer action. True if one was."""
        found = False
        seq = self._sequences.pop(zone_key, None)
        if seq is not None and seq.task is not None and not seq.task.done():
            found = True
            seq.cancelled = True
            if not seq.started:
                seq.task.cancel()  # still debouncing or queued: nothing pressed yet
        rec = self.health.commands.get(zone_key)
        if rec is not None and (rec.result == CMD_PENDING or rec.retrying):
            found = True
            rec.result = CMD_SUPERSEDED
            for key in (zone_key, f"verify_{zone_key}"):
                if key in self._cancel_confirm:
                    self._cancel_confirm.pop(key)()
            self.notify_health()
        return found

    @callback
    def start_sequence(
        self, zone_key: str, body: Callable[[RoomSequence], Awaitable[None]], debounce: float = 0.0,
    ) -> RoomSequence:
        """Run `body` as a background task through the apartment queue; a newer call for the same
        room supersedes it (latest wins)."""
        self.supersede(zone_key)
        seq = RoomSequence(zone_key)

        async def _run() -> None:
            _SEQ.set(seq)
            try:
                if debounce > 0:
                    await asyncio.sleep(debounce)
                await body(seq)
            except SequenceSuperseded:
                _LOGGER.debug("Sequence for %s superseded by a newer action", zone_key)
            except asyncio.CancelledError:
                if not seq.cancelled:
                    raise
            finally:
                if self._sequences.get(zone_key) is seq:
                    self._sequences.pop(zone_key)
                self.async_update_listeners()

        self._sequences[zone_key] = seq
        seq.task = self._spawn(_run(), f"{DOMAIN}_sequence_{zone_key}")
        return seq

    def _spawn(self, coro: Any, name: str) -> asyncio.Task:
        return self.config_entry.async_create_background_task(self.hass, coro, name, eager_start=False)

    # ---------------------------------------------------------------- scheduling
    @callback
    def schedule_refresh_after_command(self, delay: float = REFRESH_AFTER_COMMAND) -> None:
        if self._cancel_refresh:
            self._cancel_refresh()

        @callback
        def _fire(_now: Any) -> None:
            self._cancel_refresh = None
            self.hass.async_create_task(self.async_refresh())

        self._cancel_refresh = async_call_later(self.hass, delay, _fire)

    @callback
    def async_cancel_pending(self) -> None:
        self.door.async_cancel()
        if self._cancel_refresh:
            self._cancel_refresh()
            self._cancel_refresh = None
        for cancel in self._cancel_confirm.values():
            cancel()
        self._cancel_confirm.clear()
