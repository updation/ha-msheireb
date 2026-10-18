"""v0.3.24 investigation: 'living room reverts to Low' (automation calling set_fan_mode low)."""
from .test_off_fan_memory import fake_api, KEY, DINING, FakeApi, POWER, LOW, MED  # noqa: F401
from .test_off_fan_memory import _setup, _svc, _poll, _digital, _set_fan_at_panel, _sns


async def _settle(hass, coord, n):
    """n polls, each after the pending command's confirm window has passed."""
    for _ in range(n):
        rec = coord.health.commands.get(KEY)
        if rec is not None:
            rec.sent_monotonic -= 1000
        await _poll(hass, coord, age=1000)


def _card(hass):
    s = hass.states.get(DINING)
    return s.state, s.attributes.get("fan_mode")


async def test_off_room_shows_off_after_polls_even_if_ac_reports_low(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="off")
    await _settle(hass, coord, 3)
    assert _card(hass) == ("off", "off")
    api.commands.clear()
    _set_fan_at_panel(api, "HVAC Fan Low")  # an off AC reporting Low must not show Low
    await _settle(hass, coord, 3)
    assert _card(hass) == ("off", "off")
    assert api.commands == []  # fan changes while off are not drift: nothing pressed


async def test_low_while_off_turns_room_on_at_low(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="off")
    await _settle(hass, coord, 2)
    api.commands.clear()
    await _svc(hass, "set_fan_mode", fan_mode="low")  # by design: a speed while off = turn on
    assert _sns(api) == [POWER, LOW]
    await _settle(hass, coord, 2)
    assert _card(hass) == ("cool", "low")
    assert _digital(api)["HVAC AC"]["current_status"] == "ON"


async def test_low_while_off_unconfirmed_reverts_card_to_off(hass):
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="off")
    await _settle(hass, coord, 2)
    FakeApi.ignore_commands = True  # the AC never registers the power press
    api.commands.clear()
    await _svc(hass, "set_fan_mode", fan_mode="low")
    assert _card(hass) == ("cool", "low")  # requested state shown while presses/retries run
    await _settle(hass, coord, 4)
    assert coord.health.commands[KEY].result == "not_confirmed"
    assert _card(hass) == ("off", "off")  # never left at Low once the retries are over
    await _settle(hass, coord, 3)
    assert _card(hass) == ("off", "off")


async def test_low_from_automation_overrides_users_medium(hass):
    """Documents the reported behaviour: a later set_fan_mode low wins over the user's Medium."""
    entry, coord, api = await _setup(hass)
    await _svc(hass, "set_fan_mode", fan_mode="medium")
    await _settle(hass, coord, 2)
    assert _card(hass) == ("cool", "medium")
    api.commands.clear()
    await _svc(hass, "set_fan_mode", fan_mode="low")
    assert _sns(api) == [LOW]
    await _settle(hass, coord, 2)
    assert _card(hass) == ("cool", "low")
    assert coord.drift.desired[KEY]["fan"] == "low" and coord.drift.prev_fan[KEY] == "low"


async def test_off_then_low_in_quick_succession_ends_on_at_low(hass):
    """Two automations firing together (dirty -> off, CO2 -> low): the latest action wins."""
    entry, coord, api = await _setup(hass)
    coord.wait_for_sequences = False
    await _svc(hass, "set_fan_mode", fan_mode="off")
    await _svc(hass, "set_fan_mode", fan_mode="low")
    await hass.async_block_till_done()
    await _settle(hass, coord, 4)
    assert _card(hass) == ("cool", "low")
    assert _digital(api)["HVAC AC"]["current_status"] == "ON"
    assert _digital(api)["HVAC Fan Low"]["current_status"] == "ON"
