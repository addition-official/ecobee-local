"""Parsing of what the thermostat sends back, and of pairing files."""

import json
import time

import pytest

import ecobee_local.controller as c
from ecobee_local import EcobeeController

from conftest import (
    BUILTIN_OCCUPANCY, COMFORT_READ, COMFORT_WRITE, COOL_THRESHOLD,
    CURRENT_FAN, CURRENT_TEMP, DISPLAY_UNITS, HEAT_THRESHOLD, HUMIDITY, MODE,
    RUNNING_STATE, SENSOR_BATTERY, SENSOR_LOW_BATTERY, SENSOR_NAME,
    SENSOR_OCCUPANCY, SENSOR_TEMP, TARGET_FAN, TARGET_TEMP, Accessory, Char,
    FakeDevice, Service, HAP, default_values, write_pair_file,
)


class _P:
    def __init__(self, accessories):
        self.accessories = accessories


def ids_for(**tree_kwargs):
    ec = EcobeeController()
    return ec._find_ids(_P(FakeDevice(**tree_kwargs).accessories))


def readings(values=None):
    values = values or default_values()
    return {cid: {"value": v} for cid, v in values.items()}


def controller_with(label="main"):
    ec = EcobeeController({label: "/nonexistent/pair.json"})
    return ec


def apply(ec, values=None, label="main", **tree_kwargs):
    main_ids, specs = ids_for(**tree_kwargs)
    ec._apply_reading(label, main_ids, specs, readings(values))
    return ec.cache[label]


# ------------------------------------------------------------- conversions

@pytest.mark.parametrize("celsius, fahrenheit", [
    (0, 32), (100, 212), (21.5, 71), (22.0, 72), (-40, -40), (20.3, 69),
])
def test_c_to_f(celsius, fahrenheit):
    assert c._c_to_f(celsius) == fahrenheit


@pytest.mark.parametrize("fahrenheit, celsius", [
    (72, 22.0), (68, 20.0), (70, 21.0), (71, 21.5), (32, 0.0), ("75", 24.0),
])
def test_f_to_c_half_snaps_to_half_degrees(fahrenheit, celsius):
    assert c._f_to_c_half(fahrenheit) == celsius


# --------------------------------------------------------- finding the ids

def test_find_ids_maps_every_thermostat_characteristic():
    main_ids, _ = ids_for()
    assert main_ids["current_temp"] == CURRENT_TEMP
    assert main_ids["target_temp"] == TARGET_TEMP      # short "35" form
    assert main_ids["mode"] == MODE
    assert main_ids["running_state"] == RUNNING_STATE
    assert main_ids["cool_threshold"] == COOL_THRESHOLD
    assert main_ids["heat_threshold"] == HEAT_THRESHOLD
    assert main_ids["humidity"] == HUMIDITY
    assert main_ids["display_units"] == DISPLAY_UNITS
    assert main_ids["target_fan"] == TARGET_FAN
    assert main_ids["current_fan"] == CURRENT_FAN
    assert main_ids["comfort_read"] == COMFORT_READ
    assert main_ids["comfort_write"] == COMFORT_WRITE


def test_find_ids_ignores_vendor_lookalikes():
    # a vendor characteristic shares Mode's first 8 hex digits, and a vendor
    # service before the thermostat holds a real TargetTemperature UUID
    main_ids, _ = ids_for()
    assert main_ids["mode"] == MODE
    assert main_ids["target_temp"] == TARGET_TEMP


def test_find_ids_marks_missing_features_as_none():
    main_ids, _ = ids_for(comfort=False)
    assert main_ids["target_humidity"] is None
    assert main_ids["comfort_read"] is None
    assert main_ids["comfort_write"] is None


def test_find_ids_falls_back_when_service_type_is_unexpected():
    # some firmwares don't label the thermostat service 0000004A
    acc = Accessory(7, [Service("FFFFFFFF", [
        Char("00000035" + HAP, 3),
        Char("00000011" + HAP, 4),
    ])])
    main_ids, _ = EcobeeController()._find_ids(_P([acc]))
    assert main_ids["target_temp"] == (7, 3)
    assert main_ids["current_temp"] == (7, 4)


def test_find_ids_groups_sensors():
    _, specs = ids_for()
    builtin = [s for s in specs if s["_builtin"]]
    remote = [s for s in specs if not s["_builtin"]]
    assert len(builtin) == 1 and len(remote) == 1
    assert builtin[0]["occupancy"] == BUILTIN_OCCUPANCY
    assert builtin[0]["temperature"] == CURRENT_TEMP   # thermostat's own
    assert "name" not in builtin[0]
    assert remote[0] == {
        "name": SENSOR_NAME, "temperature": SENSOR_TEMP,
        "occupancy": SENSOR_OCCUPANCY, "battery": SENSOR_BATTERY,
        "low_battery": SENSOR_LOW_BATTERY, "_builtin": False,
    }


def test_wanted_skips_write_only_comfort_and_dedupes():
    main_ids, specs = ids_for()
    want = EcobeeController()._wanted(main_ids, specs)
    assert COMFORT_WRITE not in want
    assert len(want) == len(set(want))
    assert CURRENT_TEMP in want and SENSOR_BATTERY in want


# -------------------------------------------------------- applying readings

def test_apply_reading_converts_and_stores_both_units():
    cache = apply(controller_with())
    assert cache["online"] is True
    assert cache["current_temp"] == 71 and cache["current_temp_c"] == 21.5
    assert cache["target_temp"] == 72 and cache["target_temp_c"] == 22.0
    assert cache["cool_threshold"] == 77
    assert cache["heat_threshold"] == 68
    assert cache["humidity"] == 45                   # 44.6 rounded
    assert cache["mode"] == 1
    assert cache["running_state"] == 1
    assert cache["target_fan"] == 1
    assert cache["current_fan"] == 2
    assert cache["display_units"] == 1
    assert cache["target_humidity"] == "--"          # not on this model


@pytest.mark.parametrize("raw, name", [
    (0, "home"), (1, "sleep"), (2, "away"), (3, "hold"), (7, "hold"),
])
def test_apply_reading_comfort_names(raw, name):
    values = default_values()
    values[COMFORT_READ] = raw
    cache = apply(controller_with(), values)
    assert cache["comfort_mode"] == name
    assert cache["comfort_raw"] == raw


def test_apply_reading_comfort_garbage_is_unknown():
    values = default_values()
    values[COMFORT_READ] = "not a number"
    cache = apply(controller_with(), values)
    assert cache["comfort_mode"] == "--"
    assert cache["comfort_raw"] is None


def test_apply_reading_comfort_unsupported_model():
    cache = apply(controller_with(), comfort=False)
    assert cache["comfort_mode"] == "--"


def test_apply_reading_keeps_recently_set_comfort(monkeypatch):
    ec = controller_with()
    ec.cache["main"]["comfort_mode"] = "away"
    ec._comfort_hold["main"] = time.monotonic() + 60
    cache = apply(ec)                     # device still says home (0)
    assert cache["comfort_mode"] == "away"
    ec._comfort_hold["main"] = time.monotonic() - 1
    cache = apply(ec)
    assert cache["comfort_mode"] == "home"


def test_apply_reading_sensors():
    sensors = apply(controller_with())["sensors"]
    builtin, remote = sensors
    assert builtin == {
        "builtin": True, "name": "Built-in", "temperature": 71,
        "temperature_c": 21.5, "occupancy": True, "motion": None,
        "battery": None, "low_battery": None,
    }
    assert remote == {
        "builtin": False, "name": "Bedroom", "temperature": 66,
        "temperature_c": 19.0, "occupancy": False, "motion": None,
        "battery": 87, "low_battery": False,
    }


def test_apply_reading_sensor_fallbacks():
    values = default_values()
    values[SENSOR_NAME] = ""             # unnamed -> "Sensor N"
    values[SENSOR_OCCUPANCY] = "Y"       # some firmwares send strings
    del values[SENSOR_TEMP]
    sensors = apply(controller_with(), values)["sensors"]
    assert sensors[1]["name"] == "Sensor 2"
    assert sensors[1]["occupancy"] is True
    assert sensors[1]["temperature"] is None


# ----------------------------------------------------------- get_status

def test_status_single_setpoint():
    ec = controller_with()
    apply(ec)
    st = ec.get_status("main")
    assert st["setpoint_kind"] == "single"
    assert st["display_target"] == 72
    assert st["target_temp"] == 72


def test_status_auto_mode_uses_range():
    ec = controller_with()
    values = default_values()
    values[MODE] = 3
    apply(ec, values)
    st = ec.get_status("main")
    assert st["setpoint_kind"] == "range"
    assert st["display_target"] == {"heat": 68, "cool": 77}
    assert st["target_temp"] is None
    # the cache itself is untouched
    assert ec.cache["main"]["target_temp"] == 72


def test_status_before_first_read_is_unknown():
    st = controller_with().get_status()["main"]
    assert st["online"] is False
    assert st["current_temp"] == "--"
    assert st["sensors"] == []


# --------------------------------------------------------- pairing files

def test_read_meta(tmp_path):
    p = write_pair_file(tmp_path, "up.json", label="up", name="Upstairs")
    assert c._read_meta(str(p)) == ("up", "Upstairs")


def test_read_meta_falls_back_to_filename(tmp_path):
    p = write_pair_file(tmp_path, "garage.json", label=None, name=None)
    assert c._read_meta(str(p)) == ("garage", "garage")
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    assert c._read_meta(str(bad)) == ("broken", "broken")
    assert c._read_meta(str(tmp_path / "missing.json")) == ("missing", "missing")


def test_from_folder_discovers_files(tmp_path):
    write_pair_file(tmp_path, "pair.json", label="main", name="Main Floor")
    write_pair_file(tmp_path, "pair1.json", label="up", name="Upstairs")
    write_pair_file(tmp_path, ".hidden.json", label="hidden")
    (tmp_path / "notes.txt").write_text("not a pairing")
    ec = EcobeeController.from_folder(app_folder=str(tmp_path))
    assert ec.labels == ["main", "up"]
    assert ec.get_name("up") == "Upstairs"
    assert ec.list_thermostats() == [
        {"label": "main", "name": "Main Floor", "online": False},
        {"label": "up", "name": "Upstairs", "online": False},
    ]


def test_from_folder_pattern_and_duplicate_labels(tmp_path):
    write_pair_file(tmp_path, "pair.json", label="main")
    write_pair_file(tmp_path, "pair1.json", label="main")
    write_pair_file(tmp_path, "other.json", label="other")
    ec = EcobeeController.from_folder(app_folder=str(tmp_path),
                                      pattern="pair*.json")
    assert ec.labels == ["main", "main_pair1"]


def test_explicit_mapping_and_name_overrides(tmp_path):
    write_pair_file(tmp_path, "a.json", name="From File")
    ec = EcobeeController({"x": "a.json"}, app_folder=str(tmp_path),
                          names={"x": "Override"})
    assert ec._pair_paths["x"] == str(tmp_path / "a.json")
    assert ec.get_name("x") == "Override"
    ec.rename("x", "Renamed")
    assert ec.get_name("x") == "Renamed"
    assert ec.get_name("unknown") == "unknown"


def test_add_thermostat(tmp_path):
    write_pair_file(tmp_path, "a.json", label="a", name="A")
    ec = EcobeeController(app_folder=str(tmp_path))
    assert ec.add_thermostat("a.json") == "a"
    assert ec.get_status("a")["online"] is False
    with pytest.raises(ValueError):
        ec.add_thermostat("a.json")
    ec._started = True
    with pytest.raises(RuntimeError):
        ec.add_thermostat("b.json", label="b")


def test_default_app_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(c.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert c.default_app_folder() == str(tmp_path / "ecobee-local")
    monkeypatch.setattr(c.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert c.default_app_folder() == str(tmp_path / "local" / "ecobee-local")


def test_from_folder_skips_json_that_is_not_a_pairing(tmp_path):
    # a project folder with pairing files next to the app's own JSON files
    write_pair_file(tmp_path, "pair.json", label="main")
    write_pair_file(tmp_path, "pair1.json", label="up")
    (tmp_path / "config.json").write_text(json.dumps({"theme": "dark"}))
    (tmp_path / "secrets.json").write_text(json.dumps({"api_key": "x"}))
    (tmp_path / "events.json").write_text(json.dumps([1, 2, 3]))
    (tmp_path / "empty.json").write_text("")
    (tmp_path / "broken.json").write_text("{not json")
    ec = EcobeeController.from_folder(app_folder=str(tmp_path))
    assert ec.labels == ["main", "up"]
