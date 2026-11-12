# Changelog

What changed in each release on [PyPI](https://pypi.org/project/ecobee-local/). Work on `main` that isn't on PyPI yet goes under **Unreleased** at the top.

## Unreleased

### Added
- Optional MQTT bridge: `pip install "ecobee-local[mqtt]"`, then run `ecobee-mqtt`. Publishes each thermostat's state to `ecobee/<label>/...` and accepts commands on `ecobee/<label>/set/<thing>`.
- Test suite (`pytest`) that runs without a thermostat, broker or network.

### Fixed
- `--dump` left a HomeKit connection open (and a socket leaking) when the thermostat didn't answer. The same leak was fixed for normal connections in 1.1.1.

### Changed
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
