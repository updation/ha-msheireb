"""Downloadable diagnostics (all personal data and secrets redacted)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_ACCESS_TOKEN, CONF_EXPIRES_AT, CONF_REFRESH_TOKEN, CONF_USER_ID, INTEGRATION_VERSION
from .coordinator import MsheirebCoordinator

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    "token",
    "apartment_ip",
    "ip",
    "externalId",
    "external_id",
    "unitName",
    "unitId",
    "buildingId",
    "yardiBuildingRef",
    "display_name",
    "access_point",
    "name_user",
    "full_name",
    "phone",
    "contact",
    "title",
    "unique_id",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: MsheirebCoordinator = entry.runtime_data
    h = coordinator.health
    contracts = []
    for cid, cd in (coordinator.data or {}).items():
        contracts.append(
            {
                "contract": async_redact_data(cd.contract, TO_REDACT),
                "controller_connected": cd.controller_connected,
                "zones": {
                    key: {
                        "room": z.room_name,
                        "device": z.device_name,
                        "controls": {r: {"sn": c.sn, "type_io": c.type_io, "type_code": c.type_code, "label": c.label}
                                     for r, c in z.controls.items()},
                        "setpoint": z.setpoint,
                        "room_temperature": z.room_temperature,
                        "digital": z.digital,
                    }
                    for key, z in cd.zones.items()
                },
                "smart_home_raw": async_redact_data(cd.raw_smart_home or {}, TO_REDACT),
                "lock": async_redact_data(cd.lock or {}, TO_REDACT),
            }
        )
    health = {
        "auth_status": h.auth_status,
        "last_success": h.last_success.isoformat() if h.last_success else None,
        "last_error": h.last_error,
        "last_error_type": h.last_error_type,
        "portal_reachable": h.portal_reachable,
        "consecutive_failures": h.consecutive_failures,
        "last_response_ms": coordinator.last_response_ms,
        "token_expires": coordinator.token_expires.isoformat() if coordinator.token_expires else None,
        "commands_sent": h.commands_sent,
        "commands_confirmed": h.commands_confirmed,
        "commands_failed": h.commands_failed,
        "last_commands": {k: {**r.as_attributes(), "result": r.result} for k, r in h.commands.items()},
        "command_history": {k: [{**r.as_attributes(), "result": r.result} for r in v]
                            for k, v in h.history.items()},
        "active_notifications": sorted(coordinator.alerts.active),
        "drift": {
            "mode": coordinator.drift.mode,
            "grace_s": coordinator.drift.grace,
            "adopt_external": coordinator.drift.adopt,
            "desired": coordinator.drift.desired,
            "auto_restore": coordinator.drift.auto_restore,
            "remembered_fan": coordinator.drift.prev_fan,
            "last_fan_mode": coordinator.drift.last_fan,
            "target_offsets": coordinator.drift.offsets,
            "offset_pending": sorted(coordinator.drift.offset_pending),
            "max_cooling_override": coordinator.drift.override,
            "override_paused": sorted(coordinator.drift.override_paused),
            "episodes": {k: {"polls": e.polls, "diff": {kk: list(v) for kk, v in e.diff.items()},
                             "handled": e.handled, "restored": e.restored}
                         for k, e in coordinator.drift.episodes.items()},
            "events": coordinator.drift.events,
            "last_event": coordinator.drift.last_event.__dict__ if coordinator.drift.last_event else None,
        },
    }
    data = dict(entry.data)
    data.pop(CONF_EXPIRES_AT, None)
    data["contracts"] = [async_redact_data(c, TO_REDACT) for c in data.get("contracts") or []]
    return {
        "integration_version": INTEGRATION_VERSION,
        "zone_count": sum(len(cd.zones) for cd in (coordinator.data or {}).values()),
        "entry": {
            "data": async_redact_data(data, TO_REDACT),
            "options": dict(entry.options),
            "version": entry.version,
        },
        "health": health,
        "last_unlock": {"contracts": len(coordinator.last_unlock),
                        "results": [r.get("result") for r in coordinator.last_unlock.values()]},
        "door_states": [d.state for d in coordinator.door.doors.values()],
        "contracts": contracts,
    }
