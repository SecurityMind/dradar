import dradar.runner as runner_mod
from dradar.runner import CLAUDE_DISALLOWED_TOOLS, build_pier_command


def _assignment(agent, model="gpt-5.5", effort="medium"):
    return {"assignment_id": "a1", "task_id": "abs-module-cache-flags",
            "agent": agent, "model": model, "effort": effort}


def _stub_pier(monkeypatch):
    # build_pier_command resolves pier via shutil.which; stub it so the test
    # doesn't depend on pier being on the runner's PATH.
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _: "/usr/bin/pier")


def test_codex_disables_web_search(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    # make the local task path exist so build_pier_command doesn't bail
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir()
    build_pier_command(_assignment("codex"), tmp_path, tmp_path / "jobs", "j", home)
    allowlist = (home / "codex-chatgpt-allowlist.toml").read_text()
    # web_search must be a top-level string key BEFORE any [table] header, or
    # TOML nests it and codex ignores it (verified: bool/nested = no effect).
    assert 'web_search = "disabled"' in allowlist
    assert allowlist.index("web_search") < allowlist.index("[__pier_allowlist]")


def test_claude_code_disallows_web_tools(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    cmd = build_pier_command(_assignment("claude-code", model="claude-sonnet-5", effort="high"),
                             tmp_path, tmp_path / "jobs", "j", tmp_path / "home")
    assert f"disallowed_tools={CLAUDE_DISALLOWED_TOOLS}" in cmd
    assert "WebSearch" in CLAUDE_DISALLOWED_TOOLS and "WebFetch" in CLAUDE_DISALLOWED_TOOLS


# --- self-bootstrap (ensure_pier / ensure_tasks_root) ------------------------
import subprocess
from pathlib import Path

import pytest
from dradar.runner import RunnerError, ensure_pier, ensure_tasks_root


def test_ensure_pier_noop_when_present(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: "/usr/bin/pier")
    called = []
    monkeypatch.setattr(runner_mod.subprocess, "run", lambda *a, **k: called.append(a))
    ensure_pier()
    assert called == []            # already there -> never shells out


def test_ensure_pier_installs_via_uv_when_missing(monkeypatch):
    seen = {"pier": None}  # pier missing first, present after "install"
    def which(name):
        if name == "uv":
            return "/usr/bin/uv"
        return seen["pier"]
    monkeypatch.setattr(runner_mod.shutil, "which", which)
    def fake_run(cmd, *a, **k):
        assert cmd == ["/usr/bin/uv", "tool", "install", "datacurve-pier"]
        seen["pier"] = "/root/.local/bin/pier"   # simulate the install landing
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    ensure_pier()                  # should not raise


def test_ensure_pier_errors_when_no_uv(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: None)
    with pytest.raises(RunnerError, match="uv"):
        ensure_pier()


def test_ensure_tasks_root_noop_when_present(tmp_path, monkeypatch):
    tr = tmp_path / "deep-swe" / "tasks"
    tr.mkdir(parents=True)
    monkeypatch.setattr(runner_mod.subprocess, "run",
                        lambda *a, **k: pytest.fail("should not clone"))
    ensure_tasks_root(tr)          # exists -> no clone


def test_ensure_tasks_root_rejects_non_tasks_path(tmp_path):
    with pytest.raises(RunnerError, match="deep-swe/tasks"):
        ensure_tasks_root(tmp_path / "somewhere" / "else")


def test_ensure_tasks_root_wont_clobber_nonempty_parent(tmp_path):
    repo = tmp_path / "deep-swe"
    repo.mkdir()
    (repo / "junk").write_text("x")   # parent exists, non-empty, no tasks/
    with pytest.raises(RunnerError, match="not touching"):
        ensure_tasks_root(repo / "tasks")


def test_ensure_tasks_root_clones_when_missing(tmp_path, monkeypatch):
    tr = tmp_path / "deep-swe" / "tasks"
    def fake_run(cmd, *a, **k):
        assert cmd[0] == "git" and cmd[1] == "clone"
        (tr).mkdir(parents=True)   # simulate the clone creating tasks/
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    ensure_tasks_root(tr)
    assert tr.is_dir()


# --- run_trial / summarize_result (stubbed pier) ------------------------------
import json

from dradar.runner import _trial_timeout_sec, run_trial, summarize_result


def _fake_pier(monkeypatch, work_dir, *, patch=True, trajectory=True,
               result=None, rc=0):
    """Stub build_pier_command + subprocess.run; the fake 'pier' lays down the
    trial-dir layout the real one would. Returns a dict capturing job_name."""
    captured = {}

    def fake_build(assignment, tasks_root, jobs_dir, job_name, home, dev_agent=None):
        captured["job_name"] = job_name
        return ["pier", "run", job_name]

    def fake_run(cmd, **kw):
        trial = work_dir / "jobs" / captured["job_name"] / "task__t0"
        (trial / "artifacts").mkdir(parents=True)
        (trial / "agent").mkdir()
        if patch:
            (trial / "artifacts" / "model.patch").write_text("diff")
        if trajectory:
            (trial / "agent" / "trajectory.json").write_text("[]")
        if result is not None:
            (trial / "result.json").write_text(json.dumps(result))
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(runner_mod, "build_pier_command", fake_build)
    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    return captured


def test_run_trial_on_started_exception_is_swallowed(tmp_path, monkeypatch):
    _fake_pier(monkeypatch, tmp_path)
    calls = []
    def boom():
        calls.append(True)
        raise RuntimeError("network hiccup")
    # a failed started-ping must never abort a quota-burning trial
    art = run_trial(_assignment("codex"), tmp_path, tmp_path, on_started=boom)
    assert calls == [True]
    assert art.returncode == 0 and art.patch.is_file()
    assert art.trajectory is not None and art.trajectory.is_file()


def test_run_trial_timeout_raises_naming_log(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "build_pier_command", lambda *a, **k: ["pier"])
    def fake_run(cmd, **kw):
        # the wedged "pier" wrote its dying words to the log before hanging
        kw["stdout"].write("docker: no space left on device\n")
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    with pytest.raises(RunnerError) as exc:
        run_trial(_assignment("codex"), tmp_path, tmp_path)
    assert str(tmp_path / "aa1.log") in str(exc.value)
    # the actual cause is inlined, not just the file name
    assert "docker: no space left on device" in str(exc.value)


def test_run_trial_missing_patch_raises(tmp_path, monkeypatch):
    _fake_pier(monkeypatch, tmp_path, patch=False)
    with pytest.raises(RunnerError, match="model.patch missing"):
        run_trial(_assignment("codex"), tmp_path, tmp_path)


def test_run_trial_missing_patch_message_includes_log_tail(tmp_path, monkeypatch):
    captured = {}
    def fake_build(assignment, tasks_root, jobs_dir, job_name, home, dev_agent=None):
        captured["job_name"] = job_name
        return ["pier"]
    def fake_run(cmd, **kw):
        trial = tmp_path / "jobs" / captured["job_name"] / "task__t0"
        (trial / "artifacts").mkdir(parents=True)   # no model.patch inside
        kw["stdout"].write("agent auth rejected (401)\n")
        return subprocess.CompletedProcess(cmd, 1)
    monkeypatch.setattr(runner_mod, "build_pier_command", fake_build)
    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    with pytest.raises(RunnerError) as exc:
        run_trial(_assignment("codex"), tmp_path, tmp_path)
    assert "model.patch missing" in str(exc.value)
    assert "agent auth rejected (401)" in str(exc.value)


def test_tail_keeps_only_the_last_n_lines(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("".join(f"line{i}\n" for i in range(30)))
    tail = runner_mod._tail(p, n=15)
    got = tail.splitlines()
    assert got[0] == "line15" and got[-1] == "line29" and len(got) == 15


def test_tail_of_a_missing_log_is_empty(tmp_path):
    assert runner_mod._tail(tmp_path / "nope.log") == ""


def test_run_trial_missing_trajectory_and_result_are_none(tmp_path, monkeypatch):
    _fake_pier(monkeypatch, tmp_path, trajectory=False, result=None)
    art = run_trial(_assignment("codex"), tmp_path, tmp_path)
    assert art.trajectory is None and art.result is None


def test_run_trial_stale_job_dir_gets_suffixed_name(tmp_path, monkeypatch):
    # leftover dir from an earlier run of the same lease must not collide
    (tmp_path / "jobs" / "aa1").mkdir(parents=True)
    captured = _fake_pier(monkeypatch, tmp_path)
    art = run_trial(_assignment("codex"), tmp_path, tmp_path)
    assert captured["job_name"].startswith("aa1-")
    assert art.trial_dir.is_dir()


def test_trial_timeout_defaults_and_floor():
    # missing/None estimate falls back to 30 min -> 30*60*4
    assert _trial_timeout_sec({}) == 7200
    assert _trial_timeout_sec({"est_minutes": None}) == 7200
    assert _trial_timeout_sec({"est_minutes": 5}) == 1800   # floor wins


def test_trial_timeout_scales_with_estimate():
    assert _trial_timeout_sec({"est_minutes": 30}) == 7200
    assert _trial_timeout_sec({"est_minutes": 120}) == 28800


def test_summarize_result_exception_info_present(tmp_path):
    p = tmp_path / "result.json"
    p.write_text(json.dumps({"agent_result": {"n_input_tokens": 5},
                             "exception_info": {"type": "RateLimit"}}))
    s = summarize_result(p)
    assert s["exception_info"] is True and s["n_input_tokens"] == 5


def test_summarize_result_exception_info_absent(tmp_path):
    p = tmp_path / "result.json"
    p.write_text(json.dumps({"agent_result": {"n_output_tokens": 7}}))
    s = summarize_result(p)
    assert s["exception_info"] is False and s["n_output_tokens"] == 7


def test_summarize_result_corrupt_json(tmp_path):
    p = tmp_path / "result.json"
    p.write_text("{not json")
    assert summarize_result(p) == {}
