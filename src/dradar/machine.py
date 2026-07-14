"""Per-machine guardrails for the run loop (born from a volunteer running
three dradar sessions at once, 2026-07-14):

- single-instance lock: two dradar runners on one machine fetch the SAME held
  batch and race each other cell by cell — the loser of every race uploads a
  duplicate the server 409s away, pure quota waste. An OS-level file lock
  (auto-released on process death, so never stale) makes the second runner
  refuse to start instead.
- orphan compose sweep: pier launches each trial as a docker compose project
  named <task>__<trialid>; a killed dradar/pier never runs `compose down`, so
  the agent keeps running (and burning quota) inside a container nobody will
  ever harvest. With the instance lock held, any such project that exists
  BEFORE we launch our first trial belongs to a dead run — offer to clean it.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# pier compose projects look like <task-slug>__<7-char trial id>. Anchored and
# specific on purpose: this pattern decides what the sweep offers to docker
# compose down, and a false positive would kill a stranger's containers.
_PIER_PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9-]*__[a-z0-9]{6,8}$", re.IGNORECASE)

_lock_handle = None  # keeps the OS lock alive for the process lifetime


def acquire_run_lock(home: Path) -> None:
    """Take the per-machine runner lock or exit with a clear explanation.
    flock/msvcrt locks evaporate with the process — a crash can't strand a
    stale lock, so there is deliberately no timeout/cleanup machinery."""
    global _lock_handle
    home.mkdir(parents=True, exist_ok=True)
    path = home / "run.lock"
    fh = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        holder = fh.read().strip() or "unknown PID"
        fh.close()
        sys.exit(
            f"another dradar run is already active on this machine ({holder}).\n"
            "Running two at once makes them race each other over the same "
            "claimed cells — the duplicate runs are rejected on upload and "
            "their quota is simply wasted. Wait for it to finish (or stop it), "
            "then re-run. To run in parallel, use a second machine with its "
            "own account.")
    fh.seek(0)
    fh.truncate()
    fh.write(f"PID {os.getpid()}")
    fh.flush()
    _lock_handle = fh


def sweep_orphan_compose(assume_yes: bool) -> None:
    """Find pier-shaped compose projects that predate this run and offer to
    take them down. Only callable while holding the run lock — that is what
    makes 'it exists now' imply 'no live dradar on this machine owns it'.
    Every failure path is silent: this is a courtesy sweep, never a reason
    to block a real run."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "ls", "--format", "json"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return
    if proc.returncode != 0:
        return
    try:
        listed = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return
    orphans = [p.get("Name", "") for p in listed
               if _PIER_PROJECT_RE.fullmatch(p.get("Name", "") or "")]
    if not orphans:
        return
    print(f"found {len(orphans)} leftover task container project(s) from a "
          "previous run — the agent inside may STILL be burning your quota, "
          "and nothing will ever collect its result:")
    for name in orphans:
        print(f"  - {name}")
    if not assume_yes:
        answer = input("stop and remove them now? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            print("left alone — you can clean them later with "
                  "`docker compose -p <name> down`")
            return
    for name in orphans:
        try:
            subprocess.run(["docker", "compose", "-p", name, "down",
                            "--remove-orphans"],
                           capture_output=True, timeout=180)
            print(f"  cleaned {name}")
        except (OSError, subprocess.TimeoutExpired):
            print(f"  couldn't clean {name} — try `docker compose -p {name} down`")
