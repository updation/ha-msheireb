"""In-memory fake of MsheirebApi that simulates the controller (no network)."""
import copy

from custom_components.msheireb.api import MsheirebAuthError


class FakeApi:
    instances: list["FakeApi"] = []
    payload: dict = {}
    controller = "connected"
    fail_login = None  # exception to raise on login
    fail_refresh = None  # exception to raise when signing in with a refresh token
    sign_in_contracts = None  # contracts argument last passed to the refresh-token sign-in
    no_contracts = False  # sign-in without pasted contracts finds none
    fail_fetch = None  # exception to raise on data fetch
    ignore_commands = False  # simulate presses the AC never registers
    drop_pulses = 0  # drop the next N presses (simulate missed presses)

    def __init__(self, session, email, password, access_token=None, refresh_token=None,
                 expires_at=None, token_callback=None, status_callback=None, contracts=None):
        self.email, self.password = email, password
        self.access_token, self.refresh_token, self.expires_at = access_token, refresh_token, expires_at or 0
        self._cb = token_callback
        self._status_cb = status_callback
        self.auth_status = "ok"
        self.last_response_ms = 123.4
        self.contracts = [{"id": 4242, "unitId": "DEMO01"}]
        self.commands: list[dict] = []
        self.state = copy.deepcopy(FakeApi.payload)
        FakeApi.instances.append(self)

    async def async_login(self):
        if FakeApi.fail_login:
            raise FakeApi.fail_login
        self.access_token, self.refresh_token, self.expires_at = "A", "R", 9e9
        if self._cb:
            self._cb("A", "R", 9e9)
        return {"access_token": "A", "user": {"id": 1263}, "contracts": self.contracts}

    async def async_sign_in_with_refresh_token(self, refresh_token, contracts=None):
        if FakeApi.fail_refresh:
            raise FakeApi.fail_refresh
        self.access_token, self.refresh_token, self.expires_at = "SA", "SR", 9e9
        self.user = {"id": 1263, "email": "sess@example.com"}
        FakeApi.sign_in_contracts = contracts
        if contracts:
            self.contracts = contracts
        elif FakeApi.no_contracts:
            self.contracts = []
        return {"user": self.user, "contracts": self.contracts}

    async def async_get_contracts(self):
        if FakeApi.fail_fetch:
            raise FakeApi.fail_fetch
        return self.contracts

    async def async_get_smart_home(self, cid):
        return copy.deepcopy(self.state)

    async def async_get_controller_status(self, ip):
        return {"ip": ip, "status": FakeApi.controller}

    unlock_error = None  # MsheirebError to raise from the (mock) unlock
    unlock_calls: list = []
    lock_reads = 0

    async def async_unlock_door(self, contract_id, duration="5s"):
        FakeApi.unlock_calls.append((contract_id, duration))
        if FakeApi.unlock_error:
            raise FakeApi.unlock_error
        return None

    async def async_get_lock_status(self, cid):
        FakeApi.lock_reads += 1
        return {"contract_id": cid, "locks": [{"display_name": "L1", "low_battery": False,
                                                "current_lock_status": {"connected": True}}]}

    # --- simulated device behaviour ---
    def _device(self, sn, code):
        for room in self.state["rooms"]:
            for dev in room["devices"]:
                for c in dev["controls"]:
                    if c["sn"] == sn and c["type_code"] == code:
                        return dev, c["label"]
        raise AssertionError(f"unknown control {code}{sn}")

    async def async_send_command(self, ip, sn, type_io, type_code, value):
        self.commands.append({"ip": ip, "sn": sn, "type_io": type_io, "type_code": type_code, "value": value})
        dev, label = self._device(sn, type_code)
        if FakeApi.ignore_commands:
            return {"ok": True}
        if FakeApi.drop_pulses > 0:
            FakeApi.drop_pulses -= 1
            return {"ok": True}
        an = {a["label"]: a for a in dev["status"]["analog"]}
        dg = {d["label"]: d for d in dev["status"]["digital"]}
        if label == "HVAC Temp Up":
            an["HVAC Setpoint"]["current_status"] += 50
        elif label == "HVAC Temp Down":
            an["HVAC Setpoint"]["current_status"] -= 50
        elif label == "HVAC AC":
            dg["HVAC AC"]["current_status"] = "OFF" if dg["HVAC AC"]["current_status"] == "ON" else "ON"
        elif label.startswith("HVAC Fan"):
            for k in ("HVAC Fan Auto", "HVAC Fan Low", "HVAC Fan Medium", "HVAC Fan High"):
                dg[k]["current_status"] = "ON" if k == label else "OFF"
        return {"ok": True}
