"""
Shared fakes for the test suite. Nothing here touches a real thermostat or the
network: aiohomekit's IpPairing/Controller are replaced with FakePairing, which
serves a hand-built HomeKit accessory tree from a FakeDevice, and any attempt
to open a real socket fails the test.
"""

import asyncio
import json
import socket
import time

import pytest

import ecobee_local.controller as controller_mod
from ecobee_local import EcobeeController

HAP = "-0000-1000-8000-0026BB765291"

# (aid, iid) of each characteristic in the fake thermostat
CURRENT_TEMP = (1, 10)
TARGET_TEMP = (1, 11)
MODE = (1, 12)
RUNNING_STATE = (1, 13)
COOL_THRESHOLD = (1, 14)
HEAT_THRESHOLD = (1, 15)
HUMIDITY = (1, 16)
DISPLAY_UNITS = (1, 17)
TARGET_FAN = (1, 18)
CURRENT_FAN = (1, 19)
COMFORT_READ = (1, 20)
COMFORT_WRITE = (1, 21)
VENDOR_MODE_LOOKALIKE = (1, 9)
VENDOR_TARGET_DECOY = (1, 5)
BUILTIN_OCCUPANCY = (1, 40)
SENSOR_NAME = (2, 2)
SENSOR_TEMP = (2, 3)
SENSOR_OCCUPANCY = (2, 4)
SENSOR_BATTERY = (2, 5)
SENSOR_LOW_BATTERY = (2, 6)


# --------------------------------------------------------------- the network

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if anything tries to reach a real host."""
    def blocked(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


# ------------------------------------------------------- fake HomeKit objects

class Char:
    def __init__(self, type_, iid, value=None, perms=("pr", "ev"),
                 description="", unit=None, minValue=None, maxValue=None,
                 minStep=None):
        self.type = type_
        self.iid = iid
        self.value = value
        self.perms = list(perms)
        self.description = description
        self.unit = unit
        self.minValue = minValue
        self.maxValue = maxValue
        self.minStep = minStep


class Service:
    def __init__(self, type_, characteristics):
        self.type = type_
        self.characteristics = characteristics


class Accessory:
    def __init__(self, aid, services):
        self.aid = aid
        self.services = services


def build_accessories(comfort=True, humidity_target=False):
    """
    A realistic ecobee Premium-style tree: the thermostat accessory (with a
    vendor service and a vendor characteristic that share UUID prefixes with
    real thermostat characteristics, to catch loose matching), a built-in
    occupancy sensor, and one remote SmartSensor.
    """
    thermostat_chars = [
        # vendor characteristic whose first 8 hex digits equal Mode's.
        # A loose prefix match would read this instead of the real mode.
        Char("00000033-ABCD-4ECB-B5C1-2B5F0B5D1E1A", VENDOR_MODE_LOOKALIKE[1],
             perms=("pr",)),
        Char("00000011" + HAP, CURRENT_TEMP[1], unit="celsius",
             description="Current Temperature"),
        Char("35", TARGET_TEMP[1], perms=("pr", "pw", "ev"), unit="celsius",
             minValue=7.0, maxValue=33.3, minStep=0.1,
             description="Target Temperature"),      # short-form UUID
        Char("00000033" + HAP, MODE[1], perms=("pr", "pw", "ev")),
        Char("0000000F" + HAP, RUNNING_STATE[1]),
        Char("0000000D" + HAP, COOL_THRESHOLD[1], perms=("pr", "pw", "ev")),
        Char("00000012" + HAP, HEAT_THRESHOLD[1], perms=("pr", "pw", "ev")),
        Char("00000010" + HAP, HUMIDITY[1]),
        Char("00000036" + HAP, DISPLAY_UNITS[1], perms=("pr", "pw", "ev")),
        Char("000000BF" + HAP, TARGET_FAN[1], perms=("pr", "pw", "ev")),
        Char("000000AF" + HAP, CURRENT_FAN[1]),
    ]
    if humidity_target:
        thermostat_chars.append(
            Char("00000034" + HAP, 22, perms=("pr", "pw", "ev")))
    if comfort:
        thermostat_chars += [
            Char(controller_mod._COMFORT_READ, COMFORT_READ[1]),
            Char(controller_mod._COMFORT_WRITE, COMFORT_WRITE[1],
                 perms=("pw",)),
        ]
    return [
        Accessory(1, [
            Service("0000003E" + HAP, [
                Char("00000023" + HAP, 2, value="Main Floor"),
            ]),
            # vendor service listed BEFORE the thermostat, containing a real
            # TargetTemperature UUID. Must not be mistaken for the thermostat.
            Service("E9A2B3C4-0000-1000-8000-ECOBEE000001", [
                Char("00000035" + HAP, VENDOR_TARGET_DECOY[1]),
            ]),
            Service("0000004A" + HAP, thermostat_chars),
            Service("00000086" + HAP, [
                Char("00000071" + HAP, BUILTIN_OCCUPANCY[1]),
            ]),
        ]),
        Accessory(2, [
            Service("0000008A" + HAP, [
                Char("00000023" + HAP, SENSOR_NAME[1]),
                Char("00000011" + HAP, SENSOR_TEMP[1]),
            ]),
            Service("00000086" + HAP, [
                Char("00000071" + HAP, SENSOR_OCCUPANCY[1]),
            ]),
            Service("00000096" + HAP, [
                Char("00000068" + HAP, SENSOR_BATTERY[1]),
                Char("00000079" + HAP, SENSOR_LOW_BATTERY[1]),
            ]),
        ]),
    ]


def default_values():
    return {
        CURRENT_TEMP: 21.5,       # 70.7F -> 71
        TARGET_TEMP: 22.0,        # 71.6F -> 72
        MODE: 1,                  # heat
        RUNNING_STATE: 1,
        COOL_THRESHOLD: 25.0,     # 77F
        HEAT_THRESHOLD: 20.0,     # 68F
        HUMIDITY: 44.6,
        DISPLAY_UNITS: 1,
        TARGET_FAN: 1,
        CURRENT_FAN: 2,
        COMFORT_READ: 0,          # home
        VENDOR_MODE_LOOKALIKE: 99,
        VENDOR_TARGET_DECOY: 5.0,
        BUILTIN_OCCUPANCY: 1,
        SENSOR_NAME: "Bedroom",
        SENSOR_TEMP: 19.0,        # 66.2F -> 66
        SENSOR_OCCUPANCY: 0,
        SENSOR_BATTERY: 87,
        SENSOR_LOW_BATTERY: 0,
    }


class FakeDevice:
    """
    What the 'thermostat' looks like and how it misbehaves. Tests set the
    *_exc attributes to make a step raise, read_errors / write_errors to make
    individual characteristics fail the way aiohomekit reports it (a status
    dict instead of a value), or hang=True to make every call block.
    """

    def __init__(self, **tree_kwargs):
        self.accessories = build_accessories(**tree_kwargs)
        self.values = default_values()
        self.connect_exc = None
        self.read_exc = None
        self.write_exc = None
        self.subscribe_exc = None
        self.read_errors = {}     # {(aid, iid): hap status code}
        self.write_errors = {}    # {(aid, iid): hap status code}
        self.hang = False
        # turn a cancel into an ordinary error, as connection code can when
        # it's torn down mid-call (and as Python 3.10's asyncio.wait_for can
        # in a race)
        self.swallow_cancel = False
        self.connect_delay = 0    # seconds before connecting succeeds/fails
        self.write_delay = 0      # seconds before a write is applied
        self.writes = []          # every (aid, iid, value) written
        self.pairings = []        # every FakePairing created for this device
        self.reads = 0

    async def _maybe_hang(self):
        if self.hang:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # like a real connection closing its socket, cancelling
                # takes a few trips round the event loop
                for _ in range(20):
                    await asyncio.sleep(0)
                await asyncio.sleep(0.1)
                if self.swallow_cancel:
                    self.hang = False
                    raise OSError("connection closed")
                raise


class FakePairing:
    def __init__(self, device, controller, creds):
        self.device = device
        self.controller = controller
        self.creds = creds
        self.accessories = []
        self.closed = False
        self.subscribed = None
        self.callback = None
        device.pairings.append(self)

    async def list_accessories_and_characteristics(self):
        await self.device._maybe_hang()
        await asyncio.sleep(self.device.connect_delay)
        if self.device.connect_exc:
            raise self.device.connect_exc
        self.accessories = self.device.accessories

    async def get_characteristics(self, ids):
        await self.device._maybe_hang()
        self.device.reads += 1
        if self.device.read_exc:
            raise self.device.read_exc
        out = {}
        for cid in ids:
            if cid in self.device.read_errors:
                out[cid] = {"status": self.device.read_errors[cid],
                            "description": "Resource is busy, try again."}
            else:
                out[cid] = {"value": self.device.values.get(cid)}
        return out

    async def put_characteristics(self, chars):
        await self.device._maybe_hang()
        await asyncio.sleep(self.device.write_delay)
        if self.device.write_exc:
            raise self.device.write_exc
        # like aiohomekit: rejected writes come back as a status dict
        # (it does NOT raise), accepted ones are absent from the result
        result = {}
        for aid, iid, value in chars:
            self.device.writes.append((aid, iid, value))
            if (aid, iid) in self.device.write_errors:
                result[(aid, iid)] = {
                    "status": self.device.write_errors[(aid, iid)],
                    "description": "Value supplied is not valid.",
                }
            else:
                self.device.values[(aid, iid)] = value
        return result

    async def subscribe(self, ids):
        if self.device.subscribe_exc:
            raise self.device.subscribe_exc
        self.subscribed = set(ids)

    def dispatcher_connect(self, cb):
        self.callback = cb

    async def close(self):
        self.closed = True


@pytest.fixture
def device(monkeypatch):
    """A fake thermostat; IpPairing/Controller in the controller talk to it."""
    dev = FakeDevice()
    monkeypatch.setattr(controller_mod, "Controller", lambda *a, **k: object())
    monkeypatch.setattr(
        controller_mod, "IpPairing",
        lambda controller, creds: FakePairing(dev, controller, creds))
    return dev


# ------------------------------------------------------------ pairing files

CREDS = {
    "AccessoryPairingID": "AA:BB:CC:DD:EE:FF",
    "AccessoryLTPK": "00" * 32,
    "iOSPairingId": "11111111-2222-3333-4444-555555555555",
    "iOSDeviceLTSK": "11" * 32,
    "iOSDeviceLTPK": "22" * 32,
    "AccessoryIP": "192.0.2.10",
    "AccessoryPort": 1200,
    "Connection": "IP",
}


def write_pair_file(folder, filename="pair.json", label="main",
                    name="Main Floor", **extra):
    data = dict(CREDS)
    if label is not None:
        data["_label"] = label
    if name is not None:
        data["_name"] = name
    data.update(extra)
    path = folder / filename
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def pair_folder(tmp_path):
    write_pair_file(tmp_path)
    return tmp_path


# ------------------------------------------------------------- controllers

def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def make_controller():
    """Build controllers and always stop them at the end of the test."""
    made = []

    def make(folder, **kwargs):
        kwargs.setdefault("poll_interval", 3600)
        kwargs.setdefault("comfort_warning", False)
        ec = EcobeeController.from_folder(app_folder=str(folder), **kwargs)
        made.append(ec)
        return ec

    yield make
    for ec in made:
        ec.stop()


@pytest.fixture
def connected(device, pair_folder, make_controller):
    """A started controller whose 'main' thermostat has finished connecting."""
    ec = make_controller(pair_folder)
    ec.start()
    assert wait_until(lambda: ec.cache["main"]["online"]), "never connected"
    return ec


def run_async(ec, coro_fn):
    """
    Run a controller coroutine on a fresh event loop without start(), for
    tests of the async internals. Creates the per-label locks first.
    """
    async def go():
        for label in ec.labels:
            ec._state[label]["lock"] = asyncio.Lock()
        return await coro_fn()
    return asyncio.run(go())
