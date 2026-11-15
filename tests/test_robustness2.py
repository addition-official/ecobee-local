"""Odd input found in the second review round."""

import json
import sys

import pytest

import ecobee_local.controller as c
from ecobee_local import EcobeeController

from conftest import (
    ALL_MODES, MODE, build_accessories, run_async, write_pair_file,
)

ALL = ["off", "heat", "cool", "auto"]


def connect_with_mode_meta(device, pair_folder, make_controller, **meta):
    """Connect to a thermostat whose mode characteristic publishes `meta`."""
    tree = build_accessories(mode_values=None)
    mode = [ch for ch in tree[0].services[2].characteristics
            if ch.iid == MODE[1]][0]
    raw = mode.raw

    def raw_with_meta(value):
        d = raw(value)
        d.update(meta)
        return d
    mode.raw = raw_with_meta
    device.accessories = tree
    ec = make_controller(pair_folder)
    ec.start()
    ec.wait_ready(timeout=5)
    return ec


@pytest.mark.parametrize("meta", [
    {"valid-values": "123"},
    {"valid-values": 3},
    {"valid-values": [[1]]},
    {"valid-values": {"a": 1}},
    {"valid-values": [7, 9]},                 # nothing we know
    {"valid-values": [True, False]},
    {"valid-values-range": ["0", "3"]},
    {"valid-values-range": [None, 3]},
    {"valid-values-range": [1]},
    {"valid-values-range": 5},
    {"valid-values-range": [3, 0]},           # reversed: nothing in it
])
def test_odd_mode_metadata_does_not_break_the_connection(
        device, pair_folder, make_controller, meta):
    ec = connect_with_mode_meta(device, pair_folder, make_controller, **meta)
    st = ec.get_status("main")
    assert st["online"] is True and st["offline_reason"] is None
    assert st["modes"] == ALL                 # treated as "not published"


@pytest.mark.parametrize("rng", [[1], 5, "13", [1, 2, 3], {"a": 1}])
def test_dump_survives_odd_valid_values_range(device, pair_folder,
                                              make_controller, rng):
    connect_with_mode_meta(device, pair_folder, make_controller,
                           **{"valid-values-range": rng})
    ec = make_controller(pair_folder)
    ec.start(connect=False)
    rows = ec.dump_characteristics("main")
    assert isinstance(rows, list)
    row = [r for r in rows if (r["aid"], r["iid"]) == MODE][0]
    assert row["valid"] == repr(rng) if not (
        isinstance(rng, list) and len(rng) == 2) else "1-2"


def test_dump_shows_a_proper_range(device, pair_folder, make_controller):
    connect_with_mode_meta(device, pair_folder, make_controller,
                           **{"valid-values-range": [0, 2]})
    ec = make_controller(pair_folder)
    ec.start(connect=False)
    row = [r for r in ec.dump_characteristics("main")
           if (r["aid"], r["iid"]) == MODE][0]
    assert row["valid"] == "0-2"


def test_no_overlap_between_configured_and_published_modes_warns(
        device, pair_folder, make_controller, capsys):
    device.accessories = build_accessories(mode_values=[0, 1, 2])
    ec = make_controller(pair_folder, modes={"main": ["auto"]})
    ec.start()
    ec.wait_ready(timeout=5)
    assert ec.get_status("main")["modes"] == []
    err = capsys.readouterr().err
    assert "none of the configured modes" in err and "main" in err


@pytest.mark.parametrize("lo, hi", [(True, False), (30.0, 10.0),
                                    ("10", 30)])
def test_limits_reject_bools_and_reversed(device, pair_folder,
                                          make_controller, lo, hi):
    tree = build_accessories()
    target = [ch for ch in tree[0].services[2].characteristics
              if ch.type == "35"][0]
    target.minValue, target.maxValue = lo, hi
    device.accessories = tree
    ec = make_controller(pair_folder)
    ec.start()
    ec.wait_ready(timeout=5)
    assert ec.get_status("main")["limits"]["target"] is None


@pytest.mark.parametrize("call", [
    lambda ec: ec.set_temp("main", 1e308),
    lambda ec: ec.set_temp_c("main", -1.7e308),
    lambda ec: ec.set_heat_threshold("main", 10 ** 300),
    lambda ec: ec.set_cool_threshold_c("main", 1e7),
    lambda ec: ec.set_range("main", -1e308, 1e308),
    lambda ec: ec.set_range("main", 68, 1e308),
])
def test_huge_numbers_return_an_error(connected, device, call):
    res = call(connected)
    assert res["ok"] is False and "invalid temperature" in res["error"]
    assert device.writes == []


def test_huge_number_in_cli_is_a_clean_error(connected, device, monkeypatch,
                                             pair_folder, capsys):
    monkeypatch.setattr(sys, "argv", [
        "ecobee-hub", "--folder", str(pair_folder), "--label", "main",
        "--set-temp", "1" + "0" * 400])
    with pytest.raises(SystemExit) as exc:
        c.main()
    assert exc.value.code == 1
    assert "invalid temperature" in capsys.readouterr().out


def test_set_range_rounding_message(connected, device):
    res = connected.set_range("main", 70.9, 71)      # both 21.5 C
    assert res["ok"] is False
    assert "0.5" in res["error"] and "below" in res["error"]


@pytest.mark.parametrize("contents", ["[1, 2]", '"text"', "42"])
def test_pairing_file_that_is_not_an_object(device, tmp_path, contents):
    (tmp_path / "pair.json").write_text(contents)
    ec = EcobeeController({"main": str(tmp_path / "pair.json")})
    assert run_async(ec, lambda: ec._connect("main")) is False
    assert ec.get_status("main")["offline_reason"] == "unpaired"


@pytest.mark.parametrize("contents", ["{broken", "[1, 2]"])
def test_dump_with_broken_pairing_file(device, tmp_path, contents):
    (tmp_path / "pair.json").write_text(contents)
    ec = EcobeeController({"main": str(tmp_path / "pair.json")})
    ec.start(connect=False)
    try:
        res = ec.dump_characteristics("main")
    finally:
        ec.stop()
    assert res["ok"] is False and "pairing file" in res["error"]


@pytest.mark.parametrize("other", ["--watch", "--status", "--headless"])
def test_dump_combined_with_other_modes_is_rejected(device, pair_folder,
                                                    monkeypatch, capsys,
                                                    other):
    monkeypatch.setattr(sys, "argv", ["ecobee-hub", "--folder",
                                      str(pair_folder), "--dump", other])
    with pytest.raises(SystemExit) as exc:
        c.main()
    assert exc.value.code == 2
    assert f"--dump can't be combined with {other}" in capsys.readouterr().err


def test_modes_argument_shapes(pair_folder):
    with pytest.raises(ValueError):
        EcobeeController.from_folder(app_folder=str(pair_folder), modes=[])
    # None for a label means "not configured", like add_thermostat
    ec = EcobeeController.from_folder(app_folder=str(pair_folder),
                                      modes={"main": None})
    assert ec.get_status("main")["modes"] is None
