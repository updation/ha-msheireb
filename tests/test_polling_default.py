"""Default 300 s polling: command refresh/confirmation and drift timing are independent of it."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.msheireb.const import DEFAULT_SCAN_INTERVAL, DOMAIN

from .fake_api import FakeApi

DATA = {"email": "me@example.com", "password": "pw", "access_token": "A", "refresh_token": "R", "expires_at": 9e9}
DINING = "climate.msheireb_demo01_dining_room"
LAST = "sensor.msheireb_demo01_dining_room_last_command"


@pytest.fixture(autouse=True)
def fake_api(smart_home_payload):
    FakeApi.instances.clear()
    FakeApi.payload = smart_home_payload
    FakeApi.controller = "connected"
    FakeApi.fail_login = FakeApi.fail_fetch = None
    FakeApi.ignore_commands = False
    FakeApi.drop_pulses = 0
    import custom_components.msheireb.coordinator as co
    real_sleep = co.asyncio.sleep

    async def fast_sleep(_d):
        await real_sleep(0)

    with patch("custom_components.msheireb.MsheirebApi", FakeApi), patch.object(co.asyncio, "sleep", fast_sleep):
        yield


async def _setup(hass, options=None):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="1263", options=options or {}, entry_id="e1")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    api = FakeApi.instances[-1]
    fetches = []
    orig = api.async_get_smart_home

    async def counted(cid):
        fetches.append(cid)
        return await orig(cid)

    api.async_get_smart_home = counted
    return entry, entry.runtime_data, api, fetches


async def _advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_default_interval_is_300_and_saved_option_kept(hass):
    assert DEFAULT_SCAN_INTERVAL == 300
    _, coord, _, _ = await _setup(hass)
    assert coord.update_interval == timedelta(seconds=300)


async def test_saved_option_wins(hass):
    _, coord, _, _ = await _setup(hass, {"scan_interval": 30})
    assert coord.update_interval == timedelta(seconds=30)


async def test_command_confirmed_by_post_command_refresh_not_by_poll(hass, freezer):
    _, coord, api, fetches = await _setup(hass, {"pulse_interval": 0.5})
    await hass.services.async_call("climate", "set_temperature", {"entity_id": DINING, "temperature": 20.0},
                                   blocking=True)
    assert api.commands and hass.states.get(LAST).state == "pending"
    await _advance(hass, freezer, 6)  # ~5 s post-command refresh, far before the 300 s poll
    assert len(fetches) == 1
    assert hass.states.get(LAST).state == "confirmed"
    await _advance(hass, freezer, 60)  # only the extra 10 s override refresh (v0.3.28), no polling
    assert len(fetches) == 2
    await _advance(hass, freezer, 300)  # the extra refresh restarted the 300 s poll timer
    assert len(fetches) == 3


async def test_unconfirmed_command_checked_at_window_end_and_retried(hass, freezer):
    _, coord, api, fetches = await _setup(hass, {"pulse_interval": 0.5, "max_retries": 1})
    FakeApi.ignore_commands = True
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "low"},
                                   blocking=True)
    sent = len(api.commands)
    await _advance(hass, freezer, 6)
    assert hass.states.get(LAST).state == "pending"
    await _advance(hass, freezer, 20)  # window 0.5 + 20 s -> confirm-check refresh -> retry
    assert len(api.commands) > sent
    assert coord.health.commands["4242_501"].retries == 1
    await _advance(hass, freezer, 30)  # retry window over -> final check, still long before 300 s
    assert hass.states.get(LAST).state == "not_confirmed"


async def test_drift_restores_on_second_poll_with_300s_interval(hass, freezer):
    _, coord, api, fetches = await _setup(hass, {"pulse_interval": 0.5})
    await hass.services.async_call("climate", "set_temperature", {"entity_id": DINING, "temperature": 20.0},
                                   blocking=True)
    await _advance(hass, freezer, 6)
    assert hass.states.get(LAST).state == "confirmed"
    api.commands.clear()
    dev = api.state["rooms"][0]["devices"][0]["status"]
    next(a for a in dev["analog"] if a["label"] == "HVAC Setpoint")["current_status"] = 2400  # wall panel
    await _advance(hass, freezer, 300)  # poll 1: external change seen, grace starts
    assert api.commands == []
    await _advance(hass, freezer, 300)  # poll 2: >= 60 s and >= 2 polls -> restore
    assert [c["sn"] for c in api.commands] == [7] * 8  # 24.0 -> 20.0
    assert coord.drift.events == 1
