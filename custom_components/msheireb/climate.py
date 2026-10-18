"""Climate entities: one per room HVAC zone."""
from __future__ import annotations

import asyncio  # noqa: F401  (tests patch climate.asyncio.sleep)
import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    FAN_OFF,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import MsheirebError
from .const import (
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    CONF_FAN_AUTO_WHEN_OFF,
    DEFAULT_FAN_AUTO_WHEN_OFF,
    FAN_ROLES,
    ROLE_FAN_AUTO,
    TEMP_DEBOUNCE,
    CONF_PULSE_INTERVAL,
    DEFAULT_PULSE_INTERVAL,
    ROLE_POWER,
    ROLE_TEMP_DOWN,
    ROLE_TEMP_UP,
    TEMP_STEP,
)
from .coordinator import KIND_FAN, KIND_POWER, KIND_TARGET, MsheirebCoordinator, RoomSequence, SequenceSuperseded
from .entity import MsheirebEntity, room_device, room_entity_id, zone_needs_disambiguation
from .models import HvacZone

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MsheirebCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = []
        for cid, cd in (coordinator.data or {}).items():
            for zone in sorted(cd.zones.values(), key=lambda z: z.sort_key):
                if zone.key in known:
                    continue
                known.add(zone.key)
                dup = zone_needs_disambiguation(cd, zone)
                ent = MsheirebClimate(coordinator, entry, cid, zone, dup)
                ent.entity_id = room_entity_id("climate", cd, zone)
                new.append(ent)
        if new:
            _LOGGER.debug("Adding %d climate entit(ies): %s", len(new), [e.unique_id for e in new])
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


def _round_step(value: float) -> float:
    return round(round(value / TEMP_STEP) * TEMP_STEP, 1)


class MsheirebClimate(MsheirebEntity, ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = TEMP_STEP
    _attr_precision = 0.5
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL]
    _enable_turn_on_off_backwards_compatibility = False
    _attr_translation_key = "room"  # fan mode labels (incl. 'off'); the entity name stays the room name

    def __init__(
        self,
        coordinator: MsheirebCoordinator,
        entry: ConfigEntry,
        contract_id: int,
        zone: HvacZone,
        disambiguate: bool,
    ) -> None:
        super().__init__(coordinator, contract_id)
        self._zone_key = zone.key
        self._entry = entry
        self._attr_unique_id = f"{contract_id}_{zone.device_id}_climate"
        self._attr_name = None  # main feature of the room device -> entity name = room name
        self._attr_device_info = room_device(zone, disambiguate)
        self._attr_min_temp = float(entry.options.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP))
        self._attr_max_temp = float(entry.options.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP))
        self._pulse_interval = float(entry.options.get(CONF_PULSE_INTERVAL, DEFAULT_PULSE_INTERVAL))
        self._fan_roles = tuple(r for r in FAN_ROLES if r in zone.controls)
        # 'off' first (when the room has a power control): choosing it turns the room off
        self._fan_off = bool(self._fan_roles) and ROLE_POWER in zone.controls
        # display order: Off, Low, Medium, High, Auto
        self._attr_fan_modes = ([FAN_OFF] if self._fan_off else []) + [r for r in self._fan_roles if r != ROLE_FAN_AUTO] + [r for r in self._fan_roles if r == ROLE_FAN_AUTO]
        features = ClimateEntityFeature.TARGET_TEMPERATURE
        if self._fan_roles:
            features |= ClimateEntityFeature.FAN_MODE
        if ROLE_POWER in zone.controls:
            features |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        self._attr_supported_features = features
        # requested state (power/fan/target) shown while the room's sequence + retries are in flight
        self._intent: dict[str, Any] = {}
        self._fan_if_differs = False

    # ------------------------------------------------------------ state
    @property
    def zone(self) -> HvacZone | None:
        cd = self.contract_data
        return cd.zones.get(self._zone_key) if cd else None

    @property
    def available(self) -> bool:
        cd = self.contract_data
        return (
            super().available
            and cd is not None
            and cd.controller_connected is True
            and self.zone is not None
        )

    def _opt(self, name: str, actual: Any) -> Any:
        return self._intent[name] if name in self._intent else actual

    def _power(self, zone: HvacZone | None) -> bool | None:
        """Requested/actual power; a room paused by the max cooling override still counts as on."""
        actual = zone.power if zone else None
        if KIND_POWER not in self._intent and self.coordinator.drift.is_paused(self._zone_key):
            return True
        return self._opt("power", actual)

    @callback
    def _handle_coordinator_update(self) -> None:
        # the requested values win over every reading until the sequence and all its retries are
        # over; then the actual state is shown (after a final failure: the old values + a notification)
        if self._intent and not self.coordinator.room_busy(self._zone_key):
            self._intent.clear()
            self._fan_if_differs = False
        super()._handle_coordinator_update()

    @property
    def current_temperature(self) -> float | None:
        zone = self.zone
        return zone.room_temperature if zone else None

    @property
    def target_temperature(self) -> float | None:
        # the AC runs at target + offset; HA shows the target (AC setpoint - offset)
        zone = self.zone
        if self.coordinator.drift.is_override(self._zone_key):
            # the AC stays at max cooling: the shown target only drives the override's hysteresis
            shown = self.coordinator.drift.desired.get(self._zone_key, {}).get(KIND_TARGET)
            if shown is not None:
                return self._opt("target", float(shown))
        actual = None
        if zone is not None and zone.setpoint is not None:
            actual = self.coordinator.from_device(self._zone_key, zone.setpoint)
        return self._opt("target", actual)

    @property
    def hvac_mode(self) -> HVACMode | None:
        zone = self.zone
        power = self._power(zone)
        if power is None:
            return None
        return HVACMode.COOL if power else HVACMode.OFF

    @property
    def fan_mode(self) -> str | None:
        # kept in step with the HVAC mode: 'off' while the room is (or is being turned) off,
        # otherwise the requested/actual speed
        zone = self.zone
        if self._fan_off and self._power(zone) is False:
            return FAN_OFF
        if KIND_FAN not in self._intent and self.coordinator.drift.is_paused(self._zone_key) and zone:
            # idle phase of the max cooling override (fan at Auto): show the speed it resumes with
            resume = self.coordinator.drift._resume_fan(zone)
            if resume in self._fan_roles:
                return resume
        return self._opt("fan", zone.fan_mode(self._fan_roles) if zone else None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = self.zone
        if zone is None:
            return {}
        drift = self.coordinator.drift
        override: dict[str, Any] = {"max_cooling_override": drift.is_override(self._zone_key)}
        if override["max_cooling_override"]:
            override["override_state"] = "idle" if drift.is_paused(self._zone_key) else "cooling"
        return {
            **override,
            "room": zone.room_name,
            "device_id": zone.device_id,
            "control_sn": {role: c.sn for role, c in zone.controls.items()},
            "offset": self.coordinator.offset(self._zone_key),
            "device_setpoint": zone.setpoint,
            "saved_fan_mode": self._saved_fan_mode(),
            "last_fan_mode": self.coordinator.drift.last_fan.get(self._zone_key),
        }

    def _saved_fan_mode(self) -> str | None:
        """The remembered speed re-applied on the next turn-on (never 'off'; None if unknown)."""
        drift = self.coordinator.drift
        for fan in (drift.prev_fan.get(self._zone_key), drift.desired.get(self._zone_key, {}).get(KIND_FAN)):
            if fan and fan != FAN_OFF and fan in self._fan_roles:
                return fan
        return None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_offset_listener(self._zone_key, self._offset_changed))

    async def _offset_changed(self, old: float, new: float) -> None:
        """The room's target offset changed: keep showing the same target and, if the room is on,
        re-drive the AC to target + new offset (normal queue, debounced like temperature taps)."""
        zone = self.zone
        if zone is None:
            return
        if self.coordinator.drift.is_override(self._zone_key):
            self.async_write_ha_state()  # the AC stays at max cooling: no setpoint to re-drive
            return
        if KIND_TARGET in self._intent:
            target = self._intent[KIND_TARGET]
        elif zone.setpoint is not None:
            target = _round_step(zone.setpoint - old)  # what the card showed with the old offset
        else:
            self.async_write_ha_state()
            return
        power = self._opt(KIND_POWER, zone.power)
        if not power or not self.available:
            # off (or offline): nothing is pressed now; the next turn-on from HA applies it
            drift = self.coordinator.drift
            if drift.desired.get(zone.key, {}).get(KIND_TARGET) is None:
                drift.desired.setdefault(zone.key, {})[KIND_TARGET] = target  # keep the shown target
            drift.set_offset_pending(zone.key, True)
            self.async_write_ha_state()
            return
        self.coordinator.drift.set_offset_pending(zone.key, False)
        self._intent[KIND_TARGET] = target
        await self._start_if_needed(zone, TEMP_DEBOUNCE)

    # ------------------------------------------------------------ commands
    def _require_zone(self) -> HvacZone:
        zone = self.zone
        if zone is None or not self.available:
            raise HomeAssistantError(f"{self.name}: controller not available")
        return zone

    async def _launch(self, debounce: float = 0.0) -> None:
        """Show the request now and run the presses in the background (latest action wins)."""
        seq = self.coordinator.start_sequence(self._zone_key, self._run_sequence, debounce=debounce)
        self.async_write_ha_state()
        if self.coordinator.wait_for_sequences and seq.task is not None:  # test hook only
            await asyncio.shield(seq.task)

    async def _run_sequence(self, seq: RoomSequence) -> None:
        coord = self.coordinator
        queued = coord.command_lock(self._contract_id).locked()
        async with coord.command_lock(self._contract_id):
            if seq.cancelled:
                raise SequenceSuperseded
            seq.started = True
            zone = self.zone
            expected = {k: v for k, v in self._intent.items()}
            if zone is None or not expected:
                return
            if queued or zone.setpoint is None:
                # another sequence ran meanwhile: decide from a fresh reading, never toggle blindly
                zone = await coord.async_read_zone(zone) or zone
            if coord.zone_matches_target(zone, expected):
                coord.async_set_desired(zone.key, expected)
                return
            parts = ", ".join(f"{k}={v}" for k, v in expected.items())
            try:
                await coord.async_command(zone, expected, f"set ({parts})", self._pulse_interval,
                                          fan_if_differs=self._fan_if_differs)
            except MsheirebError as err:  # tracked as failed + notified by the coordinator
                _LOGGER.warning("%s: command failed: %s", self.name, err)
        coord.schedule_refresh_after_command()

    def _hvac_intent(self, hvac_mode: HVACMode, zone: HvacZone) -> None:
        """OFF: remember the fan speed, power off, fan to Auto. COOL: power on, re-apply the speed."""
        if hvac_mode not in (HVACMode.OFF, HVACMode.COOL):
            raise HomeAssistantError(f"Unsupported HVAC mode {hvac_mode}")
        if ROLE_POWER not in zone.controls:
            raise HomeAssistantError(f"{self.name}: no power control")
        want_on = hvac_mode == HVACMode.COOL
        drift = self.coordinator.drift
        current_fan = zone.fan_mode(self._fan_roles)
        fan_auto = bool(self._entry.options.get(CONF_FAN_AUTO_WHEN_OFF, DEFAULT_FAN_AUTO_WHEN_OFF))
        self._intent[KIND_POWER] = want_on
        self._fan_if_differs = False
        if want_on and zone.key in drift.offset_pending and KIND_TARGET not in self._intent \
                and not drift.is_override(zone.key):
            # the offset changed while off: drive the AC to target + new offset after power-on
            target = drift.desired.get(zone.key, {}).get(KIND_TARGET)
            if target is None and zone.setpoint is not None:
                target = _round_step(zone.setpoint - self.coordinator.offset(zone.key))
            if target is not None:
                self._intent[KIND_TARGET] = float(target)
        if not want_on:
            drift.set_last_fan(zone.key, FAN_OFF)
            memory = drift.prev_fan.get(zone.key)
            remembered = current_fan or drift.desired.get(zone.key, {}).get(KIND_FAN)
            pending_fan = self._intent.get(KIND_FAN)
            if pending_fan in self._fan_roles and self.coordinator.room_busy(zone.key):
                remembered = pending_fan  # a speed just chosen from HA beats a reading taken before it applied
            # Only remember a real speed taken while on; never replace a remembered speed with
            # Auto (Auto may come from our own off-sequence, a drift restore or an AC restart).
            if remembered and (memory is None or (zone.power is not False and remembered != ROLE_FAN_AUTO)):
                drift.remember_fan(zone.key, remembered)
            if fan_auto and ROLE_FAN_AUTO in self._fan_roles:
                self._intent[KIND_FAN] = ROLE_FAN_AUTO
            elif not fan_auto:
                self._intent.pop(KIND_FAN, None)
                if current_fan:
                    drift.set_desired(zone.key, {KIND_FAN: current_fan})  # power only; fan untouched
        else:
            fan = drift.prev_fan.get(zone.key) or drift.desired.get(zone.key, {}).get(KIND_FAN)
            if fan not in self._fan_roles:
                drift.forget_desired(zone.key, KIND_FAN)  # unknown: leave the fan as it is
                self._intent.pop(KIND_FAN, None)
            else:
                self._intent[KIND_FAN] = fan
                # with 'Fan to Auto when off' disabled the speed is only pressed if the actual
                # speed (read after the power change) differs from the remembered/desired one
                self._fan_if_differs = not fan_auto

    async def _start_if_needed(self, zone: HvacZone, debounce: float = 0.0) -> None:
        if not self.coordinator.room_busy(self._zone_key) and self.coordinator.zone_matches_target(zone, self._intent):
            self.coordinator.async_set_desired(zone.key, dict(self._intent))
            self._intent.clear()
            self._fan_if_differs = False
            self.async_write_ha_state()
            return
        await self._launch(debounce)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Returns at once; power first, then fan, run in the background through the apartment queue,
        each part only pulsed when the reported state differs, then confirmed/retried."""
        zone = self._require_zone()
        self._hvac_intent(hvac_mode, zone)
        await self._start_if_needed(zone)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.COOL)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode == FAN_OFF and self._fan_off:
            # fan Off = room off (incl. 'Fan to Auto when off': fan Auto, speed remembered)
            await self.async_set_hvac_mode(HVACMode.OFF)
            return
        if fan_mode not in self._fan_roles:
            raise HomeAssistantError(f"Unsupported fan mode {fan_mode}")
        zone = self._require_zone()
        self.coordinator.drift.set_last_fan(zone.key, fan_mode)
        if self.coordinator.drift.is_paused(zone.key) and KIND_POWER not in self._intent:
            # idle phase of the max cooling override: nothing pressed now (the unit is off with the
            # fan at Auto); the speed is stored and applied when the override powers the room on
            self.coordinator.drift.remember_fan(zone.key, fan_mode)
            self.coordinator.async_set_desired(zone.key, {KIND_FAN: fan_mode})
            self.async_write_ha_state()
            return
        if self._fan_off and self._power(zone) is False:
            # a speed chosen while off: turn on, then (after the power -> fan delay) that speed;
            # it replaces the remembered speed
            self.coordinator.drift.remember_fan(zone.key, fan_mode)
            self._hvac_intent(HVACMode.COOL, zone)
            self._intent[KIND_FAN] = fan_mode
            await self._start_if_needed(zone)
            return
        # an explicit choice from HA is what to re-apply on the next turn-on (on or off)
        self.coordinator.drift.remember_fan(zone.key, fan_mode)
        self._intent[KIND_FAN] = fan_mode
        self._fan_if_differs = False
        await self._start_if_needed(zone)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        mode = kwargs.get(ATTR_HVAC_MODE)
        if temp is None and mode is None:
            return
        zone = self._require_zone()
        if mode is not None:
            self._hvac_intent(mode, zone)
        debounce = 0.0
        if temp is not None:
            target = _round_step(min(self.max_temp, max(self.min_temp, float(temp))))
            current = zone.setpoint
            if current is None:
                raise HomeAssistantError(f"{self.name}: current setpoint unknown")
            override = self.coordinator.drift.is_override(zone.key)
            needed = 0 if override else round((self.coordinator.device_target(zone.key, target) - current) / TEMP_STEP)
            if needed:
                role = ROLE_TEMP_UP if needed > 0 else ROLE_TEMP_DOWN
                if role not in zone.controls:
                    raise HomeAssistantError(f"{self.name}: no {role} control")
            if override:
                # max cooling override: no setpoint presses, the target only drives the hysteresis
                self.coordinator.drift.set_desired(zone.key, {KIND_TARGET: target})
                self.async_write_ha_state()
                if mode is None:
                    return
            else:
                self._intent[KIND_TARGET] = target
                debounce = TEMP_DEBOUNCE  # rapid +/- taps: one target, pressed after the taps stop
        await self._start_if_needed(zone, debounce)
