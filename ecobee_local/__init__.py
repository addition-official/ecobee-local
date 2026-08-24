"""
ecobee-local: control an Ecobee thermostat locally over Apple HomeKit,
with no cloud, no ecobee developer key, and no Home Assistant.

Typical use:

    from ecobee_local import EcobeeController
    ec = EcobeeController.from_folder()
    ec.start()
    ec.set_temp("main", 72)
    ec.stop()
"""

from .controller import EcobeeController, default_app_folder

__version__ = "1.2.0"
__all__ = ["EcobeeController", "default_app_folder"]
