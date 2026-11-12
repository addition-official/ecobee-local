"""Talking to the (fake) thermostat: connecting, writing, and failing."""

import asyncio

import pytest
from aiohomekit.exceptions import (
    AccessoryDisconnectedError, AuthenticationError,
)

import ecobee_local.controller as c
from ecobee_local import EcobeeController

from conftest import (
    COMFORT_READ, COMFORT_WRITE, COOL_THRESHOLD, CREDS, CURRENT_TEMP,
    DISPLAY_UNITS, HEAT_THRESHOLD, HUMIDITY, MODE, SENSOR_TEMP, TARGET_FAN, TARGET_TEMP, build_accessories, run_async, wait_until, write_pair_file,
)


# ------------------------------------------------------------ connecting

def test_connect_reads_status_and_subscribes(connected, device):
    st = connected.get_status("main")
    assert st["online"] is True
    assert st["current_temp"] == 71
    assert st["target_temp"] == 72
    assert st["comfort_mode"] == "home"
    assert [s["name"] for s in st["sensors"]] == ["Built-in", "Bedroom"]
    pairing = device.pairings[0]
    assert COMFORT_WRITE not in pairing.subscribed
    assert MODE in pairing.subscribed
    assert pairing.callback is not None


def test_connect_strips_metadata_from_credentials(connected, device):
    assert device.pairings[0].creds == CREDS


def test_subscribe_failure_still_connects(device, pair_folder,
                                          make_controller, capsys):
    device.subscribe_exc = RuntimeError("no events on this firmware")
    ec = make_controller(pair_folder)
    assert run_async(ec, lambda: ec._connect("main")) is True
    assert ec.cache["main"]["online"] is True
    assert "polling only" in capsys.readouterr().out


def test_push_event_triggers_refresh(connected, device):
    device.values[TARGET_TEMP] = 23.0
    reads = device.reads
    device.pairings[0].callback({(1, 11): {"value": 23.0}})
    assert wait_until(lambda: connected.cache["main"]["target_temp"] == 73)
    assert device.reads > reads


@pytest.mark.parametrize("exc", [
    AuthenticationError("pair verify failed"),     # keys no longer accepted
    asyncio.TimeoutError(),
    AccessoryDisconnectedError("connection refused"),
    OSError("host unreachable"),
])
def test_connect_failure_goes_offline_and_closes(device, pair_folder,
                                                 make_controller, capsys, exc):
    device.connect_exc = exc
    ec = make_controller(pair_folder)
    assert run_async(ec, lambda: ec._connect("main")) is False
    assert ec.cache["main"]["online"] is False
    assert ec._state["main"]["pairing"] is None
    # regression test for the 1.1.1 socket leak: the half-open pairing
    # must be closed or aiohomekit keeps retrying it forever
    assert device.pairings[0].closed is True
    assert "connect failed" in capsys.readouterr().out


def test_read_failure_during_connect_closes(device, pair_folder,
                                            make_controller):
    device.read_exc = asyncio.TimeoutError()
    ec = make_controller(pair_folder)
    assert run_async(ec, lambda: ec._connect("main")) is False
    assert device.pairings[0].closed is True


def test_connect_actually_times_out_on_a_silent_device(
        device, pair_folder, make_controller, monkeypatch):
    # the device never answers; shorten the controller's own timeouts so the
    # test is fast, and prove wait_for (not luck) gets us out
    real_wait_for = asyncio.wait_for
    monkeypatch.setattr(c.asyncio, "wait_for",
                        lambda aw, timeout: real_wait_for(aw, 0.05))
    device.hang = True
    ec = make_controller(pair_folder)
    ok = run_async(ec, lambda: real_wait_for(ec._connect("main"), 5))
    assert ok is False
    assert device.pairings[0].closed is True


def test_write_times_out_on_a_silent_device(connected, device, monkeypatch):
    real_wait_for = asyncio.wait_for
    monkeypatch.setattr(c.asyncio, "wait_for",
                        lambda aw, timeout: real_wait_for(aw, 0.05))
    device.hang = True
    res = connected.set_temp("main", 75, timeout=5)
    assert res["ok"] is False
    assert "TimeoutError" in res["error"]
    assert connected.cache["main"]["target_temp"] == 72


@pytest.mark.parametrize("contents, message", [
    (None, "pairing file missing"),
    ("{}", "pairing file empty"),
])
def test_bad_pairing_file(device, tmp_path, make_controller, capsys,
                          contents, message):
    if contents is not None:
        (tmp_path / "pair.json").write_text(contents)
    ec = EcobeeController({"main": str(tmp_path / "pair.json")})
    assert run_async(ec, lambda: ec._connect("main")) is False
    assert ec.cache["main"]["online"] is False
    assert message in capsys.readouterr().out
    assert device.pairings == []


def test_corrupt_pairing_file(device, tmp_path, capsys):
    (tmp_path / "pair.json").write_text("{not json")
    ec = EcobeeController({"main": str(tmp_path / "pair.json")})
    assert run_async(ec, lambda: ec._connect("main")) is False
    assert "connect failed: JSONDecodeError" in capsys.readouterr().out


# ------------------------------------------------------------ refreshing

def test_refresh_picks_up_changes(connected, device):
    device.values[MODE] = 2
    connected.refresh("main")
    assert connected.get_status("main")["mode"] == 2


def test_refresh_failure_reconnects(connected, device):
    first = device.pairings[0]
    device.read_exc = AccessoryDisconnectedError("dropped")
    connected.refresh("main")
    assert first.closed is True
    assert connected.cache["main"]["online"] is False
    device.read_exc = None
    connected.refresh("main")            # pairing is None -> reconnect
    assert connected.cache["main"]["online"] is True
    assert len(device.pairings) == 3     # original, failed retry, success


def test_health_loop_survives_errors(device, pair_folder, make_controller,
                                     capsys):
    ec = make_controller(pair_folder)

    async def boom(label):
        raise RuntimeError("kaboom")

    ec._refresh = boom
    ec.poll_interval = 0

    async def go():
        task = asyncio.ensure_future(ec._health_loop())
        await asyncio.sleep(0.05)
        task.cancel()
    run_async(ec, go)
    assert "health loop error: kaboom" in capsys.readouterr().out


# ---------------------------------------------------------------- writing

def test_set_temp_writes_half_degree_celsius(connected, device):
    assert connected.set_temp("main", 71) == {"ok": True}
    assert device.writes[-1] == (*TARGET_TEMP, 21.5)
    assert connected.cache["main"]["target_temp"] == 71
    assert connected.cache["main"]["target_temp_c"] == 21.5


def test_celsius_setters_snap_to_half_degrees(connected, device):
    assert connected.set_temp_c("main", 22.3)["ok"]
    assert device.writes[-1] == (*TARGET_TEMP, 22.5)
    assert connected.set_heat_threshold_c("main", 19.0)["ok"]
    assert device.writes[-1] == (*HEAT_THRESHOLD, 19.0)
    assert connected.set_cool_threshold_c("main", 24.74)["ok"]
    assert device.writes[-1] == (*COOL_THRESHOLD, 24.5)


def test_thresholds(connected, device):
    assert connected.set_heat_threshold("main", 66)["ok"]
    assert device.writes[-1] == (*HEAT_THRESHOLD, 19.0)
    assert connected.set_cool_threshold("main", 78)["ok"]
    assert device.writes[-1] == (*COOL_THRESHOLD, 25.5)
    assert connected.cache["main"]["cool_threshold"] == 78


def test_mode_fan_units(connected, device):
    assert connected.set_mode("main", EcobeeController.MODE_COOL)["ok"]
    assert device.writes[-1] == (*MODE, 2)
    assert connected.cache["main"]["mode"] == 2
    assert connected.set_fan("main", auto=False)["ok"]
    assert device.writes[-1] == (*TARGET_FAN, 0)
    assert connected.set_display_units("main", fahrenheit=False)["ok"]
    assert device.writes[-1] == (*DISPLAY_UNITS, 0)


def test_set_comfort(connected, device):
    assert connected.set_comfort_mode("main", "AWAY") == {"ok": True}
    assert device.writes[-1] == (*COMFORT_WRITE, 2)
    assert connected.get_comfort_mode("main") == "away"
    # a lagging read-back of the old profile doesn't flip it back
    connected.refresh("main")
    assert connected.get_comfort_mode("main") == "away"


def test_set_comfort_rejects_unknown_mode(connected, device):
    res = connected.set_comfort_mode("main", "vacation")
    assert res["ok"] is False and "unknown comfort mode" in res["error"]
    assert device.writes == []


def test_comfort_warning_printed_once(device, pair_folder, make_controller,
                                      capsys):
    ec = make_controller(pair_folder, comfort_warning=True)
    ec.get_comfort_mode("main")
    ec.get_comfort_mode("main")
    assert capsys.readouterr().err.count("comfort control") == 1


def test_unsupported_characteristic(connected, device):
    res = connected.set_target_humidity("main", 40)
    assert res == {"ok": False,
                   "error": "this thermostat does not expose 'target_humidity'"}
    assert device.writes == []


def test_unsupported_comfort(device, pair_folder, make_controller):
    device.accessories = build_accessories(comfort=False)
    ec = make_controller(pair_folder)
    ec.start()
    assert wait_until(lambda: ec.cache["main"]["online"])
    res = ec.set_comfort_mode("main", "home")
    assert res["ok"] is False and "comfort" in res["error"]


@pytest.mark.parametrize("call", [
    lambda ec: ec.set_temp("main", 70),
    lambda ec: ec.set_mode("main", 1),
    lambda ec: ec.set_comfort_mode("main", "home"),
])
def test_write_when_not_connected(device, pair_folder, make_controller, call):
    device.connect_exc = OSError("unreachable")
    ec = make_controller(pair_folder)
    ec.start()
    assert wait_until(lambda: len(device.pairings) >= 1)
    assert call(ec) == {"ok": False, "error": "not connected"}


def test_write_exception_is_reported(connected, device):
    device.write_exc = AccessoryDisconnectedError("gone")
    res = connected.set_mode("main", 0)
    assert res["ok"] is False
    assert "AccessoryDisconnectedError" in res["error"]
    assert connected.cache["main"]["mode"] == 1


@pytest.mark.filterwarnings("ignore:coroutine .* was never awaited")
def test_calls_before_start():
    ec = EcobeeController({"main": "/nope.json"})
    assert ec.set_temp("main", 70) == {"ok": False,
                                       "error": "controller not started"}


# ---------------------------------------------------------- start / stop

def test_stop_closes_connections(connected, device):
    connected.stop()
    assert device.pairings[0].closed is True
    assert connected.cache["main"]["online"] is False
    connected.stop()                       # safe twice


def test_start_without_connect_opens_nothing(device, pair_folder,
                                             make_controller):
    ec = make_controller(pair_folder)
    ec.start(connect=False)
    assert wait_until(lambda: ec._state["main"]["lock"] is not None)
    assert device.pairings == []


# ----------------------------------------------------------------- dump

def test_dump_uses_temporary_connection(device, pair_folder, make_controller):
    ec = make_controller(pair_folder)
    ec.start(connect=False)
    rows = ec.dump_characteristics("main")
    assert len(device.pairings) == 1 and device.pairings[0].closed is True
    target = [r for r in rows if (r["aid"], r["iid"]) == TARGET_TEMP][0]
    assert target["readable"] and target["writable"] and target["notifies"]
    assert target["unit"] == "celsius" and target["max"] == 33.3
    comfort_w = [r for r in rows if (r["aid"], r["iid"]) == COMFORT_WRITE][0]
    assert comfort_w["writable"] and not comfort_w["readable"]


def test_dump_failure_closes_temporary_connection(device, pair_folder,
                                                  make_controller):
    # regression test for the --dump socket leak
    device.connect_exc = asyncio.TimeoutError()
    ec = make_controller(pair_folder)
    ec.start(connect=False)
    with pytest.raises(asyncio.TimeoutError):
        ec.dump_characteristics("main")
    assert device.pairings[0].closed is True


def test_dump_reuses_open_connection(connected, device):
    rows = connected.dump_characteristics("main")
    assert rows and len(device.pairings) == 1
    assert device.pairings[0].closed is False


def test_dump_missing_file(device, tmp_path, make_controller):
    ec = EcobeeController({"main": str(tmp_path / "gone.json")})
    ec.start(connect=False)
    try:
        assert wait_until(lambda: ec._state["main"]["lock"] is not None)
        rows = ec.dump_characteristics("main")
    finally:
        ec.stop()
    assert "pairing file missing" in rows[0]["error"]


# ------------------------------------------- characteristics that fail to read

def test_failed_read_keeps_thermostat_online(connected, device):
    # aiohomekit reports a failed characteristic as {"status": ...} with no
    # "value". The rest of the reading is still good.
    first = device.pairings[0]
    device.read_errors = {TARGET_TEMP: -70402, COMFORT_READ: -70402,
                          SENSOR_TEMP: -70402}
    device.values[CURRENT_TEMP] = 23.0
    device.values[MODE] = 2
    connected.refresh("main")
    st = connected.get_status("main")
    assert st["online"] is True
    assert st["current_temp"] == 73 and st["mode"] == 2     # updated
    assert st["target_temp"] == 72                          # last good value
    assert st["target_temp_c"] == 22.0
    assert st["comfort_mode"] == "home"                     # last good value
    assert st["sensors"][1]["temperature"] is None          # unknown
    assert st["sensors"][1]["battery"] == 87
    # the connection was kept, not dropped and rebuilt
    assert len(device.pairings) == 1 and not first.closed


def test_failed_read_on_first_connect_is_unknown(device, pair_folder,
                                                 make_controller):
    device.read_errors = {TARGET_TEMP: -70402, HUMIDITY: -70402}
    ec = make_controller(pair_folder)
    assert run_async(ec, lambda: ec._connect("main")) is True
    st = ec.get_status("main")
    assert st["online"] is True
    assert st["target_temp"] == "--" and st["target_temp_c"] is None
    assert st["humidity"] == "--"
    assert st["current_temp"] == 71


def test_every_read_failing_keeps_connection(connected, device):
    device.read_errors = {cid: -70403 for cid in device.values}
    connected.refresh("main")
    st = connected.get_status("main")
    assert st["online"] is True and st["target_temp"] == 72
    assert len(device.pairings) == 1


# --------------------------------------------- writes the thermostat rejects

@pytest.mark.parametrize("call, cid, key, before", [
    (lambda ec: ec.set_temp("main", 90), TARGET_TEMP, "target_temp", 72),
    (lambda ec: ec.set_temp_c("main", 40), TARGET_TEMP, "target_temp", 72),
    (lambda ec: ec.set_heat_threshold("main", 40), HEAT_THRESHOLD,
     "heat_threshold", 68),
    (lambda ec: ec.set_mode("main", 7), MODE, "mode", 1),
    (lambda ec: ec.set_fan("main", False), TARGET_FAN, "target_fan", 1),
])
def test_rejected_write_reports_failure(connected, device, call, cid, key,
                                        before):
    # aiohomekit does NOT raise when the thermostat rejects a write; it
    # returns {(aid, iid): {"status": ..., "description": ...}}
    device.write_errors = {cid: -70410}
    res = call(connected)
    assert res["ok"] is False
    assert "Value supplied is not valid" in res["error"]
    assert "-70410" in res["error"]
    assert connected.cache["main"][key] == before


def test_rejected_comfort_write(connected, device):
    device.write_errors = {COMFORT_WRITE: -70404}
    res = connected.set_comfort_mode("main", "away")
    assert res["ok"] is False and "-70404" in res["error"]
    assert connected.get_comfort_mode("main") == "home"
    assert connected.cache["main"]["comfort_raw"] == 0
    # no optimistic hold either: a change made elsewhere shows up right away
    device.values[COMFORT_READ] = 1
    connected.refresh("main")
    assert connected.get_comfort_mode("main") == "sleep"


def test_write_with_success_status_is_ok(connected, device, monkeypatch):
    # some firmwares answer every write with a status, 0 meaning success
    pairing = device.pairings[0]
    real_put = pairing.put_characteristics

    async def put(chars):
        await real_put(chars)
        return {(aid, iid): {"status": 0, "description": "Success"}
                for aid, iid, _ in chars}
    monkeypatch.setattr(pairing, "put_characteristics", put)
    assert connected.set_temp("main", 70) == {"ok": True}
    assert connected.cache["main"]["target_temp"] == 70


# --------------------------------------------------- startup connection race

def test_command_right_after_start_does_not_open_second_connection(
        device, pair_folder, make_controller):
    # the thermostat is slow to answer the first connection; a command sent
    # meanwhile used to open a second connection and leak the first
    device.connect_delay = 0.3
    ec = make_controller(pair_folder)
    ec.start()
    assert ec.set_temp("main", 70) == {"ok": True}
    assert len(device.pairings) == 1
    assert not device.pairings[0].closed
    assert ec._state["main"]["pairing"] is device.pairings[0]


def test_refresh_right_after_start_does_not_open_second_connection(
        device, pair_folder, make_controller):
    device.connect_delay = 0.3
    ec = make_controller(pair_folder)
    ec.start()
    ec.refresh("main")
    assert ec.cache["main"]["online"] is True
    assert len(device.pairings) == 1
