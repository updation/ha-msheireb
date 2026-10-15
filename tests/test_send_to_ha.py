"""v0.3.24: one-tap Send to HA (session webhook)."""
import logging
import shutil
import subprocess
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components import webhook
from homeassistant.data_entry_flow import FlowResultType

from custom_components.msheireb import session_webhook
from custom_components.msheireb.const import DOMAIN

from .fake_api import FakeApi
from .test_portal_session import ENTRY_DATA, _start, fake_api  # noqa: F401

SECRET = '{"state":{"refreshToken":"SUPERSECRET-RT-123","accessToken":"X"},"version":0}'


class _Content:
    def __init__(self, body):
        self._b = body.encode()

    async def read(self, n=-1):
        return self._b if n < 0 else self._b[:n]


class _Req:
    def __init__(self, body):
        self.content = _Content(body)


def _handlers(hass):
    return hass.data.get(webhook._HANDLERS, {})


async def _post(hass, body=SECRET):
    wid = await session_webhook.async_get_webhook_id(hass)
    return await session_webhook._handle(hass, wid, _Req(body))


async def test_rejected_when_no_flow_waiting(hass):
    resp = await _post(hass)
    assert resp.status == 409


async def test_registered_post_only_and_stable_id(hass):
    await hass.config.async_update(external_url="https://example.ui.nabu.casa")
    result = await _start(hass)
    wid = await session_webhook.async_get_webhook_id(hass)
    assert _handlers(hass)[wid].allowed_methods == frozenset({"POST"})
    assert wid in result["description_placeholders"]["send_bookmarklet"]
    assert "https://example.ui.nabu.casa/api/webhook/" + wid in result["description_placeholders"]["send_bookmarklet"]
    hass.config_entries.flow.async_abort(result["flow_id"])
    hass.data.pop(session_webhook._DATA)  # simulate restart: id comes back from storage
    result = await _start(hass)
    assert await session_webhook.async_get_webhook_id(hass) == wid
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_parse_fills_flow_and_empty_submit_uses_it(hass):
    result = await _start(hass)
    resp = await _post(hass)
    assert resp.status == 200
    assert FakeApi.sign_in_contracts is None or isinstance(FakeApi.sign_in_contracts, list)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["refresh_token"] == "SR"


async def test_empty_submit_without_received_value_errors(hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"refresh_token": ""})
    assert result["errors"] == {"base": "empty_session"}
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_garbage_and_rejected_session(hass):
    result = await _start(hass)
    assert (await _post(hass, "two words")).status == 400
    FakeApi.fail_refresh = __import__("custom_components.msheireb.api", fromlist=["x"]).MsheirebAuthError("no")
    assert (await _post(hass)).status == 400
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_unregistered_on_finish(hass):
    result = await _start(hass)
    wid = await session_webhook.async_get_webhook_id(hass)
    await _post(hass)
    await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert wid not in _handlers(hass)
    assert (await _post(hass)).status == 409


async def test_unregistered_on_abort(hass):
    result = await _start(hass)
    wid = await session_webhook.async_get_webhook_id(hass)
    assert wid in _handlers(hass)
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert wid not in _handlers(hass)
    assert (await _post(hass)).status == 409


async def test_reauth_send_to_ha(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={**ENTRY_DATA, "password": ""}, unique_id="1263")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_portal_session"
    assert (await _post(hass)).status == 200
    with patch("homeassistant.config_entries.ConfigEntries.async_reload", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["refresh_token"] == "SR"


def test_send_bookmarklet_node_syntax(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = session_webhook.send_bookmarklet("https://x.ui.nabu.casa/api/webhook/ab'c")
    assert js.startswith("javascript:")
    for s in ("mode:'no-cors'", "method:'POST'", "'Content-Type':'text/plain'", "body:v",
              "Sent to Home Assistant, go back and tap Submit", "Not signed in"):
        assert s in js
    f = tmp_path / "b.js"
    f.write_text(js.removeprefix("javascript:"))
    subprocess.run([node, "--check", str(f)], check=True)


async def test_no_secret_in_logs(hass, caplog):
    caplog.set_level(logging.DEBUG)
    result = await _start(hass)
    await _post(hass)
    await _post(hass, "two words SUPERSECRET-RT-123")
    await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert "SUPERSECRET" not in caplog.text
