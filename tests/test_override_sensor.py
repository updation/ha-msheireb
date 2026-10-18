"""v0.3.29: per-room external temperature sensor as the Max cooling override reference."""
import logging

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.util import dt as dt_util
from datetime import timedelta

from custom_components.msheireb.const import CONF_OVERRIDE_SENSORS, OVERRIDE_SENSOR_DEBOUNCE

from .test_drift import DINING, KEY, _poll, fake_api, fast_sleep, external_power  # noqa: F401
from .test_max_cooling_override import (  # noqa: F401
    OVR, _expire, _power, _room_temp, _setup_override, _switch,
)

SENSOR = "sensor.living_temp"


def _sens(hass, value, unit="°C"):
    hass.states.async_set(SENSOR, value, {"unit_of_measurement": unit, "device_class": "temperature"})


async def _setup_ext(hass, target=20.0, value="22", unit="°C"):
    _sens(hass, value, unit)
    return await _setup_override(hass, target, {CONF_OVERRIDE_SENSORS: {KEY: SENSOR}})


async def _fire(hass, seconds=OVERRIDE_SENSOR_DEBOUNCE + 1):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_sensor_decides_not_ac(hass, fast_sleep):
    entry, coord, api = await _setup_ext(hass, 20.0, "22")
    _room_temp(api, 18.0)  # AC says cold, sensor says hot -> stays on
    await _poll(hass, coord)
    assert _power(api)
    a = hass.states.get(DINING).attributes
    assert a["override_reference"] == SENSOR and a["override_reference_temperature"] == 22.0
    assert a["current_temperature"] == 18.0
    _sens(hass, "19.4")  # sensor cold, AC hot
    _room_temp(api, 25.0)
    await _poll(hass, coord)
    assert not _power(api)
    _expire(coord)
    _sens(hass, "20.5")
    _room_temp(api, 15.0)
    await _poll(hass, coord)
    assert _power(api)


async def test_fahrenheit_converted(hass, fast_sleep):
    entry, coord, api = await _setup_ext(hass, 20.0, "75", "°F")  # 23.9 °C
    _room_temp(api, 18.0)
    await _poll(hass, coord)
    assert _power(api)
    assert hass.states.get(DINING).attributes["override_reference_temperature"] == 23.89
    _sens(hass, "66.0", "°F")  # 18.89 °C < 19.5
    await _poll(hass, coord)
    assert not _power(api)


async def test_fallback_and_recovery_logging(hass, fast_sleep, caplog):
    caplog.set_level(logging.INFO)
    entry, coord, api = await _setup_ext(hass, 20.0, "22")
    _room_temp(api, 19.0)  # AC alone would switch off
    for bad in ("unavailable", "unknown", "abc"):
        _sens(hass, bad)
        await _poll(hass, coord)
        assert not _power(api)  # fell back to the AC reading
        a = hass.states.get(DINING).attributes
        assert a["override_reference"] == "ac" and a["override_reference_temperature"] == 19.0
        assert a["current_temperature"] == 19.0
        _expire(coord)
        await _poll(hass, coord)
        break
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "using the AC" in r.message]
    assert len(warns) == 1
    await _poll(hass, coord)
    await _poll(hass, coord)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING and "using the AC" in r.message]) == 1
    _sens(hass, "22")
    await _poll(hass, coord)
    assert any("is back" in r.message for r in caplog.records)
    assert hass.states.get(DINING).attributes["override_reference"] == SENSOR


async def test_state_change_triggers_debounced_evaluation(hass, fast_sleep):
    entry, coord, api = await _setup_ext(hass, 20.0, "22")
    assert _power(api)
    _sens(hass, "19.0")
    await hass.async_block_till_done()
    assert _power(api)  # debounced: nothing yet
    _sens(hass, "18.0")  # second change restarts the wait
    await hass.async_block_till_done()
    await _fire(hass, 2)
    assert _power(api)
    await _fire(hass, OVERRIDE_SENSOR_DEBOUNCE + 1)
    assert not _power(api)
    assert hass.states.get(DINING).attributes["override_reference_temperature"] == 18.0


async def test_state_change_respects_min_toggle_interval(hass, fast_sleep):
    entry, coord, api = await _setup_ext(hass, 20.0, "22")
    coord.drift.override_last_toggle[KEY] = __import__("time").monotonic()
    _sens(hass, "18.0")
    await _fire(hass)
    assert _power(api)  # toggled <180 s ago: refused
    _expire(coord)
    _sens(hass, "17.9")
    await _fire(hass)
    assert not _power(api)


async def test_no_sensor_keeps_ac_reference(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _poll(hass, coord)
    a = hass.states.get(DINING).attributes
    assert a["override_reference"] == "ac" and "override_reference_temperature" in a


async def test_unsubscribed_on_unload(hass, fast_sleep):
    entry, coord, api = await _setup_ext(hass, 20.0, "22")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    _sens(hass, "10")
    await hass.async_block_till_done()
    assert coord._cancel_sensor_eval is None


async def test_options_flow_set_and_clear(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    defaults = {str(k): k.default() for k in result["data_schema"].schema
                if getattr(k, "default", None) is not None and k.default.__class__.__name__ != "Undefined"}
    defaults["edit_room_sensors"] = True
    result = await hass.config_entries.options.async_configure(result["flow_id"], defaults)
    assert result["step_id"] == "room_sensors"
    label = "Dining Room – temperature sensor for Max cooling override"
    assert label in [str(k) for k in result["data_schema"].schema]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {label: SENSOR})
    assert result["type"] == "create_entry"
    assert entry.options[CONF_OVERRIDE_SENSORS] == {KEY: SENSOR}
    assert "edit_room_sensors" not in entry.options
    await hass.async_block_till_done()

    # reopen: suggested value is the current sensor; the general form alone keeps it
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], defaults | {"edit_room_sensors": False})
    assert entry.options[CONF_OVERRIDE_SENSORS] == {KEY: SENSOR}
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], defaults)
    key = next(k for k in result["data_schema"].schema if str(k) == label)
    assert key.description == {"suggested_value": SENSOR} and key.default.__class__.__name__ == "Undefined"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})  # cleared
    assert result["type"] == "create_entry"
    assert CONF_OVERRIDE_SENSORS not in entry.options
