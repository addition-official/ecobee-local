"""Packaging: versions agree, public names import, commands are wired up."""

import importlib
import re
from importlib.metadata import entry_points
from pathlib import Path

import ecobee_local

ROOT = Path(__file__).resolve().parent.parent


def pyproject_version():
    text = (ROOT / "pyproject.toml").read_text()
    return re.search(r'^version = "([^"]+)"', text, re.M).group(1)


def test_versions_match():
    assert ecobee_local.__version__ == pyproject_version()


def test_changelog_has_unreleased_on_top():
    text = (ROOT / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings[0] == "Unreleased"


def test_star_import():
    ns = {}
    exec("from ecobee_local import *", ns)
    assert "EcobeeController" in ns and "default_app_folder" in ns


def test_console_scripts_resolve():
    scripts = {ep.name: ep for ep in entry_points(group="console_scripts")
               if ep.value.startswith("ecobee_local.")}
    assert set(scripts) == {"ecobee-hub", "ecobee-pair", "ecobee-mqtt"}
    for ep in scripts.values():
        module, func = ep.value.split(":")
        assert callable(getattr(importlib.import_module(module), func))
