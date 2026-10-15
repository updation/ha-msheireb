"""Regression (v0.3.0 bug): no room entities after the config flow / a restart.

Live: /user/refresh-token returns NO `contracts` key (only /user/login does). v0.3.0 built the
API client from stored tokens without contracts, refreshed, got none, and silently created no
room entities. These tests use the real client + real response shapes (redacted).
"""
import json
import pathlib
import time

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.msheireb.const import API_BASE, DOMAIN

SHAPES = json.loads((pathlib.Path(__file__).parent / "fixtures" / "live_shapes.json").read_text())


def _mock_portal(aioclient_mock, smart_home_payload):
    aioclient_mock.post(f"{API_BASE}/user/login", json=SHAPES["login"])
    aioclient_mock.post(f"{API_BASE}/user/refresh-token", json=SHAPES["refresh"])
    aioclient_mock.get(f"{API_BASE}/user/contracts/4242/smart-home",
                       json={"status": "success", "message": None, "data": smart_home_payload})
    aioclient_mock.get(f"{API_BASE}/controller-status/10.0.0.10", json=SHAPES["controller_status"])
    aioclient_mock.get(f"{API_BASE}/smart-lock/contract-status/4242", json=SHAPES["lock_status"])


def _calls(aioclient_mock, suffix):
    return [c for c in aioclient_mock.mock_calls if str(c[1]).endswith(suffix)]


async def _setup(hass, data):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="9001", data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_created_when_entry_has_tokens_but_no_contracts(hass, aioclient_mock, smart_home_payload):
    """Exactly the v0.3.0 install state: tokens stored by the flow, contracts not stored."""
    _mock_portal(aioclient_mock, smart_home_payload)
    entry = await _setup(hass, {"email": "me@example.com", "password": "pw",
                                "access_token": "OLD", "refresh_token": "OLD_R", "expires_at": time.time() - 5})
    climates = sorted(s.entity_id for s in hass.states.async_all("climate"))
    assert climates == ["climate.msheireb_demo01_bedroom_1", "climate.msheireb_demo01_dining_room",
                        "climate.msheireb_demo01_master_bedroom"]
    assert len(hass.states.async_all("switch")) == 6  # auto-restore + max cooling override per room
    assert hass.states.get("climate.msheireb_demo01_dining_room").attributes["current_temperature"] == 21.5
    assert len(_calls(aioclient_mock, "/user/login")) == 1  # one login to obtain the contract list
    assert entry.data["contracts"][0]["id"] == 4242  # persisted for next restart
    assert "buildingId" not in entry.data["contracts"][0]  # slimmed


async def test_restart_with_stored_contracts_needs_no_login(hass, aioclient_mock, smart_home_payload):
    _mock_portal(aioclient_mock, smart_home_payload)
    await _setup(hass, {"email": "me@example.com", "password": "pw", "access_token": "A", "refresh_token": "R",
                        "expires_at": time.time() + 80000,
                        "contracts": [{"id": 4242, "unitId": "DEMO01"}]})
    assert len(hass.states.async_all("climate")) == 3
    assert _calls(aioclient_mock, "/user/login") == []
    assert _calls(aioclient_mock, "/user/refresh-token") == []


async def test_refresh_without_contracts_keeps_existing_list(hass, aioclient_mock, smart_home_payload):
    """A token refresh (no contracts in the response) must not wipe the contract list."""
    _mock_portal(aioclient_mock, smart_home_payload)
    entry = await _setup(hass, {"email": "me@example.com", "password": "pw", "access_token": "A",
                                "refresh_token": "R", "expires_at": time.time() - 5,
                                "contracts": [{"id": 4242, "unitId": "DEMO01"}]})
    assert len(_calls(aioclient_mock, "/user/refresh-token")) == 1
    assert _calls(aioclient_mock, "/user/login") == []
    assert len(hass.states.async_all("climate")) == 3
    assert entry.data["refresh_token"] == "REFRESHED_REFRESH"
    assert entry.data["contracts"] == [{"id": 4242, "unitId": "DEMO01"}]
    # polling again does not refresh/login repeatedly
    await entry.runtime_data.async_refresh(); await hass.async_block_till_done()
    assert len(_calls(aioclient_mock, "/user/refresh-token")) == 1
    assert _calls(aioclient_mock, "/user/login") == []


async def test_config_flow_stores_contracts(hass, aioclient_mock, smart_home_payload):
    from unittest.mock import patch
    from homeassistant import config_entries
    _mock_portal(aioclient_mock, smart_home_payload)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "password"})
    with patch("custom_components.msheireb.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "me@example.com", "password": "pw"})
    assert result["data"]["contracts"] == [{"id": 4242, "unitId": "DEMO01", "unitName": "000/00/000/00",
                                            "externalId": "t0000000", "type": "Residential", "status": "active",
                                            "startDate": "2025-01-01", "endDate": "2027-01-01"}]
