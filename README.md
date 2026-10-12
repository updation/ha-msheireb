# Msheireb Smart Home for Home Assistant (v0.3.0)

[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration) [Releases](https://github.com/sample-of-one/ha-msheireb/releases) · [Issues](https://github.com/sample-of-one/ha-msheireb/issues)

Unofficial custom integration for the in-apartment AC (fan coil units) at **Msheireb Downtown Doha**. It works through the resident portal (`web-prod.mp-mdd.com`), using the same cloud API the portal uses (`mob-prod.mp-mdd.com:8443/api`).

- One **climate** entity per room: Off/Cool, target temperature (0.5 °C steps), current room temperature, fan Off/Auto/Low/Medium/High (fan Off = room off).
- **Binary sensors** (diagnostic): apartment controller connected, door lock connection, door lock low battery, portal reachable.
- **Monitoring sensors** (diagnostic, on a "Msheireb portal" device): auth status (ok / refreshing / relogin / failed), last successful update, last error (with `type` attribute), API response time (ms), consecutive failures, token expiry, commands sent / confirmed / failed, command retries. There is also one **"<Room> last command"** sensor per room (pending / confirmed / not_confirmed / failed), with attributes for what was sent, the expected state, when it was sent, the confirmation window, the retry count, and how long it took to confirm.
- **Notifications** (built-in persistent notifications, can be turned off in Options). Sent when the login fails and re-authentication is needed, when the apartment controller has been offline for more than 5 min, when the portal has been unreachable for more than 5 min, or when a command is still not confirmed after the final automatic retry. They are dismissed automatically on recovery. A login failure also raises a **Repairs** issue.
- **External change detection and restore** ("drift"): for each room, the integration remembers the state you last set from Home Assistant (setpoint, power, fan) in HA storage, so it survives restarts. If a poll shows the room differs from that (for example after a power-outage reset, or a change from the wall panel or portal) while no HA command is in progress, that counts as an external change.
  - It acts only after a grace period (default 60 s) **and** at least 2 polls, to avoid flapping. With the default 5-min polling, an external change is acted on at the second poll after it happens, i.e. roughly 5–10 min later (lower the polling interval for faster reaction).
  - **On external change** option: *Restore + notify* (default), *Restore*, *Notify only*, or *Ignore*.
  - Restoring uses the same safe command path: it re-reads the state, sends only the needed presses, never toggles power blindly, and retries.
  - The notification names the room, what changed (from → to), and the action taken.
  - Each room has an **"<Room> auto-restore"** switch to turn restoring on or off quickly. It is saved across restarts.
  - Option **Adopt external changes as the new desired state** (default off): the external value becomes the new target instead of being restored.
  - An **External change events** counter sensor shows the last room, changes and action as attributes.
  - Rooms you never controlled from HA are not watched. A command that just failed is not silently repeated by the restore logic.
- **Target temperature offset** (per room, v0.3.17): each room device has a **"<Room> Target temperature offset"** number (configuration entity, −3 to +3 °C in 0.5 steps, default 0) for an AC that settles off its setpoint. The card shows the target you set, and the AC is driven to target + offset. For example, with offset −1 you set 20 and the AC is set to 19. Readings are shown as AC setpoint − offset. Confirmation, retries and external-change detection compare the AC against target + offset. Min/max limit the target you set, not the AC setpoint. Changing the offset while the room is on re-drives the AC, and the card keeps showing the same target. While the room is off, the offset is applied at the next turn-on from HA. The climate entity shows `offset` and `device_setpoint` attributes. The offset is saved across restarts.
- **Max cooling override** (per room, v0.3.21): for an AC left at max cooling where only power and fan are controlled. Each room has a **"<Room> Max cooling override"** switch (configuration entity, off by default, saved across restarts). With it on and target T, the card still shows Cool and T, but Home Assistant cycles the room's power on the measured room temperature: off below T − 0.5 °C, on at T + 0.5 °C or more, unchanged in between. No setpoint presses are sent. Power uses the normal command queue with power settle/confirm, and the saved fan speed is restored on power-on. Toggles are at least 3 minutes apart. If T equals the configured minimum temperature the AC runs continuously. Turning the room Off (HVAC or fan Off) stops cycling. The paused phases are not treated as external changes. The climate entity shows `max_cooling_override` and `override_state` (`cooling` / `idle`). Switching the override off powers a paused room back on and applies the setpoint normally.
- **Diagnostics download** (Settings → Devices & services → Msheireb → ⋮ → Download diagnostics), with credentials, tokens, IPs, unit/contract IDs and lock names redacted.
- Supports multiple contracts on one account. Polls every 5 min by default (configurable 15–600 s); after each command it refreshes on its own schedule (see below), independent of the polling interval.

> Not affiliated with Msheireb Properties. It uses an undocumented API that may change without notice.

## Turning a room off and on

The behaviour below is the default (**Fan to Auto when off** = on). With that option **off**:

- **Off** presses only *AC power* (confirmed like any command); the fan speed is left untouched and the desired state keeps the actual speed (no Auto expectation).
- **On** presses only *AC power*. After the power change is confirmed it compares the actual fan with the remembered/desired speed; only if they differ does it wait the *Power → fan delay* and press the speed, with the same two-read verification and retries.

- **Off from Home Assistant:** the integration remembers the room's current fan speed (per room, in HA storage, survives restarts), presses *AC power*, waits the *Power settle time* (default 10 s; the real unit takes ~7 s to switch), then waits until the controller reports the AC off (re-reading every 2 s, up to 30 s more), waits the *Power → fan delay* (default 5 s) and then presses *Fan Auto* if needed.
- **On (cool) from Home Assistant:** *AC power*, wait the *Power settle time*, wait until the AC reports on, wait the *Power → fan delay*, then press the remembered speed if it differs. Without a remembered speed it uses the last fan speed set from HA, otherwise it leaves the fan alone. (A fan press sent while the AC is still starting can be shown briefly by the controller and then dropped by the AC.)
- **Fan mode Off** (v0.3.18): each room's fan modes are *Off, Low, Medium, High, Auto*. Choosing **Off** turns the room off exactly like HVAC Off (including *Fan to Auto when off*). Choosing a **speed while the room is off** turns it on (*Power → fan delay* included) at that speed, which becomes the remembered speed. A speed chosen while on works as before. While the room is off (from HA or outside), the fan mode shows *Off*; while on, the actual speed. *Off* is never stored as a fan speed and is never an external fan change.
- If the power never changes, the fan is not pressed; the command is retried/reported like any other.
- The settle time is applied after **every** power press: turn-on, turn-off (also when only power is pressed), retries and external-change restores. Nothing is checked or pressed during it.
- **Fan verification:** a fan change only counts as confirmed when the actual readings show it on **two reads at least 5 s apart**, the first taken only after the *Fan settle time* (default 10 s) has passed since the press. This applies to every fan press: the speed restored on turn-on, *Fan Auto* on turn-off, direct fan changes, retries and restores after an external change. Reads taken during the settle time are ignored (not counted, never flagged). Otherwise only the fan is pressed again (up to *Automatic retries*), then a notification is shown.
- While on, the fan mode shown in HA is the **actual** reported one. Exactly one fan reading must be ON; several ON at once (e.g. Auto + High) is treated as unknown, never as a match. The requested value is shown only while the command is running, then the actual reading wins.
- The remembered speed is only replaced by a real speed taken while the room is on, or by a fan speed you choose in HA. It is never replaced by Auto from the off-sequence, a drift restore or an AC restart.
- The desired state used for external-change detection is *off + fan Auto* while off and the restored speed after turning on. While a room is off, fan differences are ignored; turning it on at the wall panel is still detected.
- Debug logging shows the raw fan readings and the derived fan mode for every read.

## Door unlock button and Door sensor

The apartment device has an **Unlock door** button (the only control, opt-in) and a **Door** sensor that shows what is happening. The button does exactly what the portal's *Unlock (5s)* button does: `POST /smart-lock/contract-access-point` with `{"contract_id": …, "state": "unlock", "duration": "5s"}`. This is a temporary unlock of the apartment's smart lock; the portal asks for no PIN or OTP. There is no lock entity, because the portal cannot lock the door: it locks again by itself.

**Door sensor states (mirroring the portal's spinner):**

| State | Icon | When |
|---|---|---|
| `locked` | `mdi:lock` | Normal state. |
| `unlocking` | `mdi:lock-clock` | Shown as soon as the button is pressed, while the request and then the lock status re-read are pending (the portal waits for both). Shown for at least ~2 s so it is visible even if the portal answers faster. |
| `unlocked` | `mdi:lock-open-variant` | From ~1.5 s after the portal accepts (the lock needs a moment to release), for the portal unlock duration + 1 s (5 + 1 = 6 s), matching the real lock. |
| `failed` | `mdi:lock-alert` | For ~10 s if the portal refused or could not be reached, with the portal's message in `message`/`problem`; then `locked`. |

- **No real lock state:** the portal's lock status endpoint reports only *connected*, *outdated* and *low battery*, not locked/unlocked/open. So *unlocked* is based on the accepted request plus those timings, the same success condition the portal uses.
- **Polls don't interfere:** the state comes from the unlock itself, so regular polls never overwrite it. A second press while *unlocking* is refused.
- **Attributes** (these replace the former *Last unlock* sensor): `last_result` (`success`/`failed`), `last_unlock_at`, `unlock_duration`, `message`, `problem` (cleared by the next successful unlock), `lock_connected`, `lock_outdated`, `low_battery`.
- **Upgrade from 0.3.12:** the *Door* lock entity and the *Last unlock* sensor are removed from the entity registry automatically.

**Event for automations:** every unlock attempt fires `msheireb_door_unlocked` with `contract_id`, `entity_id`, `result` (`success`/`failed`), `duration`, `message` and `source` (`button`). Example trigger:

```yaml
trigger:
  - platform: event
    event_type: msheireb_door_unlocked
    event_data:
      result: success
```

> **Security note:** anyone or anything that can press the button in Home Assistant can open your front door: users, dashboards, automations, voice assistants, the companion app. It is therefore **off twice by default**:
> 1. the **Unlock door** button is **disabled** (enable it under the apartment device → Entities), **and**
> 2. the option **Enable door unlock** must be turned on; otherwise an unlock is refused with an error.
>
> Only enable it if you need it, restrict who has HA access, avoid exposing the button to voice assistants, and consider wrapping it in a confirmation (e.g. a script with a confirmation dialog). Each unlock is logged at INFO level (without IDs).

## How it controls the AC
The portal's controller accepts **pulse** commands. Each *Temp Up/Down* pulse moves the setpoint by 0.5 °C (verified). To set a temperature, the integration sends the needed number of Up/Down pulses one at a time, 5.0 s apart start-to-start by default (configurable 0.5–10 s; the real AC also registered 3 presses 1.2–1.5 s apart, so you can lower it for faster changes), and re-reads the setpoint every 3 pulses. It never sends more pulses than initially needed. Power and fan pulses are only sent when the reported state differs from the requested one. Control serial numbers are discovered from the control labels, not hard-coded. An extra refresh runs about 5 s after a command, independent of the polling interval.

### Instant climate card

- Every climate action (on/off, fan mode, target temperature) is written to the card **immediately** and the service call returns at once. The presses, settle times, verification and retries run in the background, one at a time per apartment.
- While that sequence and its retries are in flight, the card keeps showing the **requested** values; poll readings that still show the old state (e.g. during the power or fan settle time) are ignored, so the card does not flicker back. External-change detection is paused for that room meanwhile.
- If the final retry fails, the card reverts to the **actual** reported state and a notification is shown (as before). The room's *Last command* sensor shows the result.
- A newer action for the same room **supersedes** the running one (latest wins): it stops at the next safe point (never between a power press and its power check) and the newer request is applied from a fresh reading. The old command shows as *superseded*.
- Temperature taps are **debounced**: quick +/- taps within ~1.5 s are combined into one target before any press.

## Installation

### Option A: HACS (custom repository)
1. In Home Assistant: **HACS → ⋮ → Custom repositories**. Add `https://github.com/sample-of-one/ha-msheireb` with category **Integration**.
2. Search for **Msheireb Smart Home** in HACS, then **Download**.
3. Restart Home Assistant.

### Option B: manual copy (Samba share or File editor / Studio Code Server add-on)
1. Download the source zip from the [latest release](https://github.com/sample-of-one/ha-msheireb/releases/latest) and copy `custom_components/msheireb/` to `/config/custom_components/msheireb/` on your HA Green. The final path must be `/config/custom_components/msheireb/manifest.json`.
2. Restart Home Assistant.

### Configure
**Settings → Devices & services → Add integration → Msheireb Smart Home**. Enter your portal email and password once. Tokens are stored in the config entry and refreshed automatically (24 h access token, rotating 7-day refresh token). If the refresh token expires, the integration logs in again with the stored credentials. If the password changes, Home Assistant prompts you to re-authenticate.

**Options** (Configure button; all numeric options are number boxes with units):

| Option | Default | Range | Details |
|---|---|---|---|
| Minimum / maximum target temperature | 18 / 30 °C | 10–35 °C, 0.5 steps | Limits of the thermostat cards. |
| Polling interval | 300 s (5 min) | 15–600 s | How often the portal is polled. Commands get their own refreshes (~5 s after sending and at the end of the confirmation window), independent of this. Existing installs keep their saved value. |
| Pulse spacing | 5.0 s | 0.5–10 s, 0.5 steps | Time between consecutive Temp Up/Down presses; the AC misses presses sent too fast. |
| Fan to Auto when off | on | on / off | Off: fan Auto + speed remembered; on: speed re-applied. When disabled, only power is pressed (see above). |
| Power settle time | 10 s | 0–60 s | Fixed wait after every power press before the first power check or any next press (the real unit takes ~7 s). |
| Fan settle time | 10 s | 0–60 s | Fixed wait after every fan press before the first fan verification read (the fan is slow to change). |
| Power → fan delay | 5 s | 0–30 s | Wait after the power change is confirmed, before the fan press. |
| Automatic retries | 2 | 0–5 (0 = off) | Re-sends a command the AC did not confirm (see below). |
| On external change | restore + notify | restore + notify / restore / notify / ignore | What to do when the wall panel, the portal or a power outage changes a room. |
| Grace period | 60 s | 0–3600 s | How long a change must persist, **and** at least 2 polls, before acting. With 5-min polling that is about 5–10 min after the change. |
| Adopt external changes | off | | Treat external changes as the new desired state instead of restoring. |
| Enable door unlock | off | on / off | Must be on for the (disabled-by-default) *Unlock door* button to unlock. See the security note. |
| Notifications | on | | Login failure, controller/portal offline, unconfirmed commands. |

A command counts as *confirmed* when a later poll shows the requested state (setpoint, power or fan). The confirmation window scales with the number of presses: **presses × pulse spacing + 20 s**, counted from the first press. For example, a 2 °C change is 4 presses, which gives 4 × 5 s + 20 s = 40 s. If the state doesn't match within that window, the integration **retries automatically** (default 2 retries, configurable 0–5). It re-reads the actual state first. For temperature, it sends only the presses still needed from the actual setpoint. For power and fan, it re-sends the press only if the actual state still differs, so it never toggles blindly. Each retry gets its own window (retry presses × spacing + 20 s). Any power wait (*Power settle time*, power polls, *Power → fan delay*) is added to the window, and a command with a fan press also gets *Fan settle time* + 5 s (the gap before the second fan read), so a slow fan is never flagged *not confirmed* too early. Only after the final retry fails does the command become *not confirmed*, which increments "Commands failed" and sends a notification. Counters reset when Home Assistant restarts. Confirmation does not wait for the regular poll: besides the refresh ~5 s after the command, the integration refreshes again when each confirmation window ends.

## Notes / limitations
- Commands are queued by the cloud. A successful API call does not guarantee execution, so state is confirmed by polling.
- The direct setpoint write (`A1`) is accepted by the API but ignored by the controller, so pulses are used instead.
- Only Cool/Off is exposed. The controller reports no heating or dry modes.
- Your email and password are stored in Home Assistant's config entry storage (`.storage/core.config_entries`), like other cloud integrations that need re-login.

## Troubleshooting

- **Devices:** *Msheireb portal* (health/diagnostics), *Msheireb <unit>* (the apartment: controller and
  lock status) and, since v0.3.2, **one device per room** (Dining Room, Master Bedroom, ...) holding the
  room's thermostat (climate), its *Auto-restore* switch and *Last command* sensor.
- **Which version is running?** Open any Msheireb device: *Firmware* shows the integration version
  (v0.3.2+). The log also prints `Msheireb Smart Home <version>: N contract(s), M room HVAC zone(s)` at
  startup. After a HACS update you must **restart Home Assistant** (not just reload the integration).
- **No room/climate entities (v0.3.0):** the portal's token-refresh response carries no contract list,
  so after setup/restart v0.3.0 found zero contracts and silently created no rooms. Fixed in v0.3.1;
  contracts are stored from login and re-fetched with one login if missing. If the portal returns no
  contracts at all, the integration now shows *Retrying setup* with the reason instead of loading empty.
- **Debug logging** (per-poll contract/room/HVAC-zone counts, unrecognised labels):

```yaml
logger:
  logs:
    custom_components.msheireb: debug
```

## Development
```
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```
