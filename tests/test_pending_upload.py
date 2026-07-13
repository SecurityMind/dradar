"""Upload resilience: a trial that ran but failed to upload must survive on
disk and be retryable without re-running, via a local pending-upload ledger.
"""

from pathlib import Path

import pytest

from dradar import pending, runloop
from dradar.api_client import ApiError


# --- pending.py: the ledger itself ------------------------------------------

def test_ledger_round_trip(tmp_path: Path):
    assert pending.load(tmp_path) == []
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})
    pending.record(tmp_path, {"assignment_id": "a2", "task_id": "t2"})
    entries = pending.load(tmp_path)
    assert {e["assignment_id"] for e in entries} == {"a1", "a2"}


def test_record_replaces_same_assignment(tmp_path: Path):
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1", "attempt": 1})
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1", "attempt": 2})
    entries = pending.load(tmp_path)
    assert len(entries) == 1 and entries[0]["attempt"] == 2


def test_remove_is_idempotent(tmp_path: Path):
    pending.record(tmp_path, {"assignment_id": "a1"})
    pending.remove(tmp_path, "a1")
    pending.remove(tmp_path, "a1")  # no error on double-remove
    assert pending.load(tmp_path) == []


def test_load_tolerates_corrupt_file(tmp_path: Path):
    (tmp_path / "pending_uploads.json").write_text("{ not json")
    assert pending.load(tmp_path) == []


def test_load_tolerates_non_list_json(tmp_path: Path):
    (tmp_path / "pending_uploads.json").write_text('{"oops": "not a list"}')
    assert pending.load(tmp_path) == []


def test_save_is_atomic_failed_commit_does_not_corrupt_existing_ledger(tmp_path: Path, monkeypatch):
    # This is a crash-safety net; a save that can itself leave a truncated
    # file on disk would defeat the whole point. Assert the INVARIANT rather
    # than the mechanism (temp file + os.replace today): kill the save
    # mid-write, leaving whatever partial content it got out on disk — the
    # ledger a subsequent load() sees must be the complete ORIGINAL, never a
    # truncated/corrupt version (which load() would drop wholesale).
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})
    before = pending.load(tmp_path)

    real_write_text = Path.write_text

    def partial_write(self, data, *a, **kw):
        real_write_text(self, data[: len(data) // 2], *a, **kw)  # half lands on disk
        raise OSError("simulated crash mid-write")
    monkeypatch.setattr(Path, "write_text", partial_write)
    with pytest.raises(OSError):
        pending.record(tmp_path, {"assignment_id": "a2", "task_id": "t2"})
    monkeypatch.undo()

    assert pending.load(tmp_path) == before  # untouched, not truncated/corrupted


# --- runloop._upload_trial: the shared upload+scrub+ledger logic -----------

class FakeClient:
    def __init__(self, behavior):
        self.behavior = behavior  # callable(assignment_id) -> dict | raises ApiError
        self.calls = []

    def submit(self, assignment_id, nonce, patch, trajectory, result, meta, outcome="completed"):
        self.calls.append(assignment_id)
        return self.behavior(assignment_id)


def _make_trial_dir(tmp_path: Path, name: str = "t") -> Path:
    trial_dir = tmp_path / name
    (trial_dir / "artifacts").mkdir(parents=True)
    (trial_dir / "artifacts" / "model.patch").write_text("diff --git a b\n")
    return trial_dir


def _entry(trial_dir: Path, **overrides) -> dict:
    """A pending-ledger entry dict — the shape _upload_trial takes."""
    e = {"assignment_id": "a1", "nonce": "nonce1", "task_id": "t1",
         "trial_dir": str(trial_dir), "meta": {}, "outcome": "completed",
         "job_dir": None, "keep": True}
    e.update(overrides)
    return e


def test_upload_success_clears_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})
    client = FakeClient(lambda aid: {"submission_id": "s1", "grade_status": "pending"})
    outcome = runloop._upload_trial(client, _entry(trial_dir, meta={"k": "v"}))
    assert outcome == "submitted"
    assert pending.load(tmp_path) == []


def test_upload_failure_records_ledger_entry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)

    def fail(aid):
        raise ApiError("server returned 500: internal error", status_code=500)
    client = FakeClient(fail)
    attempted = _entry(trial_dir, meta={"k": "v"})
    outcome = runloop._upload_trial(client, attempted)
    assert outcome == "upload-failed"
    entries = pending.load(tmp_path)
    assert len(entries) == 1
    assert entries[0] == attempted  # exactly what was attempted persists
    assert entries[0]["trial_dir"] == str(trial_dir)


def test_upload_409_means_already_landed_clears_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})

    def already(aid):
        raise ApiError("server returned 409: already submitted", status_code=409)
    client = FakeClient(already)
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "submitted"
    assert pending.load(tmp_path) == []


def test_upload_410_means_expired_clears_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})

    def expired(aid):
        raise ApiError("server returned 410: lease expired", status_code=410)
    client = FakeClient(expired)
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "expired"
    assert pending.load(tmp_path) == []


def test_transient_failure_with_409_in_message_is_not_misread_as_conflict(tmp_path: Path, monkeypatch):
    # A transport-level failure (never got a real HTTP response) has no
    # status_code -- even if the formatted message happens to contain the
    # digits "409" (e.g. embedded in the server URL/port), it must NOT be
    # treated as "already submitted". Only a real 409 response may do that.
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})

    def fail(aid):
        raise ApiError("cannot reach https://dradar.example.com:8409: connection refused")
    client = FakeClient(fail)
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "upload-failed"  # not "submitted"
    assert len(pending.load(tmp_path)) == 1  # entry survives for a real retry


def test_secret_patch_not_retryable_clears_any_ledger_entry(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = tmp_path / "t"
    (trial_dir / "artifacts").mkdir(parents=True)
    (trial_dir / "artifacts" / "model.patch").write_text(
        "diff --git a b\n+ghp_ABCDEFghijkl0123456789ABCDEFghijkl0123\n")
    pending.record(tmp_path, {"assignment_id": "a1", "task_id": "t1"})
    client = FakeClient(lambda aid: {"submission_id": "s1"})
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "not-uploaded"
    assert pending.load(tmp_path) == []  # not left to retry forever against the same secret
    assert not client.calls  # never even attempted the upload


def test_crash_mid_submit_leaves_a_ledger_entry(tmp_path: Path, monkeypatch):
    # The entry is recorded BEFORE the submit attempt: a process death
    # mid-POST (Ctrl-C/kill/OOM while the multipart upload is in flight)
    # must not orphan a completed, quota-burning trial. Simulate the death
    # with an exception that is not an ApiError, so nothing catches it.
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)

    def die(aid):
        raise KeyboardInterrupt
    client = FakeClient(die)
    attempted = _entry(trial_dir)
    with pytest.raises(KeyboardInterrupt):
        runloop._upload_trial(client, attempted)
    entries = pending.load(tmp_path)
    assert len(entries) == 1
    assert entries[0] == attempted  # survives for the next go/retry-upload


def test_successful_submit_leaves_no_pre_recorded_entry_behind(tmp_path: Path, monkeypatch):
    # Negative control for record-before-submit: on success the pre-recorded
    # entry must be removed, not linger and get re-uploaded on the next go.
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    trial_dir = _make_trial_dir(tmp_path)
    client = FakeClient(lambda aid: {"submission_id": "s1", "grade_status": "pending"})
    outcome = runloop._upload_trial(client, _entry(trial_dir))
    assert outcome == "submitted"
    assert client.calls == ["a1"]  # the upload really happened
    assert pending.load(tmp_path) == []


# --- retry scan: reconstructs artifacts from a trial_dir alone -------------

def test_artifacts_from_trial_dir_matches_runner_layout(tmp_path: Path):
    trial_dir = tmp_path / "trial"
    (trial_dir / "artifacts").mkdir(parents=True)
    (trial_dir / "artifacts" / "model.patch").write_text("diff\n")
    (trial_dir / "agent").mkdir()
    (trial_dir / "agent" / "trajectory.json").write_text("{}")
    (trial_dir / "result.json").write_text("{}")
    patch, traj, result = runloop._artifacts_from_trial_dir(trial_dir)
    assert patch == trial_dir / "artifacts" / "model.patch"
    assert traj == trial_dir / "agent" / "trajectory.json"
    assert result == trial_dir / "result.json"


def test_artifacts_from_trial_dir_tolerates_missing_optional_files(tmp_path: Path):
    trial_dir = tmp_path / "trial"
    (trial_dir / "artifacts").mkdir(parents=True)
    (trial_dir / "artifacts" / "model.patch").write_text("diff\n")
    patch, traj, result = runloop._artifacts_from_trial_dir(trial_dir)
    assert patch.is_file() and traj is None and result is None


def test_retry_scan_uploads_each_pending_entry(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    t1, t2 = _make_trial_dir(tmp_path, "t1"), _make_trial_dir(tmp_path, "t2")
    pending.record(tmp_path, {"assignment_id": "a1", "nonce": "n1", "task_id": "task1",
                              "trial_dir": str(t1), "meta": {}, "outcome": "completed"})
    pending.record(tmp_path, {"assignment_id": "a2", "nonce": "n2", "task_id": "task2",
                              "trial_dir": str(t2), "meta": {}, "outcome": "completed"})
    client = FakeClient(lambda aid: {"submission_id": f"s-{aid}", "grade_status": "pending"})
    runloop._retry_pending_uploads(client)
    assert set(client.calls) == {"a1", "a2"}
    assert pending.load(tmp_path) == []


def test_retry_scan_drops_entries_with_missing_local_artifacts(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    pending.record(tmp_path, {"assignment_id": "gone", "nonce": "n", "task_id": "t",
                              "trial_dir": str(tmp_path / "never-existed"), "meta": {},
                              "outcome": "completed"})
    client = FakeClient(lambda aid: {"submission_id": "s"})
    runloop._retry_pending_uploads(client)
    assert not client.calls  # never even tried the network call
    assert pending.load(tmp_path) == []


def test_retry_scan_honors_keep_flag_recorded_at_failure_time(tmp_path: Path, monkeypatch):
    # `dradar go --keep` must still mean "keep the job dir" even if the
    # upload fails and gets replayed later by retry-upload -- the ledger
    # entry is where that intent has to survive to, since the original
    # process (and its args.keep) is long gone by retry time.
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    t1 = _make_trial_dir(tmp_path, "t1")
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    pending.record(tmp_path, {"assignment_id": "a1", "nonce": "n1", "task_id": "task1",
                              "trial_dir": str(t1), "meta": {}, "outcome": "completed",
                              "job_dir": str(job_dir), "keep": True})
    client = FakeClient(lambda aid: {"submission_id": "s1", "grade_status": "pending"})
    runloop._retry_pending_uploads(client)
    assert job_dir.is_dir()  # --keep honored even on a replayed upload


def test_retry_scan_cleans_job_dir_when_keep_was_not_set(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    t1 = _make_trial_dir(tmp_path, "t1")
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    pending.record(tmp_path, {"assignment_id": "a1", "nonce": "n1", "task_id": "task1",
                              "trial_dir": str(t1), "meta": {}, "outcome": "completed",
                              "job_dir": str(job_dir)})  # no "keep" -- defaults False
    client = FakeClient(lambda aid: {"submission_id": "s1", "grade_status": "pending"})
    runloop._retry_pending_uploads(client)
    assert not job_dir.exists()


def test_retry_scan_is_silent_noop_when_nothing_pending(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    client = FakeClient(lambda aid: {"submission_id": "s"})
    runloop._retry_pending_uploads(client)
    assert not client.calls
    assert capsys.readouterr().out == ""


# --- cmd_retry_upload: the standalone command -------------------------------

def test_cmd_retry_upload_reports_all_clear(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_load_config", lambda: {"server": "https://x", "token": "t"})
    monkeypatch.setattr(runloop, "_client", lambda cfg: FakeClient(lambda aid: {"submission_id": "s"}))
    rc = runloop.cmd_retry_upload(None)
    assert rc == 0
    assert "nothing pending" in capsys.readouterr().out


def test_cmd_retry_upload_flushes_and_succeeds(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    t1 = _make_trial_dir(tmp_path, "t1")
    pending.record(tmp_path, {"assignment_id": "a1", "nonce": "n1", "task_id": "task1",
                              "trial_dir": str(t1), "meta": {}, "outcome": "completed"})
    monkeypatch.setattr(runloop, "_load_config", lambda: {"server": "https://x", "token": "t"})
    monkeypatch.setattr(runloop, "_client", lambda cfg: FakeClient(lambda aid: {"submission_id": "s"}))
    rc = runloop.cmd_retry_upload(None)
    assert rc == 0
    assert "all clear" in capsys.readouterr().out
    assert pending.load(tmp_path) == []


def test_cmd_retry_upload_partial_failure_reports_rc_1_and_keeps_entry(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    t1 = _make_trial_dir(tmp_path, "t1")
    pending.record(tmp_path, {"assignment_id": "a1", "nonce": "n1", "task_id": "task1",
                              "trial_dir": str(t1), "meta": {}, "outcome": "completed"})

    def fail(aid):
        raise ApiError("server returned 500: internal error", status_code=500)
    monkeypatch.setattr(runloop, "_load_config", lambda: {"server": "https://x", "token": "t"})
    monkeypatch.setattr(runloop, "_client", lambda cfg: FakeClient(fail))
    rc = runloop.cmd_retry_upload(None)
    assert rc == 1
    assert "still pending" in capsys.readouterr().out
    entries = pending.load(tmp_path)
    assert len(entries) == 1 and entries[0]["assignment_id"] == "a1"  # kept for the next retry
