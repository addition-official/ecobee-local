# Changelog

What changed in each release on [PyPI](https://pypi.org/project/ecobee-local/). Work on `main` that isn't on PyPI yet goes under **Unreleased** at the top.

## Unreleased

### Added
- Optional MQTT bridge: `pip install "ecobee-local[mqtt]"`, then run `ecobee-mqtt`. Publishes each thermostat's state to `ecobee/<label>/...` and accepts commands on `ecobee/<label>/set/<thing>`.
- Test suite (`pytest`) that runs without a thermostat, broker or network, run by GitHub Actions on Python 3.10 and 3.15.
- `wait_ready()`: after `start()`, waits until every thermostat has connected (or failed to), so the first `get_status()` has real readings.
- `ecobee-hub` commands no longer wait a fixed 6 seconds before acting; they continue as soon as the thermostats are connected, usually in a second or two.
- `ecobee-hub --status` prints each thermostat's status and exits (add `--label` for just one). Before, seeing the status meant running `--headless` and pressing Ctrl+C.
- `ecobee-hub --set-mode` accepts names: `off`, `heat`, `cool`, `auto`. The numbers 0-3 still work.
- Releases are published to PyPI from GitHub Actions with trusted publishing. The sdist now includes the tests and this changelog.

### Fixed
- `ecobee-hub --label` with a label that doesn't exist crashed with a traceback. It now says so and lists the labels it found.
- `ecobee-hub --set-temp` (and the other `--set-*` options) without `--label` silently did nothing. It now says `--label` is needed.
- `--folder` (and `EcobeeController.from_folder()`) treated every `.json` file in the folder as a thermostat, so pointing it at an app folder with other JSON files (config, caches, ...) listed each of them as an offline thermostat. Files that aren't HomeKit pairings are now skipped.
- If the thermostat couldn't read one value (it reports "busy" now and then), or sent one that can't be used (empty, text, infinity), the whole thermostat went offline and reconnected on every poll. Now it stays online and that value keeps its last reading, or `--` if it has never been read.
- When the thermostat rejected a change (for example a temperature outside its range), the setters still returned `{"ok": True}` and showed the rejected value until the next poll. They now return `{"ok": False, "error": ...}` with the thermostat's reason and leave the status unchanged.
- `ecobee-mqtt`: some bad commands raised an error inside the MQTT client, which could stop the bridge from receiving further commands: a thermostat label that doesn't exist (e.g. `ecobee/typo/set/target`), a number like `inf`, or any unexpected error while applying the command. These are now logged and ignored.
- A command or refresh sent while the controller was still making its first connection could open a second connection and leak the first, or fail with "not connected". It now waits for the first connection.
- A command that didn't finish within its `timeout` raised `TimeoutError` instead of returning `{"ok": False, "error": ...}` as documented, and the thermostat could still apply the change afterwards. It now returns the error and the change is cancelled.
- `--dump` left a HomeKit connection open (and a socket leaking) when the thermostat didn't answer. The same leak was fixed for normal connections in 1.1.1.

### Changed
- One-shot `ecobee-hub` commands close their thermostat connections before exiting, freeing the thermostat's limited HomeKit connection slots right away.
- Messages, help text and comments that still named the old scripts (`pair_ecobee.py`, `ecobee_controller.py`, `ecobee_mqtt.py`) now name the `ecobee-pair`, `ecobee-hub` and `ecobee-mqtt` commands.
- "No pairing files found" now says which folder was searched and points to `ecobee-pair` and `--folder` (it named the old `pair_ecobee.py`).
- Requires Python 3.10+. `aiohomekit` 3.0 and later already required it, so installs on 3.9 were failing anyway.
- Removed the duplicate `ecobee_mqtt.py` from the repository root. It was never part of the package; use `ecobee-mqtt` or `python -m ecobee_local.mqtt`.

## 1.1.1

### Fixed
- When a thermostat was unreachable, each reconnect attempt left a HomeKit connection open that kept retrying in the background, leaking a socket every 30 seconds until the program ran out of file descriptors.

## 1.1.0

### Added
- Comfort profiles (home, sleep, away, hold) on models that support them (Smart/Premium, not the ecobee3 lite): `get_comfort_mode()`, `set_comfort_mode()` and `ecobee-hub --set-comfort`.
- A one-time note that comfort control depends on the thermostat's firmware. Turn it off with `comfort_warning=False`.
- A just-set comfort profile isn't overwritten by the thermostat briefly reporting the old one.

## 1.0.0

First release on PyPI.

- `EcobeeController`: read status, remote sensors and occupancy; set temperature, mode, heat/cool thresholds, fan, target humidity and display units, in Fahrenheit or Celsius. Push updates with a polling fallback.
- `ecobee-pair`: pairing wizard that finds the thermostat on the network and saves its HomeKit credentials.
- `ecobee-hub`: control window, `--headless` service mode, one-shot `--set-*` actions and `--dump` to list everything a thermostat exposes.
