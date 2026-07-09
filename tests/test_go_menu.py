"""_go_menu: quota is informational only now — the disclosure line fires
exactly when it should (real run + nonzero estimate) and stays quiet
otherwise (dev-agent runs, zero/missing estimate). No case here should ever
refuse to proceed on quota grounds -- that gate was deliberately removed."""

import argparse
from pathlib import Path

from dradar import runloop

ASSIGNMENT = {
    "assignment_id": "a1", "task_id": "t1", "model": "m", "effort": "e",
    "agent": "claude", "expires_at": "2099-01-01T00:00:00Z",
    "est_minutes": 42, "est_quota_pct": 17, "nonce": "n1",
    "deep_swe_commit": None,
}


class FakeClient:
    def __init__(self, assignment_data):
        self._data = assignment_data

    def get_assignment(self):
        return self._data


def _args(yes=True, dev_agent=None):
    return argparse.Namespace(yes=yes, dev_agent=dev_agent, resume=False,
                              allow_task_drift=False, keep=False)


def _patch_run(monkeypatch, outcome="submitted"):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **k: None)
    monkeypatch.setattr(runloop, "_run_and_submit", lambda *a, **k: outcome)


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
    assert "aborted (lease stays active" in out
    assert rc == 1
