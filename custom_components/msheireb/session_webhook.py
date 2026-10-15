"""One-tap "Send to HA" handoff of the portal session (v0.3.24).

While a portal-session sign-in step is open, a POST-only webhook is registered. Its id is random and
kept in hass storage so the user's bookmark stays the same across sign-ins. The handler only accepts
data while a session step is actually waiting; the value is never logged.
"""
from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store

from .const import DOMAIN

if TYPE_CHECKING:
    from .config_flow import MsheirebConfigFlow

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.session_webhook"
STORAGE_VERSION = 1
MAX_BODY = 64 * 1024
_DATA = f"{DOMAIN}_session_webhook"


def _state(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.setdefault(_DATA, {"id": None, "flow": None})


async def async_get_webhook_id(hass: HomeAssistant) -> str:
    state = _state(hass)
    if state["id"]:
        return state["id"]
    store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load() or {}
    wid = data.get("webhook_id")
    if not wid:
        wid = secrets.token_hex(32)
        await store.async_save({"webhook_id": wid})
    state["id"] = wid
    return wid


def webhook_url(hass: HomeAssistant, webhook_id: str) -> str | None:
    for kwargs in ({"prefer_external": True}, {"allow_external": False}):
        try:
            return get_url(hass, **kwargs).rstrip("/") + webhook.async_generate_path(webhook_id)
        except NoURLAvailableError:
            continue
    return None


def send_bookmarklet(url: str) -> str:
    return (
        "javascript:(()=>{const v=localStorage.getItem('msheireb-auth');if(!v){alert('Not signed in to the "
        "Msheireb portal on this page. Open web-prod.mp-mdd.com, sign in, then tap this bookmark again.');"
        "return;}fetch(" + _js_str(url) + ",{method:'POST',mode:'no-cors',headers:{'Content-Type':"
        "'text/plain'},body:v}).then(()=>alert('Sent to Home Assistant, go back and tap Submit'),"
        "()=>alert('Could not reach Home Assistant at this address. Use the copy bookmark instead.'));})()"
    )


def _js_str(value: str) -> str:
    out = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "").replace("\r", "")
    return f"'{out}'"


async def async_attach(hass: HomeAssistant, flow: MsheirebConfigFlow) -> str:
    """Make `flow` the waiting session step; register the webhook if needed. Returns the id."""
    wid = await async_get_webhook_id(hass)
    state = _state(hass)
    state["flow"] = flow
    try:
        webhook.async_register(
            hass, DOMAIN, "Msheireb portal session", wid, _handle, allowed_methods=["POST"]
        )
    except ValueError:
        pass  # already registered by an earlier step render
    return wid


def async_detach(hass: HomeAssistant, flow: MsheirebConfigFlow) -> None:
    state = _state(hass)
    if state["flow"] is not flow:
        return
    state["flow"] = None
    if state["id"]:
        webhook.async_unregister(hass, state["id"])


async def _handle(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
    flow = _state(hass)["flow"]
    if flow is None or not flow.session_waiting:
        _LOGGER.debug("Session webhook called while no sign-in step is waiting; ignored")
        return web.Response(status=409, text="No Home Assistant sign-in step is waiting")
    raw = await request.content.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        return web.Response(status=413)
    ok = await flow.async_receive_session(raw.decode("utf-8", "replace"))
    return web.Response(status=200 if ok else 400, text="ok" if ok else "rejected")
