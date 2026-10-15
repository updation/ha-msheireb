"""Async client for the Msheireb resident portal API (mob-prod.mp-mdd.com)."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import API_BASE, DEFAULT_TOKEN_LIFETIME, TOKEN_REFRESH_MARGIN, WEB_ORIGIN

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = aiohttp.ClientTimeout(total=30)


_QUOTES = "'\"`\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f\u00ab\u00bb"
_JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_LONG_WORD_RE = re.compile(r"[A-Za-z0-9_\-.~+/=]{20,}")


def clean_paste(text: str | None) -> str:
    """Trim whitespace and stray (straight or smart) quotes around a pasted value."""
    if not isinstance(text, str):
        return ""
    return text.strip().strip(_QUOTES + " \t\r\n\u200b\ufeff").strip()


def _json_objects(text: str, depth: int = 0) -> list[dict[str, Any]]:
    """JSON objects in the paste: the whole value (also double-encoded), else each balanced one in the text."""
    raw = text.strip().strip("\u200b\ufeff").strip()
    value = clean_paste(text)
    if not value or depth > 3:
        return []
    parsed = None
    for candidate in (raw, value):  # raw first: surrounding quotes may be a JSON string wrapper
        try:
            parsed = json.loads(candidate)
            break
        except ValueError:
            continue
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, str):  # JSON string wrapping the value (double-encoded)
        return _json_objects(parsed, depth + 1)
    found: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    pos = value.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(value, pos)
        except ValueError:
            pos = value.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            found.append(obj)
        pos = value.find("{", end)
    return found


def _token_from_object(obj: dict[str, Any]) -> str | None:
    for scope in (obj, obj.get("state")):
        if isinstance(scope, dict):
            for key in ("refreshToken", "refresh_token"):
                token = scope.get(key)
                if isinstance(token, str) and clean_paste(token):
                    return clean_paste(token)
    return None


def parse_refresh_token(text: str | None) -> str | None:
    """Extract a refresh token from whatever the user pasted.

    Accepts the whole ``msheireb-auth`` JSON (``{"state": {"refreshToken": ...}}``, also
    ``refresh_token`` keys), that JSON wrapped in quotes, a raw token, or either surrounded by
    other text (first balanced JSON object, else a JWT-like token, else a long token-like word).
    """
    value = clean_paste(text)
    if not value:
        return None
    objects = _json_objects(text)
    if objects:
        for obj in objects:
            token = _token_from_object(obj)
            if token:
                return token
        return None  # JSON without a refresh token: do not mistake e.g. the access token for one
    if value[0] in "{[":
        return None
    if not any(ch.isspace() for ch in value):
        return value
    match = _JWT_RE.search(value) or _LONG_WORD_RE.search(value)
    return clean_paste(match.group(0)) if match else None


def parse_contracts(text: str | None) -> list[dict[str, Any]]:
    """Extract the contract list from a pasted ``msheireb-auth`` localStorage value.

    Looks at ``state.contracts`` (zustand persist) and top-level ``contracts``.
    Returns an empty list when the text holds no JSON object or no contracts.
    """
    for obj in _json_objects(text or ""):
        for scope in (obj.get("state"), obj):
            if isinstance(scope, dict) and isinstance(scope.get("contracts"), list):
                contracts = [c for c in scope["contracts"] if isinstance(c, dict)]
                if contracts:
                    return contracts
    return []


class MsheirebError(Exception):
    """Generic API error."""


class MsheirebAuthError(MsheirebError):
    """Credentials rejected / session cannot be restored."""


class MsheirebConnectionError(MsheirebError):
    """Network / server problem (portal unreachable or erroring)."""


class MsheirebNotFoundError(MsheirebError):
    """Resource does not exist (e.g. contract without smart home)."""


TokenCallback = Callable[[str, str, float], None]
StatusCallback = Callable[[str], None]

AUTH_OK = "ok"
AUTH_REFRESHING = "refreshing"
AUTH_RELOGIN = "relogin"
AUTH_FAILED = "failed"


class MsheirebApi:
    """Thin client. Handles bearer auth, refresh-token rotation and re-login."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
        expires_at: float | None = None,
        token_callback: TokenCallback | None = None,
        status_callback: StatusCallback | None = None,
        contracts: list[dict[str, Any]] | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at or 0.0
        self._token_callback = token_callback
        self._status_callback = status_callback
        self.auth_status = AUTH_OK
        self.last_response_ms: float | None = None
        self._auth_lock = asyncio.Lock()
        self.user: dict[str, Any] = {}
        # Contracts only come with /user/login (the refresh response has none), so they are
        # persisted by the caller and passed back in here.
        self.contracts: list[dict[str, Any]] = list(contracts or [])

    # ------------------------------------------------------------------ auth
    def _headers(self, auth: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Origin": WEB_ORIGIN,
            "Referer": f"{WEB_ORIGIN}/",
        }
        if auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    async def _raw(
        self, method: str, path: str, body: Any = None, auth: bool = True
    ) -> tuple[int, Any]:
        start = time.monotonic()
        try:
            async with self._session.request(
                method,
                f"{API_BASE}{path}",
                json=body,
                headers=self._headers(auth),
                timeout=_TIMEOUT,
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    data = None
                self.last_response_ms = round((time.monotonic() - start) * 1000, 1)
                return resp.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise MsheirebConnectionError(f"{method} {path}: {err}") from err

    def _store_session(self, data: dict[str, Any]) -> None:
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        lifetime = data.get("expires_in") or DEFAULT_TOKEN_LIFETIME
        try:
            lifetime = int(lifetime)
        except (TypeError, ValueError):
            lifetime = DEFAULT_TOKEN_LIFETIME
        self.expires_at = time.time() + lifetime
        if isinstance(data.get("user"), dict):
            self.user = data["user"]
        if isinstance(data.get("contracts"), list) and data["contracts"]:
            self.contracts = data["contracts"]
        if self._token_callback:
            self._token_callback(self.access_token, self.refresh_token, self.expires_at)

    async def async_login(self) -> dict[str, Any]:
        """Full login with email/password."""
        if not self._password:
            # Portal-session entries have no password: never attempt a login, ask for reauth.
            raise MsheirebAuthError("no stored password (portal session); sign in again")
        status, data = await self._raw(
            "POST",
            "/user/login",
            {"email": self._email, "password": self._password},
            auth=False,
        )
        payload = (data or {}).get("data") if isinstance(data, dict) else None
        if status == 200 and payload and payload.get("access_token"):
            self._store_session(payload)
            return payload
        if status in (400, 401, 403, 404, 422):
            raise MsheirebAuthError(_message(data) or f"login rejected ({status})")
        raise MsheirebConnectionError(f"login failed ({status}): {_message(data)}")

    async def async_refresh(self) -> bool:
        """Rotate tokens with the refresh token. Returns False if refresh is impossible."""
        if not self.refresh_token:
            return False
        status, data = await self._raw(
            "POST",
            "/user/refresh-token",
            {"refresh_token": self.refresh_token},
            auth=False,
        )
        payload = (data or {}).get("data") if isinstance(data, dict) else None
        if status == 200 and payload and payload.get("access_token"):
            self._store_session(payload)
            return True
        if status >= 500:
            raise MsheirebConnectionError(f"refresh failed ({status})")
        _LOGGER.debug("Token refresh rejected (%s), will re-login", status)
        return False

    async def async_sign_in_with_refresh_token(
        self, refresh_token: str, contracts: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Validate a portal-session refresh token and load the contracts.

        The refresh response carries no contracts. Use the ones from the pasted portal session
        (login response persisted by the web app); otherwise try GET /user/contracts, best effort.
        """
        self.refresh_token = refresh_token
        if not await self.async_refresh():
            raise MsheirebAuthError("refresh token rejected")
        if contracts:
            self.contracts = [c for c in contracts if isinstance(c, dict)]
        if not self.contracts:
            status, data = await self._raw("GET", "/user/contracts")
            payload = (data or {}).get("data") if isinstance(data, dict) else None
            if isinstance(payload, dict):
                payload = payload.get("contracts") or payload.get("data")
            if status == 200 and isinstance(payload, list):
                self.contracts = [c for c in payload if isinstance(c, dict)]
        return {"user": self.user, "contracts": self.contracts}

    def _set_auth_status(self, status: str) -> None:
        if status != self.auth_status:
            self.auth_status = status
            if self._status_callback:
                self._status_callback(status)

    async def _renew(self, force: bool = False) -> None:
        async with self._auth_lock:
            if not force and self.access_token and time.time() < self.expires_at - TOKEN_REFRESH_MARGIN:
                return
            self._set_auth_status(AUTH_REFRESHING)
            try:
                if await self.async_refresh():
                    self._set_auth_status(AUTH_OK)
                    return
                self._set_auth_status(AUTH_RELOGIN)
                await self.async_login()
            except MsheirebAuthError:
                self._set_auth_status(AUTH_FAILED)
                raise
            except MsheirebError:
                # transient: keep previous credentials, report as still refreshing/relogin
                raise
            self._set_auth_status(AUTH_OK)

    async def async_ensure_session(self) -> None:
        """Make sure we have a valid access token."""
        if not self.access_token or time.time() >= self.expires_at - TOKEN_REFRESH_MARGIN:
            await self._renew()

    async def _request(self, method: str, path: str, body: Any = None) -> Any:
        await self.async_ensure_session()
        token_used = self.access_token
        status, data = await self._raw(method, path, body)
        if status == 401:
            if self.access_token == token_used:
                await self._renew(force=True)
            status, data = await self._raw(method, path, body)
            if status == 401:
                self._set_auth_status(AUTH_FAILED)
                raise MsheirebAuthError("unauthorized after token renewal")
        if status == 404:
            raise MsheirebNotFoundError(f"{method} {path} -> 404: {_message(data)}")
        if status >= 500 or status in (408, 429):
            raise MsheirebConnectionError(f"{method} {path} -> {status}: {_message(data)}")
        if status >= 400 or not isinstance(data, dict):
            raise MsheirebError(f"{method} {path} -> {status}: {_message(data)}")
        if data.get("status") not in (None, "success"):
            raise MsheirebError(f"{method} {path}: {_message(data)}")
        return data.get("data")

    # ------------------------------------------------------------------ data
    async def async_get_contracts(self) -> list[dict[str, Any]]:
        await self.async_ensure_session()
        if not self.contracts:
            # Refresh-token responses carry no contracts; only a full login returns them.
            _LOGGER.debug("No stored contracts; logging in to fetch the contract list")
            async with self._auth_lock:
                if not self.contracts:
                    self._set_auth_status(AUTH_RELOGIN)
                    try:
                        await self.async_login()
                    except MsheirebAuthError:
                        self._set_auth_status(AUTH_FAILED)
                        raise
                    self._set_auth_status(AUTH_OK)
            if not self.contracts:
                _LOGGER.warning("The Msheireb account has no linked contracts")
        return [c for c in self.contracts if c.get("id") is not None]

    async def async_get_smart_home(self, contract_id: int | str) -> dict[str, Any]:
        return await self._request("GET", f"/user/contracts/{contract_id}/smart-home") or {}

    async def async_get_controller_status(self, ip: str) -> dict[str, Any]:
        return await self._request("GET", f"/controller-status/{quote(ip.strip(), safe='')}") or {}

    async def async_get_lock_status(self, contract_id: int | str) -> dict[str, Any]:
        return await self._request("GET", f"/smart-lock/contract-status/{contract_id}") or {}

    async def async_unlock_door(self, contract_id: int | str, duration: str = "5s") -> Any:
        """Temporary door unlock, exactly like the web portal's 'Unlock (5s)' button.

        POST /smart-lock/contract-access-point {contract_id, state: "unlock", duration}.
        No PIN/OTP is involved in the portal flow. Errors carry the portal's message.
        """
        body = {"contract_id": int(contract_id), "state": "unlock", "duration": duration}
        status, data = await self._raw_authed("POST", "/smart-lock/contract-access-point", body)
        if status >= 400 or not isinstance(data, dict) or data.get("status") not in (None, "success"):
            raise MsheirebError(_message(data) or f"unlock rejected (HTTP {status})")
        return data.get("data") if isinstance(data, dict) else None

    async def _raw_authed(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """Authenticated call returning (status, json) without generic error mapping."""
        await self.async_ensure_session()
        token_used = self.access_token
        status, data = await self._raw(method, path, body)
        if status == 401:
            if self.access_token == token_used:
                await self._renew(force=True)
            status, data = await self._raw(method, path, body)
            if status == 401:
                self._set_auth_status(AUTH_FAILED)
                raise MsheirebAuthError("unauthorized after token renewal")
        return status, data

    async def async_send_command(
        self, ip: str, sn: int, type_io: str, type_code: str, value: str
    ) -> dict[str, Any]:
        """Publish a controller command (same payload shape as the web portal).

        The server only queues the command; a 200 does not confirm execution.
        """
        body = {"ip": ip, "sn": sn, "type_io": type_io, "type_code": type_code, "value": str(value)}
        _LOGGER.debug("Sending command %s", body)
        return await self._request("POST", "/user/smart-home/controller-command", body) or {}


def _message(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or "")
    return ""
