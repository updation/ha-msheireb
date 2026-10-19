"""Per-room Max cooling override margin (number entity)."""
import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.msheireb.const import OVERRIDE_REFRESH_DELAY

from .test_drift import DINING, KEY, _poll, fake_api, fast_sleep, external_power  # noqa: F401
from .test_max_cooling_override import _expire, _power, _room_temp, _setup_override

MARGIN = "number.msheireb_demo01_dining_room_max_cooling_override_margin"


async def _set_margin(hass, value):
    await hass.services.async_call("number", "set_value", {"entity_id": MARGIN, "value": value}, blocking=True)
    await hass.async_block_till_done()


async def test_defaults(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _poll(hass, coord)
    s = hass.states.get(MARGIN)
    assert float(s.state) == 0.5
    assert s.attributes["min"] == 0.1 and s.attributes["max"] == 2.0 and s.attributes["step"] == 0.1
    assert s.attributes["mode"] == "box" and s.attributes["unit_of_measurement"] == "°C"
    assert s.attributes["friendly_name"] == "Dining Room Max cooling override margin"
    assert er.async_get(hass).async_get(MARGIN).entity_category == "config"
    assert hass.states.get(DINING).attributes["override_margin"] == 0.5
    _room_temp(api, 19.5)  # default behaviour unchanged: off at T-0.5
    await _poll(hass, coord)
    assert _power(api)
    _room_temp(api, 19.4)
    await _poll(hass, coord)
    assert not _power(api)


async def test_margin_03_switches_at_03(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _set_margin(hass, 0.3)
    assert hass.states.get(DINING).attributes["override_margin"] == 0.3
    _room_temp(api, 19.8)
    await _poll(hass, coord)
    assert _power(api)  # 19.8 = T-0.2: inside the band
    _room_temp(api, 19.6)  # T-0.4: off (would stay on with 0.5)
    await _poll(hass, coord)
    assert not _power(api)
    _expire(coord)
    _room_temp(api, 20.2)
    await _poll(hass, coord)
    assert not _power(api)
    _room_temp(api, 20.3)  # T+0.3: on
    await _poll(hass, coord)
    assert _power(api)


async def test_persistence_and_range(hass):
    entry, coord, api = await _setup_override(hass, 20.0)
    await _set_margin(hass, 0.3)
    assert coord.drift._data_to_save()["margins"] == {KEY: 0.3}
    await _set_margin(hass, 0.5)
    assert coord.drift._data_to_save()["margins"] == {}
    await _set_margin(hass, 1.2)
    saved = coord.drift._data_to_save()
    coord.drift.margins = {}
    coord.drift.store.async_load = lambda: _ret(saved)
    await coord.drift.async_load()
    assert coord.drift.margin(KEY) == 1.2
    for bad in (0.0, 2.1, -1):
        with pytest.raises(ServiceValidationError):
            await _set_margin(hass, bad)
    assert coord.drift.set_margin(KEY, 9) == 2.0 and coord.drift.set_margin(KEY, 0) == 0.1


async def _ret(v):
    return v
