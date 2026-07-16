"""Local pending-upload ledger: a trial that finished but failed to upload
must not just print a path and be forgotten. The volunteer's most expensive
artifact — a real trial that already burned real quota — gets a safety net.

On an upload failure (network drop, timeout, server 5xx), _run_and_submit
records everything needed to retry WITHOUT re-running the trial: the raw
trial_dir (patch/trajectory/result live under it, untouched by scrubbing —
scrubbing writes to a fresh tempdir and never mutates the originals) plus the
already-built client_meta and outcome. `dradar retry-upload` (and an
automatic scan at the top of `dradar go`) replays the upload later.

Entries are self-pruning: a retry that gets back 409 specifically saying
"already submitted" (some earlier attempt actually landed) or 410 (lease
expired — unsalvageable, the cell already reopened for someone else) removes
the entry. A 409 recovery-generation conflict is not success and stays queued.
Anything else keeps it for the next retry.
"""

import json
import os
from pathlib import Path

_FILENAME = "pending_uploads.json"


def _path(home: Path) -> Path:
    return home / _FILENAME


def load(home: Path) -> list[dict]:
    path = _path(home)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save(home: Path, entries: list[dict]) -> None:
    # A safety-net ledger that isn't itself crash-safe defeats the point: a
    # plain write_text() truncates the file before writing, so a kill/OOM/
    # power-loss mid-write leaves truncated JSON and load() would then drop
    # EVERY pending entry, not just the one being saved. Write-to-temp +
    # atomic rename means the file on disk is always either the old or the
    # new complete version, never a partial one.
    home.mkdir(parents=True, exist_ok=True)
    path = _path(home)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2) + "\n")
    os.replace(tmp, path)


def record(home: Path, entry: dict) -> None:
    """Add or replace (by assignment_id) a pending-upload entry."""
    entries = [e for e in load(home) if e.get("assignment_id") != entry.get("assignment_id")]
    entries.append(entry)
    _save(home, entries)


def remove(home: Path, assignment_id: str) -> None:
    entries = [e for e in load(home) if e.get("assignment_id") != assignment_id]
    _save(home, entries)
