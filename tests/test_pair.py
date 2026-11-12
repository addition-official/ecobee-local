"""The pairing wizard (ecobee-pair), with mDNS and HomeKit faked out."""

import asyncio
import json
import os
import socket
import sys

import pytest
from aiohomekit.exceptions import AuthenticationError

import ecobee_local.pair as p


# --------------------------------------------------------------- helpers

@pytest.mark.parametrize("raw, code", [
    ("12345678", "123-45-678"),
    ("1234 5678", "123-45-678"),
    ("123-45-678", "123-45-678"),
    (" 123 45 678 ", "123-45-678"),
    ("1234567", None),
    ("123456789", None),
    ("abcdefgh", None),
    ("", None),
])
def test_normalize_code(raw, code):
    assert p.normalize_code(raw) == code


@pytest.mark.parametrize("text, expected", [
    ("Main Floor", "mainfloor"),
    ("Up-stairs_2!", "up-stairs_2"),
    ("--x--", "x"),
    ("!!!", "fallback"),
    ("", "fallback"),
])
def test_sanitize(text, expected):
    assert p.sanitize(text, "fallback") == expected


def test_next_auto_filename(tmp_path):
    assert p.next_auto_filename(str(tmp_path)) == "pair.json"
    (tmp_path / "pair.json").write_text("{}")
    assert p.next_auto_filename(str(tmp_path)) == "pair1.json"
    (tmp_path / "pair1.json").write_text("{}")
    (tmp_path / "pair3.json").write_text("{}")
    assert p.next_auto_filename(str(tmp_path)) == "pair2.json"


def test_get_app_folder_creates_it(tmp_path):
    target = tmp_path / "creds"
    assert p.get_app_folder(str(target)) == str(target)
    assert target.is_dir()


# ------------------------------------------------------------ discovery

class FakeInfo:
    def __init__(self, ip="192.0.2.10", port=1200, props=None):
        self.addresses = [socket.inet_aton(ip)] if ip else []
        self.port = port
        self.properties = props if props is not None else {
            b"id": b"AA:BB:CC:DD:EE:FF", b"md": b"ecobee3 lite"}


class FakeZC:
    def __init__(self, infos):
        self.infos = infos
        self.closed = False

    def get_service_info(self, type_, name):
        return self.infos.get(name)

    def close(self):
        self.closed = True


def test_listener_collects_and_dedupes():
    name = "Main Floor._hap._tcp.local."
    zc = FakeZC({name: FakeInfo()})
    lst = p.HapListener()
    lst.add_service(zc, "_hap._tcp.local.", name)
    lst.add_service(zc, "_hap._tcp.local.", name)       # seen twice
    assert lst.devices == {"AA:BB:CC:DD:EE:FF": {
        "ip": "192.0.2.10", "port": 1200, "name": "Main Floor",
        "id": "AA:BB:CC:DD:EE:FF", "raw_name": name,
    }}


def test_listener_filters_and_skips_bad_records():
    zc = FakeZC({
        "Upstairs._hap._tcp.local.": FakeInfo(props={b"id": b"11:22"}),
        "Kitchen Light._hap._tcp.local.": FakeInfo(props={b"id": b"33:44"}),
        "No Address._hap._tcp.local.": FakeInfo(ip=None),
    })
    lst = p.HapListener(name_filter="STAIRS")
    for name in zc.infos:
        lst.add_service(zc, "_hap._tcp.local.", name)
    lst.add_service(zc, "_hap._tcp.local.", "Vanished._hap._tcp.local.")
    assert list(lst.devices) == ["11:22"]

    everything = p.HapListener()
    for name in list(zc.infos) + ["Vanished._hap._tcp.local."]:
        everything.add_service(zc, "_hap._tcp.local.", name)
    assert sorted(everything.devices) == ["11:22", "33:44"]


def test_scan_closes_zeroconf(monkeypatch):
    zc = FakeZC({"Main._hap._tcp.local.": FakeInfo()})
    monkeypatch.setattr(p, "Zeroconf", lambda: zc)

    def browser(zeroconf, type_, listener):
        listener.add_service(zeroconf, type_, "Main._hap._tcp.local.")
    monkeypatch.setattr(p, "ServiceBrowser", browser)
    devices = asyncio.run(p.scan("main", timeout=0.5))
    assert [d["name"] for d in devices] == ["Main"]
    assert zc.closed


# -------------------------------------------------------------- pairing

TARGET = {"id": "AA:BB:CC:DD:EE:FF", "ip": "192.0.2.10", "port": 1200,
          "name": "Main Floor"}


@pytest.fixture
def fake_discovery(monkeypatch):
    """Replace aiohomekit's IpDiscovery; `state` controls what pairing does."""
    import aiohomekit.controller.ip.discovery as disc_mod

    state = {"code": None, "pairing_data": {"AccessoryPairingID": "AA",
                                            "iOSDeviceLTSK": "secret"},
             "start_returns": True}

    class Pairing:
        def __init__(self):
            self.pairing_data = state["pairing_data"]
            self.id = "AA"

    class Discovery:
        def __init__(self, controller, service):
            state["service"] = service

        async def async_start_pairing(self, device_id):
            if not state["start_returns"]:
                return None

            async def finish(code):
                state["code"] = code
                if code != "123-45-678":
                    raise AuthenticationError("wrong setup code")
                return Pairing()
            return finish

    class Controller:
        def __init__(self):
            self.discoveries = {}
            self.pairing_data = {"iOSDeviceLTSK": "from-controller"}

    monkeypatch.setattr(disc_mod, "IpDiscovery", Discovery)
    monkeypatch.setattr(p, "Controller", Controller)
    return state


def test_pair_returns_credentials(fake_discovery):
    data = asyncio.run(p.pair(TARGET, "123-45-678"))
    assert data == {"AccessoryPairingID": "AA", "iOSDeviceLTSK": "secret",
                    "AccessoryIP": "192.0.2.10", "AccessoryPort": 1200,
                    "Connection": "IP"}
    assert fake_discovery["service"].address == "192.0.2.10"


def test_pair_wrong_code_raises(fake_discovery):
    with pytest.raises(AuthenticationError):
        asyncio.run(p.pair(TARGET, "000-00-000"))


def test_pair_falls_back_to_controller_data(fake_discovery):
    fake_discovery["pairing_data"] = None
    data = asyncio.run(p.pair(TARGET, "123-45-678"))
    assert data["iOSDeviceLTSK"] == "from-controller"
    assert data["AccessoryPairingID"] == "AA"


def test_pair_handshake_refused(fake_discovery, capsys):
    fake_discovery["start_returns"] = False
    assert asyncio.run(p.pair(TARGET, "123-45-678")) is None


# --------------------------------------------------------------- wizard

def scripted_input(monkeypatch, answers):
    answers = list(answers)

    def fake_input(prompt=""):
        if not answers:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return answers.pop(0)
    monkeypatch.setattr("builtins.input", fake_input)
    return answers


@pytest.fixture
def scans(monkeypatch):
    """Each wizard scan returns the next list from `results`."""
    results = []

    async def fake_scan(name_filter="", timeout=8.0):
        return results.pop(0) if results else []
    monkeypatch.setattr(p, "scan", fake_scan)
    return results


@pytest.fixture
def fake_pair(monkeypatch):
    calls = []

    async def pair(target, code):
        calls.append((target, code))
        if code != "123-45-678":
            raise AuthenticationError("wrong setup code")
        return {"AccessoryPairingID": "AA", "AccessoryIP": target["ip"]}
    monkeypatch.setattr(p, "pair", pair)
    return calls


def test_wizard_saves_pairing(tmp_path, monkeypatch, scans, fake_pair, capsys):
    scans.append([TARGET])
    left = scripted_input(monkeypatch, [
        "main",             # search term
        "1",                # pick
        "",                 # friendly name -> device name
        "",                 # label -> sanitized name
        "",                 # filename -> pair.json
        "1234",             # too short, asked again
        "1234 5678",
    ])
    asyncio.run(p.wizard(str(tmp_path)))
    assert left == []
    saved = json.loads((tmp_path / "pair.json").read_text())
    assert saved == {"AccessoryPairingID": "AA", "AccessoryIP": "192.0.2.10",
                     "_label": "mainfloor", "_name": "Main Floor"}
    assert fake_pair == [(TARGET, "123-45-678")]
    assert "isn't 8 digits" in capsys.readouterr().out


def test_wizard_rescans_until_found(tmp_path, monkeypatch, scans, fake_pair):
    scans.extend([[], [TARGET]])
    scripted_input(monkeypatch, [
        "main", "r",                       # nothing found -> rescan
        "1", "Den", "den", "den", "12345678",
    ])
    asyncio.run(p.wizard(str(tmp_path)))
    saved = json.loads((tmp_path / "den.json").read_text())
    assert saved["_label"] == "den" and saved["_name"] == "Den"


def test_wizard_wrong_code_saves_nothing(tmp_path, monkeypatch, scans,
                                         fake_pair, capsys):
    scans.append([TARGET])
    scripted_input(monkeypatch, ["main", "1", "", "", "", "87654321"])
    asyncio.run(p.wizard(str(tmp_path)))
    assert list(tmp_path.iterdir()) == []
    assert "pairing failed: AuthenticationError" in capsys.readouterr().out


def test_wizard_refuses_to_overwrite(tmp_path, monkeypatch, scans, fake_pair):
    (tmp_path / "pair.json").write_text('{"keep": true}')
    scans.append([TARGET])
    scripted_input(monkeypatch, ["main", "1", "", "", "pair.json", "n"])
    asyncio.run(p.wizard(str(tmp_path)))
    assert json.loads((tmp_path / "pair.json").read_text()) == {"keep": True}
    assert fake_pair == []


def test_wizard_blank_search_needs_confirmation(tmp_path, monkeypatch, scans):
    scripted_input(monkeypatch, ["", "n"])
    asyncio.run(p.wizard(str(tmp_path)))
    assert list(tmp_path.iterdir()) == []


def test_wizard_quit(tmp_path, monkeypatch, scans):
    scans.append([TARGET])
    scripted_input(monkeypatch, ["main", "q"])
    asyncio.run(p.wizard(str(tmp_path)))
    assert list(tmp_path.iterdir()) == []


def test_main_passes_folder(monkeypatch, tmp_path):
    seen = {}

    async def wizard(folder):
        seen["folder"] = folder
    monkeypatch.setattr(p, "wizard", wizard)
    monkeypatch.setattr(sys, "argv", ["ecobee-pair", "--folder",
                                      str(tmp_path)])
    p.main()
    assert seen == {"folder": str(tmp_path)}


# ------------------------------------------------- keeping the keys safe

posix_only = pytest.mark.skipif(sys.platform == "win32",
                                reason="POSIX file permissions")


@posix_only
def test_saved_pairing_is_private(tmp_path, monkeypatch, scans, fake_pair):
    monkeypatch.setattr(p.os, "umask", p.os.umask)   # (real umask 022)
    scans.append([TARGET])
    scripted_input(monkeypatch, ["main", "1", "", "", "", "12345678"])
    asyncio.run(p.wizard(str(tmp_path)))
    assert (tmp_path / "pair.json").stat().st_mode & 0o077 == 0
    assert [f.name for f in tmp_path.iterdir()] == ["pair.json"]  # no temp


@posix_only
def test_default_folder_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(p.sys, "platform", "linux")
    folder = p.get_app_folder()
    assert oct(os.stat(folder).st_mode & 0o777) == "0o700"
    os.chmod(folder, 0o755)                  # made by an older version
    p.get_app_folder()
    assert oct(os.stat(folder).st_mode & 0o777) == "0o700"


@posix_only
def test_custom_folder_permissions_left_alone(tmp_path):
    folder = tmp_path / "myapp"
    folder.mkdir(mode=0o755)
    os.chmod(folder, 0o755)
    p.get_app_folder(str(folder))
    assert oct(os.stat(folder).st_mode & 0o777) == "0o755"


def test_overwrite_replaces_file(tmp_path, monkeypatch, scans, fake_pair):
    (tmp_path / "pair.json").write_text('{"old": true}')
    scans.append([TARGET])
    scripted_input(monkeypatch, ["main", "1", "", "", "pair.json", "y",
                                 "12345678"])
    asyncio.run(p.wizard(str(tmp_path)))
    assert json.loads((tmp_path / "pair.json").read_text())[
        "AccessoryPairingID"] == "AA"


def test_failed_save_prints_the_keys(tmp_path, monkeypatch, scans,
                                     fake_pair, capsys):
    scans.append([TARGET])
    scripted_input(monkeypatch, ["main", "1", "", "", "", "12345678"])

    def disk_full(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(p.os, "replace", disk_full)
    asyncio.run(p.wizard(str(tmp_path)))
    out = capsys.readouterr().out
    assert "could not save" in out and "No space left" in out
    assert '"AccessoryPairingID": "AA"' in out     # keys not lost
    assert list(tmp_path.iterdir()) == []          # no half-written file


@pytest.mark.parametrize("answers, saved_as", [
    (["Main.JSON"], "Main.json"),
    (["den"], "den.json"),
    (["sub/x", "..\\x", ".hidden", "ok"], "ok.json"),   # asked again
])
def test_filename_checked_before_pairing(tmp_path, monkeypatch, scans,
                                         fake_pair, capsys, answers,
                                         saved_as):
    scans.append([TARGET])
    scripted_input(monkeypatch,
                   ["main", "1", "", "", *answers, "12345678"])
    asyncio.run(p.wizard(str(tmp_path)))
    assert [f.name for f in tmp_path.iterdir()] == [saved_as]
    if len(answers) > 1:
        assert capsys.readouterr().out.count("just a file name") == 3


@pytest.mark.parametrize("exc, code", [(EOFError, 1),
                                       (KeyboardInterrupt, 130)])
def test_quitting_at_a_prompt(tmp_path, monkeypatch, capsys, exc, code):
    def quit_now(prompt=""):
        raise exc
    monkeypatch.setattr("builtins.input", quit_now)
    monkeypatch.setattr(sys, "argv", ["ecobee-pair", "--folder",
                                      str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        p.main()
    assert e.value.code == code
    assert "aborted" in capsys.readouterr().out


# ------------------------------------------------ odd mDNS / code input

def test_listener_survives_bad_records():
    zc = FakeZC({
        "Bad Bytes._hap._tcp.local.": FakeInfo(
            props={b"id": b"\xff\xfe", b"md": b"\xc3"}),
        "No Id A._hap._tcp.local.": FakeInfo(props={b"md": b"x"}),
        "No Id B._hap._tcp.local.": FakeInfo(props={}),
    })
    lst = p.HapListener()
    for name in zc.infos:
        lst.add_service(zc, "_hap._tcp.local.", name)
    names = sorted(d["name"] for d in lst.devices.values())
    assert names == ["Bad Bytes", "No Id A", "No Id B"]


def test_listener_never_raises():
    class Exploding:
        def get_service_info(self, type_, name):
            raise RuntimeError("zeroconf hiccup")
    lst = p.HapListener()
    lst.add_service(Exploding(), "_hap._tcp.local.", "X._hap._tcp.local.")
    assert lst.devices == {}


def test_normalize_code_ascii_digits_only():
    assert p.normalize_code("²²²²²²²²") is None
    assert p.normalize_code("１２３４５６７８") is None
