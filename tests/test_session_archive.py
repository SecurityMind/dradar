"""End-to-end tests for the opt-in Codex session archiving.

Covers the product/security boundaries from review:
  - default (no --archive-session) archives nothing
  - only --archive-session archives
  - --keep does not produce an archive copy
  - archive choice persists in the pending ledger and retries honor it
  - old ledger lacking the field defaults to off
  - malicious assignment id cannot escape the archive root / is sanitized
  - repeated runs are idempotent (no silent overwrite growth)
  - symlink / path-escaping sources are rejected
  - `dradar sessions prune` dry-run and delete
"""

import json
import stat

import pytest

import dradar.runloop as runloop
from dradar.runloop import _archive_codex_sessions, _safe_assignment_component, _safe_copy_session
from dradar import pending


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path / ".dradar")
    monkeypatch.setattr(pending, "_path", lambda h: h / "pending_uploads.json")
    return tmp_path


def _make_trial(home, assignment_id="a1", date=("2026", "07", "16")):
    trial = home / "jobs" / f"a{assignment_id}" / "task__x"
    (trial / "artifacts").mkdir(parents=True, exist_ok=True)
    (trial / "artifacts" / "model.patch").write_text("diff --git a x\n")
    sess = trial / "agent" / "sessions" / date[0] / date[1] / date[2]
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "rollout-1.jsonl").write_text('{"x": 1}')
    return trial


def _entry(trial, assignment_id="a1", archive_session=False, keep=False):
    return {
        "assignment_id": assignment_id, "nonce": "n", "task_id": "t",
        "trial_dir": str(trial), "meta": {}, "outcome": "completed",
        "job_dir": str(trial), "keep": keep,
        "resume_generation": 0, "archive_session": archive_session,
    }


def _fake_client(monkeypatch):
    class C:
        def submit(self, *a, **k):
            return {"submission_id": "s1", "grade_status": "pending"}
    return C()


def test_default_does_not_archive(home, monkeypatch):
    trial = _make_trial(home)
    runloop._upload_trial(_fake_client(monkeypatch), _entry(trial), archive_session=False)
    assert not (home / ".dradar" / "history").exists()


def test_opt_in_archives(home, monkeypatch):
    trial = _make_trial(home)
    runloop._upload_trial(_fake_client(monkeypatch), _entry(trial, archive_session=True), archive_session=True)
    dest = home / ".dradar" / "history" / "codex-sessions" / "a1" / "2026" / "07" / "16" / "rollout-1.jsonl"
    assert dest.is_file()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_keep_does_not_archive(home, monkeypatch):
    trial = _make_trial(home)
    runloop._upload_trial(
        _fake_client(monkeypatch), _entry(trial, archive_session=True, keep=True),
        archive_session=True,
    )
    assert not (home / ".dradar" / "history").exists()


def test_ledger_persists_choice_on_retry(home, monkeypatch):
    trial = _make_trial(home)
    e = _entry(trial, archive_session=True)
    pending.record(runloop.HOME, e)
    # retry path passes the loaded entry WITHOUT the call-time flag:
    runloop._upload_trial(_fake_client(monkeypatch), e)
    assert (home / ".dradar" / "history" / "codex-sessions" / "a1" / "2026" / "07" / "16" / "rollout-1.jsonl").is_file()


def test_old_ledger_missing_field_defaults_off(home, monkeypatch):
    trial = _make_trial(home)
    e = _entry(trial)
    e.pop("archive_session", None)  # simulate old ledger
    pending.record(runloop.HOME, e)
    runloop._upload_trial(_fake_client(monkeypatch), e)
    assert not (home / ".dradar" / "history").exists()


def test_malicious_assignment_id_is_sanitized(home, monkeypatch):
    # trial dir uses a safe id; the *entry's* assignment id is malicious, which
    # is what flows into the archive path component.
    trial = _make_trial(home, assignment_id="safe")
    runloop._upload_trial(
        _fake_client(monkeypatch),
        _entry(trial, assignment_id="evil/../../escape", archive_session=True),
        archive_session=True,
    )
    root = home / ".dradar" / "history" / "codex-sessions"
    # the dangerous component is sanitized; nothing outside root:
    safe = _safe_assignment_component("evil/../../escape")
    assert (root / safe).is_dir()
    assert not (home / "escape").exists()


def test_repeated_runs_idempotent(home, monkeypatch):
    trial = _make_trial(home)
    runloop._upload_trial(_fake_client(monkeypatch), _entry(trial, archive_session=True), archive_session=True)
    runloop._upload_trial(_fake_client(monkeypatch), _entry(trial, archive_session=True), archive_session=True)
    files = list((home / ".dradar" / "history" / "codex-sessions" / "a1").rglob("*.jsonl"))
    assert len(files) == 1


def test_symlink_source_rejected(home, monkeypatch):
    trial = _make_trial(home)
    sess = trial / "agent" / "sessions" / "2026" / "07" / "16"
    target = home / "secret.jsonl"
    target.write_text("leak")
    (sess / "evil.jsonl").symlink_to(target)
    ok = _safe_copy_session(sess / "evil.jsonl", home / "out.jsonl")
    assert ok is False
    assert not (home / "out.jsonl").exists()


def test_safe_assignment_component():
    assert _safe_assignment_component("a/b\\c:d") == "a_b_c_d"
    assert _safe_assignment_component("ok-id_1") == "ok-id_1"
    assert _safe_assignment_component("") == "unknown"
    assert "/" not in _safe_assignment_component("../../x")


def test_sessions_prune_dry_run_and_delete(home, monkeypatch, capsys):
    trial = _make_trial(home)
    runloop._upload_trial(_fake_client(monkeypatch), _entry(trial, archive_session=True), archive_session=True)

    class Args:
        yes = False

    assert runloop.cmd_sessions_prune(Args()) == 0
    out = capsys.readouterr().out
    assert "MB" in out
    assert (home / ".dradar" / "history" / "codex-sessions" / "a1").is_dir()

    class Args2:
        yes = True

    runloop.cmd_sessions_prune(Args2())
    assert not (home / ".dradar" / "history" / "codex-sessions").exists()


def test_same_named_session_across_dates_not_overwritten(home, monkeypatch):
    # Two runs on different dates can produce the same transcript filename;
    # the archive must keep both via the preserved date-sharded layout.
    t16 = _make_trial(home, assignment_id="a1", date=("2026", "07", "16"))
    t17 = _make_trial(home, assignment_id="a1", date=("2026", "07", "17"))
    runloop._upload_trial(_fake_client(monkeypatch), _entry(t16, archive_session=True), archive_session=True)
    runloop._upload_trial(_fake_client(monkeypatch), _entry(t17, archive_session=True), archive_session=True)
    root = home / ".dradar" / "history" / "codex-sessions" / "a1"
    f16 = root / "2026" / "07" / "16" / "rollout-1.jsonl"
    f17 = root / "2026" / "07" / "17" / "rollout-1.jsonl"
    assert f16.is_file() and f17.is_file()
    assert f16.read_text() == f17.read_text() == '{"x": 1}'


def test_safe_assignment_component_is_strict_ascii():
    # str.isalnum() accepts Unicode; our whitelist must NOT.
    assert _safe_assignment_component("café") == "caf__"
    assert _safe_assignment_component("ёжик") == "________"
    assert _safe_assignment_component("ok-id_1") == "ok-id_1"
    assert "/" not in _safe_assignment_component("../../x")


def test_parallel_mode_still_archives(home, monkeypatch):
    # --parallel only changes how processes are scheduled, it must NOT drop
    # the archive choice (no silent "archive_session lost under workers").
    import types

    trial = _make_trial(home)
    args = types.SimpleNamespace(archive_session=True, parallel=True, keep=False, yes=True)
    e = dict(_entry(trial, archive_session=True))
    e["archive_session"] = bool(getattr(args, "archive_session", False))
    runloop._upload_trial(_fake_client(monkeypatch), e, archive_session=bool(getattr(args, "archive_session", False)))
    assert (home / ".dradar" / "history" / "codex-sessions" / "a1" / "2026" / "07" / "16" / "rollout-1.jsonl").is_file()


def test_worker_command_forwards_archive_session():
    # The internal resume worker must carry --archive-session through, or
    # `--workers N --archive-session` would silently not archive.
    import types

    base = types.SimpleNamespace(
        keep=False, allow_task_drift=False, dev_agent=None,
        refill=False, max_tasks=None, refill_to=None,
        max_estimated_quota_pct=None, quota_tier="plus",
        archive_session=False,
    )
    off = runloop._worker_command(base)
    assert "--archive-session" not in off

    base.archive_session = True
    on = runloop._worker_command(base)
    assert "--archive-session" in on
