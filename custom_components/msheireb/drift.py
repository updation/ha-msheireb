"""Desired-state persistence and drift (external change) detection/restore."""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ADOPT_EXTERNAL,
    CONF_OVERRIDE_SENSORS,
    CONF_DRIFT_GRACE,
    CONF_EXTERNAL_CHANGE,
    DEFAULT_ADOPT_EXTERNAL,
    DEFAULT_DRIFT_GRACE,
    DEFAULT_EXTERNAL_CHANGE,
    DOMAIN,
    DRIFT_MIN_POLLS,
    EXT_IGNORE,
    EXT_NOTIFY,
    EXT_RESTORE,
    EXT_RESTORE_NOTIFY,
    FAN_ROLES,
    CONF_MIN_TEMP,
    DEFAULT_MIN_TEMP,
    OFFSET_MAX,
    OFFSET_MIN,
    OFFSET_STEP,
    OVERRIDE_HYSTERESIS,
    OVERRIDE_MIN_TOGGLE_INTERVAL,
    ROLE_FAN_AUTO,
    STORE_VERSION,
)

if TYPE_CHECKING:
    from .coordinator import MsheirebCoordinator, MsheirebData
    from .models import HvacZone

_LOGGER = logging.getLogger(__name__)

KEYS_ORDER = ("power", "target", "fan")


FAN_OFF = "off"  # the climate fan mode 'off' (= room off); never stored as a fan speed


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.desired"


def actual_values(zone: HvacZone) -> dict[str, Any]:
    fan = zone.fan_mode(FAN_ROLES)
    if fan == FAN_OFF:
        fan = None  # 'off' is not a speed reading: never a fan-speed drift
    return {"power": zone.power, "target": zone.setpoint, "fan": fan}


def _differs(kind: str, want: Any, have: Any) -> bool:
    if have is None or want is None:
        return False  # unknown actual: never treat as drift
    if kind == "target":
        return abs(float(want) - float(have)) > 0.01
    return want != have


def clamp_offset(value: Any) -> float:
    """Bound a target offset to OFFSET_MIN..OFFSET_MAX in OFFSET_STEP steps."""
    v = round(float(value) / OFFSET_STEP) * OFFSET_STEP
    return round(min(OFFSET_MAX, max(OFFSET_MIN, v)), 1) + 0.0  # + 0.0: no -0.0


def describe(kind: str, value: Any) -> str:
    if kind == "target":
        return f"{float(value):.1f} °C"
    if kind == "power":
        return "on" if value else "off"
    return str(value)


LABELS = {"target": "setpoint", "power": "power", "fan": "fan"}


@dataclass
class Episode:
    since: float
    polls: int
    diff: dict[str, tuple[Any, Any]]  # kind -> (desired, actual)
    handled: bool = False
    restored: bool = False


@dataclass
class DriftEvent:
    room: str
    changes: dict[str, dict[str, Any]]
    action: str
    at: str


@dataclass
class DriftManager:
    hass: HomeAssistant
    coordinator: MsheirebCoordinator
    store: Store = field(init=False)
    desired: dict[str, dict[str, Any]] = field(default_factory=dict)
    auto_restore: dict[str, bool] = field(default_factory=dict)
    episodes: dict[str, Episode] = field(default_factory=dict)
    events: int = 0
    last_event: DriftEvent | None = None
    restoring: set[str] = field(default_factory=set)
    prev_fan: dict[str, str] = field(default_factory=dict)  # zone_key -> fan speed before HA turned it off
    # zone_key -> exact last fan mode chosen from HA (set_fan_mode / turn off), incl. 'off'
    last_fan: dict[str, str] = field(default_factory=dict)
    # zone_key -> target offset (°C): the AC is driven to (shown target + offset)
    offsets: dict[str, float] = field(default_factory=dict)
    # rooms whose offset changed while off: the new offset is applied on the next turn-on from HA
    offset_pending: set[str] = field(default_factory=set)
    # Max cooling override: per-room switch, rooms whose power the override has paused, last toggle time
    override: dict[str, bool] = field(default_factory=dict)
    override_paused: set[str] = field(default_factory=set)
    override_last_toggle: dict[str, float] = field(default_factory=dict)
    # rooms whose configured external sensor is currently unusable (AC reading used): warn once
    sensor_fallback: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.store = Store(self.hass, STORE_VERSION, store_key(self.coordinator.config_entry.entry_id))

    # ---------------------------------------------------------------- persistence
    async def async_load(self) -> None:
        data = await self.store.async_load() or {}
        self.desired = {k: dict(v) for k, v in (data.get("desired") or {}).items()}
        self.auto_restore = {k: bool(v) for k, v in (data.get("auto_restore") or {}).items()}
        self.prev_fan = {k: str(v) for k, v in (data.get("prev_fan") or {}).items() if v and v != FAN_OFF}
        self.last_fan = {k: str(v) for k, v in (data.get("last_fan") or {}).items() if v}
        for want in self.desired.values():
            if want.get("fan") == FAN_OFF:
                want.pop("fan")  # 'off' is a power state, never a desired fan speed
        self.offsets = {}
        for k, v in (data.get("offsets") or {}).items():
            try:
                if clamp_offset(v):
                    self.offsets[k] = clamp_offset(v)
            except (TypeError, ValueError):
                continue
        self.offset_pending = {str(k) for k in data.get("offset_pending") or []}
        self.override = {k: bool(v) for k, v in (data.get("override") or {}).items() if v}
        self.override_paused = {str(k) for k in data.get("override_paused") or [] if self.override.get(str(k))}

    def _data_to_save(self) -> dict[str, Any]:
        return {"desired": self.desired, "auto_restore": self.auto_restore, "prev_fan": self.prev_fan,
                "last_fan": self.last_fan,
                "offsets": self.offsets, "offset_pending": sorted(self.offset_pending),
                "override": self.override, "override_paused": sorted(self.override_paused)}

    @callback
    def _save(self) -> None:
        self.store.async_delay_save(self._data_to_save, 1)

    # ---------------------------------------------------------------- options
    @property
    def _opts(self) -> dict[str, Any]:
        return self.coordinator.config_entry.options

    @property
    def mode(self) -> str:
        return self._opts.get(CONF_EXTERNAL_CHANGE, DEFAULT_EXTERNAL_CHANGE)

    @property
    def grace(self) -> float:
        return float(self._opts.get(CONF_DRIFT_GRACE, DEFAULT_DRIFT_GRACE))

    @property
    def adopt(self) -> bool:
        return bool(self._opts.get(CONF_ADOPT_EXTERNAL, DEFAULT_ADOPT_EXTERNAL))

    # ---------------------------------------------------------------- state
    def offset(self, zone_key: str) -> float:
        return self.offsets.get(zone_key, 0.0)

    @callback
    def set_offset(self, zone_key: str, value: Any) -> float:
        value = clamp_offset(value)
        if value:
            self.offsets[zone_key] = value
        else:
            self.offsets.pop(zone_key, None)
        self._save()
        return value

    @callback
    def set_offset_pending(self, zone_key: str, pending: bool) -> None:
        if pending:
            self.offset_pending.add(zone_key)
        else:
            self.offset_pending.discard(zone_key)
        self._save()

    @callback
    def set_desired(self, zone_key: str, values: dict[str, Any]) -> None:
        values = dict(values)
        if values.get("fan") == FAN_OFF:
            # fan 'off' means the room is off: desired power off, never a desired fan speed of 'off'
            values.pop("fan")
            values["power"] = False
        self.desired.setdefault(zone_key, {}).update(values)
        if "power" in values:
            self.override_paused.discard(zone_key)  # an explicit power intent (user) ends a pause
        if "target" in values:
            self.offset_pending.discard(zone_key)  # the target is being driven with the current offset
        self.episodes.pop(zone_key, None)  # a new intent ends any drift episode
        self.coordinator.alerts.clear(f"drift_{zone_key}")
        self._save()

    @callback
    def remember_fan(self, zone_key: str, fan: str | None) -> None:
        """Remember the fan speed to re-apply when HA turns the room back on."""
        if fan == FAN_OFF:
            return  # 'off' is not a speed
        if fan:
            self.prev_fan[zone_key] = fan
        else:
            self.prev_fan.pop(zone_key, None)
        self._save()

    @callback
    def set_last_fan(self, zone_key: str, mode: str) -> None:
        """Record the exact fan mode last chosen from HA ('off' when the room was turned off)."""
        if self.last_fan.get(zone_key) != mode:
            self.last_fan[zone_key] = mode
            self._save()

    @callback
    def pop_remembered_fan(self, zone_key: str) -> str | None:
        fan = self.prev_fan.pop(zone_key, None)
        if fan is not None:
            self._save()
        return fan

    @callback
    def forget_desired(self, zone_key: str, kind: str) -> None:
        if self.desired.get(zone_key, {}).pop(kind, None) is not None:
            self._save()

    def is_auto_restore(self, zone_key: str) -> bool:
        return self.auto_restore.get(zone_key, True)

    @callback
    def set_auto_restore(self, zone_key: str, on: bool) -> None:
        self.auto_restore[zone_key] = on
        self._save()
        self.coordinator.notify_health()

    # ---------------------------------------------------------------- max cooling override
    def is_override(self, zone_key: str) -> bool:
        return self.override.get(zone_key, False)

    def is_paused(self, zone_key: str) -> bool:
        """Room power is off because of the override (desired state stays Cool/on)."""
        return self.is_override(zone_key) and zone_key in self.override_paused

    def _find_zone(self, zone_key: str) -> HvacZone | None:
        for cd in (self.coordinator.data or {}).values():
            if zone_key in cd.zones:
                return cd.zones[zone_key]
        return None

    @callback
    def set_override(self, zone_key: str, on: bool) -> None:
        if on == self.is_override(zone_key):
            return
        zone = self._find_zone(zone_key)
        if on:
            self.override[zone_key] = True
            want = self.desired.setdefault(zone_key, {})
            if want.get("target") is None and zone is not None and zone.setpoint is not None:
                want["target"] = self.coordinator.from_device(zone_key, zone.setpoint)  # keep the shown target
        else:
            self.override.pop(zone_key, None)
            was_paused = zone_key in self.override_paused
            self.override_paused.discard(zone_key)
            self.override_last_toggle.pop(zone_key, None)
            if zone is not None:
                self._resume_normal(zone, was_paused)
        self.episodes.pop(zone_key, None)
        self._save()
        self.coordinator.notify_health()

    def _resume_normal(self, zone: HvacZone, was_paused: bool) -> None:
        """Override switched off: power a paused room back on and apply the setpoint normally."""
        want = self.desired.get(zone.key, {})
        if want.get("power") is False:
            return  # the user turned the room off: stay off
        expected: dict[str, Any] = {}
        if was_paused:
            expected["power"] = True
            fan = self._resume_fan(zone)
            if fan:
                expected["fan"] = fan
        if want.get("target") is not None:
            expected["target"] = want["target"]
        if not expected or (zone.power is not False and self.coordinator.zone_matches_target(zone, expected)):
            return
        self._start_override_command(zone, expected, "max cooling override off", fan_if_differs=True)

    def _resume_fan(self, zone: HvacZone) -> str | None:
        fan = self.prev_fan.get(zone.key) or self.desired.get(zone.key, {}).get("fan")
        return fan if fan in FAN_ROLES else None

    def _start_override_command(
        self, zone: HvacZone, expected: dict[str, Any], description: str, fan_if_differs: bool = False
    ) -> None:
        self.restoring.add(zone.key)  # keeps drift detection away until the command is done
        self.coordinator.start_sequence(
            zone.key, lambda seq: self._async_override_command(zone, expected, description, fan_if_differs, seq))

    async def _async_override_command(
        self, zone: HvacZone, expected: dict[str, Any], description: str, fan_if_differs: bool, seq: Any = None
    ) -> None:
        from .api import MsheirebError
        from .coordinator import SequenceSuperseded

        try:
            async with self.coordinator.command_lock(zone.contract_id):
                if seq is not None:
                    if seq.cancelled:
                        raise SequenceSuperseded
                    seq.started = True
                fresh = await self.coordinator.async_read_zone(zone) or zone
                if self.coordinator.zone_matches_target(fresh, expected):
                    return
                await self.coordinator.async_command(
                    fresh, expected, description, self.coordinator.pulse_interval,
                    set_desired=False, fan_if_differs=fan_if_differs,
                )
        except MsheirebError as err:
            _LOGGER.warning("Max cooling override command for %s failed: %s", zone.room_name, err)
        finally:
            self.restoring.discard(zone.key)
        self.coordinator.schedule_refresh_after_command()

    def room_sensor(self, zone_key: str) -> str | None:
        """External temperature sensor configured for this room's override (None = the AC's own)."""
        sensors = self._opts.get(CONF_OVERRIDE_SENSORS) or {}
        return (sensors.get(zone_key) or None) if isinstance(sensors, dict) else None

    def override_reference(self, zone: HvacZone) -> tuple[str, float | None]:
        """(source, °C) used for the override hysteresis: the sensor's entity id, or 'ac' (the AC's
        reading) when none is configured or the sensor is unavailable / not numeric."""
        entity_id = self.room_sensor(zone.key)
        if not entity_id:
            self.sensor_fallback.discard(zone.key)
            return "ac", zone.room_temperature
        state = self.hass.states.get(entity_id)
        value: float | None = None
        if state is not None:
            try:
                value = float(state.state)
            except (TypeError, ValueError):
                value = None
            if value is not None and not math.isfinite(value):
                value = None
            unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
            if value is not None and unit == "°F":
                value = (value - 32.0) * 5.0 / 9.0
            elif value is not None and unit == "K":
                value -= 273.15
        if value is None:
            if zone.key not in self.sensor_fallback:
                self.sensor_fallback.add(zone.key)
                _LOGGER.warning(
                    "Max cooling override for %s: temperature sensor %s is unavailable or not numeric; "
                    "using the AC's own reading", zone.room_name, entity_id)
            return "ac", zone.room_temperature
        if zone.key in self.sensor_fallback:
            self.sensor_fallback.discard(zone.key)
            _LOGGER.info("Max cooling override for %s: temperature sensor %s is back; using it",
                         zone.room_name, entity_id)
        return entity_id, round(value, 2)

    @callback
    def evaluate_override(self, data: MsheirebData) -> None:
        """Hysteresis control of room power around the target (runs on every coordinator update)."""
        min_temp = float(self._opts.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP))
        now = time.monotonic()
        for cd in data.values():
            if cd.controller_connected is False:
                continue
            for key, zone in cd.zones.items():
                if not self.is_override(key):
                    continue
                want = self.desired.get(key, {})
                paused = key in self.override_paused
                if want.get("power") is False:
                    self.override_paused.discard(key)  # user turned the room off: stay off
                    continue
                if zone.power is None:
                    continue
                if self._in_flight(zone):
                    continue
                target = want.get("target")
                if target is None:
                    continue
                target = float(target)
                _ref, temp = self.override_reference(zone)
                continuous = target <= min_temp + 0.001
                if continuous:
                    desired_on = True
                elif temp is None:
                    continue
                elif temp < target - OVERRIDE_HYSTERESIS - 1e-9:
                    desired_on = False
                elif temp >= target + OVERRIDE_HYSTERESIS - 1e-9:
                    desired_on = True
                else:
                    desired_on = zone.power  # between the thresholds: keep the current state
                if desired_on == zone.power:
                    if desired_on and paused:
                        self.override_paused.discard(key)  # powered on outside the override: not paused
                        self._save()
                    continue
                if not paused and not zone.power:
                    continue  # off but not paused by us: that is drift, not the override
                last = self.override_last_toggle.get(key)
                if not (continuous and desired_on) and last is not None \
                        and now - last < OVERRIDE_MIN_TOGGLE_INTERVAL:
                    continue
                self.override_last_toggle[key] = now
                if desired_on:
                    self.override_paused.discard(key)
                    expected: dict[str, Any] = {"power": True}
                    fan = self._resume_fan(zone)
                    if fan:
                        expected["fan"] = fan
                    self._save()
                    self._start_override_command(zone, expected, "max cooling override: power on", True)
                else:
                    fan = zone.fan_mode(FAN_ROLES)
                    # remember the running speed only if no speed is known yet: the user's choice
                    # (remembered / desired) is never replaced by a reading, and never by Auto
                    if fan in FAN_ROLES and fan != ROLE_FAN_AUTO and self._resume_fan(zone) is None:
                        self.remember_fan(key, fan)
                    self.override_paused.add(key)
                    self._save()
                    expected = {"power": False}
                    if ROLE_FAN_AUTO in zone.controls:
                        # power off alone leaves the fan coil blowing at the selected speed: also
                        # set the fan to Auto (like the normal off sequence), whatever the option says
                        expected["fan"] = ROLE_FAN_AUTO
                    self._start_override_command(zone, expected, "max cooling override: power off")

    @callback
    def mark_handled(self, zone_key: str) -> None:
        ep = self.episodes.get(zone_key)
        if ep is None:
            self.episodes[zone_key] = Episode(since=time.monotonic(), polls=0, diff={}, handled=True)
        else:
            ep.handled = True

    def _in_flight(self, zone: HvacZone) -> bool:
        rec = self.coordinator.health.commands.get(zone.key)
        if rec is not None and (rec.result == "pending" or rec.retrying):
            return True
        if zone.key in self.restoring or self.coordinator.room_busy(zone.key):
            return True
        return self.coordinator.command_lock(zone.contract_id).locked()

    # ---------------------------------------------------------------- detection
    @callback
    def evaluate(self, data: MsheirebData) -> None:
        if self.mode == EXT_IGNORE:
            self.episodes.clear()
            return
        now = time.monotonic()
        for cd in data.values():
            if cd.controller_connected is False:
                continue  # can't act (and readings may be stale) while offline
            for key, zone in cd.zones.items():
                want = self.desired.get(key)
                if not want:
                    continue
                if self._in_flight(zone):
                    # never act on readings taken while our own presses settle; start over afterwards
                    ep = self.episodes.get(key)
                    if ep is not None and not ep.handled:
                        self.episodes.pop(key, None)
                    continue
                have = actual_values(zone)
                # the desired target is the one shown in HA; the AC itself should be at target + offset.
                # Compare at device level, but keep (and act on / adopt) the HA-facing values.
                want_dev = self.coordinator.to_device(key, want)
                have_user = dict(have)
                if have.get("target") is not None:
                    have_user["target"] = self.coordinator.from_device(key, have["target"])
                diff = {k: (want[k], have_user.get(k)) for k in KEYS_ORDER
                        if k in want and _differs(k, want_dev[k], have.get(k))}
                if want.get("power") is False and have.get("power") is False:
                    diff.pop("fan", None)  # fan speed is irrelevant while the room is off
                    if key in self.offset_pending:
                        diff.pop("target", None)  # new offset: applied on the next turn-on from HA
                if self.is_override(key):
                    diff.pop("target", None)  # the AC stays at max cooling; the shown target is virtual
                    if key in self.override_paused:
                        diff.pop("power", None)  # paused by the override: not an external change
                        diff.pop("fan", None)
                ep = self.episodes.get(key)
                if not diff:
                    if ep is not None:
                        if ep.restored:
                            self.coordinator.alerts.clear(f"drift_{key}")
                        self.episodes.pop(key, None)
                    continue
                if ep is None or (not ep.handled and set(ep.diff) != set(diff)):
                    ep = Episode(since=now, polls=1, diff=diff, handled=ep.handled if ep else False)
                    self.episodes[key] = ep
                else:
                    ep.polls += 1
                    ep.diff = diff
                if ep.handled:
                    continue
                if now - ep.since >= self.grace and ep.polls >= DRIFT_MIN_POLLS:
                    self._act(zone, ep)

    def _act(self, zone: HvacZone, ep: Episode) -> None:
        ep.handled = True
        self.events += 1
        mode = self.mode
        notify = mode in (EXT_NOTIFY, EXT_RESTORE_NOTIFY)
        want_restore = mode in (EXT_RESTORE, EXT_RESTORE_NOTIFY)
        if self.adopt:
            self.desired.setdefault(zone.key, {}).update({k: a for k, (_d, a) in ep.diff.items()})
            self._save()
            action = "adopted as the new desired state"
        elif want_restore and self.is_auto_restore(zone.key):
            ep.restored = True
            expected = {k: d for k, (d, _a) in ep.diff.items()}
            self.restoring.add(zone.key)
            # a room sequence like a user action: a newer action from HA supersedes the restore
            self.coordinator.start_sequence(zone.key, lambda seq: self._async_restore(zone, expected, seq))
            action = "restoring: " + ", ".join(f"{LABELS[k]} → {describe(k, v)}" for k, v in expected.items())
        elif want_restore:
            action = "not restored (Auto-restore is off for this room)"
        else:
            action = "no action (notify only)"
        changes = {
            LABELS[k]: {"from": describe(k, d), "to": describe(k, a)} for k, (d, a) in ep.diff.items()
        }
        self.last_event = DriftEvent(room=zone.room_name, changes=changes, action=action, at=dt_util.utcnow().isoformat())
        _LOGGER.info("External change in %s: %s; %s", zone.room_name, changes, action)
        if notify:
            lines = "\n".join(f"- {name}: {c['from']} → {c['to']}" for name, c in changes.items())
            self.coordinator.alerts.raise_(
                f"drift_{zone.key}",
                f"Msheireb: {zone.room_name} changed outside Home Assistant",
                f"**{zone.room_name}** no longer matches the state last set from Home Assistant:\n{lines}\n\n"
                f"**Action:** {action}.",
            )
        self.coordinator.notify_health()

    async def _async_restore(self, zone: HvacZone, expected: dict[str, Any], seq: Any = None) -> None:
        from .api import MsheirebError
        from .coordinator import SequenceSuperseded

        try:
            async with self.coordinator.command_lock(zone.contract_id):
                if seq is not None:
                    if seq.cancelled:
                        raise SequenceSuperseded
                    seq.started = True
                fresh = await self.coordinator.async_read_zone(zone) or zone
                await self.coordinator.async_command(
                    fresh, expected, "restore after external change",
                    self.coordinator.pulse_interval, set_desired=False,
                )
        except MsheirebError as err:
            _LOGGER.warning("Restore of %s failed: %s", zone.room_name, err)
        finally:
            self.restoring.discard(zone.key)
        self.coordinator.schedule_refresh_after_command()
