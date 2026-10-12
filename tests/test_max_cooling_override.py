"""v0.3.21: per-room Max cooling override (power cycling with hysteresis around the target)."""
import time

from custom_components.msheireb import drift as drift_mod
from custom_components.msheireb.const import OVERRIDE_MIN_TOGGLE_INTERVAL

from .test_drift import (  # noqa: F401
    DINING,
    KEY,
    _dining,
    _poll,
    _set_temp,
    _setup,
    external_power,
    fake_api,
    fast_sleep,
)

OVR = "switch.msheireb_demo01_dining_room_max_cooling_override"


def _room_temp(api, t):
    _dining(api)[0]["HVAC Room Temperature"]["current_status"] = int(round(t * 100))


def _power(api):
    return _dining(api)[1]["HVAC AC"]["current_status"] == "ON"


async def _switch(hass, on):
    await hass.services.async_call("switch", "turn_on" if on else "turn_off", {"entity_id": OVR}, blocking=True)
    await hass.async_block_till_done()


async def _setup_override(hass, target=20.0, options=None):
    entry, coord, api = await _setup(hass, {"drift_grace": 0, **(options or {})})
    await _set_temp(hass, target)
    await _poll(hass, coord)
    await _switch(hass, True)
    api.commands.clear()
    return entry, coord, api


def _expire(coord):
    coord.drift.override_last_toggle.clear()


async def test_switch_default_off_and_persisted(hass):
    entry, coord, api = await _setup(hass)
    assert hass.states.get(OVR).state == "off"
    assert hass.states.get(DINING).attributes["max_cooling_override"] is False
    assert "override_state" not in hass.states.get(DINING).attributes
    await _switch(hass, True)
    assert coord.drift._data_to_save()["override"] == {KEY: True}


async def test_off_below_threshold_not_at_it(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    _room_temp(api, 19.5)
    await _poll(hass, coord)
    assert _power(api) and api.commands == []  # 19.5 is not < 19.5
    _room_temp(api, 19.4)
    await _poll(hass, coord)
    assert not _power(api)
    s = hass.states.get(DINING)
    assert s.state == "cool" and s.attributes["temperature"] == 20.0
    assert s.attributes["override_state"] == "idle" and s.attributes["max_cooling_override"] is True
    assert s.attributes["fan_mode"] != "off"


async def test_on_at_threshold_not_below(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    _room_temp(api, 19.0)
    await _poll(hass, coord)
    assert not _power(api)
    _expire(coord)
    api.commands.clear()
    _room_temp(api, 20.4)
    await _poll(hass, coord)
    assert not _power(api) and api.commands == []
    _room_temp(api, 20.5)
    await _poll(hass, coord)
    assert _power(api)
    await _poll(hass, coord)
    assert hass.states.get(DINING).attributes["override_state"] == "cooling"
    assert _dining(api)[1]["HVAC Fan High"]["current_status"] == "ON"  # fan speed restored/kept


async def test_no_setpoint_presses_while_override_on(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _set_temp(hass, 22.0)
    await hass.async_block_till_done()
    assert api.commands == []
    assert hass.states.get(DINING).attributes["temperature"] == 22.0
    assert coord.drift.desired[KEY]["target"] == 22.0


async def test_no_cycling_at_min_temperature(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 18.0)  # default min 18
    _room_temp(api, 15.0)
    await _poll(hass, coord)
    assert _power(api) and api.commands == []
    # paused (e.g. target lowered to min while paused): resumed, even inside the toggle interval
    await _switch(hass, False)
    await _switch(hass, True)
    coord.drift.override_paused.add(KEY)
    external_power(api, False)
    await _poll(hass, coord)
    assert _power(api)
    assert KEY not in coord.drift.override_paused


async def test_user_off_stops_cycling(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": DINING, "hvac_mode": "off"}, blocking=True)
    await _poll(hass, coord)
    assert not _power(api) and coord.drift.desired[KEY]["power"] is False
    api.commands.clear()
    _room_temp(api, 25.0)
    _expire(coord)
    await _poll(hass, coord)
    await _poll(hass, coord)
    assert not _power(api) and api.commands == []
    assert hass.states.get(DINING).state == "off"


async def test_user_fan_off_stops_cycling_when_paused(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    _room_temp(api, 19.0)
    await _poll(hass, coord)
    assert coord.drift.is_paused(KEY)
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "off"}, blocking=True)
    await hass.async_block_till_done()
    assert not coord.drift.is_paused(KEY) and coord.drift.desired[KEY]["power"] is False
    _room_temp(api, 25.0)
    _expire(coord)
    api.commands.clear()
    await _poll(hass, coord)
    assert not _power(api) and api.commands == []


async def test_no_false_drift_or_restore_while_paused(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    _room_temp(api, 19.0)
    await _poll(hass, coord)
    assert not _power(api)
    api.commands.clear()
    for _ in range(4):
        await _poll(hass, coord)
    assert coord.drift.events == 0 and KEY not in coord.drift.episodes and api.commands == []
    # and while powered with the AC at a different setpoint than the shown target
    _expire(coord)
    _room_temp(api, 21.0)
    await _poll(hass, coord)
    for _ in range(3):
        await _poll(hass, coord)
    assert _power(api) and coord.drift.events == 0


async def test_min_toggle_interval(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    _room_temp(api, 19.0)
    await _poll(hass, coord)
    assert not _power(api)
    api.commands.clear()
    _room_temp(api, 22.0)
    await _poll(hass, coord)
    assert not _power(api) and api.commands == []  # too soon after the last toggle
    coord.drift.override_last_toggle[KEY] -= OVERRIDE_MIN_TOGGLE_INTERVAL + 1
    await _poll(hass, coord)
    assert _power(api)


async def test_override_off_resumes_normal_behaviour(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    _room_temp(api, 19.0)
    await _poll(hass, coord)
    assert not _power(api)
    api.commands.clear()
    await _switch(hass, False)
    assert _power(api)  # powered back on
    assert _dining(api)[0]["HVAC Setpoint"]["current_status"] == 2000  # setpoint applied normally
    s = hass.states.get(DINING)
    assert "override_state" not in s.attributes and s.attributes["max_cooling_override"] is False
    await _poll(hass, coord)
    assert s.state == "cool" and coord.drift.events == 0
    api.commands.clear()
    _room_temp(api, 15.0)
    await _poll(hass, coord)
    assert _power(api) and api.commands == []  # no more cycling
