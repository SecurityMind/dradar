"""deep-swe version pin: client-side commit detection (compared against the
server's advertised grading commit — see the server repo's test suite for
the server-side half of this pin)."""

import os
import subprocess

import pytest

from dradar import runloop
from dradar.runner import local_deep_swe_commit


def test_local_deep_swe_commit_non_repo(tmp_path):
    assert local_deep_swe_commit(tmp_path) is None


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)
def test_local_deep_swe_commit_real_repo(tmp_path):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(tmp_path),  # ignore user gitconfig (signing hooks etc.)
    }
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, env=env, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "f").write_text("x")
    git("add", "f")
    git("commit", "-q", "-m", "c1")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # tasks_root is typically a subdir of the repo; both must resolve.
    sub = tmp_path / "tasks"
    sub.mkdir()
    assert local_deep_swe_commit(tmp_path) == head
    assert local_deep_swe_commit(sub) == head


# --- _check_version_pin: drift handling (self-heal / hard-stop / --allow-task-drift)

LOCAL = "a" * 40
PINNED = "b" * 40


def _pin(monkeypatch, tmp_path, *, sync_ok, allow_drift, pinned=PINNED):
    monkeypatch.setattr(runloop, "local_deep_swe_commit", lambda root: LOCAL)
    synced = []

    def fake_sync(root, commit):
        synced.append(commit)
        return sync_ok

    monkeypatch.setattr(runloop, "sync_deep_swe_commit", fake_sync)
    return runloop._check_version_pin(pinned, tmp_path, allow_drift), synced


def test_version_pin_match_is_silent(monkeypatch, tmp_path, capsys):
    got, synced = _pin(monkeypatch, tmp_path, sync_ok=False, allow_drift=False,
                       pinned=LOCAL)
    assert got == LOCAL
    assert synced == []  # no drift -> no sync attempt
    assert capsys.readouterr().out == ""


def test_version_pin_drift_self_heals_via_sync(monkeypatch, tmp_path, capsys):
    got, synced = _pin(monkeypatch, tmp_path, sync_ok=True, allow_drift=False)
    assert got == PINNED
    assert synced == [PINNED]
    assert "synced" in capsys.readouterr().out


def test_version_pin_drift_sync_failure_hard_stops_with_fix(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as ei:
        _pin(monkeypatch, tmp_path, sync_ok=False, allow_drift=False)
    msg = str(ei.value)
    # names both commits and gives the exact recovery commands
    assert LOCAL in msg and PINNED in msg
    assert "git" in msg and "fetch" in msg and "checkout" in msg


def test_version_pin_drift_allowed_warns_and_proceeds(monkeypatch, tmp_path, capsys):
    got, synced = _pin(monkeypatch, tmp_path, sync_ok=False, allow_drift=True)
    assert got == LOCAL
    assert "warning" in capsys.readouterr().out
