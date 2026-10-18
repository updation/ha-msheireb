"""v0.3.28: Auto must be pressed after the override powers a room off, even if the unit reports the
power change late (after the 10 s settle + 30 s power-confirm window)."""
import pytest

from .test_power_fan_sequence import (  # noqa: F401
    DINING, KEY, _advance, _setup, _state, _svc, ac,
)

pytestmark = pytest.mark.real_sequencing

OVR = "switch.msheireb_demo01_dining_room_max_cooling_override"


def _room_temp(api, t):
    for a in api.state["rooms"][0]["devices"][0]["status"]["analog"]:
        if a["label"] == "HVAC Room Temperature":
            a["current_status"] = int(round(t * 100))


async def _override_on(hass, coord, target=20.0):
    coord.drift.set_desired(KEY, {"target": target})  # (the AC model has no setpoint buttons)
    await hass.services.async_call("switch", "turn_on", {"entity_id": OVR}, blocking=True)
    await hass.async_block_till_done()


def _assert_auto_follows_power(ac, latency):
    times = dict((role, t) for role, t in reversed(ac.press_times))  # first press of each role
    assert "auto" in times, ac.presses
    power_off_at = times["power"] + latency
    assert times["auto"] >= power_off_at  # never before the unit is really off
    assert times["auto"] - power_off_at <= 25, ac.press_times  # soon after it reads off, not minutes later
    assert times["auto"] - times["power"] < 200  # far below the 300 s regular poll
    assert ac.presses.count("power") == 1 and ac.presses.count("auto") == 1
    assert not ac.power and ac.fan == "auto"


@pytest.mark.parametrize("latency", [7.0, 45.0, 70.0])
@pytest.mark.parametrize("refresh_fails", [False, True])
async def test_override_idle_presses_auto_even_when_power_reads_off_late(hass, ac, freezer, latency, refresh_fails):
    ac.power, ac.fan, ac.power_latency = True, "medium", latency
    entry, coord = await _setup(hass)
    await _override_on(hass, coord)
    api = coord.api
    _room_temp(api, 19.0)
    await coord.async_refresh()
    await hass.async_block_till_done()
    if refresh_fails:  # the portal poll fails while we wait: only a dedicated follow-up can finish the job
        from custom_components.msheireb.api import MsheirebConnectionError
        from .fake_api import FakeApi
        FakeApi.fail_fetch = MsheirebConnectionError("down")
    await _advance(hass, freezer, 200)
    _assert_auto_follows_power(ac, latency)


@pytest.mark.parametrize("latency", [45.0, 70.0])
async def test_normal_off_presses_auto_even_when_power_reads_off_late(hass, ac, freezer, latency):
    ac.power, ac.fan, ac.power_latency = True, "medium", latency
    entry, coord = await _setup(hass)
    await _svc(hass, "turn_off")
    await _advance(hass, freezer, 200)
    _assert_auto_follows_power(ac, latency)


# ---- v0.3.28: extra refresh 10 s after a target change / override switch-on (debounced, own timer)
def _count_refreshes(coord):
    calls = []
    orig = coord.async_refresh

    async def counted(*a, **k):
        calls.append(1)
        return await orig(*a, **k)

    coord.async_refresh = counted
    return calls


async def _set_target(hass, t):
    await hass.services.async_call("climate", "set_temperature", {"entity_id": DINING, "temperature": t},
                                   blocking=True)


async def test_target_change_schedules_refresh_after_10s_debounced(hass, ac, freezer):
    ac.power, ac.fan = True, "medium"
    entry, coord = await _setup(hass)
    coord.drift.override[KEY] = True  # override: no setpoint presses, so no other refresh is involved
    calls = _count_refreshes(coord)
    await _set_target(hass, 22.0)
    await _advance(hass, freezer, 6)
    await _set_target(hass, 23.0)  # restarts the 10 s wait
    await _advance(hass, freezer, 6)
    assert calls == []  # 12 s after the first change, only 6 s after the last
    await _advance(hass, freezer, 5)
    assert len(calls) == 1  # 10 s after the LAST change


async def test_override_switch_on_schedules_refresh_and_does_not_use_shared_timer(hass, ac, freezer):
    ac.power, ac.fan = True, "medium"
    entry, coord = await _setup(hass)
    coord.drift.set_desired(KEY, {"target": 20.0})
    calls = _count_refreshes(coord)
    await hass.services.async_call("switch", "turn_on", {"entity_id": OVR}, blocking=True)
    coord.schedule_refresh_after_command()  # the shared timer replaced/cancelled meanwhile ...
    coord.schedule_refresh_after_command()
    await _advance(hass, freezer, 4)
    assert len(calls) == 0  # (5 s shared timer not due yet)
    await _advance(hass, freezer, 2)
    assert len(calls) == 1  # shared timer fired
    await _advance(hass, freezer, 5)
    assert len(calls) == 2  # ... the override's own 10 s refresh still ran
