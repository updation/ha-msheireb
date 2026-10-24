"""v0.3.17: per-room target temperature offset (the AC is driven to target + offset)."""
import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.msheireb.drift import clamp_offset, store_key

from .fake_api import FakeApi  # noqa: F401
from .test_drift import (  # noqa: F401
    DINING,
    KEY,
    _dining,
    _poll,
    _set_temp,
    _setup,
    external_power,
    external_setpoint,
    fake_api,
    fast_sleep,
)

OFFSET = "number.msheireb_demo01_dining_room_target_temperature_offset"


def _setpoint(api):
    return _dining(api)[0]["HVAC Setpoint"]["current_status"] / 100


async def _set_offset(hass, value):
    await hass.services.async_call("number", "set_value", {"entity_id": OFFSET, "value": value}, blocking=True)
    await hass.async_block_till_done()


async def test_offset_entity_defaults(hass):
    entry, coord, api = await _setup(hass)
    s = hass.states.get(OFFSET)
    assert float(s.state) == 0.0
    assert s.attributes["min"] == -4.0 and s.attributes["max"] == 4.0 and s.attributes["step"] == 0.5
    assert s.attributes["mode"] == "box" and s.attributes["unit_of_measurement"] == "°C"
    assert s.attributes["friendly_name"] == "Dining Room Target temperature offset"
    from homeassistant.helpers import entity_registry as er
    assert er.async_get(hass).async_get(OFFSET).entity_category == "config"
    assert hass.states.get(DINING).attributes["offset"] == 0.0


async def test_set_20_with_offset_minus_1_drives_19(hass, fast_sleep):
    entry, coord, api = await _setup(hass)
    await _set_offset(hass, -1)
    await _poll(hass, coord)
    assert _setpoint(api) == 18.5  # the offset change itself re-drove 19.5 -> 18.5 (shown 19.5)
    assert hass.states.get(DINING).attributes["temperature"] == 19.5
    api.commands.clear()
    await _set_temp(hass, 20.0)
    await hass.async_block_till_done()
    assert _setpoint(api) == 19.0
    assert hass.states.get(DINING).attributes["temperature"] == 20.0
    await _poll(hass, coord)
    assert coord.health.commands[KEY].result == "confirmed"
    assert hass.states.get(DINING).attributes["temperature"] == 20.0
    assert hass.states.get(DINING).attributes["device_setpoint"] == 19.0
    assert coord.drift.desired[KEY]["target"] == 20.0  # the desired state is the shown target


async def test_device_reading_19_with_offset_shows_20(hass):
    entry, coord, api = await _setup(hass)
    coord.drift.set_offset(KEY, -1)
    external_setpoint(api, 1900)
    await _poll(hass, coord)
    s = hass.states.get(DINING)
    assert s.attributes["temperature"] == 20.0
    assert s.attributes["current_temperature"] == 21.5  # room temperature unchanged
    assert s.attributes["offset"] == -1.0


async def test_no_drift_at_target_plus_offset_drift_at_plain_target(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0})
    await _set_offset(hass, -1)
    await _poll(hass, coord)
    await _set_temp(hass, 20.0)
    await _poll(hass, coord)  # confirmed at 19.0
    api.commands.clear()
    await _poll(hass, coord); await _poll(hass, coord)
    assert api.commands == [] and KEY not in coord.drift.episodes
    external_setpoint(api, 2000)  # AC at the plain target, ignoring the offset -> drift
    await _poll(hass, coord)
    assert coord.drift.episodes[KEY].diff == {"target": (20.0, 21.0)}  # shown values
    await _poll(hass, coord)
    assert [c["sn"] for c in api.commands] == [7, 7]  # restored 20.0 -> 19.0
    assert _setpoint(api) == 19.0
    await _poll(hass, coord)
    assert KEY not in coord.drift.episodes
    assert hass.states.get(DINING).attributes["temperature"] == 20.0


async def test_adopt_external_stores_shown_target(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0, "adopt_external": True})
    await _set_offset(hass, -1)
    await _poll(hass, coord)
    await _set_temp(hass, 20.0); await _poll(hass, coord)
    external_setpoint(api, 2200)
    await _poll(hass, coord); await _poll(hass, coord)
    assert coord.drift.desired[KEY]["target"] == 23.0  # AC 22 - offset -1


async def test_changing_offset_redrives_device(hass, fast_sleep):
    entry, coord, api = await _setup(hass)
    await _set_temp(hass, 20.0); await _poll(hass, coord)
    assert _setpoint(api) == 20.0
    api.commands.clear()
    await _set_offset(hass, -1.5)
    assert [c["sn"] for c in api.commands] == [7, 7, 7]  # 20.0 -> 18.5
    assert _setpoint(api) == 18.5
    assert hass.states.get(DINING).attributes["temperature"] == 20.0
    await _poll(hass, coord)
    assert coord.health.commands[KEY].result == "confirmed"
    assert hass.states.get(DINING).attributes["temperature"] == 20.0
    api.commands.clear()
    await _set_offset(hass, 0.5)
    assert [c["sn"] for c in api.commands] == [6, 6, 6, 6]  # 18.5 -> 20.5
    await _poll(hass, coord)
    assert hass.states.get(DINING).attributes["temperature"] == 20.0


async def test_offset_change_while_off_waits_for_turn_on(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"drift_grace": 0})
    await _set_temp(hass, 20.0); await _poll(hass, coord)
    await hass.services.async_call("climate", "turn_off", {"entity_id": DINING}, blocking=True)
    await _poll(hass, coord)
    api.commands.clear()
    await _set_offset(hass, -1)
    assert api.commands == []  # off: nothing pressed
    assert hass.states.get(OFFSET).attributes["pending_until_turn_on"] is True
    await _poll(hass, coord); await _poll(hass, coord)
    assert api.commands == []  # not treated as an external change while off
    await hass.services.async_call("climate", "turn_on", {"entity_id": DINING}, blocking=True)
    await hass.async_block_till_done()
    assert _setpoint(api) == 19.0 and _dining(api)[1]["HVAC AC"]["current_status"] == "ON"
    assert KEY not in coord.drift.offset_pending
    assert hass.states.get(DINING).attributes["temperature"] == 20.0


async def test_min_max_apply_to_shown_target(hass, fast_sleep):
    entry, coord, api = await _setup(hass, {"max_temp": 27.0})
    assert hass.states.get(DINING).attributes["max_temp"] == 27.0  # the limit is on the shown target
    await _set_offset(hass, 2)
    await _poll(hass, coord)
    await _set_temp(hass, 27.0)
    assert hass.states.get(DINING).attributes["temperature"] == 27.0
    assert _setpoint(api) == 29.0  # the AC itself may go past the max by the offset


async def test_offset_bounded(hass):
    entry, coord, api = await _setup(hass)
    assert [clamp_offset(v) for v in (-5, -4.2, -3.2, -1.3, -0.2, 0.26, 1.74, 9)] == [-4.0, -4.0, -3.0, -1.5, 0.0, 0.5, 1.5, 4.0]
    for ok in (4.0, -4.0, 3.5, -3.5):
        await _set_offset(hass, ok)
        assert float(hass.states.get(OFFSET).state) == ok
    for bad in (4.5, -4.5):
        with pytest.raises(ServiceValidationError):
            await _set_offset(hass, bad)
    await _set_offset(hass, 1.2)  # in range but off-step: snapped to 0.5 steps
    assert float(hass.states.get(OFFSET).state) == 1.0
    assert coord.offset(KEY) == 1.0


async def test_offset_persisted_across_restart(hass, hass_storage):
    hass_storage[store_key("e1")] = {"version": 1, "minor_version": 1, "key": store_key("e1"),
                                    "data": {"desired": {}, "auto_restore": {}, "offsets": {KEY: -1.5}}}
    entry, coord, api = await _setup(hass)
    assert float(hass.states.get(OFFSET).state) == -1.5
    assert hass.states.get(DINING).attributes["temperature"] == 21.0  # AC 19.5 - (-1.5)
    await _set_offset(hass, 0.5)
    await coord.drift.store.async_save(coord.drift._data_to_save())
    assert hass_storage[store_key("e1")]["data"]["offsets"][KEY] == 0.5


async def test_offset_in_diagnostics(hass):
    from custom_components.msheireb.diagnostics import async_get_config_entry_diagnostics
    entry, coord, api = await _setup(hass)
    coord.drift.set_offset(KEY, -0.5)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["health"]["drift"]["target_offsets"] == {KEY: -0.5}
