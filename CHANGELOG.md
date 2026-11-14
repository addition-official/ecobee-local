# Changelog

What changed in each release on [PyPI](https://pypi.org/project/ecobee-local/). Work on `main` that isn't on PyPI yet goes under **Unreleased** at the top.

## Unreleased

### Added
- `get_status()` has new fields per thermostat:
  - `modes`: the modes the thermostat says it allows over HomeKit, e.g. `["off", "heat", "cool"]` (re-read on every reconnect; all four if it publishes nothing). Ecobees always publish all four, even with Auto Heat/Cool disabled, and accept auto over HomeKit anyway (tested on real hardware).
  - `limits` / `limits_c`: the allowed `[min, max]` for the target and the heat/cool thresholds, in °F / °C, as published by the thermostat.
  - `offline_reason`: `"unpaired"` (re-pair it) or `"unreachable"` (network, power), `None` while online.
  - `updated_at`: when the last reading arrived (epoch seconds).
- `set_mode()` refuses a mode the thermostat doesn't allow, with e.g. `"auto mode is turned off on this thermostat"`, instead of sending it.
- Choose the allowed modes yourself, per thermostat: `EcobeeController(..., modes={"main": ["off", "heat", "cool"]})` or `"_modes": [...]` in the pairing file (the constructor wins if both are set). Needed on Ecobees, which don't report Auto Heat/Cool over HomeKit. `get_status()["modes"]`, `set_mode()`, `--status` and the control window follow it, and `ecobee-pair` keeps `"_modes"` when re-pairing over the same file.
- `set_range(label, heat, cool)` and `ecobee-hub --set-range HEAT COOL` set both auto-mode thresholds in one request, and return what the thermostat actually kept. Ecobees accept any gap over HomeKit (tested down to 0.5°C in auto mode).
- `--status` shows the new fields, `--watch` reports changes to `modes` and `offline_reason`, `--dump` has a VALID column, and the control window only offers the modes the thermostat allows.

### Fixed
- `--dump` could show generic HomeKit default MIN/MAX values that the thermostat never published (filled in by aiohomekit). It now shows only what the thermostat itself publishes.

## 1.2.0

### Added
- Optional MQTT bridge: `pip install "ecobee-local[mqtt]"`, then run `ecobee-mqtt`. Publishes each thermostat's state to `ecobee/<label>/...` and accepts commands on `ecobee/<label>/set/<thing>`.
- Test suite (`pytest`) that runs without a thermostat, broker or network, run by GitHub Actions on Python 3.10 and 3.15.
- `wait_ready()`: after `start()`, waits until every thermostat has connected (or failed to), so the first `get_status()` has real readings.
- `ecobee-hub` commands no longer wait a fixed 6 seconds before acting; they continue as soon as the thermostats are connected, usually in a second or two.
- `ecobee-hub --status` prints each thermostat's status and exits (add `--label` for just one). Before, seeing the status meant running `--headless` and pressing Ctrl+C.
- `ecobee-hub --set-mode` accepts names: `off`, `heat`, `cool`, `auto`. The numbers 0-3 still work.
- `ecobee-mqtt` reads the broker password from `ECOBEE_MQTT_PASSWORD` when `--password` isn't given, keeping it out of the process list.
- `ecobee-hub --set-temp-c`, `--set-heat-c` and `--set-cool-c` set temperatures in Celsius (0.5° steps), like the library's `set_temp_c()` and friends.
- `with EcobeeController.from_folder() as ec:` connects (waiting for the first readings) and always closes the connections at the end of the block, even if your code raises an error.
- `ec.on_change(callback)`: `callback(label, status)` is called whenever a thermostat's status changes, from anywhere (the thermostat, the ecobee app, a schedule, a sensor, going offline, or your own commands). Returns a function that removes the callback.
- `ecobee-hub --watch` prints a line whenever something changes, e.g. `14:02:11 main (Main Floor): target_temp 69 -> 70` (add `--label` for just one thermostat).
- `set_mode()` also accepts a name: `"off"`, `"heat"`, `"cool"` or `"auto"`.
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
- A connection attempt that was cancelled (by `stop()`, or by a command that timed out) left that half-open connection running, using up one of the thermostat's few HomeKit connection slots. It's now closed.
- After `stop()`, commands waited out their whole timeout (12 seconds by default) before failing. They now fail straight away with "controller not started".
- Calling `start()` again after `stop()` on the same controller sent commands to the old, stopped loop, so they timed out.
- A refresh triggered by a push update from the thermostat could reconnect after `stop()`. Bursts of push updates are now merged into one read instead of one read each.
- If a `with` block was interrupted (e.g. Ctrl+C) while connecting, the controller was left running.
- `stop(timeout=...)` with a short timeout could cut the shutdown off halfway, leaving connections open. The shutdown now always finishes; `stop()` just stops waiting for it after `timeout`.
- Library calls raised exceptions on bad input instead of returning `{"ok": False, "error": ...}`: an unknown thermostat label (`KeyError`), or a value like `nan`, `inf`, `"abc"` or `None`. Some bad values were even sent: `set_temp(..., True)` wrote -17 °C and `set_mode(..., 2.7)` wrote 2. These now all return an error and send nothing. Numeric strings like `"72"` still work.
- `dump_characteristics()` raised when the thermostat couldn't be reached; it now returns `{"ok": False, "error": ...}`.
- For 12 seconds after `set_comfort_mode()`, every comfort reading from the thermostat was ignored, including a real change made at the wall in that time, and nothing re-checked it afterwards. Now only readings that still show the previous profile are ignored, and the thermostat is read again when the 12 seconds are up.
- `ecobee-hub` on a machine with no display said "falling back to headless mode" but then exited. It now keeps running like `--headless`.
- `ecobee-hub --dump` crashed with a traceback when a thermostat couldn't be reached. It now prints the error and goes on to the next thermostat.
- `ecobee-hub` exited with code 0 even when a `--set-*` failed, or when there were no pairing files for a `--status`, `--dump`, `--watch` or `--set-*` run. Those now exit with 1. `--dump` combined with `--set-*` silently ignored the `--set-*`; it's now an error.
- A pairing file whose `_label` wasn't text (e.g. a list or number in a hand-edited file) crashed folder discovery or `--label`. It now falls back to the filename. Two files with the same label could make a third thermostat disappear; every file now gets its own label, and duplicates print a warning saying which file got which label.
- **Security:** `ecobee-pair` saved pairing files (which hold the thermostat's long-term keys) readable by every user on the machine. They're now saved readable only by you, and the default `ecobee-local` folder is made private too. Files from older versions keep their permissions; to fix them run `chmod 600 ~/ecobee-local/*.json` (or on your own folder).
- `ecobee-pair` could lose the keys of a thermostat it had just paired, making you reset HomeKit on the thermostat: if saving failed (e.g. a filename in a folder that doesn't exist, or a full disk). Filenames are now checked before pairing, the file is written safely (all or nothing), and if saving still fails the keys are printed so you can save them yourself. A name like `Main.JSON` (which `ecobee-hub` couldn't find on Linux/macOS) is saved as `Main.json`.
- `ecobee-pair`: Ctrl+C or Ctrl+D at a prompt printed a traceback; it now just says "aborted". One HomeKit device on the network with malformed mDNS data could stop the scan from finding others, and devices without an id were merged into one entry.
- `ecobee-mqtt`:
  - `set/fan` turned the fan on for any payload other than `auto`, including `off`. Only `auto` and `on` are accepted now.
  - With a prefix containing `/` (e.g. `--prefix home/ecobee`), every command was silently ignored.
  - A thermostat label containing `+` or `#` crashed the bridge (and one containing `/` couldn't be controlled). Such thermostats are now skipped with a warning, and one failed publish no longer stops the bridge.
  - **Security:** retained command messages were applied again every time the bridge reconnected, so a command could keep taking effect long after it was sent. Retained commands are now ignored.
  - Text from the broker is escaped in log messages, so it can't inject terminal control codes.
  - `set/mode` also accepts `0`-`3`, like `ecobee-hub`.
- Stopping the controller while a thermostat was still connecting (or reconnecting) could leave a task running, so `stop()` took 2 extra seconds and Python printed "Task was destroyed but it is pending!" on exit.
- `--dump` left a HomeKit connection open (and a socket leaking) when the thermostat didn't answer. The same leak was fixed for normal connections in 1.1.1.

### Changed
- The GitHub workflows pin every action to an exact commit and give jobs a read-only token unless they need more (only the PyPI upload gets `id-token: write`).
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
