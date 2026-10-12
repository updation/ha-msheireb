"""Simulate exactly: an entry saved by v0.3.0 (tokens, no contracts) + its registries -> load this version.

Uses the real API client against the real (redacted) response shapes.
"""
import json
import pathlib
import time

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.msheireb.const import API_BASE, DOMAIN, INTEGRATION_VERSION

SHAPES = json.loads((pathlib.Path(__file__).parent / "fixtures" / "live_shapes.json").read_text())
V030_DATA = {  # exactly what the v0.3.0 config flow stored
    "email": "me@example.com",
    "password": "pw",
    "access_token": "OLD",
    "refresh_token": "OLD_R",
    "expires_at": time.time() + 3600,
}
ROOMS = {"climate.msheireb_demo01_bedroom_1", "climate.msheireb_demo01_dining_room",
         "climate.msheireb_demo01_master_bedroom"}


def _portal(aioclient_mock, smart_home, login=None):
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{API_BASE}/user/login", json=login or SHAPES["login"])
    aioclient_mock.post(f"{API_BASE}/user/refresh-token", json=SHAPES["refresh"])
    aioclient_mock.get(f"{API_BASE}/user/contracts/4242/smart-home",
                       json={"status": "success", "message": None, "data": smart_home})
    aioclient_mock.get(f"{API_BASE}/controller-status/10.0.0.10", json=SHAPES["controller_status"])
    aioclient_mock.get(f"{API_BASE}/smart-lock/contract-status/4242", json=SHAPES["lock_status"])


def _v030_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="9001", version=1, title="Msheireb (me@example.com)",
                            data=dict(V030_DATA))
    entry.add_to_hass(hass)
    return entry


async def test_v030_entry_with_old_registry_upgrades_to_room_devices(hass, aioclient_mock, smart_home_payload):
    entry = _v030_entry(hass)
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    # Registry state an earlier install can leave behind: apartment device + a climate entity on it.
    apt = dev_reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "contract_4242")},
                                      name="Msheireb DEMO01")
    dev_reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"account_{entry.entry_id}")},
                                name="Msheireb portal")
    old = ent_reg.async_get_or_create("climate", DOMAIN, "4242_501_climate", config_entry=entry,
                                      device_id=apt.id, suggested_object_id="msheireb_demo01_dining_room")
    _portal(aioclient_mock, smart_home_payload)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert {s.entity_id for s in hass.states.async_all("climate")} == ROOMS
    assert len(hass.states.async_all("switch")) == 6  # auto-restore + max cooling override per room
    assert entry.data["contracts"][0]["id"] == 4242

    # per-room devices, attached to the apartment, carrying the running version
    apt = next(d for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
               if (DOMAIN, "contract_4242") in d.identifiers)
    assert apt.sw_version == INTEGRATION_VERSION
    rooms = [d for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id) if d.model == "Room HVAC"]
    assert sorted(d.name for d in rooms) == ["Bedroom 1", "Dining Room", "Master Bedroom"]
    assert all(d.via_device_id == apt.id and d.sw_version == INTEGRATION_VERSION for d in rooms)
    for ent_id in ROOMS:
        reg = ent_reg.async_get(ent_id)
        assert dev_reg.async_get(reg.device_id).model == "Room HVAC"
    # the pre-existing entity kept its entity_id and moved to its room device
    moved = ent_reg.async_get(old.entity_id)
    assert dev_reg.async_get(moved.device_id).name == "Dining Room"
    assert hass.states.get("climate.msheireb_demo01_dining_room").name == "Dining Room"
    dining = dev_reg.async_get(moved.device_id)
    names = {e.entity_id for e in er.async_entries_for_device(ent_reg, dining.id)}
    assert names == {"climate.msheireb_demo01_dining_room", "switch.msheireb_demo01_dining_room_auto_restore",
                     "switch.msheireb_demo01_dining_room_max_cooling_override",
                     "sensor.msheireb_demo01_dining_room_last_command",
                     "number.msheireb_demo01_dining_room_target_temperature_offset"}

    # HA restart: entry now has contracts -> same entities, no login
    assert await hass.config_entries.async_unload(entry.entry_id)
    _portal(aioclient_mock, smart_home_payload)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert {s.entity_id for s in hass.states.async_all("climate") if s.state != "unavailable"} == ROOMS
    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/user/login")]


async def test_rooms_appearing_after_first_refresh_are_added(hass, aioclient_mock, smart_home_payload):
    entry = _v030_entry(hass)
    _portal(aioclient_mock, {"apartment_ip": "10.0.0.10", "rooms": []})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.async_all("climate") == []
    _portal(aioclient_mock, smart_home_payload)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert {s.entity_id for s in hass.states.async_all("climate")} == ROOMS
    assert len(hass.states.async_all("switch")) == 6  # auto-restore + max cooling override per room


async def test_no_contracts_is_visible_setup_retry(hass, aioclient_mock, smart_home_payload):
    entry = _v030_entry(hass)
    login = json.loads(json.dumps(SHAPES["login"]))
    login["data"]["contracts"] = []
    _portal(aioclient_mock, smart_home_payload, login=login)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "no contracts" in (entry.reason or "")
