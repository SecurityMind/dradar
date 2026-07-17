"""Shared client-side state: the local config file and constants used across
the CLI's identity/doctor/run-loop modules. Deliberately dependency-free (no
imports from sibling dradar.* modules) so every other client module can
import from here without risking a cycle.
"""

import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ.get("DRADAR_HOME", Path.home() / ".dradar"))
CONFIG_PATH = HOME / "config.json"


def default_tasks_root() -> Path:
    """Hidden default checkout used when the volunteer did not choose one.

    Derive it at call time rather than import time so DRADAR_HOME overrides
    and tests that isolate HOME continue to affect every caller consistently.
    """
    return HOME / "deep-swe" / "tasks"


def tasks_root_from_config(cfg: dict) -> Path:
    """Preserve an explicit/legacy checkout, otherwise use the hidden one."""
    configured = cfg.get("tasks_root")
    return (Path(configured).expanduser() if configured
            else default_tasks_root())


def _load_config(fresh_on_corrupt: bool = False) -> dict:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            # fresh_on_corrupt: `dradar login` is about to rewrite the file
            # anyway, so it must not die on a corrupt one — it IS the
            # recovery path the error below recommends.
            if fresh_on_corrupt:
                print(f"config at {CONFIG_PATH} was corrupt — starting fresh "
                      "(login will rewrite it)")
                return {}
            # every other command loads the config first; a raw traceback
            # here tells the volunteer nothing about how to get unstuck.
            sys.exit(
                f"config at {CONFIG_PATH} is corrupt — run `dradar login "
                "--github` to recover a linked identity (it rewrites the "
                "config), or grab a fresh token on the radar page and paste "
                "its login command"
            )
    return {}


def _save_config(cfg: dict) -> None:
    # Mirror pending._save: write-to-temp + atomic os.replace, so a kill
    # mid-write can never truncate the volunteer's ONLY copy of their token.
    # The temp file is created 0600 BEFORE any bytes land (the old
    # write-then-chmod left a brief world-readable window on the token).
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)  # O_CREAT's mode is ignored when tmp pre-existed
    os.replace(tmp, CONFIG_PATH)
