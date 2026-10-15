"""v0.3.22: sign in with a portal session (refresh token), no password."""
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.msheireb.api import (
    MsheirebApi,
    MsheirebAuthError,
    MsheirebError,
    parse_contracts,
    parse_refresh_token,
)
from custom_components.msheireb.const import DOMAIN

from .fake_api import FakeApi

ENTRY_DATA = {"email": "me@example.com", "password": "pw", "access_token": "A",
              "refresh_token": "R", "expires_at": 9e9}


@pytest.fixture(autouse=True)
def fake_api(smart_home_payload):
    FakeApi.instances.clear()
    FakeApi.payload = smart_home_payload
    FakeApi.fail_login = None
    FakeApi.fail_refresh = None
    FakeApi.no_contracts = False
    FakeApi.sign_in_contracts = None
    with patch("custom_components.msheireb.MsheirebApi", FakeApi), \
         patch("custom_components.msheireb.config_flow.MsheirebApi", FakeApi):
        yield


@pytest.mark.parametrize("text,expected", [
    ("abc.def.ghi", "abc.def.ghi"),
    ('  "abc.def"\n', "abc.def"),
    ("'tok'", "tok"),
    ('{"state":{"refreshToken":"T1","accessToken":"X"},"version":0}', "T1"),
    ('{"refreshToken":"T2"}', "T2"),
    ('{"refresh_token":"T3"}', "T3"),
    ('{"state":{"refresh_token":"T4"}}', "T4"),
    ('{"state":{"accessToken":"X"}}', None),
    ('{"state": broken', None),
    ("two words", None),
    ("   ", None),
    (None, None),
])
def test_parse_refresh_token(text, expected):
    assert parse_refresh_token(text) == expected


async def _start(hass, step="portal_session"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU and set(result["menu_options"]) == {"password", "portal_session"}
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": step})


async def test_config_flow_via_session(hass):
    result = await _start(hass)
    assert result["step_id"] == "portal_session"
    with patch("custom_components.msheireb.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"refresh_token": '{"state":{"refreshToken":"PASTED","accessToken":"x"},"version":0}'})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["password"] == "" and result["data"]["refresh_token"] == "SR"
    assert result["data"]["contracts"] == [{"id": 4242, "unitId": "DEMO01"}]
    assert result["result"].unique_id == "1263"
    assert result["title"] == "Msheireb (sess@example.com)"


async def test_session_flow_errors(hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"refresh_token": "a b"})
    assert result["errors"] == {"base": "invalid_token"}
    FakeApi.fail_refresh = MsheirebAuthError("rejected")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"refresh_token": "tok"})
    assert result["errors"] == {"base": "invalid_session"}


async def test_reauth_via_session(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="1263")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reauth_portal_session"})
    with patch("custom_components.msheireb.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"refresh_token": "NEWTOK"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["password"] == "" and entry.data["refresh_token"] == "SR"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize("message,err", [
    ("Captcha verification failed", "captcha_required"),
    ("turnstile token missing", "captcha_required"),
    ("Invalid credentials", "invalid_auth"),
])
@pytest.mark.parametrize("reauth", [False, True])
async def test_password_failure_hints(hass, message, err, reauth):
    FakeApi.fail_login = MsheirebAuthError(message)
    if reauth:
        entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="1263")
        entry.add_to_hass(hass)
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "reauth_password"})
    else:
        result = await _start(hass, "password")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "me@example.com", "password": "x"})
    assert result["errors"] == {"base": err}


async def test_empty_password_never_logs_in(hass):
    api = MsheirebApi(None, "me@example.com", "")
    with pytest.raises(MsheirebAuthError):
        await api.async_login()
    assert issubclass(MsheirebAuthError, MsheirebError)


@pytest.mark.parametrize("text,expected", [
    ('{"state":{"refreshToken":"T","contracts":[{"id":1},"x",{"id":2}]},"version":0}', [{"id": 1}, {"id": 2}]),
    ('{"contracts":[{"id":3}]}', [{"id": 3}]),
    ('{"state":{"contracts":[]},"contracts":[{"id":4}]}', [{"id": 4}]),
    ('{"state":{"refreshToken":"T"}}', []),
    ('{"state":{"contracts":"nope"}}', []),
    ("raw.token.value", []),
    ("{broken", []),
    (None, []),
])
def test_parse_contracts(text, expected):
    assert parse_contracts(text) == expected


async def test_sign_in_uses_pasted_contracts_and_persists(hass):
    result = await _start(hass)
    pasted = '{"state":{"refreshToken":"T","contracts":[{"id":77,"unitId":"U7","junk":1}]},"version":0}'
    with patch("custom_components.msheireb.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"refresh_token": pasted})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert FakeApi.sign_in_contracts == [{"id": 77, "unitId": "U7", "junk": 1}]
    assert result["data"]["contracts"] == [{"id": 77, "unitId": "U7"}]


async def test_token_only_without_contracts_errors(hass):
    FakeApi.no_contracts = True
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"refresh_token": "tok"})
    assert FakeApi.sign_in_contracts is None
    assert result["errors"] == {"base": "no_contracts"}


def _api_with_responses(responses):
    api = MsheirebApi(None, "", "")
    calls = []

    async def raw(method, path, *args, **kwargs):
        calls.append((method, path))
        return responses[(method, path)]

    api._raw = raw
    return api, calls


REFRESH_OK = (200, {"data": {"access_token": "AT", "refresh_token": "RT", "expires_in": 100, "user": {"id": 5}}})


async def test_api_sign_in_prefers_given_contracts():
    api, calls = _api_with_responses({("POST", "/user/refresh-token"): REFRESH_OK})
    out = await api.async_sign_in_with_refresh_token("R", contracts=[{"id": 9}, "bad"])
    assert out["contracts"] == [{"id": 9}] and ("GET", "/user/contracts") not in calls


async def test_api_sign_in_falls_back_to_contracts_endpoint():
    api, calls = _api_with_responses({
        ("POST", "/user/refresh-token"): REFRESH_OK,
        ("GET", "/user/contracts"): (200, {"data": [{"id": 8}]}),
    })
    out = await api.async_sign_in_with_refresh_token("R")
    assert out["contracts"] == [{"id": 8}]
    api, _ = _api_with_responses({
        ("POST", "/user/refresh-token"): REFRESH_OK,
        ("GET", "/user/contracts"): (404, {}),
    })
    await api.async_sign_in_with_refresh_token("R")
    assert not api.contracts
