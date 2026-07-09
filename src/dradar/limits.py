"""Real account rate-limit probe via `codex app-server` (JSON-RPC over stdio).

This is the same mechanism the quota-radar pipeline uses: initialize with
experimentalApi, then account/rateLimits/read. It returns the ACCOUNT's true
5h and weekly windows (used percent + exact reset instants). This API is
experimental and may move between codex versions, so every caller must
tolerate None (no local fallback exists — quota is the volunteer's own to
track; see runloop._go_menu).
"""

import json
import selectors
import shutil
import subprocess
import time

PROBE_TIMEOUT_SEC = 20


def _parse(rate_limits: dict) -> dict:
    primary = rate_limits.get("primary") or {}
    secondary = rate_limits.get("secondary") or {}
    return {
        "five_hour_used_pct": primary.get("usedPercent"),
        "five_hour_resets_at": primary.get("resetsAt"),
        "weekly_used_pct": secondary.get("usedPercent"),
        "weekly_resets_at": secondary.get("resetsAt"),
        "plan_type": rate_limits.get("planType"),
    }


def read_rate_limits(timeout: float = PROBE_TIMEOUT_SEC) -> dict | None:
    """One-shot probe of the logged-in codex account. None on ANY failure
    (codex missing, not logged in, API shape changed, timeout)."""
    codex = shutil.which("codex")
    if not codex:
        return None
    try:
        proc = subprocess.Popen(
            [codex, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
    except OSError:
        return None
    try:
        def send(obj):
            proc.stdin.write(json.dumps(obj, separators=(",", ":")) + "\n")
            proc.stdin.flush()

        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "dradar", "title": None, "version": "0"},
            "capabilities": {"experimentalApi": True, "requestAttestation": False,
                             "optOutNotificationMethods": []}}})
        time.sleep(0.2)
        send({"method": "initialized"})
        time.sleep(0.2)
        send({"id": 2, "method": "account/rateLimits/read"})
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        end = time.time() + timeout
        while time.time() < end:
            for key, _ in sel.select(timeout=0.5):
                line = key.fileobj.readline()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("id") == 2:
                    result = obj.get("result") or {}
                    limits = result.get("rateLimits")
                    if not isinstance(limits, dict):
                        return None
                    parsed = _parse(limits)
                    return parsed if parsed["five_hour_used_pct"] is not None else None
        return None
    except (OSError, ValueError):
        return None
    finally:
        try:
            proc.terminate()
        except OSError:
            pass
