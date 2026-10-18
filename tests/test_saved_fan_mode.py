"""v0.3.25: saved_fan_mode attribute (remembered speed re-applied on the next turn-on)."""
from custom_components.msheireb.drift import store_key

from .test_off_fan_memory import fake_api, KEY, DINING, DATA  # noqa: F401
from .test_off_fan_memory import _setup, _svc, _poll


def _saved(hass):
    return hass.states.get(DINING).attributes.get("saved_fan_mode")


async def test_saved_fan_mode_follows_set_fan_mode(hass):
    entry, coord, api = await _setup(hass)
    assert _saved(hass) is None  # nothing remembered/desired yet
    for fan in ("medium", "low", "auto", "high"):
        await _svc(hass, "set_fan_mode", fan_mode=fan)
        await _poll(hass, coord)
        assert _saved(hass) == fan


async def test_saved_fan_mode_unchanged_by_fan_off(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    await _poll(hass, coord)
    await _svc(hass, "set_fan_mode", fan_mode="off")  # room off, fan -> Auto
    await _poll(hass, coord)
    s = hass.states.get(DINING)
    assert s.state == "off" and s.attributes["fan_mode"] == "off"
    assert _saved(hass) == "medium"  # never 'off', never the Auto pressed by the off sequence
    await _svc(hass, "turn_on")
    await _poll(hass, coord)
    assert hass.states.get(DINING).attributes["fan_mode"] == "medium"


async def test_saved_fan_mode_falls_back_to_desired_real_speed(hass):
    entry, coord, api = await _setup(hass)
    coord.drift.prev_fan.clear()
    coord.drift.desired[KEY] = {"fan": "low"}
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert _saved(hass) == "low"
    coord.drift.desired[KEY] = {"fan": "off"}  # never shown
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert _saved(hass) is None


async def test_saved_fan_mode_persists_across_restart(hass, hass_storage):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    await _svc(hass, "set_fan_mode", fan_mode="off")
    await _poll(hass, coord)
    await coord.drift.store.async_save(coord.drift._data_to_save())
    assert hass_storage[store_key("e1")]["data"]["prev_fan"] == {KEY: "medium"}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _saved(hass) == "medium"


async def test_off_right_after_choosing_speed_remembers_the_choice(hass):
    """Regression: turning off before the next poll used to remember the stale reading (high)."""
    entry, coord, api = await _setup(hass)  # fixture: on, fan high
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    await _svc(hass, "turn_off")  # no poll in between: the stored reading still says high
    assert coord.drift.prev_fan[KEY] == "medium"
    assert _saved(hass) == "medium"


def _last(hass):
    return hass.states.get(DINING).attributes.get("last_fan_mode")


async def test_last_fan_mode_tracks_choices_including_off(hass):
    entry, coord, api = await _setup(hass)
    assert _last(hass) is None
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    await _poll(hass, coord)
    assert _last(hass) == "medium" and _saved(hass) == "medium"
    await _svc(hass, "set_fan_mode", fan_mode="off")
    await _poll(hass, coord)
    assert _last(hass) == "off" and _saved(hass) == "medium"
    await _svc(hass, "set_fan_mode", fan_mode="low")  # while off: turns on at low
    await _poll(hass, coord)
    assert _last(hass) == "low" and _saved(hass) == "low"
    await _svc(hass, "turn_off")
    await _poll(hass, coord)
    assert _last(hass) == "off" and _saved(hass) == "low"
    await _svc(hass, "set_fan_mode", fan_mode="auto")
    await _poll(hass, coord)
    assert _last(hass) == "auto"


async def test_last_fan_mode_ignores_invalid_mode(hass):
    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.exceptions import ServiceValidationError
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="high")
    with pytest.raises((HomeAssistantError, ServiceValidationError)):
        await _svc(hass, "set_fan_mode", fan_mode="turbo")
    assert coord.drift.last_fan[KEY] == "high"


async def test_last_fan_mode_persists_across_restart(hass, hass_storage):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    await _svc(hass, "set_fan_mode", fan_mode="off")
    await _poll(hass, coord)
    await coord.drift.store.async_save(coord.drift._data_to_save())
    assert hass_storage[store_key("e1")]["data"]["last_fan"] == {KEY: "off"}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _last(hass) == "off" and _saved(hass) == "medium"
