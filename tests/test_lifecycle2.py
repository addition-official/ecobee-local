"""Lifecycle edge cases found in the second review round."""

import threading
import time

from aiohomekit.exceptions import AccessoryDisconnectedError

from conftest import COMFORT_READ, wait_until


def in_background(fn):
    """Run fn() on another thread; returns a dict that gets 'result'."""
    box = {}

    def go():
        box["result"] = fn()
    t = threading.Thread(target=go, daemon=True)
    t.start()
    box["thread"] = t
    return box


def test_restart_while_previous_shutdown_still_finishing(connected, device):
    # closing the old connection is slow, and stop() gives up waiting
    device.close_delay = 0.5
    connected.stop(timeout=0.05)
    connected.start()
    assert connected.wait_ready(timeout=5)
    time.sleep(1.0)               # the old shutdown has long finished now
    st = connected.get_status("main")
    assert st["online"] is True
    assert connected.set_temp("main", 70) == {"ok": True}
    live = connected._state["main"]["pairing"]
    assert live is not None and not live.closed
    assert all(p.closed for p in device.pairings if p is not live)


def test_command_in_flight_at_stop_returns_quickly(connected, device):
    device.write_delay = 2
    box = in_background(lambda: connected.set_temp("main", 70, timeout=6))
    time.sleep(0.3)
    t0 = time.monotonic()
    connected.stop()
    box["thread"].join(timeout=6)
    assert time.monotonic() - t0 < 1.5
    assert box["result"]["ok"] is False
    assert all(p.closed for p in device.pairings)


def test_reconnect_in_flight_at_stop_closes_its_pairing(connected, device):
    device.read_exc = AccessoryDisconnectedError("dropped")
    device.connect_delay = 1
    box = in_background(lambda: connected.refresh("main", timeout=6))
    assert wait_until(lambda: len(device.pairings) >= 2)
    t0 = time.monotonic()
    connected.stop()
    box["thread"].join(timeout=6)
    assert time.monotonic() - t0 < 1.5
    assert box["result"]["ok"] is False
    assert all(p.closed for p in device.pairings)


def test_run_racing_stop_returns_error(connected, device, monkeypatch):
    # stop() (as if from another thread) completes between _run's first
    # check and its submit to the loop
    calls = []
    real = connected._on_loop_thread

    def stop_in_between():
        if not calls:
            calls.append(1)
            connected.stop()
        return real()
    monkeypatch.setattr(connected, "_on_loop_thread", stop_in_between)
    t0 = time.monotonic()
    res = connected.set_temp("main", 70, timeout=3)
    assert res["ok"] is False
    assert time.monotonic() - t0 < 1


def test_stop_right_after_start_before_loop_exists(device, pair_folder,
                                                   make_controller,
                                                   monkeypatch):
    ec = make_controller(pair_folder)
    slow_main = ec._thread_main

    def delayed():
        time.sleep(0.3)
        slow_main()
    monkeypatch.setattr(ec, "_thread_main", delayed)
    ec.start(wait_timeout=0.01)          # returns before the loop exists
    ec.stop()
    assert ec._started is False
    time.sleep(0.5)
    assert ec.get_status("main")["online"] is False
    assert all(p.closed for p in device.pairings)


def test_get_status_returns_independent_copies(connected, device):
    st = connected.get_status("main")
    st["modes"].remove("auto")
    st["limits"]["target"][0] = 99
    st["sensors"].clear()
    fresh = connected.get_status("main")
    assert fresh["modes"] == ["off", "heat", "cool", "auto"]
    assert fresh["limits"]["target"] == [45, 92]
    assert fresh["sensors"]
    assert connected.set_mode("main", "auto") == {"ok": True}
    every = connected.get_status()
    every["main"]["modes"].clear()
    assert connected.get_status("main")["modes"]


def test_comfort_reread_after_restart(connected, device):
    connected._COMFORT_HOLD_SECONDS = 0.3
    # the thermostat accepts the write but really stays on "home"
    assert connected.set_comfort_mode("main", "away")["ok"]
    connected.stop()
    connected.start()
    assert connected.wait_ready(timeout=5)
    assert device.values[COMFORT_READ] == 0
    assert wait_until(lambda: connected.get_comfort_mode("main") == "home",
                      timeout=3)
