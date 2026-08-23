"""
ecobee_controller.py
====================
A lightweight, standalone, fully-local controller for Ecobee thermostats over
Apple HomeKit (HAP). No cloud, no OAuth, no ecobee developer key, no Home
Assistant. Pair once with pair_ecobee.py, then use this to read and control.

Design
------
HomeKit access via aiohomekit is async, but most apps that want to use a
thermostat (Flask servers, scripts, GUIs) are synchronous. This module runs a
dedicated asyncio event loop on its own background thread and exposes a small
*synchronous* API that marshals calls onto that loop. It keeps a persistent
connection per thermostat, subscribes for push updates when possible, and runs
a periodic health poll as a safety net. Reads hit an in-memory cache so callers
never block on the device; only writes touch the thermostat.

Pairing files
-------------
Each thermostat is one JSON file created by pair_ecobee.py. A file may include
two extra keys that this module reads (and pair_ecobee.py writes):

    "_label":  short id used in code / API calls (e.g. "main")
    "_name":   friendly display name (e.g. "Main Floor")

Older files without these still work; the label falls back to the filename stem.

Three ways to tell the controller which thermostats to use
----------------------------------------------------------
1. Auto-discover EVERY pairing file in the folder (the easy default):

       ec = EcobeeController.from_folder()
       ec.start()

2. Auto-discover files matching a glob (e.g. only pair*.json):

       ec = EcobeeController.from_folder(pattern="pair*.json")

3. Explicit mapping of label -> filename or path (full control):

       ec = EcobeeController({"main": "pair.json", "up": "pair1.json"})

Then:

    ec.get_status()               # all thermostats
    ec.get_status("main")         # one
    ec.set_temp("main", 72)       # Fahrenheit
    ec.set_mode("main", EcobeeController.MODE_HEAT)
    ec.get_name("main")           # friendly display name

Modes (HomeKit TargetHeatingCoolingState): 0=off, 1=heat, 2=cool, 3=auto.
"""

import asyncio
import glob
import json
import os
import sys
import threading

from aiohomekit.controller import Controller
from aiohomekit.controller.ip.pairing import IpPairing


# HomeKit characteristic UUID prefixes for a thermostat service.
# aid/iid vary per device, so we locate characteristics by type prefix.
# All of these are standard Apple-defined characteristics (documented in the
# HAP spec), so they behave the same across ecobee models and firmwares.
_UUID = {
    "current_temp":    "00000011",  # CurrentTemperature (read, celsius)
    "target_temp":     "00000035",  # TargetTemperature (read/write, celsius)
    "mode":            "00000033",  # TargetHeatingCoolingState 0off1heat2cool3auto
    "running_state":   "0000000F",  # CurrentHeatingCoolingState 0off1heat2cool
    "cool_threshold":  "0000000D",  # CoolingThresholdTemperature (r/w, celsius)
    "heat_threshold":  "00000012",  # HeatingThresholdTemperature (r/w, celsius)
    "humidity":        "00000010",  # CurrentRelativeHumidity (read, percent)
    "display_units":   "00000036",  # TemperatureDisplayUnits 0=C 1=F (r/w)
    "target_fan":      "000000BF",  # TargetFanState 0=manual 1=auto (r/w)
    "current_fan":     "000000AF",  # CurrentFanState 0=inactive 1=idle 2=blowing
    "target_humidity": "00000034",  # TargetRelativeHumidity (r/w, percent) *
}
# * only present on models with a humidifier/dehumidifier (e.g. Smart Premium).
#   Setters/readers skip it gracefully when the characteristic is absent.

# Characteristics that live on remote SENSOR accessories (and the thermostat's
# own built-in sensor). Standard Apple UUIDs, so uniform across models.
_SENSOR_UUID = {
    "name":         "00000023",  # Name (which room this sensor is)
    "temperature":  "00000011",  # CurrentTemperature (celsius)
    "occupancy":    "00000071",  # OccupancyDetected 0/1
    "motion":       "00000022",  # MotionDetected bool
    "battery":      "00000068",  # BatteryLevel percent
    "low_battery":  "00000079",  # StatusLowBattery 0 ok / 1 low
}

# which of the above are temperatures (stored celsius, exposed fahrenheit)
_TEMP_KEYS = ("current_temp", "target_temp", "cool_threshold", "heat_threshold")
# which are plain integer passthroughs in the cache
_INT_KEYS = ("mode", "running_state", "display_units", "target_fan",
             "current_fan", "target_humidity")

# keys pair_ecobee.py adds that are metadata, not HomeKit credentials
_META_KEYS = ("_label", "_name")


def _blank_cache():
    """
    A fresh, all-unknown cache entry for one thermostat. Everything starts as
    "--" meaning 'not read yet / not supported by this model'. A real reading
    overwrites the fields the device actually exposes; anything the model lacks
    (e.g. target_humidity on a basic thermostat) stays "--" rather than a
    misleading 0.
    """
    entry = {"online": False}
    for k in _TEMP_KEYS:
        entry[k] = "--"
        entry[k + "_c"] = None
    for k in _INT_KEYS:
        entry[k] = "--"
    entry["humidity"] = "--"
    entry["sensors"] = []  # list of remote-sensor dicts (Premium etc.)
    return entry


# Folder name where pairing files live. Neutral by default so the library
# isn't tied to any one app. Override per-call with app_folder=... or the
# --folder CLI flag if you keep your credentials somewhere else (e.g. an
# existing project folder).
APP_NAME = "ecobee-local"


def default_app_folder():
    """Return the per-user credentials folder (matches pair_ecobee.py)."""
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def _c_to_f(c):
    return int(round(c * 9 / 5 + 32))


def _f_to_c_half(f):
    """Fahrenheit -> Celsius, snapped to 0.5 steps (HomeKit requirement)."""
    c = (float(f) - 32) * 5 / 9
    return round(c * 2) / 2


def _read_meta(path):
    """Return (label, name) from a pairing file, with sensible fallbacks."""
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, "r") as f:
            data = json.load(f)
        label = data.get("_label") or stem
        name = data.get("_name") or label
        return label, name
    except Exception:
        return stem, stem


class EcobeeController:
    MODE_OFF = 0
    MODE_HEAT = 1
    MODE_COOL = 2
    MODE_AUTO = 3

    def __init__(self, pair_files=None, app_folder=None, poll_interval=30.0,
                 names=None, debug=False):
        """
        pair_files: dict of {label: filename_or_path}. Bare filenames are
                    resolved inside app_folder; absolute paths used as-is.
                    If None, nothing is loaded (use from_folder instead).
        app_folder: where bare filenames resolve (defaults to the app folder).
        poll_interval: seconds between safety-net health polls.
        names: optional {label: friendly_name} overrides. If not given, the
               name is read from the file's "_name" key, else equals the label.
        debug: if True, print HomeKit subscription events as they arrive
               (noisy; useful only when diagnosing push-update behaviour).
        """
        self.app_folder = app_folder or default_app_folder()
        self.poll_interval = poll_interval
        self.debug = debug
        pair_files = pair_files or {}
        names = names or {}

        self._pair_paths = {}
        self._names = {}
        for label, fn in pair_files.items():
            path = fn if os.path.isabs(fn) else os.path.join(self.app_folder, fn)
            self._pair_paths[label] = path
            # priority: explicit names arg > file's _name > label
            _, file_name = _read_meta(path)
            self._names[label] = names.get(label, file_name)

        self.labels = list(self._pair_paths.keys())

        self.cache = {label: _blank_cache() for label in self.labels}

        self._state = {
            label: {"pairing": None, "ids": None, "sensors": None, "lock": None}
            for label in self.labels
        }

        self._loop = None
        self._loop_ready = threading.Event()
        self._started = False

    # ---------------------------------------------------------- constructors

    @classmethod
    def from_folder(cls, app_folder=None, pattern="*.json", **kwargs):
        """
        Build a controller from every pairing file in a folder.

        app_folder: folder to scan (defaults to smartdash).
        pattern:    glob to match, e.g. "*.json" (default) or "pair*.json".
                    Files whose stem starts with "." are skipped.
        Labels come from each file's "_label" key, else the filename stem.
        Duplicate labels are disambiguated by appending the stem.
        """
        folder = app_folder or default_app_folder()
        pair_files = {}
        for path in sorted(glob.glob(os.path.join(folder, pattern))):
            base = os.path.basename(path)
            if base.startswith("."):
                continue
            label, _ = _read_meta(path)
            if label in pair_files:
                label = f"{label}_{os.path.splitext(base)[0]}"
            pair_files[label] = path
        return cls(pair_files, app_folder=folder, **kwargs)

    def add_thermostat(self, filename, label=None, name=None):
        """
        Register one more pairing file after construction (before start()).
        Returns the label used.
        """
        if self._started:
            raise RuntimeError("add_thermostat must be called before start()")
        path = filename if os.path.isabs(filename) \
            else os.path.join(self.app_folder, filename)
        file_label, file_name = _read_meta(path)
        label = label or file_label
        if label in self._pair_paths:
            raise ValueError(f"label '{label}' already registered")
        self._pair_paths[label] = path
        self._names[label] = name or file_name
        self.labels.append(label)
        self.cache[label] = _blank_cache()
        self._state[label] = {"pairing": None, "ids": None, "sensors": None, "lock": None}
        return label

    # ---------------------------------------------------------------- public

    def start(self, wait_timeout=5.0, connect=True):
        """
        Spin up the background event loop. Idempotent.

        connect=True  (default): also open persistent connections to every
                      thermostat and run the health/poll loop. Use for normal
                      operation.
        connect=False: just start the loop and create locks, without opening
                      any connections. Use before a one-off dump_characteristics
                      so it's the only connection to the device (Ecobees limit
                      concurrent HomeKit connections).
        """
        if self._started:
            return
        self._started = True
        self._auto_connect = connect
        threading.Thread(target=self._thread_main, daemon=True).start()
        self._loop_ready.wait(timeout=wait_timeout)

    def get_status(self, label=None):
        """
        Return cached status for one label, or all if label is None.

        In addition to the raw characteristics, each status includes computed
        fields so a UI never has to know HomeKit's quirks:

          setpoint_kind : "single" in heat/cool/off mode, "range" in auto mode
          display_target: the value(s) a UI should actually show:
                          - single-setpoint modes -> one number (F)
                          - auto mode              -> dict {"heat":F, "cool":F}
          target_temp   : blanked to None in auto mode, because the single
                          target is meaningless there (the device drives off
                          the heat/cool thresholds instead). Use heat_threshold
                          and cool_threshold, or display_target, in auto.
        """
        if label is None:
            return {lbl: self._status_for(lbl) for lbl in self.labels}
        return self._status_for(label)

    def _status_for(self, label):
        s = dict(self.cache[label])
        if s.get("mode") == self.MODE_AUTO:
            s["setpoint_kind"] = "range"
            s["display_target"] = {
                "heat": s.get("heat_threshold"),
                "cool": s.get("cool_threshold"),
            }
            # the lone target is not meaningful in auto; don't present it
            s["target_temp"] = None
        else:
            s["setpoint_kind"] = "single"
            s["display_target"] = s.get("target_temp")
        return s

    def get_name(self, label):
        """Friendly display name for a label."""
        return self._names.get(label, label)

    def list_thermostats(self):
        """Return [{'label':..., 'name':..., 'online':...}, ...]."""
        return [
            {"label": lbl, "name": self._names.get(lbl, lbl),
             "online": self.cache[lbl]["online"]}
            for lbl in self.labels
        ]

    def rename(self, label, name):
        """Change the in-memory display name (does not rewrite the file)."""
        if label in self._names:
            self._names[label] = name

    def set_temp(self, label, temp_f, timeout=12.0):
        """Set target temperature (Fahrenheit). Returns {'ok': bool, ...}."""
        return self._run(self._set_temp(label, temp_f), timeout=timeout)

    def set_mode(self, label, mode_int, timeout=12.0):
        """Set mode (use MODE_* constants). Returns {'ok': bool, ...}."""
        return self._run(self._set_mode(label, mode_int), timeout=timeout)

    def set_heat_threshold(self, label, temp_f, timeout=12.0):
        """
        Set the heating threshold temperature (Fahrenheit). In auto mode the
        thermostat heats when the room falls below this. Returns {'ok': ...}.
        """
        return self._run(
            self._set_temp_char(label, "heat_threshold", temp_f), timeout=timeout)

    def set_cool_threshold(self, label, temp_f, timeout=12.0):
        """
        Set the cooling threshold temperature (Fahrenheit). In auto mode the
        thermostat cools when the room rises above this. Returns {'ok': ...}.
        """
        return self._run(
            self._set_temp_char(label, "cool_threshold", temp_f), timeout=timeout)

    def set_fan(self, label, auto, timeout=12.0):
        """
        Set fan mode. auto=True -> fan runs on the thermostat's schedule/auto;
        auto=False -> fan runs continuously (manual/on). Returns {'ok': ...}.
        """
        val = 1 if auto else 0
        return self._run(
            self._set_int_char(label, "target_fan", val), timeout=timeout)

    def set_display_units(self, label, fahrenheit, timeout=12.0):
        """Set the on-device display units. fahrenheit=True -> F, False -> C."""
        val = 1 if fahrenheit else 0
        return self._run(
            self._set_int_char(label, "display_units", val), timeout=timeout)

    def set_target_humidity(self, label, percent, timeout=12.0):
        """
        Set the target relative humidity (percent), on models with a
        humidifier/dehumidifier (e.g. Smart Premium). Returns an error dict
        on models that don't expose it.
        """
        return self._run(
            self._set_int_char(label, "target_humidity", int(percent)),
            timeout=timeout)

    def refresh(self, label, timeout=12.0):
        """Force an immediate read from the device into the cache."""
        return self._run(self._refresh(label), timeout=timeout)

    def dump_characteristics(self, label, timeout=20.0):
        """
        Return a full inventory of every accessory/service/characteristic this
        thermostat exposes over HomeKit, including UUID type, description,
        current value, permissions (read/write/notify), unit and value range.

        Useful for discovering what a specific model supports so you can map
        extra characteristics (thresholds, humidity, air quality, ecobee
        vendor-specific ones) into the controller. Returns a list of dicts.
        """
        return self._run(self._dump(label), timeout=timeout)

    # ------------------------------------------------------------- internals

    def _run(self, coro, timeout=12.0):
        if self._loop is None:
            return {"ok": False, "error": "controller not started"}
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.create_task(self._main())
        self._loop.run_forever()

    async def _main(self):
        # locks are needed by every path (including dump), so always create them
        for label in self.labels:
            self._state[label]["lock"] = asyncio.Lock()
        if not getattr(self, "_auto_connect", True):
            return  # loop-only mode; caller will drive dump/etc. explicitly
        for label in self.labels:
            await self._connect(label)
        await self._health_loop()

    def _find_ids(self, pairing):
        """
        Return (main_ids, sensor_specs).

        main_ids: {key -> (aid, iid)} for the thermostat's own characteristics.
        sensor_specs: list of {key -> (aid,iid)} dicts, one per sensor accessory
                      (remote SmartSensors plus the thermostat's built-in
                      occupancy sensor), each carrying whatever of the
                      _SENSOR_UUID characteristics it exposes.

        Characteristics are read ONLY from inside the standard HomeKit
        Thermostat service (type 0000004A). This matters on feature-rich models
        (e.g. Smart Premium) whose accessory also exposes vendor-specific and
        Siri/Alexa services that can contain characteristics with overlapping
        type prefixes; scoping to the Thermostat service avoids grabbing the
        wrong one (which previously produced bogus mode/setpoint readings).
        """
        THERMOSTAT_SERVICE = "0000004A"
        target_prefix = _UUID["target_temp"]

        def svc_type(srv):
            return str(getattr(srv, "type", "")).upper()

        # locate the accessory + service that is the actual thermostat
        thermostat_aid = None
        thermostat_srv = None
        for acc in pairing.accessories:
            for srv in acc.services:
                if svc_type(srv).startswith(THERMOSTAT_SERVICE):
                    thermostat_aid = acc.aid
                    thermostat_srv = srv
                    break
            if thermostat_srv is not None:
                break

        # fallback: some firmwares may not label the service type as expected;
        # then fall back to "the service containing TargetTemperature"
        if thermostat_srv is None:
            for acc in pairing.accessories:
                for srv in acc.services:
                    for ch in srv.characteristics:
                        if ch.type.upper().startswith(target_prefix):
                            thermostat_aid = acc.aid
                            thermostat_srv = srv
                            break
                    if thermostat_srv is not None:
                        break
                if thermostat_srv is not None:
                    break

        # main ids: match characteristics ONLY within the thermostat service.
        # Accept either the full standard HAP UUID or the exact short form,
        # but NOT a loose prefix match (which could grab a vendor characteristic
        # that merely shares the first hex digits).
        HAP_SUFFIX = "-0000-1000-8000-0026BB765291"

        def type_matches(ch_type, prefix):
            t = ch_type.upper()
            p = prefix.upper()
            full = (prefix + HAP_SUFFIX).upper()
            # full 36-char UUID, or exact short forms like "00000035" / "35"
            return t == full or t == p or t == p.lstrip("0") or t == p + HAP_SUFFIX.upper()

        main_ids = {}
        for key, prefix in _UUID.items():
            found = None
            if thermostat_srv is not None:
                for ch in thermostat_srv.characteristics:
                    if type_matches(str(ch.type), prefix):
                        found = (thermostat_aid, ch.iid)
                        break
            main_ids[key] = found

        # sensor specs: per-accessory, grouped by SERVICE so a multi-service
        # accessory (thermostat with built-in occupancy) yields sensible groups.
        sensor_specs = []

        def collect_from_service(acc, srv):
            spec = {}
            for key, prefix in _SENSOR_UUID.items():
                for ch in srv.characteristics:
                    if ch.type.upper().startswith(prefix):
                        spec[key] = (acc.aid, ch.iid)
                        break
            return spec

        for acc in pairing.accessories:
            if acc.aid == thermostat_aid:
                # the thermostat's own built-in occupancy/motion. These may be
                # split across separate services; merge them into ONE builtin
                # entry so it reads like a single sensor.
                builtin = {}
                for srv in acc.services:
                    spec = collect_from_service(acc, srv)
                    if "occupancy" in spec or "motion" in spec:
                        for k, v in spec.items():
                            builtin.setdefault(k, v)
                if builtin:
                    builtin.pop("name", None)  # use a fixed label instead
                    builtin.setdefault("temperature", main_ids.get("current_temp"))
                    builtin["_builtin"] = True
                    sensor_specs.append(builtin)
            else:
                # a separate accessory: a remote SmartSensor. Merge all its
                # services into one spec (temp, occupancy, motion, battery).
                merged = {}
                for srv in acc.services:
                    for key, val in collect_from_service(acc, srv).items():
                        merged.setdefault(key, val)
                if "temperature" in merged or "occupancy" in merged:
                    merged["_builtin"] = False
                    sensor_specs.append(merged)

        return main_ids, sensor_specs

    def _wanted(self, main_ids, sensor_specs):
        """Flatten every (aid, iid) we want to read into a list."""
        want = [v for v in main_ids.values() if v]
        for spec in sensor_specs:
            for key, val in spec.items():
                if key == "_builtin":
                    continue
                if val:
                    want.append(val)
        # dedupe (a built-in sensor may reuse the thermostat's temp id)
        seen = set()
        out = []
        for v in want:
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    def _apply_reading(self, label, main_ids, sensor_specs, readings):
        cache = self.cache[label]
        for key in _UUID:
            cid = main_ids.get(key)
            if not cid or cid not in readings:
                continue
            raw = readings[cid]["value"]
            if key in _TEMP_KEYS:
                cache[key] = _c_to_f(raw)      # Fahrenheit (rounded int)
                cache[key + "_c"] = raw        # raw Celsius from the device
            elif key == "humidity":
                cache[key] = int(round(raw))
            else:  # integer state characteristics
                cache[key] = int(raw)

        # build the sensors list fresh each read
        sensors = []
        for i, spec in enumerate(sensor_specs):
            s = {"builtin": bool(spec.get("_builtin"))}
            # name
            nid = spec.get("name")
            s["name"] = (readings.get(nid, {}) or {}).get("value") if nid else None
            if not s["name"]:
                s["name"] = "Built-in" if s["builtin"] else f"Sensor {i+1}"
            # temperature -> F, plus raw Celsius
            tid = spec.get("temperature")
            if tid and tid in readings:
                s["temperature"] = _c_to_f(readings[tid]["value"])
                s["temperature_c"] = readings[tid]["value"]
            else:
                s["temperature"] = None
                s["temperature_c"] = None
            # occupancy / motion -> bool
            for k in ("occupancy", "motion"):
                cid = spec.get(k)
                if cid and cid in readings:
                    v = readings[cid]["value"]
                    s[k] = bool(v) if not isinstance(v, str) else (v.lower() == "y")
                else:
                    s[k] = None
            # battery + low battery
            bid = spec.get("battery")
            s["battery"] = int(readings[bid]["value"]) if bid and bid in readings else None
            lid = spec.get("low_battery")
            s["low_battery"] = bool(readings[lid]["value"]) if lid and lid in readings else None
            sensors.append(s)
        cache["sensors"] = sensors
        cache["online"] = True

    def _make_callback(self, label):
        # On any subscription event we simply re-poll the floor. The exact
        # callback argument shape varies across aiohomekit versions, so rather
        # than parse it we trigger a lightweight refresh, which is always
        # correct. Events are logged only when debug=True.
        def _cb(*args, **kwargs):
            if self.debug:
                print(f"[{label}] event: args={args!r} kwargs={kwargs!r}")
            try:
                self._loop.call_soon_threadsafe(
                    lambda: self._loop.create_task(self._refresh(label))
                )
            except Exception as e:
                if self.debug:
                    print(f"[{label}] callback reschedule failed: {e}")
        return _cb

    async def _connect(self, label):
        path = self._pair_paths[label]
        if not os.path.exists(path):
            print(f"[{label}] pairing file missing: {path}")
            self.cache[label]["online"] = False
            return False
        try:
            with open(path, "r") as f:
                pdata = json.load(f)
            if not pdata:
                print(f"[{label}] pairing file empty")
                self.cache[label]["online"] = False
                return False

            # strip our metadata keys before handing creds to aiohomekit
            creds = {k: v for k, v in pdata.items() if k not in _META_KEYS}

            controller = Controller()
            pairing = IpPairing(controller, creds)

            await asyncio.wait_for(
                pairing.list_accessories_and_characteristics(), timeout=10.0
            )
            main_ids, sensor_specs = self._find_ids(pairing)

            want = self._wanted(main_ids, sensor_specs)
            readings = await asyncio.wait_for(
                pairing.get_characteristics(want), timeout=10.0
            )
            self._apply_reading(label, main_ids, sensor_specs, readings)

            try:
                await pairing.subscribe(set(want))
                pairing.dispatcher_connect(self._make_callback(label))
                if self.debug:
                    print(f"[{label}] subscribed to {len(want)} characteristics")
            except Exception as sub_err:
                print(f"[{label}] subscribe failed ({sub_err}); polling only")

            self._state[label]["pairing"] = pairing
            self._state[label]["ids"] = main_ids
            self._state[label]["sensors"] = sensor_specs
            if self.debug:
                ns = len(sensor_specs)
                print(f"[{label}] connected "
                      f"({self.cache[label]['current_temp']}F, "
                      f"target {self.cache[label]['target_temp']}F, "
                      f"{ns} sensor(s))")
            return True
        except Exception as e:
            print(f"[{label}] connect failed: {type(e).__name__}: {e}")
            self.cache[label]["online"] = False
            self._state[label]["pairing"] = None
            return False

    async def _refresh(self, label):
        st = self._state[label]
        async with st["lock"]:
            pairing = st["pairing"]
            main_ids = st["ids"]
            sensor_specs = st.get("sensors") or []
            if pairing is None:
                await self._connect(label)
                return
            try:
                want = self._wanted(main_ids, sensor_specs)
                readings = await asyncio.wait_for(
                    pairing.get_characteristics(want), timeout=8.0
                )
                self._apply_reading(label, main_ids, sensor_specs, readings)
            except Exception as e:
                if self.debug:
                    print(f"[{label}] refresh failed ({type(e).__name__}); reconnecting")
                self.cache[label]["online"] = False
                try:
                    await pairing.close()
                except Exception:
                    pass
                st["pairing"] = None
                await self._connect(label)

    async def _set_temp(self, label, temp_f):
        return await self._set_temp_char(label, "target_temp", temp_f)

    async def _set_mode(self, label, mode_int):
        return await self._set_int_char(label, "mode", int(mode_int))

    async def _set_temp_char(self, label, key, temp_f):
        """Write a temperature characteristic (given in F, sent in C, 0.5 step)."""
        st = self._state[label]
        async with st["lock"]:
            pairing = st["pairing"]
            ids = st["ids"]
            if pairing is None:
                return {"ok": False, "error": "not connected"}
            if not ids or not ids.get(key):
                return {"ok": False,
                        "error": f"this thermostat does not expose '{key}'"}
            aid, iid = ids[key]
            try:
                await asyncio.wait_for(
                    pairing.put_characteristics([(aid, iid, _f_to_c_half(temp_f))]),
                    timeout=8.0,
                )
                self.cache[label][key] = int(temp_f)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def _set_int_char(self, label, key, value):
        """Write an integer-valued state characteristic."""
        st = self._state[label]
        async with st["lock"]:
            pairing = st["pairing"]
            ids = st["ids"]
            if pairing is None:
                return {"ok": False, "error": "not connected"}
            if not ids or not ids.get(key):
                return {"ok": False,
                        "error": f"this thermostat does not expose '{key}'"}
            aid, iid = ids[key]
            try:
                await asyncio.wait_for(
                    pairing.put_characteristics([(aid, iid, int(value))]),
                    timeout=8.0,
                )
                self.cache[label][key] = int(value)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def _dump(self, label):
        st = self._state[label]
        # dump can run before a persistent connection exists; make a temp one
        pairing = st["pairing"]
        temp_pairing = None
        if pairing is None:
            path = self._pair_paths[label]
            if not os.path.exists(path):
                return [{"error": f"pairing file missing: {path}"}]
            with open(path, "r") as f:
                pdata = json.load(f)
            creds = {k: v for k, v in pdata.items() if k not in _META_KEYS}
            controller = Controller()
            temp_pairing = IpPairing(controller, creds)
            await asyncio.wait_for(
                temp_pairing.list_accessories_and_characteristics(), timeout=15.0
            )
            pairing = temp_pairing

        rows = []
        try:
            for acc in pairing.accessories:
                for srv in acc.services:
                    stype = getattr(srv, "type", "")
                    for ch in srv.characteristics:
                        perms = getattr(ch, "perms", []) or []
                        rows.append({
                            "aid": acc.aid,
                            "iid": ch.iid,
                            "service": str(stype),
                            "type": str(ch.type),
                            "description": getattr(ch, "description", "") or "",
                            "value": getattr(ch, "value", None),
                            "readable": "pr" in perms or "r" in perms,
                            "writable": "pw" in perms or "w" in perms,
                            "notifies": "ev" in perms,
                            "unit": getattr(ch, "unit", None),
                            "min": getattr(ch, "minValue", None),
                            "max": getattr(ch, "maxValue", None),
                            "step": getattr(ch, "minStep", None),
                        })
        finally:
            if temp_pairing is not None:
                try:
                    await temp_pairing.close()
                except Exception:
                    pass
        return rows

    async def _health_loop(self):
        while True:
            for label in self.labels:
                try:
                    await self._refresh(label)
                except Exception as e:
                    print(f"[{label}] health loop error: {e}")
            await asyncio.sleep(self.poll_interval)


# ---------------------------------------------------------------------------
# tiny demo / smoke test when run directly
# ---------------------------------------------------------------------------
def _print_dump(rows, truncate=True):
    """Pretty-print a characteristic inventory as an aligned table."""
    # tolerate an error dict (e.g. {"ok": False, "error": ...}) or empty result
    if isinstance(rows, dict):
        print("  " + rows.get("error", str(rows)))
        return
    if not rows:
        print("  (no characteristics returned)")
        return
    if isinstance(rows[0], dict) and "error" in rows[0] and len(rows[0]) == 1:
        print("  " + rows[0]["error"])
        return
    # column order and headers
    cols = [
        ("aid", "AID"), ("iid", "IID"), ("description", "DESCRIPTION"),
        ("type", "TYPE (UUID)"), ("value", "VALUE"),
        ("readable", "R"), ("writable", "W"), ("notifies", "N"),
        ("unit", "UNIT"), ("min", "MIN"), ("max", "MAX"), ("step", "STEP"),
    ]

    def cell(v):
        if v is True:
            return "y"
        if v is False:
            return "-"
        if v is None:
            return ""
        s = str(v)
        if truncate and len(s) > 40:
            return s[:37] + "..."
        return s

    widths = {k: len(h) for k, h in cols}
    for r in rows:
        for k, _ in cols:
            widths[k] = max(widths[k], len(cell(r.get(k))))
    header = "  ".join(h.ljust(widths[k]) for k, h in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(cell(r.get(k)).ljust(widths[k]) for k, _ in cols))


def launch_gui(ec, poll_ms=2000):
    """
    Open a simple Tkinter control window: one card per thermostat with current
    temperature, an adjustable setpoint, mode and fan controls, and any remote
    sensors. Auto-refreshes so physical changes at the thermostat show up too.

    tkinter is imported here (not at module top) so importing this module as a
    library never loads GUI code. Raises RuntimeError if no display is available
    so the caller can fall back to headless mode.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"tkinter unavailable: {e}")

    try:
        root = tk.Tk()
    except Exception as e:
        # e.g. TclError: no display name — headless machine
        raise RuntimeError(f"no display available: {e}")

    root.title("Ecobee Hub")
    root.configure(bg="#f4f4f5")

    MODE_NAMES = {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto", "--": "--"}
    MODE_COLORS = {0: "#9e9e9e", 1: "#e53935", 2: "#1e88e5", 3: "#7e57c2"}

    widgets = {}  # per-label dict of the tk widgets we update on each poll

    # hub display unit ("F" or "C"). The controller always stores temps in F;
    # this only affects what the window shows. Default to F.
    ui = {"units": "F"}

    def fmt_temp(f_val, c_val=None):
        """
        Format a temperature for display. In F mode: show the rounded
        Fahrenheit int. In C mode: show the device's own raw Celsius value
        (which is what the ecobee actually reports over HomeKit), with one
        decimal only when it isn't a whole number, e.g. 25 or 25.5. This avoids
        double-rounding through Fahrenheit.
        """
        if ui["units"] == "C":
            if not isinstance(c_val, (int, float)):
                # fall back to converting F if raw C wasn't provided
                if not isinstance(f_val, (int, float)):
                    return "--\u00b0"
                c_val = (f_val - 32) * 5 / 9
            # show a decimal only if meaningful (25.0 -> "25", 25.5 -> "25.5")
            rounded = round(c_val * 2) / 2  # snap to HomeKit's 0.5 steps
            if rounded == int(rounded):
                return f"{int(rounded)}\u00b0"
            return f"{rounded:.1f}\u00b0"
        if not isinstance(f_val, (int, float)):
            return "--\u00b0"
        return f"{int(f_val)}\u00b0"

    def to_c_half(entered_c):
        """Snap a typed Celsius value to HomeKit's 0.5 step."""
        return round(float(entered_c) * 2) / 2

    def to_f(entered):
        """Convert a number the user typed (in the current unit) to Fahrenheit."""
        if ui["units"] == "C":
            return int(round(float(entered) * 9 / 5 + 32))
        return int(round(float(entered)))

    def unit_label():
        return "\u00b0C" if ui["units"] == "C" else "\u00b0F"

    def run_bg(fn):
        """Run a blocking controller call off the UI thread."""
        threading.Thread(target=fn, daemon=True).start()

    def build_card(parent, label):
        name = ec.get_name(label)
        card = tk.Frame(parent, bg="white", bd=0, highlightthickness=1,
                        highlightbackground="#e5e5ea", width=260)
        card.pack(side="left", fill="y", padx=12, pady=12, ipadx=10, ipady=8)
        card.pack_propagate(False)  # keep a consistent column width

        tk.Label(card, text=name, bg="white", fg="#111",
                 font=("Segoe UI Semibold", 15)).pack(pady=(12, 0))

        cur = tk.Label(card, text="--\u00b0", bg="white", fg="#111",
                       font=("Segoe UI", 40, "bold"))
        cur.pack()

        sub = tk.Label(card, text="", bg="white", fg="#666",
                       font=("Segoe UI", 10))
        sub.pack()

        # target row (read-only label; editing happens in a popup)
        trow = tk.Frame(card, bg="white")
        trow.pack(pady=8)
        tk.Label(trow, text="Target", bg="white", fg="#888",
                 font=("Segoe UI", 10)).pack()
        tgt = tk.Label(trow, text="--", bg="white", fg="#111",
                       font=("Segoe UI", 30, "bold"))
        tgt.pack()

        def open_temp_popup():
            st = ec.get_status(label)
            is_range = st.get("setpoint_kind") == "range"

            pop = tk.Toplevel(root)
            pop.title("Change temperature")
            pop.configure(bg="white")
            pop.resizable(False, False)
            pop.transient(root)
            pop.grab_set()

            tk.Label(pop, text=f"{ec.get_name(label)}", bg="white", fg="#111",
                     font=("Segoe UI Semibold", 13)).pack(padx=28, pady=(18, 2))

            err = tk.Label(pop, text="", bg="white", fg="#dc2626",
                           font=("Segoe UI", 9))

            if is_range:
                # AUTO mode: two setpoints, heat-to and cool-to
                tk.Label(pop, text=f"Auto mode: set the heat/cool range ({unit_label()})",
                         bg="white", fg="#666", font=("Segoe UI", 10)).pack(padx=28)
                dt = st.get("display_target") or {}
                fields = tk.Frame(pop, bg="white")
                fields.pack(padx=28, pady=10)

                tk.Label(fields, text="Heat to", bg="white", fg="#e53935",
                         font=("Segoe UI", 10)).grid(row=0, column=0, padx=8)
                tk.Label(fields, text="Cool to", bg="white", fg="#1e88e5",
                         font=("Segoe UI", 10)).grid(row=0, column=1, padx=8)

                heat_e = tk.Entry(fields, font=("Segoe UI", 22), justify="center", width=5)
                heat_e.grid(row=1, column=0, padx=8)
                cool_e = tk.Entry(fields, font=("Segoe UI", 22), justify="center", width=5)
                cool_e.grid(row=1, column=1, padx=8)
                h = st.get("heat_threshold"); hc = st.get("heat_threshold_c")
                c = st.get("cool_threshold"); cc = st.get("cool_threshold_c")
                # prefill in the displayed unit
                if isinstance(h, int):
                    heat_e.insert(0, fmt_temp(h, hc).rstrip("\u00b0"))
                if isinstance(c, int):
                    cool_e.insert(0, fmt_temp(c, cc).rstrip("\u00b0"))
                heat_e.focus_set()
                err.pack()

                def submit(*_):
                    try:
                        hv = to_f(float(heat_e.get().strip()))
                        cv = to_f(float(cool_e.get().strip()))
                    except ValueError:
                        err.config(text="Enter numbers for both.")
                        return
                    if hv >= cv:
                        err.config(text="Heat must be lower than cool.")
                        return
                    pop.destroy()
                    status.config(text="\u25cf sending\u2026", fg="#d97706")

                    def work():
                        r1 = ec.set_heat_threshold(label, hv)
                        r2 = ec.set_cool_threshold(label, cv)
                        ok = r1.get("ok") and r2.get("ok")
                        errmsg = r1.get("error") or r2.get("error")
                        root.after(0, lambda: status.config(
                            text="\u25cf updated" if ok else f"\u25cf {errmsg}",
                            fg="#16a34a" if ok else "#dc2626"))
                        root.after(800, lambda: refresh_one(label))
                    run_bg(work)
            else:
                # HEAT / COOL / OFF: single target
                tk.Label(pop, text=f"New target temperature ({unit_label()})",
                         bg="white", fg="#666", font=("Segoe UI", 10)).pack(padx=28)
                current = st.get("target_temp")
                current_c = st.get("target_temp_c")
                entry = tk.Entry(pop, font=("Segoe UI", 24), justify="center", width=6)
                entry.pack(padx=28, pady=10)
                if isinstance(current, int):
                    entry.insert(0, fmt_temp(current, current_c).rstrip("\u00b0"))
                entry.select_range(0, "end")
                entry.focus_set()
                err.pack()

                def submit(*_):
                    try:
                        val = to_f(float(entry.get().strip()))
                    except ValueError:
                        err.config(text="Enter a number.")
                        return
                    pop.destroy()
                    status.config(text="\u25cf sending\u2026", fg="#d97706")

                    def work():
                        res = ec.set_temp(label, val)
                        ok = res.get("ok")
                        root.after(0, lambda: status.config(
                            text="\u25cf updated" if ok else f"\u25cf {res.get('error')}",
                            fg="#16a34a" if ok else "#dc2626"))
                        root.after(800, lambda: refresh_one(label))
                    run_bg(work)

            btns = tk.Frame(pop, bg="white")
            btns.pack(pady=(4, 18))
            tk.Button(btns, text="Cancel", relief="flat", bg="#eee", fg="#111",
                      width=8, command=pop.destroy).pack(side="left", padx=6)
            tk.Button(btns, text="Set", relief="flat", bg="#22c55e", fg="white",
                      activebackground="#16a34a", activeforeground="white",
                      width=8, command=submit).pack(side="left", padx=6)
            pop.bind("<Return>", submit)
            pop.bind("<Escape>", lambda e: pop.destroy())

        change_btn = tk.Button(card, text="change temp", relief="flat",
                               bg="#22c55e", fg="white",
                               activebackground="#16a34a", activeforeground="white",
                               font=("Segoe UI Semibold", 11),
                               command=open_temp_popup)
        change_btn.pack(pady=(0, 8))

        # mode buttons
        mrow = tk.Frame(card, bg="white")
        mrow.pack(pady=(2, 6))
        mode_btns = {}
        for mval, mname in [(1, "Heat"), (2, "Cool"), (3, "Auto"), (0, "Off")]:
            b = tk.Button(mrow, text=mname, width=6, relief="flat",
                          bg="#eee", fg="#111", font=("Segoe UI", 10),
                          command=lambda mv=mval: set_mode(mv))
            b.pack(side="left", padx=3)
            mode_btns[mval] = b

        def set_mode(mv):
            status.config(text=f"\u25cf {MODE_NAMES[mv].lower()}\u2026", fg="#d97706")

            def work():
                res = ec.set_mode(label, mv)
                ok = res.get("ok")
                root.after(0, lambda: status.config(
                    text="\u25cf updated" if ok else f"\u25cf {res.get('error')}",
                    fg="#16a34a" if ok else "#dc2626"))
                root.after(800, lambda: refresh_one(label))
            run_bg(work)

        # fan buttons
        frow = tk.Frame(card, bg="white")
        frow.pack(pady=(0, 6))
        tk.Label(frow, text="Fan", bg="white", fg="#888",
                 font=("Segoe UI", 10)).pack(side="left", padx=(0, 6))
        fan_btns = {}
        for fname, fauto in [("Auto", True), ("On", False)]:
            b = tk.Button(frow, text=fname, width=5, relief="flat", bg="#eee",
                          fg="#111", font=("Segoe UI", 10),
                          command=lambda fa=fauto: set_fan(fa))
            b.pack(side="left", padx=3)
            fan_btns[1 if fauto else 0] = b  # key by target_fan value (1=auto,0=on)

        def set_fan(fauto):
            status.config(text="\u25cf fan\u2026", fg="#d97706")

            def work():
                res = ec.set_fan(label, fauto)
                ok = res.get("ok")
                root.after(0, lambda: status.config(
                    text="\u25cf updated" if ok else f"\u25cf {res.get('error')}",
                    fg="#16a34a" if ok else "#dc2626"))
                root.after(800, lambda: refresh_one(label))
            run_bg(work)

        # optional: target humidity, only on models that expose it
        st0 = ec.get_status(label)
        if st0.get("target_humidity") != "--":
            hrow = tk.Frame(card, bg="white")
            hrow.pack(pady=(0, 6))
            tk.Label(hrow, text="Humidity", bg="white", fg="#888",
                     font=("Segoe UI", 10)).pack(side="left", padx=(0, 6))
            hum_val = tk.Label(hrow, text="--", bg="white", fg="#111",
                               font=("Segoe UI Semibold", 10))
            hum_val.pack(side="left", padx=(0, 6))

            def open_hum_popup():
                cur_h = ec.get_status(label).get("target_humidity")
                pop = tk.Toplevel(root)
                pop.title("Target humidity")
                pop.configure(bg="white")
                pop.resizable(False, False)
                pop.transient(root); pop.grab_set()
                tk.Label(pop, text="Target humidity (%)", bg="white", fg="#666",
                         font=("Segoe UI", 10)).pack(padx=28, pady=(18, 4))
                e = tk.Entry(pop, font=("Segoe UI", 22), justify="center", width=5)
                e.pack(padx=28, pady=8)
                if isinstance(cur_h, int):
                    e.insert(0, str(cur_h))
                e.focus_set()
                herr = tk.Label(pop, text="", bg="white", fg="#dc2626",
                                font=("Segoe UI", 9))
                herr.pack()

                def hsub(*_):
                    try:
                        v = int(round(float(e.get().strip())))
                    except ValueError:
                        herr.config(text="Enter a number."); return
                    pop.destroy()
                    status.config(text="\u25cf sending\u2026", fg="#d97706")

                    def work():
                        res = ec.set_target_humidity(label, v)
                        ok = res.get("ok")
                        root.after(0, lambda: status.config(
                            text="\u25cf updated" if ok else f"\u25cf {res.get('error')}",
                            fg="#16a34a" if ok else "#dc2626"))
                        root.after(800, lambda: refresh_one(label))
                    run_bg(work)

                bf = tk.Frame(pop, bg="white"); bf.pack(pady=(4, 18))
                tk.Button(bf, text="Cancel", relief="flat", bg="#eee", fg="#111",
                          width=8, command=pop.destroy).pack(side="left", padx=6)
                tk.Button(bf, text="Set", relief="flat", bg="#22c55e", fg="white",
                          width=8, command=hsub).pack(side="left", padx=6)
                pop.bind("<Return>", hsub)
                pop.bind("<Escape>", lambda e: pop.destroy())

            tk.Button(hrow, text="set", relief="flat", bg="#eee", fg="#111",
                      font=("Segoe UI", 9), command=open_hum_popup).pack(side="left")
        else:
            hum_val = None

        # sensors area
        sensors_lbl = tk.Label(card, text="", bg="white", fg="#444",
                               font=("Segoe UI", 9), justify="center")
        sensors_lbl.pack(pady=(2, 4))

        status = tk.Label(card, text="\u25cf loading\u2026", bg="white",
                          fg="#777", font=("Segoe UI", 10))
        status.pack(pady=(0, 8))

        widgets[label] = {
            "cur": cur, "sub": sub, "tgt": tgt, "hum_val": hum_val,
            "mode_btns": mode_btns, "fan_btns": fan_btns,
            "sensors": sensors_lbl, "status": status,
        }

    def paint(label, st):
        w = widgets[label]
        cur = st.get("current_temp")
        cur_c = st.get("current_temp_c")
        w["cur"].config(text=fmt_temp(cur, cur_c) if cur not in (None, "--") else "--\u00b0")

        mode = st.get("mode")
        online = st.get("online")
        hum = st.get("humidity")
        subbits = []
        subbits.append(MODE_NAMES.get(mode, "--"))
        if hum not in (None, "--"):
            subbits.append(f"{hum}% RH")
        # current_fan: 0 inactive, 1 idle, 2 blowing -> only note when blowing
        if st.get("current_fan") == 2:
            subbits.append("fan on")
        if not online:
            subbits.append("offline")
        w["sub"].config(text="   ".join(subbits))

        # target: single number, or a heat-cool range in auto mode.
        # This is now a read-only label; editing happens in the popup.
        if st.get("setpoint_kind") == "range":
            h, hc = st.get("heat_threshold"), st.get("heat_threshold_c")
            c, cc = st.get("cool_threshold"), st.get("cool_threshold_c")
            ht = fmt_temp(h, hc).rstrip("\u00b0") if isinstance(h, int) else "--"
            ct = fmt_temp(c, cc) if isinstance(c, int) else "--\u00b0"
            w["tgt"].config(text=f"{ht}\u2013{ct}")
        else:
            tt = st.get("target_temp")
            ttc = st.get("target_temp_c")
            w["tgt"].config(text=fmt_temp(tt, ttc) if isinstance(tt, int) else "--")

        # recolor mode buttons
        for mv, b in w["mode_btns"].items():
            if mv == mode:
                b.config(bg=MODE_COLORS.get(mv, "#555"), fg="white")
            else:
                b.config(bg="#eee", fg="#111")

        # recolor fan buttons to show the current fan setting
        tf = st.get("target_fan")  # 1 = auto, 0 = on/continuous
        for fv, b in w["fan_btns"].items():
            if tf != "--" and fv == tf:
                b.config(bg="#555555", fg="white")
            else:
                b.config(bg="#eee", fg="#111")

        # target humidity value (only present on supporting models)
        if w.get("hum_val") is not None:
            th = st.get("target_humidity")
            w["hum_val"].config(text=f"{th}%" if isinstance(th, int) else "--")

        # sensors
        lines = []
        for s in st.get("sensors", []):
            t = (fmt_temp(s["temperature"], s.get("temperature_c"))
                 if s.get("temperature") is not None else "--")
            occ = ""
            if s.get("occupancy") is not None:
                occ = " \u25cf occupied" if s["occupancy"] else " \u25cb empty"
            batt = f"  {s['battery']}%" if s.get("battery") is not None else ""
            low = " (low batt)" if s.get("low_battery") else ""
            lines.append(f"{s['name']}: {t}{occ}{batt}{low}")
        w["sensors"].config(text="\n".join(lines))

        if widgets[label]["status"].cget("text") == "\u25cf loading\u2026":
            widgets[label]["status"].config(text="\u25cf live", fg="#16a34a")

    def refresh_one(label):
        # One-shot confirming read after the USER makes a change, so the card
        # reflects what actually landed. This is a single read, not a loop --
        # routine freshness comes from the push subscription, not from here.
        def work():
            try:
                ec.refresh(label)
            except Exception:
                pass
            st = ec.get_status(label)
            root.after(0, lambda: paint(label, st))
        run_bg(work)

    def poll():
        # Repaint from cache on a light timer. The cache is kept current by the
        # controller's HomeKit push subscription (the device notifies us on
        # change) plus its slow background health poll. We do NOT hit the device
        # on this timer -- frequent forced reads make HomeKit drop the
        # connection. This is display-only.
        for label in ec.labels:
            paint(label, ec.get_status(label))
        root.after(poll_ms, poll)

    def open_settings():
        pop = tk.Toplevel(root)
        pop.title("Settings")
        pop.configure(bg="white")
        pop.resizable(False, False)
        pop.transient(root)
        pop.grab_set()

        tk.Label(pop, text="Settings", bg="white", fg="#111",
                 font=("Segoe UI Semibold", 14)).pack(padx=32, pady=(18, 2))
        tk.Label(pop, text="Temperature units", bg="white", fg="#666",
                 font=("Segoe UI", 10)).pack(padx=32, pady=(4, 8))

        note = tk.Label(pop, text="", bg="white", fg="#777", font=("Segoe UI", 9))

        row = tk.Frame(pop, bg="white")
        row.pack(padx=32)
        fbtn = tk.Button(row, text="\u00b0F", width=6, relief="flat")
        cbtn = tk.Button(row, text="\u00b0C", width=6, relief="flat")
        fbtn.pack(side="left", padx=4)
        cbtn.pack(side="left", padx=4)

        def highlight():
            f = ui["units"] == "F"
            fbtn.config(bg="#555555" if f else "#eee", fg="white" if f else "#111")
            cbtn.config(bg="#555555" if not f else "#eee", fg="white" if not f else "#111")

        def set_units(unit):
            ui["units"] = unit
            highlight()
            # repaint immediately so the hub reflects the new unit at once
            for lbl in ec.labels:
                paint(lbl, ec.get_status(lbl))
            # also push the setting to every thermostat's own display
            note.config(text="updating thermostats\u2026", fg="#d97706")

            def work():
                results = [ec.set_display_units(lbl, unit == "F") for lbl in ec.labels]
                ok = all(r.get("ok") for r in results)
                root.after(0, lambda: note.config(
                    text="all set" if ok else "hub updated (some devices declined)",
                    fg="#16a34a" if ok else "#d97706"))
            run_bg(work)

        fbtn.config(command=lambda: set_units("F"))
        cbtn.config(command=lambda: set_units("C"))
        highlight()

        tk.Label(pop, text="Applies to the hub display and pushes to all\n"
                          "thermostats' on-device screens.",
                 bg="white", fg="#999", font=("Segoe UI", 8),
                 justify="center").pack(padx=32, pady=(10, 0))

        note.pack(pady=(8, 0))
        tk.Button(pop, text="Close", relief="flat", bg="#eee", fg="#111",
                  width=10, command=pop.destroy).pack(pady=16)
        pop.bind("<Escape>", lambda e: pop.destroy())

    # build UI
    headbar = tk.Frame(root, bg="#f4f4f5")
    headbar.pack(fill="x", pady=(12, 4))
    tk.Label(headbar, text="Ecobee Hub", bg="#f4f4f5", fg="#111",
             font=("Segoe UI", 18, "bold")).pack(side="left", padx=(18, 0))
    tk.Button(headbar, text="\u2699 Settings", relief="flat", bg="#e5e5ea",
              fg="#111", font=("Segoe UI", 10), command=open_settings).pack(
              side="right", padx=(0, 16))

    container = tk.Frame(root, bg="#f4f4f5")
    container.pack(fill="both", expand=True, padx=6, pady=(0, 8))
    for label in ec.labels:
        build_card(container, label)

    # size the window: width scales with number of cards, fixed comfortable height
    n = max(1, len(ec.labels))
    win_w = min(n * 284 + 20, 1400)   # ~284px per card column, capped
    win_h = 500
    root.geometry(f"{win_w}x{win_h}")
    root.minsize(300, 460)

    root.after(200, poll)
    root.mainloop()


if __name__ == "__main__":
    import argparse
    import time

    parser = argparse.ArgumentParser(
        description="Local Ecobee HomeKit controller / inspector"
    )
    parser.add_argument(
        "--dump", action="store_true",
        help="print every characteristic each thermostat exposes, then exit",
    )
    parser.add_argument(
        "--label", default=None,
        help="only act on this label (default: all discovered)",
    )
    parser.add_argument(
        "--set-temp", type=int, default=None, metavar="F",
        help="set target temperature in Fahrenheit (requires --label)",
    )
    parser.add_argument(
        "--set-mode", type=int, default=None, metavar="0-3",
        help="set mode: 0=off 1=heat 2=cool 3=auto (requires --label)",
    )
    parser.add_argument(
        "--set-heat", type=int, default=None, metavar="F",
        help="set heating threshold in F for auto mode (requires --label)",
    )
    parser.add_argument(
        "--set-cool", type=int, default=None, metavar="F",
        help="set cooling threshold in F for auto mode (requires --label)",
    )
    parser.add_argument(
        "--set-fan", choices=["auto", "on"], default=None,
        help="set fan to auto or on/continuous (requires --label)",
    )
    parser.add_argument(
        "--set-humidity", type=int, default=None, metavar="PCT",
        help="set target humidity %% (Premium etc.; requires --label)",
    )
    parser.add_argument(
        "--raw", action="store_true",
        help="with --dump, do not truncate long values (e.g. status strings)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="print HomeKit connection and subscription events (noisy)",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="run as a background service (print status, stay alive) instead "
             "of opening the control window",
    )
    parser.add_argument(
        "--folder", default=None,
        help="folder holding pairing files (default: per-user ecobee-local folder)",
    )
    args = parser.parse_args()

    ec = EcobeeController.from_folder(app_folder=args.folder, debug=args.debug)
    if not ec.labels:
        print("No pairing files found. Run pair_ecobee.py first.")
        sys.exit(0)

    targets = [args.label] if args.label else list(ec.labels)

    if args.dump:
        # start the event loop WITHOUT opening persistent connections, so the
        # dump's own temp connection is the only one to each device
        ec.start(connect=False)
        for label in targets:
            print()
            print(f"=== {label} ({ec.get_name(label)}) ===")
            rows = ec.dump_characteristics(label)
            _print_dump(rows, truncate=not args.raw)
        sys.exit(0)

    # Did the user request a specific one-shot action?
    any_action = any(v is not None for v in (
        args.set_temp, args.set_mode, args.set_heat, args.set_cool,
        args.set_fan, args.set_humidity))

    # Default (no action flags, not --headless): open the control window.
    if not any_action and not args.headless:
        ec.start()
        print("opening control window (use --headless for no GUI)...")
        try:
            launch_gui(ec)
            sys.exit(0)
        except RuntimeError as e:
            print(f"GUI unavailable ({e}); falling back to headless mode.")
            # fall through to headless below

    ec.start()
    print("waiting for initial connections...")
    time.sleep(6)

    if args.label:
        if args.set_temp is not None:
            print("set-temp:", ec.set_temp(args.label, args.set_temp))
        if args.set_mode is not None:
            print("set-mode:", ec.set_mode(args.label, args.set_mode))
        if args.set_heat is not None:
            print("set-heat:", ec.set_heat_threshold(args.label, args.set_heat))
        if args.set_cool is not None:
            print("set-cool:", ec.set_cool_threshold(args.label, args.set_cool))
        if args.set_fan is not None:
            print("set-fan:", ec.set_fan(args.label, args.set_fan == "auto"))
        if args.set_humidity is not None:
            print("set-humidity:",
                  ec.set_target_humidity(args.label, args.set_humidity))

    for t in ec.list_thermostats():
        st = ec.get_status(t["label"])
        sensors = st.pop("sensors", [])
        print(t["label"], f"({t['name']})", st)
        for s in sensors:
            temp = f"{s['temperature']}F" if s["temperature"] is not None else "--"
            occ = "" if s["occupancy"] is None else (" occupied" if s["occupancy"] else " empty")
            batt = "" if s["battery"] is None else f" batt {s['battery']}%"
            print(f"    - {s['name']}: {temp}{occ}{batt}")

    # In headless mode with no action, keep running as a live service so the
    # cache stays fresh (useful as a backend for another app).
    if args.headless and not any_action:
        print("headless service running; Ctrl+C to stop.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
