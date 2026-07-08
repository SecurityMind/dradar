"""deep-swe version pin: client-side commit detection (compared against the
server's advertised grading commit — see the server repo's test suite for
the server-side half of this pin)."""

import os
import subprocess

import pytest

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
