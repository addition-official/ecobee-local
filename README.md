[![PyPI](https://img.shields.io/pypi/v/ecobee-local?cacheSeconds=300)](https://pypi.org/project/ecobee-local/)

# Local Ecobee Control (HomeKit)

Control your Ecobee thermostat from Python over your local network, with **no cloud, no OAuth, no ecobee developer key, and no Home Assistant**. It talks to the thermostat directly using Apple's HomeKit Accessory Protocol (HAP) via [`aiohomekit`](https://github.com/Jc2k/aiohomekit).

Pair once, then read and control the thermostat from a small control-panel GUI, a command line, or as a Python library you drop into your own project.

Works with any HomeKit-capable Ecobee. Developed and tested against an **Ecobee3 lite** and an **Ecobee Smart Premium** (including its remote SmartSensor).

## What it can do

- Read current temperature, target, mode, running state, humidity, and fan state
- Set target temperature, mode (heat / cool / auto / off), heat/cool thresholds, and fan (auto / on)
- Read remote SmartSensors and the built-in occupancy sensor (temperature, occupancy, battery)
- Set target humidity on models that have a humidifier/dehumidifier (e.g. Smart Premium)
- Read and switch comfort profiles (Home / Sleep / Away) locally on supported models (see notes; the ecobee3 lite does not support this correctly)
- Fahrenheit or Celsius display, with exact half-degree Celsius setting
- A live GUI that auto-updates, or a scriptable CLI, or an importable library

## Install

    pip install ecobee-local

Or clone this repo and `pip install .` to build the exact package that's published to PyPI.

## Requirements

- Python 3.10+
- Dependencies `aiohomekit` and `zeroconf` (installed automatically by pip, or via `pip install -r requirements.txt`)
- Tkinter for the GUI. It ships with Python on Windows and macOS. On some Linux distros it's a separate package: `sudo apt install python3-tk`.

## Setup

### 1. Free the thermostat for pairing

HomeKit accessories can only be paired to one "home" at a time. **If your Ecobee is already added to Apple Home, it won't be discoverable for pairing until you reset its HomeKit info.** On the thermostat: **Menu → Settings → HomeKit → Reset HomeKit**. (This removes it from Apple Home. If you don't use Apple Home, there's nothing to do.)

### 2. Pair

```
ecobee-pair
```

(Cloned the repo instead of installing? Use `python -m ecobee_local.pair`.)

The wizard will:
- scan your network and show a numbered list of HomeKit devices (or filter by name),
- let you pick the thermostat, give it a short label and a friendly name,
- ask for the 8-digit HomeKit setup code shown on the thermostat screen (type it any way: `12345678`, `1234 5678`, or `123-45-678` all work),
- save the credentials to a file.

Run it again for each additional thermostat. Files are saved to a per-user folder (`%LOCALAPPDATA%\ecobee-local` on Windows, `~/ecobee-local` on macOS/Linux) by default; use `--folder PATH` to change it.

> **Keep your pairing files private.** Each one contains long-term keys that grant full control of that thermostat on your network. Don't commit or share them. The included `.gitignore` already excludes them.

### 3. Run

```
ecobee-hub
```

(Cloned instead of installed? Use `python -m ecobee_local.controller`.)

With no arguments this opens the **control window** (one card per thermostat). On a headless machine with no display it automatically falls back to a background service instead.

## Command line

Everything the GUI does is also scriptable. All `--set-*` actions require `--label`.

```
ecobee-hub                                            # open the GUI
ecobee-hub --status                                   # print every thermostat's status and exit
ecobee-hub --status --label main                      # just one
ecobee-hub --watch                                    # print a line whenever something changes
ecobee-hub --dump                                     # list every characteristic each thermostat exposes
ecobee-hub --dump --raw                               # same, without truncating long values
ecobee-hub --label main --set-temp 72
ecobee-hub --label main --set-temp-c 22.5             # Celsius (also --set-heat-c, --set-cool-c)
ecobee-hub --label main --set-mode cool               # off/heat/cool/auto (or 0-3)
ecobee-hub --label main --set-heat 68 --set-cool 75   # auto-mode range
ecobee-hub --label main --set-fan on
ecobee-hub --label main --set-comfort away            # home/sleep/away/hold (supported models)
ecobee-hub --headless                                 # run as a background service (no GUI)
ecobee-hub --debug                                    # print connection/subscription events
ecobee-hub --folder PATH                              # use a different credentials folder
```

(If you cloned rather than installed, replace `ecobee-hub` with `python -m ecobee_local.controller`.)

`--folder` can point at a folder that has other JSON files too (for example your own app's folder): only files that hold HomeKit pairings are used.

`--dump` is the tool for discovering what your specific model exposes: it prints every accessory, service, and characteristic with its UUID, current value, permissions, and range.

## Use it as a library

```python
from ecobee_local import EcobeeController

ec = EcobeeController.from_folder()   # auto-discovers every pairing file
ec.start()
ec.wait_ready()                       # wait for the first readings (optional)

print(ec.get_status("main"))
# {'current_temp': 78, 'target_temp': 75, 'mode': 2, 'humidity': 57,
#  'setpoint_kind': 'single', 'display_target': 75, 'sensors': [...], ...}

ec.set_temp("main", 72)                       # Fahrenheit
ec.set_temp_c("main", 22.5)                   # Celsius, exact half-degree
ec.set_mode("main", EcobeeController.MODE_COOL)
ec.set_heat_threshold("main", 68)             # auto mode
ec.set_cool_threshold("main", 75)
ec.set_fan("main", auto=True)

# comfort profiles (works on some models, not the ecobee3 lite; see notes)
print(ec.get_comfort_mode("main"))            # "home" / "sleep" / "away" / "hold"
ec.set_comfort_mode("main", "away")           # switch locally, no cloud
# the first comfort call prints a one-time firmware-caveat note to stderr;
# construct EcobeeController(..., comfort_warning=False) to silence it

ec.stop()                                     # cleanly close connections
```

Or let a `with` block start it, wait for the first readings, and always close the connections at the end, even if your code raises an error:

```python
with EcobeeController.from_folder() as ec:
    ec.set_temp("main", 72)
```

To react to changes as they happen (from the thermostat, the ecobee app, a schedule, a sensor, or your own commands) instead of polling:

```python
def changed(label, status):
    print(label, status["current_temp"], status["target_temp"])

remove = ec.on_change(changed)   # call remove() to stop
```

Callbacks run on the controller's background thread: keep them quick, and don't call the controller's commands (`set_temp` etc.) from inside one. Those return an error there; hand the work to another thread instead.

`get_status()` returns a dict per thermostat. A few fields are computed for convenience:

- `setpoint_kind` is `"single"` in heat/cool/off and `"range"` in auto.
- `display_target` is the value(s) a UI should show: one number in single-setpoint modes, `{"heat": ..., "cool": ...}` in auto.
- In auto mode `target_temp` is `None`, because a single target is meaningless there - the thermostat runs off the heat/cool thresholds. Read `heat_threshold` / `cool_threshold` (or `display_target`) instead.
- Temperatures are in Fahrenheit; the raw Celsius the device reports is also included as `*_c` (e.g. `target_temp_c`).
- Fields a given model doesn't support read as `"--"` rather than a misleading `0`.

## How it works

`ecobee_local/pair.py` performs the one-time HomeKit handshake and saves credentials. `ecobee_local/controller.py` uses those credentials to keep a persistent local connection per thermostat, subscribes for push updates (the device notifies it on change), and caches readings so the GUI/CLI never block on the network. Characteristics are located by their standard HomeKit type inside the thermostat's Thermostat service, so the same code works across different Ecobee models without hardcoding.

## Notes and limitations

- **This is local HomeKit control.** It reads and controls whatever the thermostat exposes over HomeKit.
- **Air quality is not available.** The Smart Premium has an air-quality sensor, but Ecobee does **not** publish air quality (VOC / CO₂) over HomeKit - only through their cloud. So no local tool, including this one, Apple Home, or Home Assistant, can read it over HomeKit.
- **The Ecobee app and HomeKit can briefly disagree.** Ecobee drives its own screen and app from its cloud/comfort-profile system. If you change settings in the **ecobee app**, the standard HomeKit state this tool reads may show a different or stale value (for example, showing "Auto" with an odd range) until the setting is next changed **through HomeKit**. Changes made with this tool are always consistent. This is an Ecobee behavior, not a bug in this project.
- **Scheduling isn't a HomeKit feature.** HomeKit has no concept of a weekly schedule; Ecobee's comfort schedules live in its own system. You can build time-based automation on top of this library, but it can't read/write Ecobee's schedules over HomeKit.
- **Comfort profiles work on some models, not all.** On supported models you can read and switch Home / Sleep / Away locally. **This does not work on the ecobee3 lite, which has a known ecobee firmware bug that always reports the same comfort value regardless of the actual setting.** For this reason the GUI shows comfort controls but lets you turn them off per-thermostat in Settings. Custom comfort profiles (e.g. a "Gym" profile) aren't individually addressable over HomeKit; they all report as "hold", and you can't create profiles from here (that's an ecobee-app action). The comfort UUIDs are vendor-specific and undocumented by ecobee, but verified on hardware and corroborated by the Home Assistant project.

## Optional: MQTT bridge

`ecobee_local/mqtt.py` is an optional MQTT bridge that mirrors your thermostats
onto an MQTT broker, so other tools (Node-RED, dashboards, custom scripts, Home
Assistant, etc.) can read state and send commands over MQTT. It's experimental,
and it uses this library under the hood.

Install it with the optional `mqtt` extra (pulls in `paho-mqtt`), and you'll
also need a running MQTT broker such as [Mosquitto](https://mosquitto.org/):

    pip install "ecobee-local[mqtt]"
    winget install --id=EclipseFoundation.Mosquitto -e   # Windows, if you need a broker

Run it:

    ecobee-mqtt                       # broker at localhost:1883
    ecobee-mqtt --host 192.168.1.50   # broker elsewhere

(Cloned instead of installed? `pip install ".[mqtt]"` from the repo, then
`python -m ecobee_local.mqtt`.)

It publishes state to `ecobee/<label>/<field>` (temperature, target, mode,
humidity, fan, comfort, plus a full JSON blob at `ecobee/<label>/state`) and
listens for commands on `ecobee/<label>/set/<thing>`:

    ecobee/<label>/set/target     72        (Fahrenheit)
    ecobee/<label>/set/target_c   22.5      (Celsius)
    ecobee/<label>/set/mode       cool      (off/heat/cool/auto)
    ecobee/<label>/set/fan        auto      (auto/on)
    ecobee/<label>/set/humidity   40
    ecobee/<label>/set/comfort    away      (home/sleep/away/hold)

Commands must be sent as normal (not retained) messages; retained ones are
ignored, because the broker would replay them every time the bridge reconnects.

If your broker needs a login, use `--username` and put the password in the
`ECOBEE_MQTT_PASSWORD` environment variable rather than `--password`, so it
doesn't show up in the process list. Plain MQTT isn't encrypted: anyone who can
publish to your broker can control the thermostat, so keep the broker on your
own network and require a login if others share it.

## Project layout

```
ecobee_local/
    __init__.py       package entry, exports EcobeeController
    controller.py     the library, CLI, and GUI
    pair.py           the pairing wizard
    mqtt.py           optional MQTT bridge (needs the [mqtt] extra)
tests/                pytest suite (no thermostat or network needed)
pyproject.toml        packaging / build config
requirements.txt      dependencies
LICENSE               MIT
```

Clone and `pip install .` to build exactly what's published to PyPI.

## Running the tests

The tests need no thermostat, MQTT broker or network:

    pip install ".[mqtt]" pytest
    pytest

They also run on GitHub for every pull request and push to `main`.
Releases are published to PyPI automatically when a GitHub release is made
(see [CHANGELOG.md](CHANGELOG.md) for what changed in each version).

## A note on how this was built

The hard part of this project, figuring out how to control an Ecobee locally
over HomeKit at all, was months of my own trial and error. The pairing wizard
(`ecobee_local/pair.py`) I wrote entirely myself.

I used AI assistance to help build out and refactor the controller, CLI, and
GUI, and to write this README. The core discovery, the debugging direction,
the testing on real hardware, and the design calls are mine.

## Acknowledgements

Built on [`aiohomekit`](https://github.com/Jc2k/aiohomekit), the same HomeKit client that powers Home Assistant's HomeKit Controller integration.
