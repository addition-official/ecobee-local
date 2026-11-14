"""1.3.0: allowed modes, limits, offline_reason, updated_at, set_range."""

import asyncio
import time

import pytest
from aiohomekit.exceptions import (
    AccessoryDisconnectedError, AuthenticationError, IncorrectPairingIdError,
    InvalidAuthTagError, UnpairedError,
)

from ecobee_local import EcobeeController

from conftest import (
    COOL_THRESHOLD, HEAT_THRESHOLD, MODE, build_accessories, run_async,
    wait_until, write_pair_file,
)


def connect(device, pair_folder, make_controller, **tree):
    if tree:
        device.accessories = build_accessories(**tree)
    ec = make_controller(pair_folder)
    ec.start()
    assert ec.wait_ready(timeout=5)
    return ec


# ------------------------------------------------------------------ modes

@pytest.mark.parametrize("tree, modes", [
    ({}, ["off", "heat", "cool", "auto"]),
    ({"mode_values": [0, 1, 2]}, ["off", "heat", "cool"]),     # auto off
    ({"mode_values": [2, 0, 1]}, ["off", "heat", "cool"]),     # any order
    ({"mode_values": [0, 1]}, ["off", "heat"]),                # heat only
    ({"mode_values": [0, 1, 7]}, ["off", "heat"]),             # unknown 7
    ({"mode_values": None, "mode_values_range": [0, 2]},
     ["off", "heat", "cool"]),
    ({"mode_values": None}, ["off", "heat", "cool", "auto"]),  # missing
    ({"mode_values": []}, ["off", "heat", "cool", "auto"]),    # empty list
])
def test_modes_from_valid_values(device, pair_folder, make_controller,
                                 tree, modes):
    ec = connect(device, pair_folder, make_controller, **tree)
    assert ec.get_status("main")["modes"] == modes


def test_modes_unknown_before_connecting(device, pair_folder,
                                         make_controller):
    ec = make_controller(pair_folder)
    assert ec.get_status("main")["modes"] is None


def test_modes_reread_on_reconnect(connected, device):
    assert connected.get_status("main")["modes"][-1] == "auto"
    # auto heat/cool turned off at the thermostat, then the connection drops
    device.accessories = build_accessories(mode_values=[0, 1, 2])
    device.read_exc = AccessoryDisconnectedError("dropped")
    connected.refresh("main")                 # fails, reconnects
    device.read_exc = None
    connected.refresh("main")
    assert connected.get_status("main")["modes"] == ["off", "heat", "cool"]


@pytest.mark.parametrize("mode, name", [(3, "auto"), ("auto", "auto")])
def test_set_mode_refuses_disallowed_mode(device, pair_folder,
                                          make_controller, mode, name):
    ec = connect(device, pair_folder, make_controller, mode_values=[0, 1, 2])
    assert ec.set_mode("main", mode) == {
        "ok": False, "error": f"{name} mode is turned off on this thermostat"}
    assert device.writes == []
    assert ec.set_mode("main", "cool") == {"ok": True}   # allowed still work


def test_set_mode_heat_off(device, pair_folder, make_controller):
    ec = connect(device, pair_folder, make_controller, mode_values=[0, 2])
    res = ec.set_mode("main", "heat")
    assert res["error"] == "heat mode is turned off on this thermostat"


def test_set_mode_without_metadata_allows_all(device, pair_folder,
                                             make_controller):
    ec = connect(device, pair_folder, make_controller, mode_values=None)
    assert ec.set_mode("main", "auto") == {"ok": True}
    assert device.writes == [(*MODE, 3)]


# ----------------------------------------------------------------- limits

def test_limits_from_metadata(connected):
    st = connected.get_status("main")
    assert st["limits"] == {"target": [45, 92], "heat_threshold": [45, 79],
                            "cool_threshold": [64, 92]}
    assert st["limits_c"] == {"target": [7.0, 33.3],
                              "heat_threshold": [7.0, 26.0],
                              "cool_threshold": [18.0, 33.3]}


def test_limits_missing_are_none(device, pair_folder, make_controller):
    # aiohomekit would fill in generic HomeKit defaults here; we must not
    ec = connect(device, pair_folder, make_controller, limits=False)
    st = ec.get_status("main")
    assert st["limits"] == {"target": None, "heat_threshold": None,
                            "cool_threshold": None}
    assert st["limits_c"] == st["limits"]


def test_limits_before_connecting(device, pair_folder, make_controller):
    st = make_controller(pair_folder).get_status("main")
    assert st["limits"] == {"target": None, "heat_threshold": None,
                            "cool_threshold": None}


# ------------------------------------------------------------- updated_at

def test_updated_at(device, pair_folder, make_controller):
    ec = make_controller(pair_folder)
    assert ec.get_status("main")["updated_at"] is None
    t0 = time.time()
    ec.start()
    assert ec.wait_ready(timeout=5)
    first = ec.get_status("main")["updated_at"]
    assert t0 <= first <= time.time()
    time.sleep(0.02)
    ec.refresh("main")
    assert ec.get_status("main")["updated_at"] > first


def test_updated_at_kept_when_offline(connected, device):
    before = connected.get_status("main")["updated_at"]
    device.read_exc = AccessoryDisconnectedError("dropped")
    device.connect_exc = OSError("unreachable")
    connected.refresh("main")
    st = connected.get_status("main")
    assert st["online"] is False and st["updated_at"] == before


def test_updated_at_alone_does_not_fire_on_change(connected, device):
    events = []
    connected.on_change(lambda label, status: events.append(status))
    connected.refresh("main")
    connected.refresh("main")
    assert events == []


# --------------------------------------------------------- offline_reason

def test_offline_reason_none_when_online_or_untried(device, pair_folder,
                                                    make_controller):
    ec = make_controller(pair_folder)
    assert ec.get_status("main")["offline_reason"] is None
    ec.start()
    assert ec.wait_ready(timeout=5)
    assert ec.get_status("main")["offline_reason"] is None


@pytest.mark.parametrize("exc, reason", [
    (AuthenticationError("pair verify failed"), "unpaired"),
    (InvalidAuthTagError("step 3"), "unpaired"),
    (IncorrectPairingIdError("step 3"), "unpaired"),   # reset: new device id
    (UnpairedError("Must be paired"), "unpaired"),
    (asyncio.TimeoutError(), "unreachable"),
    (OSError("No route to host"), "unreachable"),
    (AccessoryDisconnectedError("refused"), "unreachable"),
])
def test_offline_reason(device, pair_folder, make_controller, exc, reason):
    device.connect_exc = exc
    ec = make_controller(pair_folder)
    assert run_async(ec, lambda: ec._connect("main")) is False
    st = ec.get_status("main")
    assert st["online"] is False and st["offline_reason"] == reason


def test_offline_reason_from_timeout_behind_pairing_error(
        device, pair_folder, make_controller):
    # aiohomekit retries some pairing failures in the background, so our
    # own timeout fires first; the real cause is on the connection
    device.connect_exc = asyncio.TimeoutError()
    device.last_connector_error = IncorrectPairingIdError("step 3")
    ec = make_controller(pair_folder)
    run_async(ec, lambda: ec._connect("main"))
    assert ec.get_status("main")["offline_reason"] == "unpaired"


@pytest.mark.parametrize("contents", [None, "{}", "{broken"])
def test_offline_reason_bad_pairing_file(device, tmp_path, contents):
    if contents is not None:
        (tmp_path / "pair.json").write_text(contents)
    ec = EcobeeController({"main": str(tmp_path / "pair.json")})
    run_async(ec, lambda: ec._connect("main"))
    assert ec.get_status("main")["offline_reason"] == "unpaired"


def test_offline_reason_clears_on_reconnect(connected, device):
    device.read_exc = AccessoryDisconnectedError("dropped")
    device.connect_exc = AuthenticationError("pair verify failed")
    connected.refresh("main")
    assert connected.get_status("main")["offline_reason"] == "unpaired"
    device.read_exc = device.connect_exc = None
    connected.refresh("main")
    st = connected.get_status("main")
    assert st["online"] is True and st["offline_reason"] is None


def test_offline_reason_change_fires_on_change(connected, device):
    events = []
    connected.on_change(lambda label, status: events.append(status))
    device.read_exc = AccessoryDisconnectedError("dropped")
    device.connect_exc = OSError("unreachable")
    connected.refresh("main")
    assert events[-1]["offline_reason"] == "unreachable"


# -------------------------------------------------------------- set_range

def test_set_range_writes_both_in_one_request(connected, device):
    res = connected.set_range("main", 68, 76)
    assert res == {"ok": True, "heat_threshold": 68, "cool_threshold": 76}
    assert device.write_calls == [[(*HEAT_THRESHOLD, 20.0),
                                   (*COOL_THRESHOLD, 24.5)]]
    st = connected.get_status("main")
    assert st["heat_threshold"] == 68 and st["cool_threshold"] == 76


def test_set_range_reports_what_the_thermostat_kept(connected, device):
    # a thermostat that enforces a 2 C gap by moving the cool threshold
    device.min_gap = 2.0
    device.gap_behavior = "move_cool"
    res = connected.set_range("main", 70, 71)
    assert res["ok"] is True
    # 70F = 21.0C, so cool is pushed to 23.0C = 73F
    assert res["heat_threshold"] == 70 and res["cool_threshold"] == 73
    assert connected.get_status("main")["cool_threshold"] == 73


def test_set_range_rejected_by_thermostat(connected, device):
    device.min_gap = 2.0
    device.gap_behavior = "reject"
    res = connected.set_range("main", 70, 71)
    assert res["ok"] is False and "rejected" in res["error"]
    st = connected.get_status("main")
    assert st["heat_threshold"] == 68 and st["cool_threshold"] == 77


@pytest.mark.parametrize("heat, cool", [(75, 75), (76, 70)])
def test_set_range_heat_must_be_below_cool(connected, device, heat, cool):
    res = connected.set_range("main", heat, cool)
    assert res["ok"] is False and "below" in res["error"]
    assert device.writes == []


@pytest.mark.parametrize("heat, cool", [("x", 75), (68, float("nan")),
                                        (None, 75), (True, 75)])
def test_set_range_bad_values(connected, device, heat, cool):
    res = connected.set_range("main", heat, cool)
    assert res["ok"] is False and "invalid temperature" in res["error"]
    assert device.writes == []


def test_set_range_unknown_label_and_offline(device, pair_folder,
                                            make_controller):
    device.connect_exc = OSError("unreachable")
    ec = make_controller(pair_folder)
    ec.start()
    ec.wait_ready(timeout=5)
    assert ec.set_range("nope", 68, 76)["error"] == "unknown thermostat 'nope'"
    assert ec.set_range("main", 68, 76) == {"ok": False,
                                            "error": "not connected"}


def test_set_range_unsupported(device, pair_folder, make_controller):
    tree = build_accessories()
    thermostat = tree[0].services[2]
    thermostat.characteristics = [
        ch for ch in thermostat.characteristics
        if not ch.type.startswith(("0000000D", "00000012"))]
    device.accessories = tree
    ec = make_controller(pair_folder)
    ec.start()
    assert ec.wait_ready(timeout=5)
    res = ec.set_range("main", 68, 76)
    assert res["ok"] is False and "does not expose" in res["error"]


# ---------------------------------------------------------------- the CLI

def test_status_and_dump_show_modes(device, pair_folder, monkeypatch,
                                    capsys):
    import sys
    import ecobee_local.controller as c
    device.accessories = build_accessories(mode_values=[0, 1, 2])
    for argv in (["--status"], ["--dump"]):
        monkeypatch.setattr(sys, "argv",
                            ["ecobee-hub", "--folder", str(pair_folder), *argv])
        try:
            c.main()
        except SystemExit:
            pass
    out = capsys.readouterr().out
    assert "'modes': ['off', 'heat', 'cool']" in out
    assert "VALID" in out
    mode_row = [line for line in out.splitlines()
                if "00000033-0000-1000-8000-0026BB765291" in line][0]
    assert "0,1,2" in mode_row
    # MIN/MAX come from the thermostat; none published here for mode
    assert "35" not in mode_row.split("0,1,2")[1]


# ------------------------------------------------------------------ GUI

def _shown_mode_buttons(monkeypatch, ec):
    """Open the real control window briefly; return the mode buttons on it."""
    tk = pytest.importorskip("tkinter")
    import ecobee_local.controller as c
    try:
        tk.Tk().destroy()
    except Exception:
        pytest.skip("no display")
    shown = {}

    def run_briefly(root):
        end = time.monotonic() + 1.0          # long enough for a poll
        while time.monotonic() < end:
            root.update()
            time.sleep(0.02)
        # the mode buttons are the ones in the same row as "Heat" (the fan
        # row has an "Auto" button too)
        stack, rows = [root], []
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if isinstance(widget, tk.Button) and widget.cget("text") == "Heat":
                rows.append(widget.master)
        assert len(rows) == 1
        for widget in rows[0].winfo_children():
            if widget.winfo_manager():              # packed = on screen
                shown[widget.cget("text")] = True
        root.destroy()
    monkeypatch.setattr(tk.Tk, "mainloop", run_briefly)
    c.launch_gui(ec, poll_ms=50)
    # free the window's Tcl objects here, on the thread that made them
    import gc
    gc.collect()
    return set(shown)


def test_gui_offers_only_allowed_modes(device, pair_folder, make_controller,
                                       monkeypatch):
    ec = connect(device, pair_folder, make_controller, mode_values=[0, 1, 2])
    assert _shown_mode_buttons(monkeypatch, ec) == {"Heat", "Cool", "Off"}


def test_gui_offers_all_modes_when_allowed(device, pair_folder,
                                           make_controller, monkeypatch):
    ec = connect(device, pair_folder, make_controller)
    assert _shown_mode_buttons(monkeypatch, ec) == {"Heat", "Cool", "Auto",
                                                    "Off"}


# ------------------------------------------------- modes set by the user

def make_with_modes(pair_folder, make_controller, **kwargs):
    ec = make_controller(pair_folder, **kwargs)
    ec.start()
    assert ec.wait_ready(timeout=5)
    return ec


def test_constructor_modes(device, pair_folder, make_controller):
    ec = make_controller(pair_folder, modes={"main": ["cool", "off", "heat"]})
    # known straight away, in the usual order
    assert ec.get_status("main")["modes"] == ["off", "heat", "cool"]
    ec.start()
    assert ec.wait_ready(timeout=5)
    # the Ecobee publishes all four; the configured list still wins
    assert ec.get_status("main")["modes"] == ["off", "heat", "cool"]
    assert ec.set_mode("main", "auto") == {
        "ok": False, "error": "auto mode is turned off on this thermostat"}
    assert device.writes == []
    assert ec.set_mode("main", "heat") == {"ok": True}


def test_file_modes(device, tmp_path, make_controller):
    write_pair_file(tmp_path, _modes=["off", "cool"])
    ec = make_with_modes(tmp_path, make_controller)
    assert ec.get_status("main")["modes"] == ["off", "cool"]
    assert ec.set_mode("main", "heat")["error"] == \
        "heat mode is turned off on this thermostat"
    # never handed to aiohomekit as a credential
    assert "_modes" not in device.pairings[0].creds


def test_constructor_modes_win_over_file(device, tmp_path, make_controller):
    write_pair_file(tmp_path, _modes=["off", "cool"])
    ec = make_with_modes(tmp_path, make_controller,
                         modes={"main": ["off", "heat", "cool", "auto"]})
    assert ec.get_status("main")["modes"] == ["off", "heat", "cool", "auto"]


def test_configured_modes_limited_to_what_thermostat_has(
        device, pair_folder, make_controller):
    device.accessories = build_accessories(mode_values=[0, 1])  # heat only
    ec = make_with_modes(pair_folder, make_controller,
                         modes={"main": ["off", "heat", "cool"]})
    assert ec.get_status("main")["modes"] == ["off", "heat"]


@pytest.mark.parametrize("modes, message", [
    ({"main": ["off", "warm"]}, "unknown mode 'warm'"),
    ({"main": []}, "at least one mode"),
    ({"main": "heat"}, "list of mode names"),
    ({"nope": ["off"]}, "unknown thermostat 'nope'"),
])
def test_bad_constructor_modes(pair_folder, modes, message):
    with pytest.raises(ValueError, match=message):
        EcobeeController.from_folder(app_folder=str(pair_folder), modes=modes)


@pytest.mark.parametrize("bad", [["off", "warm"], [], "heat", {"x": 1}, 5])
def test_bad_file_modes_are_ignored_with_a_warning(device, tmp_path,
                                                   make_controller, capsys,
                                                   bad):
    write_pair_file(tmp_path, _modes=bad)
    ec = make_with_modes(tmp_path, make_controller)
    assert ec.get_status("main")["modes"] == ["off", "heat", "cool", "auto"]
    assert "_modes" in capsys.readouterr().err


def test_add_thermostat_with_modes(device, tmp_path):
    write_pair_file(tmp_path, "up.json", label="up")
    ec = EcobeeController(app_folder=str(tmp_path))
    ec.add_thermostat("up.json", modes=["off", "cool"])
    assert ec.get_status("up")["modes"] == ["off", "cool"]
    with pytest.raises(ValueError):
        ec.add_thermostat("up.json", label="up2", modes=["boost"])


def test_cli_follows_file_modes(device, tmp_path, monkeypatch, capsys):
    import sys
    import ecobee_local.controller as c
    write_pair_file(tmp_path, _modes=["off", "heat", "cool"])
    monkeypatch.setattr(sys, "argv", ["ecobee-hub", "--folder", str(tmp_path),
                                      "--label", "main", "--set-mode", "auto"])
    with pytest.raises(SystemExit) as exc:
        c.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "auto mode is turned off on this thermostat" in out
    assert "'modes': ['off', 'heat', 'cool']" in out
    assert device.writes == []


def test_gui_follows_configured_modes(device, pair_folder, make_controller,
                                      monkeypatch):
    ec = make_with_modes(pair_folder, make_controller,
                         modes={"main": ["off", "cool"]})
    assert _shown_mode_buttons(monkeypatch, ec) == {"Cool", "Off"}
