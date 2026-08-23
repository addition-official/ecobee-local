"""
pair_ecobee.py
==============
One-time pairing wizard for controlling an Ecobee thermostat locally over
Apple HomeKit (HAP), with no cloud, no OAuth, and no ecobee developer key.

It scans your local network for the thermostat, walks you through the HomeKit
pairing handshake, and writes the resulting credentials to a JSON file in the
smartdash app folder. ecobee_controller.py then reads those files.

Cross-platform: Windows, macOS, Linux.

Each saved file also stores two metadata keys the controller understands:
    "_label"  short id used in code / API calls  (e.g. "main")
    "_name"   friendly display name              (e.g. "Main Floor")

You can pair as many thermostats as you like. Filenames can be anything;
by default the wizard auto-numbers as pair.json, pair1.json, pair2.json, ...
so they all live side by side and the controller picks them all up.

IMPORTANT: the generated files contain long-term pairing keys. Treat them
like passwords. Do NOT commit or share them.

Usage
-----
    python pair_ecobee.py
"""

import asyncio
import glob
import json
import os
import re
import socket
import sys

from zeroconf import Zeroconf, ServiceBrowser
from aiohomekit.controller import Controller
from aiohomekit.model import Categories
from aiohomekit.model.status_flags import StatusFlags
from aiohomekit.model.feature_flags import FeatureFlags

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ----------------------------------------------------------------------------
# storage location
# ----------------------------------------------------------------------------
# Folder name where pairing files are saved. Keep this in sync with
# ecobee_controller.APP_NAME. Override with the --folder flag if needed.
APP_NAME = "ecobee-local"


def get_app_folder(app_folder=None):
    """Return the per-user credentials folder, creating it if needed."""
    if app_folder:
        os.makedirs(app_folder, exist_ok=True)
        return app_folder
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.path.expanduser("~")
    folder = os.path.join(base, APP_NAME)
    os.makedirs(folder, exist_ok=True)
    return folder


def next_auto_filename(folder):
    """Return the next free pairN.json name: pair.json, pair1.json, ..."""
    if not os.path.exists(os.path.join(folder, "pair.json")):
        return "pair.json"
    n = 1
    while os.path.exists(os.path.join(folder, f"pair{n}.json")):
        n += 1
    return f"pair{n}.json"


def sanitize(text, fallback):
    """Make a filesystem/label-safe token."""
    cleaned = "".join(c for c in text if c.isalnum() or c in ("-", "_")).strip("-_")
    return cleaned.lower() or fallback


def normalize_code(raw):
    """
    Accept a HomeKit setup code in any human format and return it as the
    XXX-XX-XXX form aiohomekit requires. The thermostat screen shows the
    digits without dashes (often as two stacked blocks), so we let the user
    type '12345678', '1234 5678', '123-45-678', etc.

    Returns the dashed string, or None if it isn't exactly 8 digits.
    """
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) != 8:
        return None
    return f"{digits[0:3]}-{digits[3:5]}-{digits[5:8]}"


# ----------------------------------------------------------------------------
# mDNS discovery: collect every HomeKit device, optionally filtered by name
# ----------------------------------------------------------------------------
class HapListener:
    """Collects all _hap._tcp devices; optional case-insensitive name filter."""

    def __init__(self, name_filter=""):
        self.name_filter = (name_filter or "").lower()
        self.devices = {}  # keyed by device id to dedupe

    def remove_service(self, zc, type_, name):
        pass

    def update_service(self, zc, type_, name):
        pass

    def add_service(self, zc, type_, name):
        if self.name_filter and self.name_filter not in name.lower():
            return
        info = zc.get_service_info(type_, name)
        if not info or not info.addresses:
            return
        ip = socket.inet_ntoa(info.addresses[0])
        port = info.port
        props = {
            (k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v)
            for k, v in info.properties.items()
        }
        device_id = props.get("id", "00:00:00:00:00:00")
        # dedupe on device id; clean up the noisy mDNS suffix for display
        display = name.replace("._hap._tcp.local.", "")
        self.devices[device_id] = {
            "ip": ip, "port": port, "name": display,
            "id": device_id, "raw_name": name,
        }


async def scan(name_filter="", timeout=8.0):
    """Scan the LAN and return a list of device dicts (deduped by id)."""
    zeroconf = Zeroconf()
    listener = HapListener(name_filter)
    ServiceBrowser(zeroconf, "_hap._tcp.local.", listener)
    label = f"'{name_filter}'" if name_filter else "all HomeKit devices"
    print(f"  scanning network for {label} (up to {int(timeout)}s)...")
    try:
        for _ in range(int(timeout / 0.5)):
            await asyncio.sleep(0.5)
    finally:
        zeroconf.close()
    return list(listener.devices.values())


# ----------------------------------------------------------------------------
# pairing handshake
# ----------------------------------------------------------------------------
async def pair(target, code):
    from aiohomekit.controller.ip.discovery import IpDiscovery
    from aiohomekit.zeroconf import HomeKitService

    device_id = target["id"]
    device_ip = target["ip"]
    device_port = target["port"]
    device_name = target["name"]

    hk_service = HomeKitService(
        name=device_name, id=device_id, model="ecobee",
        feature_flags=FeatureFlags(1), status_flags=StatusFlags(1),
        config_num=1, state_num=1, category=Categories(9),
        protocol_version="1.1", type="_hap._tcp.local.",
        address=device_ip, addresses=[device_ip], port=device_port,
    )

    controller = Controller()
    discovery = IpDiscovery(controller, hk_service)
    controller.discoveries[device_id] = discovery

    print("  initiating handshake...")
    finish_pairing = await discovery.async_start_pairing(device_id)
    if not finish_pairing:
        print("  ! start_pairing returned nothing")
        return None

    print("  handshake accepted, verifying code...")
    pairing = await finish_pairing(code)
    print("  paired successfully")

    # Preferred: copy the pairing data aiohomekit produced verbatim. This has
    # the correct key names for whatever version is installed
    # (AccessoryPairingID, AccessoryLTPK, iOSPairingId, iOSDeviceLTSK,
    # iOSDeviceLTPK, ...).
    pairing_data = None
    for attr in ("pairing_data", "_pairing_data"):
        data = getattr(pairing, attr, None)
        if data:
            pairing_data = dict(data)
            break

    if pairing_data is None:
        # Fallback: pull straight from the controller's pairing record. Copy
        # every key it has rather than hand-picking, so we never drop the
        # iOSDeviceLTSK private key (without it the file can't authenticate).
        print("  auto-extract unavailable, assembling credentials manually...")
        src = getattr(controller, "pairing_data", {}) or {}
        pairing_data = dict(src)
        pairing_data.setdefault("AccessoryPairingID", getattr(pairing, "id", None))
        pairing_data.setdefault("AccessoryLTPK", getattr(pairing, "owner_key", None))

    pairing_data["AccessoryIP"] = device_ip
    pairing_data["AccessoryPort"] = device_port
    pairing_data["Connection"] = "IP"
    return pairing_data


# ----------------------------------------------------------------------------
# interactive wizard
# ----------------------------------------------------------------------------
async def wizard(app_folder=None):
    print()
    print("=== Ecobee HomeKit pairing wizard ===")
    print("Pairs one thermostat and saves its credentials locally.")
    print("Run it again for each additional thermostat.")
    print()

    app_folder = get_app_folder(app_folder)
    existing = sorted(os.path.basename(p)
                      for p in glob.glob(os.path.join(app_folder, "*.json")))
    if existing:
        print(f"Existing pairing files in {app_folder}:")
        for e in existing:
            print(f"    {e}")
        print()

    # --- pick the device ---
    # search by term, or list everything (with a flood warning), then choose
    # from a numbered list. Choosing by index avoids ambiguity when two
    # thermostats share a similar name (e.g. two "Upstairs"). The whole thing
    # loops so you can rescan if the thermostat was offline / not yet in
    # pairing mode without restarting the wizard.
    print("How do you want to find the thermostat?")
    print("  - enter a search term to match part of its name (e.g. Upstairs)")
    print("  - or leave blank to list ALL HomeKit devices on the network")
    print("    (warning: on a busy network this can be a long list)")
    name_filter = input("Search term (blank = show all): ").strip()

    if not name_filter:
        confirm = input(
            "List ALL HomeKit devices? This may flood the console. [y/N]: "
        ).strip().lower()
        if confirm != "y":
            print("Aborting. Re-run and enter a search term.")
            return

    target = None
    while target is None:
        devices = await scan(name_filter)

        if not devices:
            print()
            which = f"matching '{name_filter}'" if name_filter else "on the network"
            print(f"No HomeKit devices found {which}.")
            print("Tips:")
            print("  1. Confirm the thermostat is on the same network and powered on.")
            print("  2. Open the HomeKit pairing screen on the thermostat now.")
            print("  3. If it was previously paired to Apple Home, reset HomeKit:")
            print("     Menu > Settings > HomeKit > Reset, then try again.")
            print()
            ans = input(
                "[r] rescan   [s] change search term   [q] quit: "
            ).strip().lower()
            if ans == "q":
                return
            if ans == "s":
                name_filter = input("New search term (blank = show all): ").strip()
            # anything else (including 'r' or Enter) just rescans
            continue

        # show numbered list
        devices.sort(key=lambda d: d["name"].lower())
        print()
        print(f"Found {len(devices)} device(s):")
        for i, d in enumerate(devices, 1):
            print(f"  [{i}] {d['name']}")
            print(f"      id {d['id']}  ({d['ip']}:{d['port']})")
        print("  [r] rescan   [s] change search term   [q] quit")

        if len(devices) == 1:
            pick = input("Enter 1 to select it, or r/s/q: ").strip().lower()
        else:
            pick = input(f"Pick a device [1-{len(devices)}], or r/s/q: ").strip().lower()

        if pick == "q":
            return
        if pick == "s":
            name_filter = input("New search term (blank = show all): ").strip()
            continue
        if pick == "r" or pick == "":
            continue  # rescan
        if not pick.isdigit() or not (1 <= int(pick) <= len(devices)):
            print("  not a valid choice; rescanning...")
            continue

        target = devices[int(pick) - 1]

    print(f"  selected: {target['name']} ({target['id']})")

    # friendly display name (what shows in a UI); defaults to the device name
    friendly = input(
        f"Friendly display name [{target['name']}]: "
    ).strip() or target["name"]

    # short label used in code; defaults to a sanitized version of the name
    default_label = sanitize(friendly, "thermo")
    label = sanitize(
        input(f"Short label for code/API [{default_label}]: ").strip(),
        default_label,
    )

    # filename; default auto-numbers pair.json, pair1.json, ...
    auto_name = next_auto_filename(app_folder)
    fn_in = input(f"Filename to save as [{auto_name}]: ").strip() or auto_name
    if not fn_in.lower().endswith(".json"):
        fn_in += ".json"
    target_file = os.path.join(app_folder, fn_in)
    if os.path.exists(target_file):
        if input(f"  {fn_in} exists. Overwrite? [y/N]: ").strip().lower() != "y":
            print("Aborting so the existing file is not clobbered.")
            return

    print()
    print("On the thermostat, open the HomeKit pairing screen so the setup")
    print("code is visible. It's 8 digits, usually shown as two blocks with")
    print("no dashes. You can type it any way: 12345678, 1234 5678, or")
    print("123-45-678 all work.")
    code = None
    while code is None:
        raw = input("Enter the 8-digit setup code: ").strip()
        code = normalize_code(raw)
        if code is None:
            print("  that isn't 8 digits; try again (or Ctrl+C to quit).")
    print(f"  using code {code}")


    try:
        pairing_data = await pair(target, code)
    except Exception as e:
        print(f"  ! pairing failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return

    if not pairing_data:
        return

    # attach metadata the controller reads
    pairing_data["_label"] = label
    pairing_data["_name"] = friendly

    with open(target_file, "w") as f:
        json.dump(pairing_data, f, indent=4)

    print()
    print(f"Saved '{friendly}' (label '{label}') to: {target_file}")
    print("Keep this file private; it contains long-term pairing keys.")
    print()
    print("ecobee_controller.py will auto-discover it via from_folder().")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Ecobee HomeKit pairing wizard")
    ap.add_argument(
        "--folder", default=None,
        help="where to save pairing files (default: per-user ecobee-local folder)",
    )
    cli = ap.parse_args()
    asyncio.run(wizard(cli.folder))
