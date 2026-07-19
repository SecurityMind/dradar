"""Persistent, bounded continuous-refill plans shared by local workers.

The plan contains only public assignment metadata and counters.  It never
stores the account token, assignment nonce, patch, trajectory, or Codex data.
All workers under one DRADAR_HOME serialize plan updates through an OS lock.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .api_client import ApiError

SCHEMA_VERSION = 1
PLAN_FILE = "refill-plan.json"
LOCK_FILE = "refill-plan.lock"
RUNNING_STATES = {"active", "draining"}
TIERS = ("plus", "pro-5x", "pro-20x")


class RefillError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(home: Path) -> Path:
    return home / PLAN_FILE


@contextmanager
def _locked(home: Path) -> Iterator[None]:
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)
    windows_lock = False
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows CI exercises callers
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            windows_lock = True
        yield
    finally:
        if windows_lock:  # pragma: no cover
            try:
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        os.close(fd)


def _load_unlocked(home: Path) -> dict | None:
    try:
        raw = json.loads(_path(home).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return None
    return raw


def load(home: Path) -> dict | None:
    with _locked(home):
        return _load_unlocked(home)


def _save_unlocked(home: Path, plan: dict) -> None:
    plan["updated_at"] = _now()
    path = _path(home)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(plan, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _estimate_pct(assignment: dict, tier: str, windows: dict | None) -> float | None:
    plus_pct = assignment.get("est_quota_pct")
    if plus_pct is None:
        return None
    plus_pct = float(plus_pct)
    if tier == "plus":
        return plus_pct
    current = assignment.get("tier_windows_usd") or windows or {}
    plus_window, tier_window = current.get("plus"), current.get(tier)
    if not plus_window or not tier_window:
        return None
    return plus_pct * float(plus_window) / float(tier_window)


def _reserve(plan: dict, assignment: dict) -> bool:
    assignment_id = assignment.get("assignment_id")
    if not assignment_id or assignment_id in plan["assignments"]:
        return False
    windows = assignment.get("tier_windows_usd")
    if windows and not plan.get("tier_windows_usd"):
        plan["tier_windows_usd"] = windows
    estimate = _estimate_pct(
        assignment, plan["quota_tier"], plan.get("tier_windows_usd"))
    if plan.get("max_estimated_quota_pct") is not None and estimate is None:
        raise RefillError(
            f"no {plan['quota_tier']} quota estimate for {assignment.get('task_id', '?')}; "
            "continuous refill stopped before claiming more work"
        )
    plan["assignments"][assignment_id] = {
        "task_id": assignment.get("task_id"),
        "estimated_quota_pct": estimate,
    }
    return True


def _reserved_quota(plan: dict) -> float:
    return sum(
        float(item.get("estimated_quota_pct") or 0)
        for item in plan.get("assignments", {}).values()
    )


def configure(
    home: Path,
    *,
    volunteer_id: str,
    refill_to: int,
    max_tasks: int,
    quota_tier: str,
    max_estimated_quota_pct: float | None,
    active: list[dict],
    replace_existing: bool = False,
) -> dict:
    if refill_to < 1 or max_tasks < 1 or max_tasks < len(active):
        raise RefillError("max tasks must be at least the currently held task count")
    if quota_tier not in TIERS:
        raise RefillError(f"unknown quota tier: {quota_tier}")
    desired = {
        "volunteer_id": volunteer_id,
        "refill_to": refill_to,
        "max_tasks": max_tasks,
        "quota_tier": quota_tier,
        "max_estimated_quota_pct": max_estimated_quota_pct,
    }
    with _locked(home):
        current = _load_unlocked(home)
        replaced_plan_id = None
        if current and current.get("status") in RUNNING_STATES:
            if any(current.get(key) != value for key, value in desired.items()):
                if not replace_existing:
                    raise RefillError(
                        "another refill plan is active with different limits"
                    )
                replaced_plan_id = current.get("plan_id")
                current = None
        if current and current.get("status") in RUNNING_STATES:
            plan = current
        else:
            plan = {
                "schema_version": SCHEMA_VERSION,
                "plan_id": uuid4().hex,
                "created_at": _now(),
                "updated_at": _now(),
                "status": "active",
                "stop_reason": None,
                "assignments": {},
                "tier_windows_usd": None,
                **desired,
            }
            if replaced_plan_id:
                plan["replaced_plan_id"] = replaced_plan_id
        for assignment in active:
            _reserve(plan, assignment)
        if len(plan["assignments"]) > max_tasks:
            plan["status"] = "stopped"
            plan["stop_reason"] = "held tasks exceeded max_tasks"
            _save_unlocked(home, plan)
            raise RefillError(plan["stop_reason"])
        cap = plan.get("max_estimated_quota_pct")
        if cap is not None and _reserved_quota(plan) > float(cap) + 1e-9:
            plan["status"] = "stopped"
            plan["stop_reason"] = "held tasks exceeded estimated quota limit"
            _save_unlocked(home, plan)
            raise RefillError(plan["stop_reason"])
        _save_unlocked(home, plan)
        return plan


def stop(home: Path, reason: str = "user stopped") -> dict | None:
    with _locked(home):
        plan = _load_unlocked(home)
        if not plan:
            return None
        plan["status"] = "stopped"
        plan["stop_reason"] = reason
        # A stopped plan has no recovery work left to coordinate. Keeping the
        # file only exposes an internal implementation detail and previously
        # let stale state confuse the next campaign.
        _path(home).unlink(missing_ok=True)
        return plan


def is_running(home: Path) -> bool:
    plan = load(home)
    return bool(plan and plan.get("status") in RUNNING_STATES)


def complete_if_empty(home: Path, held: int) -> None:
    if held:
        return
    with _locked(home):
        plan = _load_unlocked(home)
        if plan and plan.get("status") in RUNNING_STATES:
            _path(home).unlink(missing_ok=True)


def refill_once(home: Path, client) -> dict:
    """Reconcile server-held work and atomically refill toward the plan target.

    The lock deliberately spans the bounded HTTP calls.  It elects exactly one
    local worker as coordinator at each refill boundary and prevents a herd of
    parallel workers from all observing the same shortfall.
    """
    with _locked(home):
        plan = _load_unlocked(home)
        if not plan or plan.get("status") not in RUNNING_STATES:
            return {"status": plan.get("status") if plan else "none", "claimed": 0}
        if plan.get("status") == "draining":
            return {"status": "draining", "claimed": 0,
                    "planned": len(plan.get("assignments", {})),
                    "reason": plan.get("stop_reason")}
        data = client.get_assignment()
        active = data.get("active")
        if active is None:
            one = data.get("assignment")
            active = [one] if one else []
        try:
            for assignment in active:
                _reserve(plan, assignment)
        except RefillError as exc:
            plan["status"] = "stopped"
            plan["stop_reason"] = str(exc)
            _save_unlocked(home, plan)
            return {"status": "stopped", "claimed": 0, "reason": str(exc)}

        planned = len(plan["assignments"])
        quota_cap = plan.get("max_estimated_quota_pct")
        if planned > int(plan["max_tasks"]):
            plan["status"] = "stopped"
            plan["stop_reason"] = "held queue grew beyond max_tasks; refill stopped"
            _save_unlocked(home, plan)
            return {"status": "stopped", "claimed": 0,
                    "held": len(active), "planned": planned,
                    "reason": plan["stop_reason"]}
        if quota_cap is not None and _reserved_quota(plan) > float(quota_cap) + 1e-9:
            plan["status"] = "stopped"
            plan["stop_reason"] = "held queue grew beyond estimated quota cap; refill stopped"
            _save_unlocked(home, plan)
            return {"status": "stopped", "claimed": 0,
                    "held": len(active), "planned": planned,
                    "reason": plan["stop_reason"]}
        slots_left = max(0, int(plan["max_tasks"]) - planned)
        missing = max(0, int(plan["refill_to"]) - len(active))
        wanted = min(missing, slots_left)
        if wanted == 0:
            if slots_left == 0:
                plan["status"] = "draining"
                plan["stop_reason"] = "max_tasks reserved; draining queue"
            _save_unlocked(home, plan)
            return {"status": plan["status"], "claimed": 0,
                    "held": len(active), "planned": planned}

        claimed = 0
        # Ask for a few alternates so a stale recommendation or one expensive
        # cell does not prevent a bounded refill. The server still clamps this
        # request to the account's claim limit.
        suggestions = client.suggest(max(wanted, wanted * 3)).get("cells") or []
        quota_blocked = False
        missing_quota_estimate = False
        for cell in suggestions:
            if claimed >= wanted:
                break
            estimate = _estimate_pct(
                cell, plan["quota_tier"], plan.get("tier_windows_usd"))
            if quota_cap is not None:
                if estimate is None:
                    missing_quota_estimate = True
                    continue
                if _reserved_quota(plan) + estimate > float(quota_cap) + 1e-9:
                    quota_blocked = True
                    continue
            try:
                ack = client.claim_assignment(
                    cell["task_id"], cell["model"], cell["effort"])
            except ApiError as exc:
                if exc.status_code == 409:
                    continue
                raise
            assignment = ack.get("assignment")
            if assignment and _reserve(plan, assignment):
                claimed += 1
                _save_unlocked(home, plan)  # crash-safe after every accepted claim

        if claimed == 0 and missing_quota_estimate:
            # This is a server/client data-contract failure, not an exhausted
            # user quota.  Fail closed and say why instead of silently entering
            # a misleading draining state that can never recover by itself.
            plan["status"] = "stopped"
            plan["stop_reason"] = (
                f"recommendations lack {plan['quota_tier']} quota conversion data; "
                "refill stopped before claiming work"
            )
        elif claimed == 0 and quota_blocked:
            plan["status"] = "draining"
            plan["stop_reason"] = "no recommended task fits the estimated quota left"
        _save_unlocked(home, plan)
        return {"status": plan["status"], "claimed": claimed,
                "held": len(active) + claimed,
                "planned": len(plan["assignments"]),
                "reserved_quota_pct": _reserved_quota(plan),
                "reason": plan.get("stop_reason")}
