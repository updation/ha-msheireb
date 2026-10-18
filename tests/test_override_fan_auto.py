"""v0.3.27: Max cooling override idle = power off + fan Auto; resume = power on + remembered speed."""
from .test_drift import fake_api, fast_sleep, KEY, DINING, _dining, _poll  # noqa: F401
from .test_max_cooling_override import _setup_override, _room_temp, _power, _expire
from .fake_api import FakeApi

POWER, AUTO, LOW, MED, HIGH = 1, 2, 3, 4, 5


def _fan_on(api):
    return [k for k, d in _dining(api)[1].items() if k.startswith("HVAC Fan") and d["current_status"] == "ON"]


def _sns(api):
    return [c["sn"] for c in api.commands]


def _attrs(hass):
    return hass.states.get(DINING).attributes


async def _medium(hass, coord, api):
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "medium"},
                                   blocking=True)
    await _poll(hass, coord)
    api.commands.clear()


def _finish(coord):
    rec = coord.health.commands.get(KEY)
    if rec is not None:
        rec.sent_monotonic -= 1000


async def test_idle_is_power_off_plus_auto_and_card_keeps_speed(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _medium(hass, coord, api)
    _room_temp(api, 19.0)
    await _poll(hass, coord)
    assert _sns(api) == [POWER, AUTO]  # power first, verified off, then fan Auto
    assert not _power(api) and _fan_on(api) == ["HVAC Fan Auto"]
    for _ in range(2):
        _finish(coord)
        await _poll(hass, coord)
    assert coord.health.commands[KEY].result == "confirmed"
    s = hass.states.get(DINING)
    assert s.state == "cool" and s.attributes["fan_mode"] == "medium"  # card: Cool + picked speed
    assert s.attributes["override_state"] == "idle"
    assert s.attributes["saved_fan_mode"] == "medium" and s.attributes["last_fan_mode"] == "medium"
    assert coord.drift.prev_fan[KEY] == "medium" and coord.drift.desired[KEY]["fan"] == "medium"
    assert coord.drift.events == 0


async def test_resume_is_power_on_then_remembered_speed_and_cycles(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _medium(hass, coord, api)
    for _cycle in range(2):
        _room_temp(api, 19.0)
        _expire(coord)
        await _poll(hass, coord)
        assert _sns(api) == [POWER, AUTO]
        _finish(coord)
        await _poll(hass, coord)
        api.commands.clear()
        _room_temp(api, 21.0)
        _expire(coord)
        await _poll(hass, coord)
        assert _sns(api) == [POWER, MED]
        assert _power(api) and _fan_on(api) == ["HVAC Fan Medium"]
        _finish(coord)
        await _poll(hass, coord)
        api.commands.clear()
        a = _attrs(hass)
        assert a["fan_mode"] == "medium" and a["saved_fan_mode"] == "medium" and a["last_fan_mode"] == "medium"
        assert coord.drift.prev_fan[KEY] == "medium"  # never Auto from the cycling


async def test_idle_off_power_press_missed_is_retried(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _medium(hass, coord, api)
    FakeApi.drop_pulses = 1  # the AC misses the power press
    _room_temp(api, 19.0)
    await _poll(hass, coord)
    assert _sns(api) == [POWER]  # power never changed: nothing else pressed
    assert _power(api)
    api.commands.clear()
    _finish(coord)
    await _poll(hass, coord)  # window over -> retry: power, then Auto
    await hass.async_block_till_done()
    assert _sns(api) == [POWER, AUTO]
    assert not _power(api) and _fan_on(api) == ["HVAC Fan Auto"]
    for _ in range(2):
        _finish(coord)
        await _poll(hass, coord)
    rec = coord.health.commands[KEY]
    assert rec.result == "confirmed" and rec.retries == 1


async def test_idle_off_auto_press_missed_is_retried(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _medium(hass, coord, api)
    orig = api.async_send_command

    async def drop_auto_once(ip, sn, *a):
        if sn == AUTO and not getattr(drop_auto_once, "done", False):
            drop_auto_once.done = True
            api.commands.append({"sn": sn})
            return {"ok": True}
        return await orig(ip, sn, *a)

    api.async_send_command = drop_auto_once
    _room_temp(api, 19.0)
    await _poll(hass, coord)
    assert not _power(api) and _fan_on(api) == ["HVAC Fan Medium"]  # off but still blowing
    api.commands.clear()
    _finish(coord)
    await _poll(hass, coord)
    await hass.async_block_till_done()
    assert _sns(api) == [AUTO]  # retry sends only the missing Auto
    assert _fan_on(api) == ["HVAC Fan Auto"]


async def test_speed_chosen_while_idle_is_stored_and_used_on_resume(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _medium(hass, coord, api)
    _room_temp(api, 19.0)
    await _poll(hass, coord)
    _finish(coord)
    await _poll(hass, coord)
    api.commands.clear()
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "high"},
                                   blocking=True)
    await hass.async_block_till_done()
    assert api.commands == [] and coord.drift.is_paused(KEY)  # nothing pressed on the idle unit
    a = _attrs(hass)
    assert a["fan_mode"] == "high" and a["saved_fan_mode"] == "high" and a["last_fan_mode"] == "high"
    _room_temp(api, 21.0)
    _expire(coord)
    await _poll(hass, coord)
    assert _sns(api) == [POWER, HIGH]
