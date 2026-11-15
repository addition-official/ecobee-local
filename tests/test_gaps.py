"""Tests for gaps found by mutation testing in the second review round."""

import asyncio
import os
import stat
import sys
import threading
import time

import pytest
from aiohomekit.exceptions import AccessoryDisconnectedError, \
    AuthenticationError

import ecobee_local.controller as c
import ecobee_local.mqtt as m
import ecobee_local.pair as p
from ecobee_local import EcobeeController

from conftest import (
    COMFORT_READ, COOL_THRESHOLD, HEAT_THRESHOLD, HAP, MODE, SENSOR_NAME,
    TARGET_FAN, TARGET_TEMP, Accessory, Char, Service, build_accessories,
    wait_until, write_pair_file,
)


# ------------------------------------------- every --set-* reaches the device

@pytest.mark.parametrize("argv, writes", [
    (["--set-range", "68", "75"], [(*HEAT_THRESHOLD, 20.0),
                                   (*COOL_THRESHOLD, 24.0)]),
    (["--set-fan", "auto"], [(*TARGET_FAN, 1)]),
    (["--set-fan", "on"], [(*TARGET_FAN, 0)]),
    (["--set-heat", "66"], [(*HEAT_THRESHOLD, 19.0)]),
    (["--set-cool", "78"], [(*COOL_THRESHOLD, 25.5)]),
    (["--set-temp-c", "21"], [(*TARGET_TEMP, 21.0)]),
    (["--set-heat-c", "19.5"], [(*HEAT_THRESHOLD, 19.5)]),
    (["--set-cool-c", "26"], [(*COOL_THRESHOLD, 26.0)]),
])
def test_cli_set_options_write_the_right_values(device, pair_folder,
                                                monkeypatch, argv, writes):
    monkeypatch.setattr(sys, "argv", ["ecobee-hub", "--folder",
                                      str(pair_folder), "--label", "main",
                                      *argv])
    try:
        c.main()
    except SystemExit as e:
        assert e.code in (None, 0)
    for w in writes:
        assert w in device.writes


def test_cli_set_humidity_writes(device, pair_folder, monkeypatch):
    device.accessories = build_accessories(humidity_target=True)
    monkeypatch.setattr(sys, "argv", ["ecobee-hub", "--folder",
                                      str(pair_folder), "--label", "main",
                                      "--set-humidity", "45"])
    try:
        c.main()
    except SystemExit as e:
        assert e.code in (None, 0)
    assert (1, 22, 45) in device.writes


# ------------------------------------------------------------ push events

def test_push_during_a_refresh_is_not_lost(connected, device):
    gate = threading.Event()
    device.read_gate = gate
    cb = device.pairings[0].callback
    cb({"event": 1})                                 # refresh 1 starts...
    assert wait_until(lambda: device.read_waiting == 1)  # ...read old values
    device.values[TARGET_TEMP] = 23.0                # then it changes
    cb({"event": 2})                                 # and a push says so
    gate.set()
    assert wait_until(
        lambda: connected.get_status("main")["target_temp"] == 73, timeout=2)


def test_push_after_stop_began_does_nothing(connected, device):
    pairing = device.pairings[0]
    real_close = pairing.close

    async def close_and_push():
        pairing.callback({"event": "late"})
        await real_close()
    pairing.close = close_and_push
    reads = device.reads
    connected.stop()
    time.sleep(0.3)
    assert device.reads == reads
    assert len(device.pairings) == 1
    assert not connected._push_tasks


def test_stop_cancels_a_running_push_refresh(connected, device):
    device.read_gate = threading.Event()             # never released
    device.pairings[0].callback({"event": 1})
    assert wait_until(lambda: device.read_waiting == 1)
    t0 = time.monotonic()
    connected.stop()
    assert time.monotonic() - t0 < 2
    assert not connected._push_tasks


def test_push_works_after_restart_even_if_one_was_queued(connected, device):
    connected._push_pending.add("main")      # left over from before a stop
    connected.stop()
    connected.start()
    assert connected.wait_ready(timeout=5)
    reads = device.reads
    connected._state["main"]["pairing"].callback({"event": 1})
    assert wait_until(lambda: device.reads > reads)


# ------------------------------------------------------------ health loop

def test_health_loop_keeps_polling_after_any_error(device, pair_folder,
                                                   make_controller):
    ec = make_controller(pair_folder, poll_interval=0.02)
    calls = []

    async def flaky(label):
        calls.append(label)
        if len(calls) == 2:          # call 1 is the initial connect
            raise ValueError("odd")
    ec._refresh = flaky
    ec.start()
    assert wait_until(lambda: len(calls) >= 4)


# --------------------------------------------------------------- set_range

def test_set_range_ok_when_read_back_fails(connected, device):
    real_get = device.pairings[0].get_characteristics

    async def fails_after_write(ids):
        if device.writes:
            raise AccessoryDisconnectedError("dropped after the write")
        return await real_get(ids)
    device.pairings[0].get_characteristics = fails_after_write
    res = connected.set_range("main", 68, 75)
    assert res == {"ok": True, "heat_threshold": 68, "cool_threshold": 75}


# ---------------------------------------------------------- offline_reason

def test_offline_reason_follows_implicit_exception_chain():
    try:
        try:
            raise AuthenticationError("pair verify failed")
        except AuthenticationError:
            raise OSError("closed while handling it")
    except OSError as e:
        assert c._offline_reason(e) == "unpaired"


# ------------------------------------------------------------ comfort hold

def test_failed_comfort_read_during_hold_keeps_it(connected, device):
    assert connected.set_comfort_mode("main", "away")["ok"]
    device.read_errors = {COMFORT_READ: -70402}
    connected.refresh("main")
    device.read_errors = {}
    connected.refresh("main")            # lagging read-back of "home"
    assert connected.get_comfort_mode("main") == "away"


# ------------------------------------------------------- configured modes

def test_add_thermostat_reads_file_modes(tmp_path):
    write_pair_file(tmp_path, "up.json", label="up", _modes=["off", "heat"])
    ec = EcobeeController(app_folder=str(tmp_path))
    ec.add_thermostat("up.json")
    assert ec.get_status("up")["modes"] == ["off", "heat"]


def test_check_modes_normalises_names():
    assert c._check_modes([" Heat", "OFF "]) == ["off", "heat"]


# ---------------------------------------------------------------- sensors

def test_occupancy_only_remote_sensor_is_listed(device, pair_folder,
                                                make_controller):
    tree = build_accessories()
    tree.append(Accessory(3, [Service("00000086" + HAP, [
        Char("00000023" + HAP, 2), Char("00000071" + HAP, 3)])]))
    device.accessories = tree
    device.values[(3, 2)] = "Hallway"
    device.values[(3, 3)] = 1
    ec = make_controller(pair_folder)
    ec.start()
    assert ec.wait_ready(timeout=5)
    hall = [s for s in ec.get_status("main")["sensors"]
            if s["name"] == "Hallway"][0]
    assert hall["occupancy"] is True and hall["temperature"] is None


@pytest.mark.parametrize("raw, expected", [("n", False), ("Y", True)])
def test_text_occupancy(connected, device, raw, expected):
    from conftest import SENSOR_OCCUPANCY
    device.values[SENSOR_OCCUPANCY] = raw
    connected.refresh("main")
    assert connected.get_status("main")["sensors"][1]["occupancy"] is expected


# ------------------------------------------------------------ --watch text

def test_describe_changes_reports_removed_sensor_and_offline_reason():
    old = {"online": True, "offline_reason": None,
           "sensors": [{"name": "Den", "temperature": 70}]}
    new = {"online": False, "offline_reason": "unreachable", "sensors": []}
    changes = c._describe_changes(old, new)
    assert "sensor Den removed" in changes
    assert "offline_reason None -> unreachable" in changes


# ------------------------------------------------------------- input rules

def test_humidity_boundaries_and_rounding(device, pair_folder,
                                          make_controller):
    device.accessories = build_accessories(humidity_target=True)
    ec = make_controller(pair_folder)
    ec.start()
    assert ec.wait_ready(timeout=5)
    assert ec.set_target_humidity("main", 100) == {"ok": True}
    assert ec.set_target_humidity("main", 0) == {"ok": True}
    assert ec.set_target_humidity("main", 40.6) == {"ok": True}
    assert [w[2] for w in device.writes] == [100, 0, 41]


def test_set_temp_c_zero_is_sent(connected, device):
    assert connected.set_temp_c("main", 0) == {"ok": True}
    assert device.writes[-1] == (*TARGET_TEMP, 0.0)


def test_wait_ready_with_no_thermostats(tmp_path):
    ec = EcobeeController(app_folder=str(tmp_path))
    ec.start()
    try:
        assert ec.wait_ready(timeout=1) is False
    finally:
        ec.stop()


def test_wait_ready_without_connect_returns_at_once(device, pair_folder,
                                                   make_controller):
    ec = make_controller(pair_folder)
    ec.start(connect=False)
    t0 = time.monotonic()
    assert ec.wait_ready(timeout=5) is False
    assert time.monotonic() - t0 < 1


def test_third_duplicate_label_gets_its_own_name(tmp_path):
    write_pair_file(tmp_path, "a.json", label="x")
    write_pair_file(tmp_path, "b.json", label="x")
    write_pair_file(tmp_path, "x_b.json", label="x_b")
    ec = EcobeeController.from_folder(app_folder=str(tmp_path))
    assert len(set(ec.labels)) == 3


# ------------------------------------------------------------- pair wizard

def test_new_custom_folder_is_private(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX permissions")
    old = os.umask(0o022)
    try:
        folder = tmp_path / "new" / "creds"
        p.get_app_folder(str(folder))
    finally:
        os.umask(old)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_check_filename_rejects_backslash():
    assert p.check_filename("a\\b.json")[0] is None
