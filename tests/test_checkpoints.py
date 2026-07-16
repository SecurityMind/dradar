import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dradar import checkpoints, runloop


def _make_checkpoint(
    home: Path,
    assignment_id: str,
    *,
    checkpoint_id: str = "checkpoint-12345678",
    phase: str = "paused",
    generation: int = 0,
    updated_at: str | None = None,
    suffix: str = "one",
) -> checkpoints.Checkpoint:
    job = home / "work" / "jobs" / f"a{assignment_id}-{suffix}"
    checkpoint = job / "task__trial" / "agent" / "checkpoint"
    checkpoint.mkdir(parents=True)
    (checkpoint / "checkpoint.json").write_text(json.dumps({
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "assignment_id": assignment_id,
        "phase": phase,
        "created_at": "2026-07-16T00:00:00Z",
        "updated_at": updated_at or "2026-07-16T01:00:00Z",
        "last_heartbeat": updated_at or "2026-07-16T01:00:00Z",
        "model": "gpt-test",
        "task_id": "task-1",
        "effort": "high",
        "base_commit": "abc",
        "resume_generation": generation,
        "root_thread_id": "thread-1",
    }))
    return checkpoints.scan(home)[0]


def _assignment(assignment_id: str, generation: int = 0) -> dict:
    return {
        "assignment_id": assignment_id,
        "nonce": "server-only-nonce",
        "task_id": "task-1",
        "model": "gpt-test",
        "effort": "high",
        "resume_generation": generation,
        "checkpoint_id": "checkpoint-12345678",
        "deep_swe_commit": None,
    }


def _args(**overrides):
    values = dict(
        dev_agent=None, keep=False, allow_task_drift=False,
        parallel=False, assignment=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_scan_reads_metadata_but_never_requires_nonce_or_account_token(tmp_path: Path):
    item = _make_checkpoint(tmp_path, "1" * 32, generation=4)
    assert item.valid
    assert item.assignment_id == "1" * 32
    assert item.resume_generation == 4
    raw = item.manifest_path.read_text().lower()
    assert "nonce" not in raw
    assert "account_token" not in raw


def test_corrupt_manifest_infers_assignment_from_job_name(tmp_path: Path):
    assignment_id = "a" * 32
    item = _make_checkpoint(tmp_path, assignment_id)
    item.manifest_path.write_text("{broken")
    loaded = checkpoints.scan(tmp_path)[0]
    assert not loaded.valid
    assert loaded.phase == "invalid"
    assert loaded.assignment_id == assignment_id


def test_cleanup_removes_superseded_copies_but_can_keep_explicit_final_dir(tmp_path: Path):
    aid = "2" * 32
    old = _make_checkpoint(tmp_path, aid, suffix="old")
    new = _make_checkpoint(
        tmp_path, aid, checkpoint_id="checkpoint-abcdefgh", suffix="new",
        updated_at="2026-07-16T02:00:00Z",
    )
    checkpoints.cleanup_assignment(tmp_path, aid, keep_job_dir=new.job_dir)
    assert not old.job_dir.exists()
    assert new.job_dir.is_dir()
    checkpoints.cleanup_assignment(tmp_path, aid)
    assert not new.job_dir.exists()


def test_expiry_uses_checkpoint_heartbeat(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    item = _make_checkpoint(tmp_path, "3" * 32, updated_at=old)
    assert checkpoints.is_expired(item)


def test_assignment_lock_fences_a_second_worker_only_for_same_assignment(tmp_path: Path):
    with checkpoints.assignment_lock(tmp_path, "a1"):
        with pytest.raises(checkpoints.CheckpointBusy):
            with checkpoints.assignment_lock(tmp_path, "a1"):
                pass
        with checkpoints.assignment_lock(tmp_path, "a2"):
            pass


class _RecoveryClient:
    def __init__(self, assignment):
        self.assignment = assignment
        self.resumes = []
        self.discards = []

    def checkpoint_resume(self, assignment_id, checkpoint_id, generation, session_id=None):
        self.resumes.append((assignment_id, checkpoint_id, generation, session_id))
        resumed = dict(self.assignment, resume_generation=generation + 1)
        return {"assignment": resumed}

    def checkpoint_discard(self, assignment_id, checkpoint_id, generation, reason):
        self.discards.append((assignment_id, checkpoint_id, generation, reason))
        return {"ok": True}


def test_resume_one_passes_checkpoint_and_new_generation_to_runner(
    tmp_path: Path, monkeypatch,
):
    aid = "4" * 32
    item = _make_checkpoint(tmp_path, aid, generation=2)
    assignment = _assignment(aid, generation=2)
    client = _RecoveryClient(assignment)
    seen = {}
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **k: "base")

    def fake_run(client_, resumed, tasks_root, args, local_commit, **kwargs):
        seen["assignment"] = resumed
        seen["checkpoint"] = kwargs["resume_checkpoint"]
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", fake_run)
    outcome = runloop._resume_one_checkpoint(
        client, item, assignment, _args(), tmp_path / "tasks", None,
    )
    assert outcome == "submitted"
    assert client.resumes[0][2] == 2
    assert seen["assignment"]["resume_generation"] == 3
    assert seen["checkpoint"].checkpoint_id == item.checkpoint_id


def test_invalid_checkpoint_discards_server_lease_and_all_local_copies(
    tmp_path: Path, monkeypatch,
):
    aid = "5" * 32
    item = _make_checkpoint(tmp_path, aid)
    item.manifest_path.write_text("{broken")
    invalid = checkpoints.scan(tmp_path)[0]
    assignment = _assignment(aid)
    client = _RecoveryClient(assignment)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    outcome = runloop._resume_one_checkpoint(
        client, invalid, assignment, _args(), tmp_path / "tasks", None,
    )
    assert outcome == "discarded"
    assert client.discards[0][3] == "invalid"
    assert checkpoints.scan(tmp_path) == []
