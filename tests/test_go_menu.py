"""_go_menu: quota is informational only now — the disclosure line fires
exactly when it should (real run + nonzero estimate) and stays quiet
otherwise (dev-agent runs, zero/missing estimate). No case here should ever
refuse to proceed on quota grounds -- that gate was deliberately removed."""

import argparse
import json
from pathlib import Path

import pytest

from dradar import runloop
from dradar.api_client import ApiError
from dradar.runner import RunnerError, TrialArtifacts

ASSIGNMENT = {
    "assignment_id": "a1", "task_id": "t1", "model": "m", "effort": "e",
    "agent": "claude", "expires_at": "2099-01-01T00:00:00Z",
    "est_minutes": 42, "est_quota_pct": 17, "nonce": "n1",
    "deep_swe_commit": None,
}

MENU = [
    {"task_id": "t1", "model": "m", "effort": "e", "est_minutes": 5, "est_quota_pct": 1},
    {"task_id": "t2", "model": "m", "effort": "e", "est_minutes": 9, "est_quota_pct": 2},
]


class FakeClient:
    def __init__(self, assignment_data, claims=None, suggested=None):
        # assignment_data: one payload (repeated), or a list served in order
        # (the last one repeats). claims: scripted claim_assignment results,
        # in order — a dict is returned, an exception instance is raised.
        # suggested: the cells `suggest()` hands back for --auto.
        self._payloads = assignment_data if isinstance(assignment_data, list) else [assignment_data]
        self._claims = list(claims or [])
        self._suggested = suggested or []
        self.claim_calls = []
        self.suggest_calls = []

    def get_assignment(self):
        return self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]

    def claim_assignment(self, task_id, model, effort):
        self.claim_calls.append((task_id, model, effort))
        result = self._claims.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def suggest(self, n):
        self.suggest_calls.append(n)
        return {"cells": self._suggested}

    def checkout(self, exclude_assignment_ids=None):
        # The default fake predates the per-cell dispenser, so callers take
        # the legacy whole-batch path these tests were written against.
        raise ApiError("not found", status_code=404)


def _args(yes=True, dev_agent=None, auto=None, pick=None):
    return argparse.Namespace(yes=yes, dev_agent=dev_agent, resume=False,
                              allow_task_drift=False, keep=False, auto=auto, pick=pick)


# runloop._run_and_submit and runloop._check_version_pin are the sanctioned
# monkeypatch seams for driving _go_menu; stub them by name, not by signature.
def _patch_run(monkeypatch, outcome="submitted", ran=None):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    if ran is None:
        monkeypatch.setattr(runloop, "_run_and_submit", lambda *a, **kw: outcome)
    else:
        monkeypatch.setattr(runloop, "_run_and_submit",
                            lambda *a, **kw: ran.append(a[1]["assignment_id"]) or outcome)


def test_real_run_with_estimate_prints_quota_disclosure(monkeypatch, capsys, tmp_path: Path):
    _patch_run(monkeypatch)
    client = FakeClient({"assignment": ASSIGNMENT, "menu": None, "resumed": False})
    rc = runloop._go_menu(_args(dev_agent=None), {}, client, tmp_path)
    out = capsys.readouterr().out
    assert "it's your call" in out
    assert "nothing is counted" in out
    assert rc == 0


def test_dev_agent_run_suppresses_quota_disclosure(monkeypatch, capsys, tmp_path: Path):
    _patch_run(monkeypatch)
    client = FakeClient({"assignment": ASSIGNMENT, "menu": None, "resumed": False})
    runloop._go_menu(_args(dev_agent="nop"), {}, client, tmp_path)
    assert "it's your call" not in capsys.readouterr().out


def test_zero_estimate_suppresses_quota_disclosure(monkeypatch, capsys, tmp_path: Path):
    _patch_run(monkeypatch)
    assignment = {**ASSIGNMENT, "est_quota_pct": 0}
    client = FakeClient({"assignment": assignment, "menu": None, "resumed": False})
    runloop._go_menu(_args(dev_agent=None), {}, client, tmp_path)
    assert "it's your call" not in capsys.readouterr().out


def test_missing_estimate_suppresses_quota_disclosure(monkeypatch, capsys, tmp_path: Path):
    _patch_run(monkeypatch)
    assignment = dict(ASSIGNMENT)
    del assignment["est_quota_pct"]
    client = FakeClient({"assignment": assignment, "menu": None, "resumed": False})
    runloop._go_menu(_args(dev_agent=None), {}, client, tmp_path)
    assert "it's your call" not in capsys.readouterr().out


def test_declining_the_prompt_never_blocks_or_errors(monkeypatch, capsys, tmp_path: Path):
    """Declining just leaves the lease active for a later `dradar resume` --
    there is no quota-based refusal path left to hit."""
    _patch_run(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    client = FakeClient({"assignment": ASSIGNMENT, "menu": None, "resumed": False})
    rc = runloop._go_menu(_args(yes=False, dev_agent=None), {}, client, tmp_path)
    out = capsys.readouterr().out
    assert "it's your call" in out  # still shown before the prompt
    assert "aborted" in out and "stay active" in out
    assert rc == 1


def test_runs_the_whole_held_batch_serially(monkeypatch, capsys, tmp_path: Path):
    """Free-pick: `active` carries several claimed cells -> the loop runs each
    one, in order, with a single get_assignment call (no re-claim per cell)."""
    ran = []
    _patch_run(monkeypatch, ran=ran)
    batch = [{**ASSIGNMENT, "assignment_id": f"a{i}", "task_id": f"t{i}"} for i in range(1, 4)]
    client = FakeClient({"active": batch, "free_pick": True, "menu": None})
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert ran == ["a1", "a2", "a3"]       # all three, claim order
    assert "holding 3 cells" in capsys.readouterr().out
    assert rc == 0


def test_free_pick_with_no_held_cells_points_to_the_web(monkeypatch, capsys, tmp_path: Path):
    _patch_run(monkeypatch)
    client = FakeClient({"active": [], "free_pick": True, "menu": None})
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    out = capsys.readouterr().out
    assert "pick some on the radar page" in out
    assert rc == 0


# --- --auto / --pick: CLI-side claiming for free-pick instances (volunteer -
# issue #1, 2026-07-15) so an Agent never has to touch the web UI -----------

def test_go_rejects_auto_and_pick_together():
    with pytest.raises(SystemExit):
        runloop.cmd_go(argparse.Namespace(pick=["t1:m:e"], auto=5))


def test_auto_claims_suggested_cells_and_runs_them(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    suggested = [{"task_id": "t1", "model": "m", "effort": "e"},
                 {"task_id": "t2", "model": "m", "effort": "e"}]
    claims = [{"assignment": {**ASSIGNMENT, "assignment_id": "a1", "task_id": "t1"}},
              {"assignment": {**ASSIGNMENT, "assignment_id": "a2", "task_id": "t2"}}]
    client = FakeClient({"active": [], "free_pick": True, "menu": None},
                        claims=claims, suggested=suggested)
    rc = runloop._go_menu(_args(yes=True, auto=2), {}, client, tmp_path)
    assert client.suggest_calls == [2]
    assert client.claim_calls == [("t1", "m", "e"), ("t2", "m", "e")]
    assert ran == ["a1", "a2"]
    out = capsys.readouterr().out
    assert "t1/m@e: claimed" in out and "t2/m@e: claimed" in out
    assert rc == 0


def test_auto_skips_a_stale_suggestion_and_keeps_going(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    suggested = [{"task_id": "t1", "model": "m", "effort": "e"},
                 {"task_id": "t2", "model": "m", "effort": "e"}]
    claims = [ApiError("cell no longer available, fetch a fresh menu", status_code=409),
              {"assignment": {**ASSIGNMENT, "assignment_id": "a2", "task_id": "t2"}}]
    client = FakeClient({"active": [], "free_pick": True, "menu": None},
                        claims=claims, suggested=suggested)
    rc = runloop._go_menu(_args(yes=True, auto=2), {}, client, tmp_path)
    assert ran == ["a2"]                    # t1 skipped, t2 claimed and ran
    out = capsys.readouterr().out
    assert "t1/m@e: not claimed" in out
    assert rc == 0


def test_auto_stops_clean_at_the_concurrent_cap(monkeypatch, capsys, tmp_path: Path):
    suggested = [{"task_id": "t1", "model": "m", "effort": "e"},
                 {"task_id": "t2", "model": "m", "effort": "e"}]
    claims = [ApiError("you're already holding 10 cells (max 10) — run or finish "
                       "some before claiming more", status_code=409)]
    client = FakeClient({"active": [], "free_pick": True, "menu": None},
                        claims=claims, suggested=suggested)
    rc = runloop._go_menu(_args(yes=True, auto=2), {}, client, tmp_path)
    assert client.claim_calls == [("t1", "m", "e")]   # never tried t2 — cap already hit
    out = capsys.readouterr().out
    assert "stopping —" in out and "already holding" in out
    assert rc == 0


def test_pick_claims_exact_cells_by_id(monkeypatch, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    claims = [{"assignment": {**ASSIGNMENT, "assignment_id": "a1", "task_id": "t1"}}]
    client = FakeClient({"active": [], "free_pick": True, "menu": None}, claims=claims)
    rc = runloop._go_menu(_args(yes=True, pick=["t1:m:e"]), {}, client, tmp_path)
    assert client.claim_calls == [("t1", "m", "e")]
    assert ran == ["a1"]
    assert rc == 0


def test_pick_malformed_spec_exits_clearly():
    with pytest.raises(SystemExit):
        runloop._parse_pick("not-enough-colons")


def test_auto_and_pick_are_ignored_once_something_is_already_held(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    batch = [{**ASSIGNMENT, "assignment_id": "a1", "task_id": "t1"}]
    client = FakeClient({"active": batch, "free_pick": True, "menu": None})
    rc = runloop._go_menu(_args(yes=True, auto=5), {}, client, tmp_path)
    assert client.suggest_calls == []        # never called -- already holding cells
    assert ran == ["a1"]                     # the already-held batch still ran
    out = capsys.readouterr().out
    assert "already holding 1 cell(s) — ignoring --auto/--pick" in out
    assert rc == 0


def test_skip_in_a_batch_moves_to_the_next_cell(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    # 's' skips cell 1, 'y' runs cell 2
    answers = iter(["s", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    batch = [{**ASSIGNMENT, "assignment_id": "a1"}, {**ASSIGNMENT, "assignment_id": "a2"}]
    client = FakeClient({"active": batch, "free_pick": True, "menu": None})
    runloop._go_menu(_args(yes=False, dev_agent=None), {}, client, tmp_path)
    assert ran == ["a2"]                    # a1 skipped, a2 ran


# --- menu mode: nothing held on a non-free-pick instance -> claim here ------

def test_menu_mode_claims_the_chosen_entry_and_runs_it(monkeypatch, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient({"active": [], "free_pick": False, "menu": MENU},
                        claims=[{"assignment": ASSIGNMENT}])
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert client.claim_calls == [("t1", "m", "e")]  # -y takes the top pick
    assert ran == ["a1"]
    assert rc == 0


def test_menu_claim_409_refetches_menu_and_claims_again(monkeypatch, capsys, tmp_path: Path):
    """The chosen cell filled up between menu fetch and claim: one fresh menu
    is fetched and the claim retried before giving up."""
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient(
        [{"active": [], "free_pick": False, "menu": [MENU[0]]},
         {"active": [], "free_pick": False, "menu": [MENU[1]]}],
        claims=[ApiError("server returned 409: cell filled", status_code=409),
                {"assignment": ASSIGNMENT}])
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert client.claim_calls == [("t1", "m", "e"), ("t2", "m", "e")]
    assert "went stale" in capsys.readouterr().out
    assert ran == ["a1"]
    assert rc == 0


def test_menu_double_409_means_no_work_and_rc_0(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    menu_payload = {"active": [], "free_pick": False, "menu": [MENU[0]]}
    client = FakeClient(
        [menu_payload, menu_payload],
        claims=[ApiError("server returned 409: cell filled", status_code=409),
                ApiError("server returned 409: cell filled", status_code=409)])
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert "no work available" in capsys.readouterr().out
    assert ran == []
    assert rc == 0


def test_menu_claim_409_self_heals_to_an_already_active_lease(monkeypatch, tmp_path: Path):
    """A 409 that actually means "you already hold a lease": the fresh
    get_assignment carries no menu but an active assignment -> run that."""
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient(
        [{"active": [], "free_pick": False, "menu": [MENU[0]]},
         {"assignment": ASSIGNMENT, "menu": None, "resumed": True}],
        claims=[ApiError("server returned 409: already at cap", status_code=409)])
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert ran == ["a1"]
    assert rc == 0


def test_menu_claim_non_409_error_exits(monkeypatch, tmp_path: Path):
    _patch_run(monkeypatch)
    client = FakeClient({"active": [], "free_pick": False, "menu": [MENU[0]]},
                        claims=[ApiError("server returned 500: boom", status_code=500)])
    with pytest.raises(SystemExit) as excinfo:
        runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert "500" in str(excinfo.value)


# --- _exit_for: dead ends in the run flow come with a next step --------------

class ErrorClient:
    def __init__(self, exc):
        self._exc = exc

    def get_assignment(self):
        raise self._exc


def test_get_assignment_401_exits_with_token_recovery_hint(monkeypatch, tmp_path: Path):
    _patch_run(monkeypatch)
    client = ErrorClient(ApiError("server returned 401: invalid token", status_code=401))
    with pytest.raises(SystemExit) as excinfo:
        runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    msg = str(excinfo.value)
    assert "invalid token" in msg                # the server detail survives
    assert "dradar login --github" in msg        # ...plus how to recover
    assert "radar page" in msg


def test_get_assignment_network_error_exits_mentioning_resume(monkeypatch, tmp_path: Path):
    _patch_run(monkeypatch)
    client = ErrorClient(ApiError("cannot reach https://radar.example: boom",
                                  status_code=None))
    with pytest.raises(SystemExit) as excinfo:
        runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    msg = str(excinfo.value)
    assert "cannot reach" in msg
    assert "check your connection" in msg
    assert "leases stay active" in msg and "dradar resume" in msg


def test_get_assignment_403_passes_server_detail_through(monkeypatch, tmp_path: Path):
    # Suspension carries the server's own explanation; no bogus recovery hint.
    _patch_run(monkeypatch)
    client = ErrorClient(ApiError("server returned 403: account suspended", status_code=403))
    with pytest.raises(SystemExit) as excinfo:
        runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    msg = str(excinfo.value)
    assert "account suspended" in msg
    assert "login --github" not in msg


def test_choose_menu_entry_numeric_pick(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "2")
    assert runloop._choose_menu_entry(MENU, yes=False) is MENU[1]


def test_choose_menu_entry_empty_input_takes_top_pick_silently(monkeypatch, capsys):
    # Enter-for-default is a deliberate choice, not a typo: no announcement.
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert runloop._choose_menu_entry(MENU, yes=False) is MENU[0]
    assert "invalid choice" not in capsys.readouterr().out


def test_choose_menu_entry_invalid_input_reprompts_once(monkeypatch, capsys):
    # A typo must not silently lease the wrong cell: announce and re-prompt.
    answers = iter(["abc", "2"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert runloop._choose_menu_entry(MENU, yes=False) is MENU[1]
    out = capsys.readouterr().out
    assert "invalid choice 'abc'" in out
    assert "taking the top pick" not in out


def test_choose_menu_entry_double_invalid_falls_back_announced(monkeypatch, capsys):
    # Garbage-piping automation still terminates, but the fallback is loud.
    for pair in (("abc", "xyz"), ("99", "0")):
        answers = iter(pair)
        monkeypatch.setattr("builtins.input", lambda *_: next(answers))
        assert runloop._choose_menu_entry(MENU, yes=False) is MENU[0]
        out = capsys.readouterr().out
        assert "invalid choice" in out
        assert "taking the top pick (t1)" in out


# --- _run_and_submit: the outcome tag the server grades by ------------------
# Stubbed one level lower (runloop.run_trial) so the real outcome derivation
# and meta assembly run; the server marks `interrupted` invalid instead of
# grading it 0, so mislabeling here corrupts grading fleet-wide.

class SubmitClient(FakeClient):
    def __init__(self, assignment_data, claims=None):
        super().__init__(assignment_data, claims)
        self.submissions = []

    def submit(self, assignment_id, nonce, patch, trajectory, result, meta, outcome="completed"):
        self.submissions.append(
            {"assignment_id": assignment_id, "outcome": outcome, "meta": meta})
        return {"submission_id": f"s-{assignment_id}", "grade_status": "pending"}


def _fake_art(base: Path, rc: int = 0, result_data: dict | None = None) -> TrialArtifacts:
    trial_dir = base / "trial"
    (trial_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    patch = trial_dir / "artifacts" / "model.patch"
    patch.write_text("diff --git a b\n")
    result = None
    if result_data is not None:
        result = trial_dir / "result.json"
        result.write_text(json.dumps(result_data))
    job_dir = base / "job"
    job_dir.mkdir(exist_ok=True)
    return TrialArtifacts(job_dir=job_dir, trial_dir=trial_dir, patch=patch,
                          trajectory=None, result=result, returncode=rc,
                          duration_sec=61.0, log_path=base / "pier.log")


def test_clean_run_submits_outcome_completed(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    assert tag == "submitted"
    assert client.submissions[0]["outcome"] == "completed"


def test_nonzero_pier_rc_submits_outcome_interrupted_with_meta(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    result_data = {"agent_result": {"n_input_tokens": 10, "n_output_tokens": 3,
                                    "n_cache_tokens": 0, "n_agent_steps": 7}}
    art = _fake_art(tmp_path, rc=1, result_data=result_data)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    assert tag == "interrupted"
    sub = client.submissions[0]
    assert sub["outcome"] == "interrupted"
    assert sub["meta"]["pier_returncode"] == 1
    assert sub["meta"]["duration_sec"] == 61.0
    assert sub["meta"]["n_input_tokens"] == 10  # token stats still reported


def test_recorded_exception_info_submits_outcome_interrupted(monkeypatch, tmp_path: Path):
    # pier rc 0 but result.json recorded an exception (e.g. rate-limit death
    # inside the harness): still interrupted, never a graded 0.
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    result_data = {"exception_info": {"type": "RateLimitDeath"}, "agent_result": {}}
    art = _fake_art(tmp_path, rc=0, result_data=result_data)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    assert tag == "interrupted"
    assert client.submissions[0]["outcome"] == "interrupted"


def test_run_trial_error_is_failed_and_go_menu_rc_1(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)

    def boom(*a, **kw):
        raise RunnerError("pier exploded")
    monkeypatch.setattr(runloop, "run_trial", boom)
    client = SubmitClient({"assignment": ASSIGNMENT, "menu": None})
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert "trial failed: pier exploded" in capsys.readouterr().out
    assert client.submissions == []  # nothing uploaded
    assert rc == 1


def test_mixed_batch_one_failure_yields_rc_1(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)

    def fake_run(assignment, *a, **kw):
        if assignment["assignment_id"] == "a1":
            raise RunnerError("boom")
        return _fake_art(tmp_path / assignment["assignment_id"], rc=0)
    monkeypatch.setattr(runloop, "run_trial", fake_run)
    batch = [{**ASSIGNMENT, "assignment_id": "a1"},
             {**ASSIGNMENT, "assignment_id": "a2", "nonce": "n2"}]
    client = SubmitClient({"active": batch, "free_pick": True, "menu": None})
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert [s["assignment_id"] for s in client.submissions] == ["a2"]  # a2 still landed
    assert rc == 1  # but the batch as a whole reports failure
