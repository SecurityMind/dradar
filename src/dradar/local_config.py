"""Shared client-side state: the local config file and constants used across
the CLI's identity/doctor/run-loop modules. Deliberately dependency-free (no
imports from sibling dradar.* modules) so every other client module can
import from here without risking a cycle.
"""

import json
import os
from pathlib import Path

HOME = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
CONFIG_PATH = HOME / "config.json"

TIERS = ("plus", "pro-5x", "pro-20x")


def _load_config() -> dict:
    if CONFIG_PATH.is_file():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def _save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    CONFIG_PATH.chmod(0o600)
