"""Per-room 'Target temperature offset' number (the AC is driven to target + offset)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    OFFSET_MAX, OFFSET_MIN, OFFSET_STEP, OVERRIDE_MARGIN_MAX, OVERRIDE_MARGIN_MIN, OVERRIDE_MARGIN_STEP,
)
from .coordinator import MsheirebCoordinator
from .entity import room_device, room_entity_id, zone_needs_disambiguation


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
                if zone.key not in known:
                    known.add(zone.key)
                    ent = TargetOffsetNumber(coordinator, zone, zone_needs_disambiguation(cd, zone))
                    ent.entity_id = room_entity_id("number", cd, zone, "target temperature offset")
                    new.append(ent)
                    ment = OverrideMarginNumber(coordinator, zone, zone_needs_disambiguation(cd, zone))
                    ment.entity_id = room_entity_id("number", cd, zone, "max cooling override margin")
                    new.append(ment)
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class TargetOffsetNumber(NumberEntity):
    """Shown target T, AC driven to T + offset (e.g. offset -1: set 20, the AC runs at 19)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "target_offset"
    _attr_icon = "mdi:thermometer-plus"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = OFFSET_MIN
    _attr_native_max_value = OFFSET_MAX
    _attr_native_step = OFFSET_STEP
    # no temperature device class: an offset is a difference and must not be unit-converted
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: MsheirebCoordinator, zone, disambiguate: bool = False) -> None:
        self.coordinator = coordinator
        self._zone_key = zone.key
        self._attr_unique_id = f"{zone.key}_target_offset"
        self._attr_device_info = room_device(zone, disambiguate)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_health_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return self.coordinator.offset(self._zone_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"pending_until_turn_on": self._zone_key in self.coordinator.drift.offset_pending}

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_offset(self._zone_key, value)
        self.async_write_ha_state()


class OverrideMarginNumber(NumberEntity):
    """Max cooling override switching margin: room off below target - margin, on at target + margin."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "override_margin"
    _attr_icon = "mdi:thermometer-lines"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = OVERRIDE_MARGIN_MIN
    _attr_native_max_value = OVERRIDE_MARGIN_MAX
    _attr_native_step = OVERRIDE_MARGIN_STEP
    # a difference, not a temperature: no device class, never unit-converted
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: MsheirebCoordinator, zone, disambiguate: bool = False) -> None:
        self.coordinator = coordinator
        self._zone_key = zone.key
        self._attr_unique_id = f"{zone.key}_override_margin"
        self._attr_device_info = room_device(zone, disambiguate)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_health_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> float:
        return self.coordinator.drift.margin(self._zone_key)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_override_margin(self._zone_key, value)
        self.async_write_ha_state()
