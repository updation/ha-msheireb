"""v0.3.29: override idle on the real captured portal payload presses the real 'HVAC Fan Auto' SN."""
import json
from pathlib import Path

import pytest

from custom_components.msheireb.diagnostics import async_get_config_entry_diagnostics

from .test_drift import fake_api, fast_sleep, DINING, _poll, _dining  # noqa: F401
from .test_max_cooling_override import _setup_override, _room_temp, _power

CAPTURE = Path(__file__).parents[2] / "capture" / "smart_home_1219.json"


@pytest.fixture
def smart_home_payload():
    if not CAPTURE.exists():
        pytest.skip("capture not available")
    return json.loads(CAPTURE.read_text())["data"]


def _sn(api, label):
    for room in api.state["rooms"]:
        if room["name"] == "Dining Room":
            return next(c["sn"] for d in room["devices"] for c in d["controls"] if c["label"] == label)


async def test_override_idle_presses_real_auto_sn(hass, fast_sleep):
    entry, coord, api = await _setup_override(hass, 20.0)
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": DINING, "fan_mode": "medium"},
                                   blocking=True)
    await _poll(hass, coord)
    api.commands.clear()
    _room_temp(api, 19.0)
    await _poll(hass, coord)
    assert [c["sn"] for c in api.commands] == [_sn(api, "HVAC AC"), _sn(api, "HVAC Fan Auto")]
    assert not _power(api)
    on = [k for k, d in _dining(api)[1].items() if k.startswith("HVAC Fan") and d["current_status"] == "ON"]
    assert on == ["HVAC Fan Auto"]
    diag = await async_get_config_entry_diagnostics(hass, entry)
    hist = next(iter(diag["health"]["command_history"].values()))
    last = hist[-1]
    assert last["command"].startswith("max cooling override: power off")
    assert last["expected"] == {"power": False, "fan": "auto"}
    assert last["sent"] == [f"HVAC AC (sn {_sn(api, 'HVAC AC')})", f"HVAC Fan Auto (sn {_sn(api, 'HVAC Fan Auto')})"]
    assert len(hist) >= 2  # the earlier medium command is kept too
