"""The ecobee-hub command, run against a fake thermostat."""

import subprocess
import sys
import time

import pytest

import ecobee_local.controller as c
from ecobee_local import EcobeeController

from conftest import (
    COMFORT_WRITE, COOL_THRESHOLD, HEAT_THRESHOLD, MODE, SENSOR_OCCUPANCY,
    TARGET_TEMP, wait_until, write_pair_file,
)

REAL_SLEEP = time.sleep


@pytest.fixture
def hub(monkeypatch, device, pair_folder):
    """
    Run ecobee-hub's main() with argv. The 6-second "waiting for initial
    connections" sleep waits for the fake connection instead, and the
    headless service loop's hour-long sleep ends the run with Ctrl+C.
    """
    made = []
    real_from_folder = EcobeeController.from_folder.__func__

    def from_folder(cls, *a, **k):
        ec = real_from_folder(cls, *a, **k)
        made.append(ec)
        return ec
    monkeypatch.setattr(EcobeeController, "from_folder",
                        classmethod(from_folder))

    def connect_finished():
        if device.connect_exc:
            return bool(device.pairings)
        return all(ec._state[label]["pairing"] for ec in made
                   for label in ec.labels)

    sleeps = []

    while_running = []      # functions to run while the service is up

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if seconds >= 3600:
            for fn in while_running:
                fn(made[0])
            raise KeyboardInterrupt
        end = time.monotonic() + 5
        while time.monotonic() < end and not connect_finished():
            REAL_SLEEP(0.01)
    monkeypatch.setattr(time, "sleep", fake_sleep)

    def no_gui(ec, poll_ms=2000):
        raise RuntimeError("no display available")
    monkeypatch.setattr(c, "launch_gui", no_gui)

    def run(*argv):
        monkeypatch.setattr(sys, "argv",
                            ["ecobee-hub", "--folder", str(pair_folder), *argv])
        try:
            c.main()
        except SystemExit as e:
            return e.code
        return None

    run.sleeps = sleeps
    run.while_running = while_running
    yield run
    for ec in made:
        ec.stop()


def test_no_pairing_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ecobee-hub", "--folder", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        c.main()
    assert exc.value.code == 0
    assert "No pairing files found" in capsys.readouterr().out


def test_one_shot_actions(hub, device, capsys):
    hub("--label", "main", "--set-temp", "70", "--set-mode", "2",
        "--set-comfort", "sleep")
    assert (*TARGET_TEMP, 21.0) in device.writes
    assert (*MODE, 2) in device.writes
    assert (*COMFORT_WRITE, 1) in device.writes
    out = capsys.readouterr().out
    assert "set-temp: {'ok': True}" in out
    assert "set-mode: {'ok': True}" in out
    assert "set-comfort: {'ok': True}" in out
    assert "main (Main Floor)" in out
    assert "- Bedroom: 66F empty batt 87%" in out
    assert "- Built-in: 71F occupied" in out


def test_one_shot_reports_device_errors(hub, device, capsys):
    hub("--label", "main", "--set-humidity", "40")
    assert "set-humidity: {'ok': False" in capsys.readouterr().out
    assert device.writes == []


def test_offline_thermostat_still_prints_status(hub, device, capsys):
    device.connect_exc = OSError("unreachable")
    hub("--label", "main", "--set-fan", "on")
    out = capsys.readouterr().out
    assert "set-fan: {'ok': False, 'error': 'not connected'}" in out
    assert "'online': False" in out


def test_headless_service_stops_cleanly(hub, device, capsys):
    hub("--headless")
    out = capsys.readouterr().out
    assert "headless service running" in out
    assert "stopping..." in out
    assert device.pairings[0].closed is True


def test_gui_unavailable_falls_back_to_service(hub, device, capsys):
    hub()
    out = capsys.readouterr().out
    assert "GUI unavailable (no display available)" in out
    assert "main (Main Floor)" in out
    # keeps running like --headless instead of exiting
    assert "headless service running" in out
    assert "stopping..." in out


def test_dump(hub, device, capsys):
    assert hub("--dump") == 0
    out = capsys.readouterr().out
    assert "=== main (Main Floor) ===" in out
    assert "TYPE (UUID)" in out
    assert "B7DDB9A3-54BB-4572-91D2-F1F5B0510F8C" in out
    assert len(device.pairings) == 1 and device.pairings[0].closed


def test_rejects_bad_arguments(hub, capsys):
    assert hub("--set-fan", "sideways") == 2
    assert "invalid choice" in capsys.readouterr().err


# ---------------------------------------------------------- _print_dump

ROW = {"aid": 1, "iid": 11, "description": "Target Temperature",
       "type": "00000035-0000-1000-8000-0026BB765291", "value": 22.0,
       "readable": True, "writable": False, "notifies": True,
       "unit": "celsius", "min": None, "max": 33.3, "step": 0.1}


def test_print_dump_table(capsys):
    c._print_dump([ROW, dict(ROW, iid=12, value="x" * 60)])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["AID", "IID", "DESCRIPTION", "TYPE", "(UUID)",
                                "VALUE", "R", "W", "N", "UNIT", "MIN", "MAX",
                                "STEP", "VALID"]
    assert set(lines[1]) == {"-"}
    assert lines[2].split()[:2] == ["1", "11"]
    assert " y  -  y " in lines[2]
    assert "x" * 37 + "..." in lines[3] and "x" * 38 not in lines[3]


def test_print_dump_raw_keeps_long_values(capsys):
    c._print_dump([dict(ROW, value="x" * 60)], truncate=False)
    assert "x" * 60 in capsys.readouterr().out


@pytest.mark.parametrize("rows, text", [
    ({"ok": False, "error": "controller not started"},
     "controller not started"),
    ([], "(no characteristics returned)"),
    ([{"error": "pairing file missing: /x"}], "pairing file missing: /x"),
])
def test_print_dump_errors(rows, text, capsys):
    c._print_dump(rows)
    assert capsys.readouterr().out.strip() == text


# ------------------------------------------------- the real entry points

@pytest.mark.parametrize("module, text", [
    ("ecobee_local.controller", "--set-comfort"),
    ("ecobee_local.pair", "--folder"),
    ("ecobee_local.mqtt", "--host"),
])
def test_help_starts(module, text):
    out = subprocess.run([sys.executable, "-m", module, "--help"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert text in out.stdout


# ------------------------------------------------------ bad --label usage

@pytest.mark.parametrize("extra", [
    ["--set-temp", "70"],
    ["--dump"],
    ["--headless"],
])
def test_unknown_label_is_a_clear_error(hub, device, capsys, extra):
    assert hub("--label", "mian", *extra) == 2
    err = capsys.readouterr().err
    assert "no thermostat labelled 'mian'" in err
    assert "main" in err                   # lists the ones that exist
    assert "usage:" not in err             # just the error, no wall of text
    assert device.writes == []


@pytest.mark.parametrize("flag, value", [
    ("--set-temp", "70"), ("--set-mode", "1"), ("--set-fan", "auto"),
    ("--set-comfort", "home"), ("--set-heat", "68"), ("--set-cool", "76"),
    ("--set-humidity", "40"),
])
def test_action_without_label_is_an_error(hub, device, capsys, flag, value):
    assert hub(flag, value) == 2
    err = capsys.readouterr().err
    assert f"{flag} needs --label" in err
    assert "usage:" not in err
    assert device.writes == []


def test_commands_do_not_sleep_a_fixed_time(hub, device):
    # used to always wait 6 seconds for connections; now it waits only
    # until they are made
    hub("--label", "main", "--set-temp", "70")
    assert hub.sleeps == []
    assert (*TARGET_TEMP, 21.0) in device.writes


# ---------------------------------------------------------------- --status

def test_status_prints_and_exits(hub, device, capsys):
    assert hub("--status") in (None, 0)
    out = capsys.readouterr().out
    assert "main (Main Floor) {'online': True" in out
    assert "- Bedroom: 66F empty batt 87%" in out
    assert "headless service running" not in out
    assert "control window" not in out
    assert device.writes == []
    assert device.pairings[0].closed          # connections closed on exit


def test_status_for_one_label(hub, device, pair_folder, capsys):
    write_pair_file(pair_folder, "pair1.json", label="up", name="Upstairs")
    hub("--status", "--label", "up")
    out = capsys.readouterr().out
    assert "up (Upstairs)" in out
    assert "main (Main Floor)" not in out


def test_one_shot_action_closes_connections(hub, device):
    hub("--label", "main", "--set-temp", "70")
    assert all(p.closed for p in device.pairings)


# ------------------------------------------------------ --set-mode by name

@pytest.mark.parametrize("value, mode", [
    ("off", 0), ("heat", 1), ("COOL", 2), ("Auto", 3),
    ("0", 0), ("1", 1), ("3", 3),             # numbers still work
])
def test_set_mode_by_name_or_number(hub, device, value, mode):
    hub("--label", "main", "--set-mode", value)
    assert (*MODE, mode) in device.writes


@pytest.mark.parametrize("value", ["warm", "4", "-1", "1.5", ""])
def test_set_mode_rejects_others(hub, device, capsys, value):
    assert hub("--label", "main", "--set-mode", value) == 2
    assert "off, heat, cool, auto or 0-3" in capsys.readouterr().err
    assert device.writes == []


# ------------------------------------------------------- Celsius options

@pytest.mark.parametrize("flag, value, cid, written", [
    ("--set-temp-c", "21.5", TARGET_TEMP, 21.5),
    ("--set-temp-c", "21.3", TARGET_TEMP, 21.5),      # snapped to 0.5
    ("--set-heat-c", "19", HEAT_THRESHOLD, 19.0),
    ("--set-cool-c", "24.5", COOL_THRESHOLD, 24.5),
])
def test_celsius_options(hub, device, capsys, flag, value, cid, written):
    hub("--label", "main", flag, value)
    assert (*cid, written) in device.writes
    assert f"{flag[2:]}: {{'ok': True}}" in capsys.readouterr().out


def test_celsius_option_needs_label(hub, device, capsys):
    assert hub("--set-temp-c", "21") == 2
    assert "--set-temp-c needs --label" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["warm", "nan", "inf"])
def test_celsius_option_rejects_non_numbers(hub, device, capsys, value):
    assert hub("--label", "main", "--set-temp-c", value) == 2
    assert device.writes == []


# ----------------------------------------------------------------- --watch

def change_target(device, celsius, label="main"):
    """While --watch runs: the thermostat reports a new target."""
    def fn(ec):
        device.values[TARGET_TEMP] = celsius
        ec._state[label]["pairing"].callback(
            {TARGET_TEMP: {"value": celsius}})
        assert wait_until(
            lambda: ec._last_seen[label]["target_temp_c"] == celsius)
    return fn


def test_watch_prints_changes(hub, device, capsys):
    hub.while_running.append(change_target(device, 23.0))
    hub("--watch")
    out = capsys.readouterr().out
    assert "main (Main Floor) {'online': True" in out      # initial status
    assert "watching for changes; Ctrl+C to stop." in out
    line = [l for l in out.splitlines() if "target_temp 72 -> 73" in l]
    assert line and "main (Main Floor):" in line[0]
    assert "stopping..." in out
    assert device.pairings[0].closed


def test_watch_prints_sensor_and_offline_changes(hub, device, capsys):
    def fn(ec):
        device.values[SENSOR_OCCUPANCY] = 1
        ec.refresh("main")
        device.read_exc = OSError("gone")
        device.connect_exc = OSError("gone")
        ec.refresh("main")
    hub.while_running.append(fn)
    hub("--watch")
    out = capsys.readouterr().out
    assert "Bedroom occupancy False -> True" in out
    assert "online True -> False" in out          # went offline


def test_watch_does_not_report_its_own_shutdown(hub, device, capsys):
    hub("--watch")
    out = capsys.readouterr().out
    assert "->" not in out
    assert device.pairings[0].closed


def test_watch_one_label(hub, device, pair_folder, capsys):
    write_pair_file(pair_folder, "pair1.json", label="up", name="Upstairs")
    hub.while_running.append(change_target(device, 23.0, label="up"))
    hub("--watch", "--label", "up")
    out = capsys.readouterr().out
    assert "main (Main Floor)" not in out
    assert "up (Upstairs):" in out and "target_temp 72 -> 73" in out


def test_watch_ignores_celsius_only_wobble(hub, device, capsys):
    # 22.0C -> 22.1C is still 72F: not worth a line
    hub.while_running.append(change_target(device, 22.1))
    hub("--watch")
    out = capsys.readouterr().out
    assert "->" not in out


# -------------------------------------------------------------- exit codes

def test_failed_action_exits_1(hub, device, capsys):
    assert hub("--label", "main", "--set-humidity", "40") == 1
    assert hub("--label", "main", "--set-temp", "70") in (None, 0)


def test_offline_action_exits_1(hub, device):
    device.connect_exc = OSError("unreachable")
    assert hub("--label", "main", "--set-temp", "70") == 1


def test_dump_unreachable_prints_error_and_exits_1(hub, device,
                                                  pair_folder, capsys):
    write_pair_file(pair_folder, "pair1.json", label="up", name="Upstairs")
    device.connect_exc = OSError("unreachable")
    assert hub("--dump") == 1
    out = capsys.readouterr().out
    assert "=== main (Main Floor) ===" in out
    assert "=== up (Upstairs) ===" in out           # carried on
    assert "could not connect: OSError: unreachable" in out


def test_dump_with_set_is_rejected(hub, device, capsys):
    assert hub("--dump", "--label", "main", "--set-temp", "70") == 2
    assert "--dump can't be combined with --set-temp" in \
        capsys.readouterr().err
    assert device.writes == []


@pytest.mark.parametrize("argv, code", [
    ([], 0),
    (["--status"], 1),
    (["--dump"], 1),
    (["--label", "main", "--set-temp", "70"], 1),
])
def test_no_pairing_files_exit_code(tmp_path, monkeypatch, capsys, argv,
                                    code):
    monkeypatch.setattr(sys, "argv",
                        ["ecobee-hub", "--folder", str(tmp_path), *argv])
    with pytest.raises(SystemExit) as exc:
        c.main()
    assert exc.value.code == code
    assert "No pairing files found" in capsys.readouterr().out
