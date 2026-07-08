"""Local quota guard: refuse to start a run that would likely hit the 5h wall.

There is no reliable headless way to query a Claude/ChatGPT subscription's
remaining 5h window (the rate-limit figure is only exposed through the CLIs'
interactive statusline, which does not fire in headless mode). So instead of
probing the live subscription, dradar keeps a local ledger of the window
fraction ITS OWN runs have consumed in the trailing 5 hours (using the same
est_quota_pct the menu quotes) and refuses when there is not enough headroom.

Limitation, disclosed to the volunteer: this only sees dradar's own footprint,
not quota spent in other Claude/codex sessions — so it is a floor on
consumption, not the true remaining. The hard safety net remains the mid-run
interrupt handling (a 429 marks the trial invalid, never a model failure).
"""

import json
import time
from pathlib import Path

WINDOW_SEC = 5 * 3600
# Refuse when the trailing-window consumption plus this run's estimate, times a
# safety margin, would exceed a full window.
_MARGIN = 1.5
_FULL = 100.0


def _ledger_path(home: Path) -> Path:
    return home / "usage-ledger.jsonl"


def record_run(home: Path, est_quota_pct: float | None, now: float | None = None) -> None:
    """Append one run's estimated window cost to the ledger."""
    if not est_quota_pct:
        return
    home.mkdir(parents=True, exist_ok=True)
    entry = {"ts": now if now is not None else time.time(), "pct": float(est_quota_pct)}
    with _ledger_path(home).open("a") as f:
        f.write(json.dumps(entry) + "\n")


def window_consumed_pct(home: Path, now: float | None = None) -> float:
    """Sum of estimated window cost for runs within the trailing 5h."""
    path = _ledger_path(home)
    if not path.is_file():
        return 0.0
    cutoff = (now if now is not None else time.time()) - WINDOW_SEC
    total = 0.0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry["ts"] >= cutoff:
                total += float(entry["pct"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return total


def seconds_until_fits(home: Path, est_quota_pct: float | None,
                       now: float | None = None) -> float:
    """How long until this run fits inside the trailing window.

    0 = fits right now. A positive number = sleep that long (entries age out
    of the rolling 5h window at known instants, so this is exact, +60s
    cushion). -1 = would never fit even with an empty ledger (estimate alone
    exceeds a full window) — the caller should skip rather than sleep."""
    if not est_quota_pct:
        return 0.0
    now = now if now is not None else time.time()
    need = float(est_quota_pct) * _MARGIN
    if need > _FULL:
        return -1.0
    cutoff = now - WINDOW_SEC
    entries = []
    path = _ledger_path(home)
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
                if e["ts"] >= cutoff:
                    entries.append((float(e["ts"]), float(e["pct"])))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    entries.sort()
    total = sum(p for _, p in entries)
    if total + need <= _FULL:
        return 0.0
    freed = 0.0
    for ts, pct in entries:  # oldest expires first
        freed += pct
        if total - freed + need <= _FULL:
            return max(0.0, ts + WINDOW_SEC - now) + 60.0
    return -1.0


def check(home: Path, est_quota_pct: float | None, now: float | None = None) -> tuple[bool, str]:
    """Return (ok, message). ok=False means refuse to start."""
    if not est_quota_pct:
        return True, ""
    consumed = window_consumed_pct(home, now)
    projected = consumed + float(est_quota_pct) * _MARGIN
    if projected > _FULL:
        return False, (
            f"quota guard: dradar's own runs in the last 5h already add up to "
            f"~{consumed:.0f}% of a 5h window, and this task needs ~{est_quota_pct:.0f}% "
            f"(x{_MARGIN} margin). Starting it risks a mid-run cutoff. Wait for your "
            f"window to reset, then retry. (This only counts dradar's usage — if you've "
            f"used Claude/codex elsewhere, your real remaining is lower.)"
        )
    return True, (
        f"quota guard: ~{consumed:.0f}% of a 5h window used by dradar recently; "
        f"this task ~{est_quota_pct:.0f}%. OK to proceed."
    )
