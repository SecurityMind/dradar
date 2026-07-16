"""Interrupted-run honesty (from the first real volunteer bug report,
2026-07-13): the CLI must pass the server's agent-version pin to pier (busts
stale image caches that shipped codex too old for gpt-5.6), print the actual
in-container exception instead of a blanket "wait for your quota", and keep
the failure artifacts instead of deleting the only evidence."""
import json
from pathlib import Path

import dradar.runloop as runloop
import dradar.runner as runner_mod
from dradar.runner import build_pier_command, diagnose_exception

from test_go_menu import ASSIGNMENT, SubmitClient, _args, _fake_art

STALE_MSG = (
    'Command failed (exit 1): codex exec ...\nstdout: {"type":"error","message":'
    '"{\\"status\\":400,\\"error\\":{\\"message\\":\\"The \'gpt-5.6-sol\' model '
    'requires a newer version of Codex. Please upgrade to the latest app or CLI '
    'and try again.\\"}}"}')


def _codex_cmd(tmp_path, monkeypatch, assignment):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _: "/usr/bin/pier")
    (tmp_path / assignment["task_id"]).mkdir(exist_ok=True)
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return build_pier_command(assignment, tmp_path, tmp_path / "jobs", "j", home)


def test_pier_command_pins_server_agent_version(tmp_path, monkeypatch):
    a = {"assignment_id": "a1", "task_id": "t", "agent": "codex",
         "model": "gpt-5.6-sol", "effort": "low", "agent_version": "0.144.1"}
    cmd = _codex_cmd(tmp_path, monkeypatch, a)
    assert "version=0.144.1" in cmd
    assert cmd[cmd.index("version=0.144.1") - 1] == "--ak"


def test_pier_command_without_pin_stays_legacy(tmp_path, monkeypatch):
    a = {"assignment_id": "a1", "task_id": "t", "agent": "codex",
         "model": "gpt-5.6-sol", "effort": "low"}
    cmd = _codex_cmd(tmp_path, monkeypatch, a)
    assert not any(x.startswith("version=") for x in cmd)


def _result(tmp_path, message, exc_type="NonZeroAgentExitCodeError"):
    p = tmp_path / "result.json"
    p.write_text(json.dumps({"exception_info": {
        "exception_type": exc_type, "exception_message": message}}))
    return p


def test_diagnose_classifies_stale_agent(tmp_path):
    d = diagnose_exception(_result(tmp_path, STALE_MSG))
    assert d["kind"] == "stale-agent"
    assert d["type"] == "NonZeroAgentExitCodeError"
    assert any("requires a newer version" in ln for ln in d["tail"])


def test_diagnose_classifies_rate_limit(tmp_path):
    d = diagnose_exception(_result(tmp_path, "codex: usage_limit_reached, retry later"))
    assert d["kind"] == "rate-limit"


def test_diagnose_classifies_model_capacity(tmp_path):
    d = diagnose_exception(_result(tmp_path,
        "turn.failed: Selected model is at capacity. Please try a different model."))
    assert d["kind"] == "model-capacity"


def test_diagnose_unrecognized_has_no_kind(tmp_path):
    d = diagnose_exception(_result(tmp_path, "segfault in libfoo"))
    assert d["kind"] is None and d["tail"]


def test_diagnose_empty_without_exception(tmp_path):
    p = tmp_path / "result.json"
    p.write_text(json.dumps({"agent_result": {}}))
    assert diagnose_exception(p) == {}


class InvalidAckClient(SubmitClient):
    def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
               outcome="completed"):
        super().submit(assignment_id, nonce, patch, trajectory, result, meta,
                       outcome=outcome)
        return {"submission_id": f"s-{assignment_id}", "grade_status": "invalid"}


def test_interrupted_prints_cause_keeps_artifacts_no_quota_claim(
        monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, result_data={
        "exception_info": {"exception_type": "NonZeroAgentExitCodeError",
                           "exception_message": STALE_MSG},
        "agent_result": {}})
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = InvalidAckClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    assert tag == "interrupted"
    assert client.submissions[0]["meta"]["exception_type"] == "NonZeroAgentExitCodeError"
    out = capsys.readouterr().out
    assert "NonZeroAgentExitCodeError" in out
    assert "requires a newer version" in out       # the agent's real error, surfaced
    assert "quota" not in out.lower()              # no unfounded quota guess
    assert art.job_dir.is_dir()                    # failure artifacts survive
    assert str(art.job_dir) in out                 # ...and the path is announced


def test_interrupted_rate_limit_advice_mentions_quota(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, result_data={
        "exception_info": {"exception_type": "AgentError",
                           "exception_message": "429 Too Many Requests: rate limit"},
        "agent_result": {}})
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = InvalidAckClient({})
    runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    out = capsys.readouterr().out
    assert "rate/usage limit" in out


def test_interrupted_model_capacity_advice_is_not_a_quota_guess(
        monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, result_data={
        "exception_info": {"exception_type": "NonZeroAgentExitCodeError",
                           "exception_message":
                               "Selected model is at capacity. Please try a different model."},
        "agent_result": {}})
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = InvalidAckClient({})
    runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    out = capsys.readouterr().out
    assert "retried the original Codex session" in out
    assert "wait for your quota" not in out.lower()   # not the rate-limit advice


def test_completed_run_still_cleans_job_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    assert tag == "submitted"
    assert not art.job_dir.exists()  # tidy-by-default unchanged for successes


def test_failed_trial_reports_stopped_to_server(monkeypatch, tmp_path):
    from dradar.runner import RunnerError
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")

    def always_fails(*a, **kw):
        raise RunnerError("model.patch missing")

    monkeypatch.setattr(runloop, "run_trial", always_fails)
    stopped = []
    client = SubmitClient({})
    client.mark_stopped = lambda aid: stopped.append(aid)
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc")
    assert tag == "failed"
    assert stopped == [ASSIGNMENT["assignment_id"]]
