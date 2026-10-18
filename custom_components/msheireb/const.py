"""Constants for the Msheireb Smart Home integration."""
from __future__ import annotations

DOMAIN = "msheireb"

API_BASE = "https://mob-prod.mp-mdd.com:8443/api"
WEB_ORIGIN = "https://web-prod.mp-mdd.com"

CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_EXPIRES_AT = "expires_at"
CONF_USER_ID = "user_id"
CONF_CONTRACTS = "contracts"

_CONTRACT_FIELDS = ("id", "unitId", "unitName", "externalId", "type", "status", "startDate", "endDate")


def slim_contracts(contracts: list) -> list:
    """Keep only the contract fields the integration needs."""
    return [{k: c[k] for k in _CONTRACT_FIELDS if k in c} for c in contracts if isinstance(c, dict) and c.get("id") is not None]

CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_MIN_TEMP = 18.0
DEFAULT_MAX_TEMP = 30.0
DEFAULT_SCAN_INTERVAL = 300  # seconds (5 min); commands get their own refreshes

# Per-room target temperature offset (number entity): the AC is driven to target + offset
OFFSET_MIN = -3.0
OFFSET_MAX = 3.0
OFFSET_STEP = 0.5
# Physical setpoint limits of the AC, if known (None = unknown: target + offset is not clamped)
DEVICE_MIN_SETPOINT: float | None = None
DEVICE_MAX_SETPOINT: float | None = None

TEMP_STEP = 0.5  # one Temp Up/Down pulse = 0.5 C (measured: 1950 -> 2000)
RAW_TEMP_SCALE = 100  # API reports C x 100
PULSE_INTERVAL = 5.0  # default; see CONF_PULSE_INTERVAL
PULSES_BETWEEN_READS = 3  # re-read setpoint every N pulses
REFRESH_AFTER_COMMAND = 5.0  # seconds
OVERRIDE_REFRESH_DELAY = 10.0  # s after a target change / override switch-on: extra refresh (debounced)
# Power -> fan sequencing (real unit: a fan press sent while the AC is still starting is lost)
CONF_UNLOCK_ENABLED = "unlock_enabled"
DEFAULT_UNLOCK_ENABLED = False  # the door-unlock button refuses to work unless this is on
UNLOCK_STATE = "unlock"  # same body as the web portal's "Unlock (5s)" button
UNLOCK_DURATION = "5s"
CONF_FAN_AUTO_WHEN_OFF = "fan_auto_when_off"
DEFAULT_FAN_AUTO_WHEN_OFF = True  # off: fan -> Auto, on: re-apply the remembered speed
CONF_POWER_SETTLE = "power_settle"
DEFAULT_POWER_SETTLE = 10  # s after a power press before the first power check (real unit: ~7 s)
POWER_SETTLE_MAX = 60
CONF_FAN_SETTLE = "fan_settle"
DEFAULT_FAN_SETTLE = 10  # s after a fan press before the first fan verification read (fan is slow)
FAN_SETTLE_MAX = 60
CONF_POWER_FAN_DELAY = "power_fan_delay"
DEFAULT_POWER_FAN_DELAY = 5  # seconds after power is confirmed before the fan press
POWER_FAN_DELAY_MAX = 30
POWER_POLL_INTERVAL = 2.0  # seconds between reads while waiting for the power change
POWER_CONFIRM_MAX = 30.0  # give up this long after the settle time if the power did not change
POWER_FOLLOWUP_INTERVAL = 5.0  # after that: keep reading the room this often (until the confirm timeout)
FAN_VERIFY_DELAY = 5.0  # min gap between the two fan verification reads (the first waits the fan settle time)
FAN_VERIFY_READS = 2  # consecutive matching reads needed to confirm a fan change
OPTIMISTIC_TIMEOUT = 25.0  # seconds before an unconfirmed optimistic value is dropped
TOKEN_REFRESH_MARGIN = 600  # refresh access token this many seconds before expiry
DEFAULT_TOKEN_LIFETIME = 86400

import json as _json
from pathlib import Path as _Path

INTEGRATION_VERSION: str = _json.loads((_Path(__file__).parent / "manifest.json").read_text())["version"]

PULSE_VALUE = "PULSE"

# Logical roles, discovered from control/status labels returned by the API.
ROLE_POWER = "power"
ROLE_FAN_AUTO = "auto"
ROLE_FAN_LOW = "low"
ROLE_FAN_MEDIUM = "medium"
ROLE_FAN_HIGH = "high"
ROLE_TEMP_UP = "temp_up"
ROLE_TEMP_DOWN = "temp_down"
ROLE_SETPOINT = "setpoint"
ROLE_ROOM_TEMP = "room_temp"

FAN_ROLES = (ROLE_FAN_AUTO, ROLE_FAN_LOW, ROLE_FAN_MEDIUM, ROLE_FAN_HIGH)

CONF_NOTIFICATIONS = "notifications"
CONF_PULSE_INTERVAL = "pulse_interval"
DEFAULT_NOTIFICATIONS = True
DEFAULT_PULSE_INTERVAL = 5.0  # seconds between pulse sends (start-to-start); option range 0.5-10 s
PULSE_INTERVAL_MIN = 0.5
PULSE_INTERVAL_MAX = 10.0

CONFIRM_TIMEOUT = 20.0  # base seconds to confirm; effective = pulses x spacing + this
ALERT_AFTER = 300.0  # seconds of controller-disconnected / portal-unreachable before notifying

CMD_PENDING = "pending"
CMD_CONFIRMED = "confirmed"
CMD_NOT_CONFIRMED = "not_confirmed"
CMD_FAILED = "failed"
CMD_SUPERSEDED = "superseded"  # replaced by a newer action for the same room
CMD_RESULTS = [CMD_PENDING, CMD_CONFIRMED, CMD_NOT_CONFIRMED, CMD_FAILED, CMD_SUPERSEDED]
TEMP_DEBOUNCE = 1.5  # s: rapid +/- taps are merged into one target before pressing

CONF_MAX_RETRIES = "max_retries"
DEFAULT_MAX_RETRIES = 2
MAX_RETRIES_LIMIT = 5

# Drift detection ("On external change")
CONF_EXTERNAL_CHANGE = "external_change"
CONF_DRIFT_GRACE = "drift_grace"
CONF_ADOPT_EXTERNAL = "adopt_external"
EXT_NOTIFY = "notify"
EXT_RESTORE = "restore"
EXT_RESTORE_NOTIFY = "restore_notify"
EXT_IGNORE = "ignore"
EXT_MODES = [EXT_RESTORE_NOTIFY, EXT_RESTORE, EXT_NOTIFY, EXT_IGNORE]
DEFAULT_EXTERNAL_CHANGE = EXT_RESTORE_NOTIFY
DEFAULT_DRIFT_GRACE = 60  # seconds of sustained drift (and >= DRIFT_MIN_POLLS polls) before acting
DEFAULT_ADOPT_EXTERNAL = False
DRIFT_MIN_POLLS = 2
STORE_VERSION = 1

# Max cooling override: the integration cycles room power around the target with hysteresis
OVERRIDE_HYSTERESIS = 0.5  # °C: off below target - 0.5, on at target + 0.5 or more
CONF_OVERRIDE_SENSORS = "override_temp_sensors"  # option: {zone_key: sensor entity_id}
OVERRIDE_SENSOR_DEBOUNCE = 5.0  # s: external sensor change -> override evaluation (own timer)
OVERRIDE_MIN_TOGGLE_INTERVAL = 180.0  # s: minimum time between override power toggles per room

# Bookmarklet shown in the portal-session steps: copies the portal's localStorage 'msheireb-auth' value
BOOKMARKLET = (
    "javascript:(()=>{const v=localStorage.getItem('msheireb-auth');if(!v){alert('Not signed in to the "
    "Msheireb portal on this page. Open web-prod.mp-mdd.com, sign in, then tap this bookmark again.');"
    "return;}if(navigator.clipboard){navigator.clipboard.writeText(v).then(()=>alert('Copied! Now paste "
    "it in Home Assistant.'),()=>prompt('Copy this:',v));}else{prompt('Copy this:',v);}})()"
)
