"""The ecobee-hub command, run against a fake thermostat."""

import subprocess
import sys
import time

import pytest

import ecobee_local.controller as c
from ecobee_local import EcobeeController

from conftest import COMFORT_WRITE, MODE, TARGET_TEMP, write_pair_file

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

    def fake_sleep(seconds):
        if seconds >= 3600:
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


def test_gui_unavailable_falls_back(hub, device, capsys):
    hub()
    out = capsys.readouterr().out
    assert "GUI unavailable (no display available)" in out
    assert "main (Main Floor)" in out


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
                                "STEP"]
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
