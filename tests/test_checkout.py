"""Per-cell checkout loop: the parallel-safe run path (owner decision
2026-07-14 — sessions get work from a server-side dispenser instead of
racing over a shared batch snapshot)."""
import dradar.runloop as runloop
from dradar.api_client import ApiError

from test_go_menu import FakeClient, _args, _patch_run


def _cell(aid):
    return {"assignment_id": aid, "task_id": f"task-{aid}", "agent": "codex",
            "model": "gpt-5.6-sol", "effort": "low", "nonce": "n",
            "expires_at": "2099-01-01T00:00:00+00:00", "est_minutes": 2,
            "est_quota_pct": 0.5, "deep_swe_commit": None}


class CheckoutClient(FakeClient):
    def __init__(self, assignment_data, checkouts):
        super().__init__(assignment_data)
        self._checkouts = list(checkouts)   # dicts returned in order, or exceptions

    def checkout(self):
        result = self._checkouts.pop(0) if self._checkouts else {"assignment": None,
                                                                 "held": 0, "unstarted": 0}
        if isinstance(result, Exception):
            raise result
        return result


def test_checkout_loop_runs_dispensed_cells_until_drained(monkeypatch, capsys, tmp_path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = CheckoutClient(
        {"active": [_cell("a1")], "free_pick": True},
        [{"assignment": _cell("a1"), "held": 2, "unstarted": 1},
         {"assignment": _cell("a2"), "held": 2, "unstarted": 0},
         {"assignment": None, "held": 2, "unstarted": 0}])
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1", "a2"]
    out = capsys.readouterr().out
    assert "checked out task-a1" in out and "1 more waiting" in out


def test_checkout_404_falls_back_to_legacy_batch(monkeypatch, tmp_path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = CheckoutClient(
        {"active": [_cell("a1"), _cell("a2")], "free_pick": True},
        [ApiError("not found", status_code=404)])
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1", "a2"]   # legacy whole-batch flow took over


def test_checkout_loop_never_retries_a_cell_that_failed_this_session(
        monkeypatch, capsys, tmp_path):
    # the failure path reports 'stopped', which puts the cell back in the
    # dispenser — the loop must not chew on it forever
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    attempts = []

    def run(client, assignment, *a, **kw):
        attempts.append(assignment["assignment_id"])
        return "failed" if assignment["assignment_id"] == "bad" else "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    client = CheckoutClient(
        {"active": [_cell("bad")], "free_pick": True},
        [{"assignment": _cell("bad"), "held": 2, "unstarted": 1},
         {"assignment": _cell("bad"), "held": 2, "unstarted": 1},   # re-dispensed
         {"assignment": _cell("ok"), "held": 2, "unstarted": 0},
         {"assignment": None, "held": 2, "unstarted": 0}])
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert attempts == ["bad", "ok"]          # bad ran once, then skipped
    assert rc == 1                            # the failure still fails the run
    assert "already failed in" in capsys.readouterr().out.replace("\n", " ")


def test_interactive_run_keeps_legacy_batch_flow(monkeypatch, tmp_path):
    # no -y: the dispenser can't host confirm/skip prompts, so the legacy
    # path (with its prompts) must be the one that runs
    ran = []
    _patch_run(monkeypatch, ran=ran)
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    client = CheckoutClient(
        {"active": [_cell("a1")], "free_pick": True},
        [{"assignment": _cell("a1"), "held": 1, "unstarted": 0}])
    rc = runloop._go_menu(_args(yes=False), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1"]
    assert len(client._checkouts) == 1        # checkout endpoint never consulted
