"""Msheireb Smart Home integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
import logging

from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import MsheirebApi
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CONTRACTS,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    INTEGRATION_VERSION,
    STORE_VERSION,
    slim_contracts,
)
from .drift import store_key
from .coordinator import MsheirebCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH, Platform.BUTTON, Platform.NUMBER]

type MsheirebConfigEntry = ConfigEntry[MsheirebCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MsheirebConfigEntry) -> bool:
    @callback
    def _persist_tokens(access: str, refresh: str, expires_at: float) -> None:
        # Persist the rotated refresh token so a restart keeps the session.
        data = {
            **entry.data,
            CONF_ACCESS_TOKEN: access,
            CONF_REFRESH_TOKEN: refresh,
            CONF_EXPIRES_AT: expires_at,
        }
        if api.contracts:  # keep the contract list (only /user/login returns it)
            data[CONF_CONTRACTS] = slim_contracts(api.contracts)
        hass.config_entries.async_update_entry(entry, data=data)

    api = MsheirebApi(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        expires_at=entry.data.get(CONF_EXPIRES_AT),
        token_callback=_persist_tokens,
        status_callback=lambda status: coordinator.on_auth_status(status),
        contracts=entry.data.get(CONF_CONTRACTS),
    )
    coordinator = MsheirebCoordinator(hass, entry, api)
    await coordinator.drift.async_load()  # desired state survives restarts
    # Blocking first refresh BEFORE the platforms are forwarded; this also fetches the
    # contract list with one login when the entry has none stored (entries from <= 0.3.0).
    await coordinator.async_config_entry_first_refresh()
    data = coordinator.data or {}
    zones = sum(len(cd.zones) for cd in data.values())
    _LOGGER.info(
        "Msheireb Smart Home %s: %d contract(s), %d room HVAC zone(s)%s",
        INTEGRATION_VERSION,
        len(data),
        zones,
        "".join(f"; {cd.title}: {', '.join(z.room_name for z in cd.zones.values()) or 'no HVAC rooms'}"
                for cd in data.values()),
    )
    if not data:
        # Visible in the UI ("Retrying setup") instead of a silent, empty integration.
        raise ConfigEntryNotReady("The Msheireb portal returned no contracts for this account yet")
    entry.runtime_data = coordinator

    # Register the account + apartment devices up front (room devices reference them via_device).
    dev_reg = dr.async_get(hass)
    from .entity import apartment_device
    from .sensor import account_device

    dev_reg.async_get_or_create(config_entry_id=entry.entry_id, **account_device(coordinator))
    for cid, cd in data.items():
        dev_reg.async_get_or_create(config_entry_id=entry.entry_id, **apartment_device(cid, cd.title))
    entry.async_on_unload(coordinator.async_cancel_pending)
    entry.async_on_unload(coordinator.async_track_override_sensors())
    _remove_retired_entities(hass, entry)

    options_snapshot = dict(entry.options)

    async def _options_updated(hass: HomeAssistant, updated: ConfigEntry) -> None:
        # Token persistence also triggers update listeners; reload only on option changes.
        if dict(updated.options) != options_snapshot:
            await hass.config_entries.async_reload(updated.entry_id)

    entry.async_on_unload(entry.add_update_listener(_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MsheirebConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and getattr(entry, "runtime_data", None) is not None:
        # notifications belong to this run; the reauth repair issue is kept until fixed
        entry.runtime_data.alerts.clear_all()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: MsheirebConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, f"reauth_{entry.entry_id}")
    await Store(hass, STORE_VERSION, store_key(entry.entry_id)).async_remove()


def _remove_retired_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """0.3.13: the Door lock entity (0.3.12) and the Last unlock sensor were replaced by the Door sensor."""
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
        if (ent.domain == "lock" and ent.unique_id.endswith("_door_lock")) or (
            ent.domain == "sensor" and ent.unique_id.startswith(f"{entry.entry_id}_last_unlock_")
        ):
            _LOGGER.info("Removing retired entity %s", ent.entity_id)
            reg.async_remove(ent.entity_id)
