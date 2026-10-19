"""v0.3.31: save_state / restore_state services (named per-room slots)."""
import json

from custom_components.msheireb.drift import store_key

from .test_max_cooling_override import (  # noqa: F401
    OVR, _expire, _room_temp, _setup_override, _switch,
)
from .test_off_fan_memory import DINING, KEY, _poll, _setup, _svc, fake_api  # noqa: F401
from .test_drift import _dining, _set_temp, fast_sleep  # noqa: F401


def diag_find(node):
    """The diagnostics' drift.saved_states, wherever the report nests the drift section."""
    if isinstance(node, dict):
        if "saved_states" in node:
            return node["saved_states"]
        for v in node.values():
            found = diag_find(v)
            if found is not None:
                return found
    return None


async def _save(hass, slot, **kw):
    await hass.services.async_call("msheireb", "save_state", {"entity_id": DINING, "slot": slot, **kw}, blocking=True)


async def _restore(hass, slot, **kw):
    await hass.services.async_call("msheireb", "restore_state", {"entity_id": DINING, "slot": slot, **kw}, blocking=True)
    await hass.async_block_till_done()


def _attrs(hass):
    return hass.states.get(DINING).attributes


def _power(api):
    return _dining(api)[1]["HVAC AC"]["current_status"] == "ON"


async def test_save_and_restore_speed_with_target(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    await _set_temp(hass, 22.0)
    await _poll(hass, coord)
    await _save(hass, "door")
    assert coord.drift.saved[KEY]["door"] == {"fan": "medium", "power": True, "target": 22.0}
    assert _attrs(hass)["saved_states"] == {"door": "medium"}
    await _svc(hass, "set_fan_mode", fan_mode="low")
    await _set_temp(hass, 25.0)
    await _poll(hass, coord)
    await _restore(hass, "door")
    await _poll(hass, coord)
    a = _attrs(hass)
    assert a["fan_mode"] == "medium" and a["temperature"] == 22.0
    assert a["saved_states"] == {}  # cleared by default


async def test_save_off_and_restore_turns_off(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="off")
    await _poll(hass, coord)
    await _save(hass, "door")
    assert coord.drift.saved[KEY]["door"]["fan"] == "off" and coord.drift.saved[KEY]["door"]["power"] is False
    await _svc(hass, "set_fan_mode", fan_mode="high")
    await _poll(hass, coord)
    assert hass.states.get(DINING).state == "cool"
    await _restore(hass, "door")
    await _poll(hass, coord)
    assert hass.states.get(DINING).state == "off" and _attrs(hass)["fan_mode"] == "off"


async def test_restore_speed_turns_room_on(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="low")
    await _poll(hass, coord)
    await _save(hass, "co2")
    await _svc(hass, "turn_off")
    await _poll(hass, coord)
    await _restore(hass, "co2")
    await _poll(hass, coord)
    assert hass.states.get(DINING).state == "cool" and _attrs(hass)["fan_mode"] == "low"


async def test_overwrite_false_keeps_existing(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="low")
    await _poll(hass, coord)
    await _save(hass, "door")
    await _svc(hass, "set_fan_mode", fan_mode="high")
    await _poll(hass, coord)
    await _save(hass, "door", overwrite=False)
    assert coord.drift.saved[KEY]["door"]["fan"] == "low"
    await _save(hass, "door")  # default overwrites
    assert coord.drift.saved[KEY]["door"]["fan"] == "high"


async def test_clear_false_keeps_slot(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="low")
    await _poll(hass, coord)
    await _save(hass, "door")
    await _restore(hass, "door", clear=False)
    assert "door" in coord.drift.saved[KEY]
    await _restore(hass, "door")
    assert KEY not in coord.drift.saved


async def test_missing_slot_does_nothing(hass, caplog):
    entry, coord, api = await _setup(hass)
    await _restore(hass, "nope")
    assert api.commands == []
    assert "no saved state 'nope'" in caplog.text


async def test_two_independent_slots(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="high")
    await _poll(hass, coord)
    await _save(hass, "co2")
    await _svc(hass, "set_fan_mode", fan_mode="off")  # door opens
    await _poll(hass, coord)
    await _save(hass, "door")
    assert _attrs(hass)["saved_states"] == {"co2": "high", "door": "off"}
    await _restore(hass, "door")
    await _poll(hass, coord)
    assert _attrs(hass)["saved_states"] == {"co2": "high"}
    assert coord.drift.saved[KEY]["co2"]["fan"] == "high"


async def test_persists_across_reload_and_diagnostics(hass, hass_storage):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    await _poll(hass, coord)
    await _save(hass, "Door ")  # trimmed, not case sensitive
    await coord.drift.store.async_save(coord.drift._data_to_save())
    assert hass_storage[store_key("e1")]["data"]["saved"][KEY]["door"]["fan"] == "medium"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _attrs(hass)["saved_states"] == {"door": "medium"}
    from custom_components.msheireb.diagnostics import async_get_config_entry_diagnostics
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "medium" in json.dumps(diag) and diag_find(diag)[KEY]["door"]["fan"] == "medium"


async def test_override_on_restore_keeps_override_in_charge(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _svc(hass, "set_fan_mode", fan_mode="low")
    await _poll(hass, coord)
    await _save(hass, "co2")
    assert coord.drift.saved[KEY]["co2"] == {"fan": "low", "power": True, "target": 20.0}
    _room_temp(api, 19.0)  # override pauses the room (idle)
    await _poll(hass, coord)
    assert not _power(api)
    assert _attrs(hass)["override_state"] == "idle"
    await _save(hass, "idle")  # idle: the picked speed, still cool
    assert coord.drift.saved[KEY]["idle"]["fan"] == "low" and coord.drift.saved[KEY]["idle"]["power"] is True
    await _svc(hass, "set_fan_mode", fan_mode="high")
    await _set_temp(hass, 21.0)
    api.commands.clear()
    await _restore(hass, "co2")
    assert api.commands == [] and not _power(api)  # idle: nothing pressed, override governs power
    a = _attrs(hass)
    assert a["temperature"] == 20.0 and a["fan_mode"] == "low"
    _expire(coord)
    _room_temp(api, 21.0)
    await _poll(hass, coord)
    assert _power(api)  # override powers it on with the restored speed


async def test_override_on_restore_off_turns_room_off(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _svc(hass, "set_fan_mode", fan_mode="off")
    await _poll(hass, coord)
    await _save(hass, "door")
    await _svc(hass, "turn_on")
    await _poll(hass, coord)
    await _restore(hass, "door")
    await _poll(hass, coord)
    assert hass.states.get(DINING).state == "off" and not _power(api)
