"""Per-room 'Auto-restore' switch (restore the HA-set state after external changes)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
                    ent = AutoRestoreSwitch(coordinator, zone, zone_needs_disambiguation(cd, zone))
                    ent.entity_id = room_entity_id("switch", cd, zone, "auto restore")
                    new.append(ent)
                    ovr = MaxCoolingOverrideSwitch(coordinator, zone, zone_needs_disambiguation(cd, zone))
                    ovr.entity_id = room_entity_id("switch", cd, zone, "max cooling override")
                    new.append(ovr)
        if new:
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class AutoRestoreSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "auto_restore"
    _attr_icon = "mdi:backup-restore"

    def __init__(self, coordinator: MsheirebCoordinator, zone, disambiguate: bool = False) -> None:
        self.coordinator = coordinator
        self._zone_key = zone.key
        self._attr_unique_id = f"{zone.key}_auto_restore"
        self._attr_device_info = room_device(zone, disambiguate)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_health_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self.coordinator.drift.is_auto_restore(self._zone_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "desired": self.coordinator.drift.desired.get(self._zone_key),
            "on_external_change": self.coordinator.drift.mode,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.drift.set_auto_restore(self._zone_key, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.drift.set_auto_restore(self._zone_key, False)


class MaxCoolingOverrideSwitch(AutoRestoreSwitch):
    """Per-room 'Max cooling override': the AC stays at max cooling, HA cycles its power around the target."""

    _attr_translation_key = "max_cooling_override"
    _attr_icon = "mdi:snowflake-thermometer"

    def __init__(self, coordinator: MsheirebCoordinator, zone, disambiguate: bool = False) -> None:
        super().__init__(coordinator, zone, disambiguate)
        self._attr_unique_id = f"{zone.key}_max_cooling_override"

    @property
    def is_on(self) -> bool:
        return self.coordinator.drift.is_override(self._zone_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        drift = self.coordinator.drift
        return {"paused": drift.is_paused(self._zone_key), "target": drift.desired.get(self._zone_key, {}).get("target")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.drift.set_override(self._zone_key, True)
        self.coordinator.schedule_override_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.drift.set_override(self._zone_key, False)
