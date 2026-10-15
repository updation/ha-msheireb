from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.components.climate import HVACMode
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType

from custom_components.msheireb.api import MsheirebAuthError, MsheirebConnectionError
from custom_components.msheireb.const import DOMAIN

from .fake_api import FakeApi

ENTRY_DATA = {"email": "me@example.com", "password": "pw", "access_token": "A",
              "refresh_token": "R", "expires_at": 9e9}


@pytest.fixture(autouse=True)
def fake_api(smart_home_payload):
    FakeApi.instances.clear()
    FakeApi.payload = smart_home_payload
    FakeApi.controller = "connected"
    FakeApi.fail_login = None
    FakeApi.fail_refresh = None
    FakeApi.fail_fetch = None
    FakeApi.ignore_commands = False
    with patch("custom_components.msheireb.MsheirebApi", FakeApi), \
         patch("custom_components.msheireb.config_flow.MsheirebApi", FakeApi), \
         patch("custom_components.msheireb.climate.DEFAULT_PULSE_INTERVAL", 0), \
         patch("custom_components.msheireb.coordinator.REFRESH_AFTER_COMMAND", 0):
        yield


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="1263")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, FakeApi.instances[-1]


async def test_user_flow_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "password"})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.msheireb.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": "me@example.com", "password": "pw"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["refresh_token"] == "R" and result["data"]["password"] == "pw"
    assert result["result"].unique_id == "1263"


@pytest.mark.parametrize("exc,err", [(MsheirebAuthError("x"), "invalid_auth"),
                                     (MsheirebConnectionError("x"), "cannot_connect")])
async def test_user_flow_errors(hass, exc, err):
    FakeApi.fail_login = exc
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "password"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "me@example.com", "password": "bad"})
    assert result["errors"] == {"base": err}


async def test_reauth_flow(hass):
    entry, _ = await _setup(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "reauth_password"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "me@example.com", "password": "newpw"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["password"] == "newpw"


async def test_entities_and_state(hass):
    await _setup(hass)
    states = {s.entity_id: s for s in hass.states.async_all("climate")}
    assert set(states) == {"climate.msheireb_demo01_dining_room",
                           "climate.msheireb_demo01_master_bedroom",
                           "climate.msheireb_demo01_bedroom_1"}
    d = states["climate.msheireb_demo01_dining_room"]
    assert d.state == HVACMode.COOL
    assert d.attributes["temperature"] == 19.5
    assert d.attributes["current_temperature"] == 21.5
    assert d.attributes["fan_mode"] == "high"
    assert d.attributes["min_temp"] == 18 and d.attributes["max_temp"] == 30
    assert d.attributes["target_temp_step"] == 0.5
    assert hass.states.get("binary_sensor.msheireb_demo01_apartment_controller").state == "on"


async def test_set_temperature_uses_pulses(hass):
    _, api = await _setup(hass)
    await hass.services.async_call("climate", "set_temperature",
        {"entity_id": "climate.msheireb_demo01_dining_room", "temperature": 21.0}, blocking=True)
    assert [c["sn"] for c in api.commands] == [6, 6, 6]  # 19.5 -> 21.0 = 3 x Temp Up (sn from labels)
    assert all(c["value"] == "PULSE" and c["type_code"] == "D" and c["ip"] == "10.0.0.10" for c in api.commands)
    assert hass.states.get("climate.msheireb_demo01_dining_room").attributes["temperature"] == 21.0
    api.commands.clear()
    await hass.services.async_call("climate", "set_temperature",
        {"entity_id": "climate.msheireb_demo01_master_bedroom", "temperature": 19.0}, blocking=True)
    assert [c["sn"] for c in api.commands] == [14, 14]  # 20.0 -> 19.0 = 2 x Temp Down
    api.commands.clear()
    await hass.services.async_call("climate", "set_temperature",
        {"entity_id": "climate.msheireb_demo01_bedroom_1", "temperature": 30}, blocking=True)
    assert [c["sn"] for c in api.commands] == [20] * 20  # 20 -> 30 (max) = 20 pulses, re-reads in between


async def test_power_and_fan_only_when_different(hass):
    _, api = await _setup(hass)
    eid = "climate.msheireb_demo01_dining_room"
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": eid, "hvac_mode": "cool"}, blocking=True)
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": eid, "fan_mode": "high"}, blocking=True)
    assert api.commands == []
    await hass.services.async_call("climate", "turn_off", {"entity_id": eid}, blocking=True)
    assert hass.states.get(eid).state == HVACMode.OFF  # optimistic
    assert hass.states.get(eid).attributes["fan_mode"] == "off"  # follows HVAC off
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": eid, "fan_mode": "medium"}, blocking=True)
    # power off, fan auto (off rule); v0.3.18: medium while off = power on, then medium
    assert [c["sn"] for c in api.commands] == [1, 2, 1, 4]
    await hass.async_block_till_done()
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data.async_refresh()
    await hass.async_block_till_done()
    s = hass.states.get(eid)
    assert s.state == HVACMode.COOL and s.attributes["fan_mode"] == "medium"


async def test_unavailable_when_controller_disconnected(hass):
    entry, _ = await _setup(hass)
    FakeApi.controller = "disconnected"
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("climate.msheireb_demo01_dining_room").state == STATE_UNAVAILABLE


async def test_auth_failure_starts_reauth(hass):
    entry, api = await _setup(hass)

    async def boom(*a):
        raise MsheirebAuthError("expired")
    api.async_get_contracts = boom
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_token_persistence_does_not_reload(hass):
    entry, api = await _setup(hass)
    await api.async_login()  # triggers token callback -> entry data update
    await hass.async_block_till_done()
    assert entry.data["refresh_token"] == "R"
    assert FakeApi.instances[-1] is api  # no reload happened


async def test_options_flow_reloads_with_new_limits(hass):
    entry, _ = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"min_temp": 20, "max_temp": 26, "scan_interval": 60, "pulse_interval": 5.0, "max_retries": 2, "notifications": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    s = hass.states.get("climate.msheireb_demo01_dining_room")
    assert s.attributes["min_temp"] == 20 and s.attributes["max_temp"] == 26


async def test_default_pulse_spacing_and_option_range(hass):  # 5.0 default, 0.5-10 range
    import voluptuous as vol
    from custom_components.msheireb.const import DEFAULT_PULSE_INTERVAL
    assert DEFAULT_PULSE_INTERVAL == 5.0
    entry, _ = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    key = next(k for k in schema if k == "pulse_interval")
    assert key.default() == 5.0
    for bad in (0.4, 10.1):
        with pytest.raises(vol.Invalid):
            result["data_schema"]({"min_temp": 18, "max_temp": 30, "scan_interval": 30,
                                   "pulse_interval": bad, "max_retries": 2, "notifications": True})
    result["data_schema"]({"min_temp": 18, "max_temp": 30, "scan_interval": 30,
                           "pulse_interval": 0.5, "max_retries": 2, "notifications": True})
    result["data_schema"]({"min_temp": 18, "max_temp": 30, "scan_interval": 30,
                           "pulse_interval": 10.0, "max_retries": 0, "notifications": True})


async def test_options_use_selectors_and_store_ints(hass):
    """All numeric options are number boxes with units; whole-number options are stored as int."""
    from homeassistant.helpers.selector import BooleanSelector, NumberSelector, SelectSelector

    entry, _ = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    sel = {str(k): v for k, v in schema.items()}
    expect = {  # key: (min, max, step, unit, mode)
        "max_retries": (0, 5, 1, None, "box"),
        "pulse_interval": (0.5, 10, 0.5, "s", "box"),
        "min_temp": (10, 35, 0.5, "°C", "box"),
        "max_temp": (10, 35, 0.5, "°C", "box"),
        "scan_interval": (15, 600, 1, "s", "box"),
        "drift_grace": (0, 3600, 1, "s", "box"),
        "power_fan_delay": (0, 30, 1, "s", "box"),
        "power_settle": (0, 60, 1, "s", "box"),
        "fan_settle": (0, 60, 1, "s", "box"),
    }
    for key, (lo, hi, step, unit, mode) in expect.items():
        assert isinstance(sel[key], NumberSelector), key
        cfg = sel[key].config
        assert (cfg["min"], cfg["max"], cfg["step"], cfg.get("unit_of_measurement"), cfg["mode"]) == \
            (lo, hi, step, unit, mode), key
    assert isinstance(sel["external_change"], SelectSelector)
    assert isinstance(sel["adopt_external"], BooleanSelector) and isinstance(sel["notifications"], BooleanSelector)
    assert isinstance(sel["fan_auto_when_off"], BooleanSelector)
    assert next(k for k in schema if k == "fan_auto_when_off").default() is True

    # NumberSelector hands back floats: they must be stored as int where the option is whole-number
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "min_temp": 18.0, "max_temp": 27.0, "scan_interval": 45.0, "pulse_interval": 1.5, "max_retries": 3.0,
        "external_change": "notify", "drift_grace": 90.0, "power_fan_delay": 7.0, "power_settle": 12.0, "fan_settle": 25.0, "adopt_external": False, "notifications": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    opts = entry.options
    for key, val in (("scan_interval", 45), ("max_retries", 3), ("drift_grace", 90), ("power_fan_delay", 7), ("power_settle", 12), ("fan_settle", 25)):
        assert opts[key] == val and type(opts[key]) is int, key
    for key, val in (("min_temp", 18.0), ("max_temp", 27.0), ("pulse_interval", 1.5)):
        assert opts[key] == val and type(opts[key]) is float, key
    await hass.async_block_till_done()
    assert hass.states.get("climate.msheireb_demo01_dining_room").attributes["max_temp"] == 27

    # out-of-range values are rejected by the selectors
    import voluptuous as vol
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(vol.Invalid):
        result["data_schema"]({**opts, "max_retries": 6})


def test_help_texts_are_one_short_line():
    import json
    import pathlib

    base = pathlib.Path(__file__).parent.parent / "custom_components" / "msheireb"
    for f in ("strings.json", "translations/en.json"):
        d = json.loads((base / f).read_text())
        texts = list(d["options"]["step"]["init"]["data_description"].values())
        texts += [s["description"] for k, s in d["config"]["step"].items()
                  if "description" in s and "portal_session" not in k]  # session steps carry instructions
        for t in texts:
            assert len(t) < 60 and "\n" not in t, t
