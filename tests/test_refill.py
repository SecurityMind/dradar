import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dradar import refill, runloop


WINDOWS = {"plus": 10.0, "pro-5x": 50.0, "pro-20x": 200.0}


def _assignment(aid: str, pct: float = 1.0) -> dict:
    return {
        "assignment_id": aid, "task_id": f"task-{aid}", "model": "m",
        "effort": "e", "est_quota_pct": pct, "tier_windows_usd": WINDOWS,
        "agent": "codex", "expires_at": "2099-01-01T00:00:00Z",
        "deep_swe_commit": None,
    }


class RefillClient:
    def __init__(self, active=None, candidates=20):
        self.active = list(active or [])
        self.cells = [
            {"task_id": f"new-{i}", "model": "m", "effort": "e",
             "est_quota_pct": 1.0}
            for i in range(candidates)
        ]
        self.claimed = []
        self._lock = threading.Lock()

    def whoami(self):
        return {"volunteer_id": "v1", "claim_limit": 20, "concurrent_limit": 10}

    def get_assignment(self):
        with self._lock:
            return {"active": list(self.active), "free_pick": True}

    def suggest(self, n):
        with self._lock:
            held = {a["task_id"] for a in self.active}
            used = set(self.claimed)
            return {"cells": [c for c in self.cells
                              if c["task_id"] not in held and c["task_id"] not in used][:n]}

    def claim_assignment(self, task_id, model, effort):
        with self._lock:
            aid = f"a-{task_id}"
            assignment = _assignment(aid)
            assignment.update(task_id=task_id, model=model, effort=effort)
            self.active.append(assignment)
            self.claimed.append(task_id)
            return {"assignment": assignment}


class LoopClient(RefillClient):
    def __init__(self, active=None, candidates=20):
        super().__init__(active, candidates)
        self.checked_out = set()

    def checkout(self, exclude_assignment_ids=None, session_id=None):
        with self._lock:
            excluded = set(exclude_assignment_ids or ())
            assignment = next(
                (a for a in self.active
                 if a["assignment_id"] not in self.checked_out
                 and a["assignment_id"] not in excluded),
                None,
            )
            if assignment:
                self.checked_out.add(assignment["assignment_id"])
            return {"assignment": assignment, "held": len(self.active),
                    "unstarted": max(0, len(self.active) - len(self.checked_out))}

    def submit_locally(self, assignment_id):
        with self._lock:
            self.active = [a for a in self.active if a["assignment_id"] != assignment_id]
            self.checked_out.discard(assignment_id)


def _configure(home: Path, active, **overrides):
    values = dict(
        volunteer_id="v1", refill_to=2, max_tasks=5, quota_tier="plus",
        max_estimated_quota_pct=None, active=active,
    )
    values.update(overrides)
    return refill.configure(home, **values)


def test_plan_persists_only_bounded_public_metadata(tmp_path: Path):
    _configure(tmp_path, [_assignment("a1")])
    raw = (tmp_path / "refill-plan.json").read_text().lower()
    assert "a1" in raw and "max_tasks" in raw
    for secret in ("token", "nonce", "password", "auth.json"):
        assert secret not in raw


def test_refill_reserves_a_hard_total_and_naturally_drains(tmp_path: Path):
    client = RefillClient([_assignment("a1"), _assignment("a2")])
    _configure(tmp_path, client.active, refill_to=2, max_tasks=3)
    client.active.pop(0)  # a1 submitted; held queue fell from 2 to 1
    result = refill.refill_once(tmp_path, client)
    assert result["claimed"] == 1
    assert len(refill.load(tmp_path)["assignments"]) == 3

    client.active.pop(0)  # a2 submitted; task cap is already fully reserved
    result = refill.refill_once(tmp_path, client)
    assert result["claimed"] == 0
    assert result["status"] == "draining"


def test_estimated_quota_cap_prevents_an_expensive_refill(tmp_path: Path):
    client = RefillClient([_assignment("a1", pct=2.0)])
    _configure(
        tmp_path, client.active, refill_to=2, max_tasks=5,
        max_estimated_quota_pct=2.5,
    )
    result = refill.refill_once(tmp_path, client)
    assert result["claimed"] == 0
    assert result["status"] == "draining"
    assert client.claimed == []


def test_parallel_workers_share_one_atomic_refill_target(tmp_path: Path):
    client = RefillClient([])
    _configure(tmp_path, [], refill_to=5, max_tasks=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _n: refill.refill_once(tmp_path, client), range(2)))
    assert sum(r["claimed"] for r in results) == 5
    assert len(client.active) == 5
    assert len(refill.load(tmp_path)["assignments"]) == 5


def test_stopped_plan_never_claims_again(tmp_path: Path):
    client = RefillClient([])
    _configure(tmp_path, [], refill_to=2, max_tasks=5)
    refill.stop(tmp_path, "test stop")
    assert refill.refill_once(tmp_path, client)["claimed"] == 0
    assert client.claimed == []


def test_web_added_tasks_cannot_silently_push_plan_past_hard_cap(tmp_path: Path):
    initial = [_assignment("a1")]
    client = RefillClient(initial + [_assignment("a2"), _assignment("a3")])
    _configure(tmp_path, initial, refill_to=2, max_tasks=2)
    result = refill.refill_once(tmp_path, client)
    assert result["status"] == "stopped"
    assert "beyond max_tasks" in result["reason"]
    assert client.claimed == []


def test_setup_clamps_target_to_server_claim_limit(tmp_path: Path, monkeypatch, capsys):
    client = RefillClient([_assignment("a1")])
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    args = argparse.Namespace(
        refill=True, refill_to=50, auto=None, yes=True, max_tasks=3,
        quota_tier="plus", max_estimated_quota_pct=None,
    )
    active = runloop._setup_refill(args, client, client.active, True)
    assert len(active) == 3
    plan = refill.load(tmp_path)
    assert plan["refill_to"] == 3
    assert "using 3" in capsys.readouterr().out


def test_non_submitted_outcome_stops_shared_plan(tmp_path: Path, monkeypatch):
    from test_checkout import CheckoutClient, _cell
    from test_go_menu import _args

    assignment = _cell("bad")
    client = CheckoutClient(
        {"active": [assignment], "free_pick": True},
        [{"assignment": assignment, "held": 1, "unstarted": 0}],
    )
    _configure(tmp_path, [assignment], refill_to=1, max_tasks=3)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setattr(runloop, "_run_and_submit", lambda *a, **kw: "failed")
    args = _args()
    args.refill = True
    assert runloop._run_checkout_loop(args, client, tmp_path, [assignment]) == 1
    assert refill.load(tmp_path)["status"] == "stopped"


def test_checkout_loop_refills_until_hard_cap_then_drains(
    tmp_path: Path, monkeypatch,
):
    from test_go_menu import _args

    first = _assignment("a1")
    first.update(agent="codex", expires_at="2099-01-01T00:00:00Z",
                 deep_swe_commit=None)
    client = LoopClient([first])
    _configure(tmp_path, [first], refill_to=1, max_tasks=3)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    ran = []

    def run(_client, assignment, *_a, **_kw):
        ran.append(assignment["assignment_id"])
        client.submit_locally(assignment["assignment_id"])
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    args = _args()
    args.refill = True
    assert runloop._run_checkout_loop(args, client, tmp_path, [first]) == 0
    assert len(ran) == 3
    assert len(client.claimed) == 2
    assert refill.load(tmp_path)["status"] == "completed"
