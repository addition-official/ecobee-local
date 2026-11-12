"""The MQTT bridge (ecobee-mqtt), with the broker and thermostats faked."""

import json
import sys

import pytest

import ecobee_local.mqtt as m


STATUS = {
    "online": True, "current_temp": 71, "target_temp": 72, "mode": 2,
    "humidity": 45, "target_fan": 0, "comfort_mode": "away",
    "heat_threshold": 68, "cool_threshold": 77, "sensors": [],
}


class FakeEC:
    """Records every setter call; answers with `result`."""

    def __init__(self, labels=("main",), result=None):
        self.labels = list(labels)
        self.calls = []
        self.result = result or {"ok": True}
        self.started = self.stopped = False

    def __getattr__(self, name):
        if not name.startswith("set_"):
            raise AttributeError(name)

        def setter(*args):
            self.calls.append((name, *args))
            return self.result
        return setter

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def get_status(self, label):
        return dict(STATUS)


class Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode() if isinstance(payload, str) else payload


def send(ec, topic, payload, prefix="ecobee"):
    m.make_command_handler(ec, prefix)(None, None, Msg(topic, payload))


# ------------------------------------------------------------ state topics

def test_state_payloads():
    out = dict(m.build_state_payloads("ecobee", "main", STATUS))
    assert out.pop("ecobee/main/state") == json.dumps(STATUS)
    assert out == {
        "ecobee/main/online": "true",
        "ecobee/main/temperature": "71",
        "ecobee/main/target": "72",
        "ecobee/main/mode": "cool",
        "ecobee/main/humidity": "45",
        "ecobee/main/fan": "on",
        "ecobee/main/comfort": "away",
        "ecobee/main/heat_threshold": "68",
        "ecobee/main/cool_threshold": "77",
    }


def test_state_payloads_before_first_read():
    blank = {"online": False, "current_temp": "--", "target_temp": None,
             "mode": "--", "humidity": "--", "target_fan": "--",
             "comfort_mode": "--", "heat_threshold": "--",
             "cool_threshold": "--"}
    topics = [t for t, _ in m.build_state_payloads("home/hvac", "up", blank)]
    assert topics == ["home/hvac/up/online", "home/hvac/up/state"]
    assert dict(m.build_state_payloads("x", "up", blank))["x/up/online"] == "false"


@pytest.mark.parametrize("mode, name", [(0, "off"), (1, "heat"), (2, "cool"),
                                        (3, "auto")])
def test_state_mode_names(mode, name):
    out = dict(m.build_state_payloads("e", "l", dict(STATUS, mode=mode)))
    assert out["e/l/mode"] == name


# --------------------------------------------------------- command topics

@pytest.mark.parametrize("thing, payload, call", [
    ("target", "72", ("set_temp", "main", 72)),
    ("target", "71.6", ("set_temp", "main", 72)),
    ("target_c", "22.5", ("set_temp_c", "main", 22.5)),
    ("mode", "Cool", ("set_mode", "main", 2)),
    ("mode", "off", ("set_mode", "main", 0)),
    ("heat", "68", ("set_heat_threshold", "main", 68)),
    ("cool", "75.4", ("set_cool_threshold", "main", 75)),
    ("fan", "AUTO", ("set_fan", "main", True)),
    ("fan", "on", ("set_fan", "main", False)),
    ("humidity", "40", ("set_target_humidity", "main", 40)),
    ("comfort", "Away", ("set_comfort_mode", "main", "away")),
])
def test_commands(thing, payload, call, capsys):
    ec = FakeEC()
    send(ec, f"ecobee/main/set/{thing}", f" {payload}\n")
    assert ec.calls == [call]
    assert "-> ok" in capsys.readouterr().out


@pytest.mark.parametrize("thing, payload, error", [
    ("mode", "turbo", "bad mode 'turbo'"),
    ("target", "warm", "bad payload 'warm'"),
    ("fly", "1", "unknown command 'fly'"),
])
def test_bad_commands_are_logged_not_sent(thing, payload, error, capsys):
    ec = FakeEC()
    send(ec, f"ecobee/main/set/{thing}", payload)
    assert ec.calls == []
    assert error in capsys.readouterr().err


def test_device_error_is_logged(capsys):
    ec = FakeEC(result={"ok": False, "error": "not connected"})
    send(ec, "ecobee/main/set/target", "70")
    assert "not connected" in capsys.readouterr().err


@pytest.mark.parametrize("topic", [
    "other/main/set/target",          # different prefix
    "ecobee/main/state",              # our own state topic
    "ecobee/main/get/target",
    "ecobee/main/set/target/extra",
])
def test_ignores_other_topics(topic):
    ec = FakeEC()
    send(ec, topic, "70")
    assert ec.calls == []


def test_non_utf8_payload_does_not_crash(capsys):
    ec = FakeEC()
    send(ec, "ecobee/main/set/target", b"\xff\xfe")
    assert ec.calls == []
    assert "bad payload" in capsys.readouterr().err


# ------------------------------------------------------------ the bridge

class FakeClient:
    def __init__(self, connect_exc=None):
        self.connect_exc = connect_exc
        self.published = []
        self.subscribed = []
        self.will = None
        self.auth = None
        self.events = []

    def username_pw_set(self, u, p):
        self.auth = (u, p)

    def will_set(self, topic, payload, retain=False):
        self.will = (topic, payload, retain)

    def connect(self, host, port, keepalive=60):
        self.events.append(("connect", host, port))
        if self.connect_exc:
            raise self.connect_exc
        self.on_connect(self, None, {}, 0, None)

    def loop_start(self):
        self.events.append("loop_start")

    def loop_stop(self):
        self.events.append("loop_stop")

    def disconnect(self):
        self.events.append("disconnect")

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic):
        self.subscribed.append(topic)


@pytest.fixture
def bridge(monkeypatch):
    """Patch the bridge's collaborators; returns (ec, client, run)."""
    ec = FakeEC()
    client = FakeClient()
    seen = {}

    def from_folder(**kwargs):
        seen.update(kwargs)
        return ec
    monkeypatch.setattr(m.EcobeeController, "from_folder", from_folder)
    monkeypatch.setattr(m, "_make_client", lambda: client)

    def stop_after_one_publish(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(m.time, "sleep", stop_after_one_publish)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["ecobee-mqtt", *argv])
        m.main()
    return ec, client, seen, run


def test_bridge_publishes_and_shuts_down(bridge):
    ec, client, seen, run = bridge
    run("--host", "broker.lan", "--port", "1884", "--prefix", "hvac/",
        "--username", "u", "--password", "pw", "--retain",
        "--folder", "/creds")
    assert seen == {"app_folder": "/creds", "comfort_warning": False}
    assert client.auth == ("u", "pw")
    assert client.will == ("hvac/bridge/online", "false", True)
    assert client.subscribed == ["hvac/+/set/+"]
    assert ("hvac/bridge/online", "true", True) in client.published
    assert ("hvac/main/temperature", "71", True) in client.published
    assert client.published[-1] == ("hvac/bridge/online", "false", True)
    assert client.events == [("connect", "broker.lan", 1884), "loop_start",
                             "loop_stop", "disconnect"]
    assert ec.stopped


def test_bridge_routes_commands(bridge):
    ec, client, _, run = bridge
    run()
    client.on_message(client, None, Msg("ecobee/main/set/mode", "heat"))
    assert ec.calls == [("set_mode", "main", 1)]


def test_bridge_broker_unreachable(bridge, capsys):
    ec, client, _, run = bridge
    client.connect_exc = ConnectionRefusedError("refused")
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 1
    assert "could not connect to broker" in capsys.readouterr().err
    assert ec.stopped


def test_bridge_no_thermostats(bridge, capsys):
    ec, _, _, run = bridge
    ec.labels = []
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 1
    assert "no thermostats found" in capsys.readouterr().err


@pytest.mark.parametrize("rc, failed", [(0, False), (5, True)])
def test_on_connect_result_codes(bridge, rc, failed, capsys):
    _, client, _, run = bridge
    run()
    client.subscribed.clear()
    client.on_connect(client, None, {}, rc, None)
    assert (client.subscribed == []) is failed
    assert ("MQTT connect failed" in capsys.readouterr().err) is failed


def test_on_connect_reason_code_objects(bridge):
    # paho 2 passes a ReasonCode object, not an int
    _, client, _, run = bridge
    run()

    class RC:
        def __init__(self, failure):
            self.is_failure = failure
    client.subscribed.clear()
    client.on_connect(client, None, {}, RC(True), None)
    assert client.subscribed == []
    client.on_connect(client, None, {}, RC(False), None)
    assert client.subscribed == ["ecobee/+/set/+"]


def test_make_client_builds_a_real_paho_client():
    client = m._make_client()
    assert hasattr(client, "publish") and hasattr(client, "subscribe")
