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


def test_codex_prompt_requires_submission_artifact(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir()

    cmd = build_pier_command(_assignment("codex"), tmp_path, tmp_path / "jobs", "j", home)

    prompt = home / "codex-submission-prompt.j2"
    assert f"prompt_template_path={prompt}" in cmd
    text = prompt.read_text()
    assert "{{ instruction }}" in text
    assert "bash /tests/pre_artifacts.sh" in text
    assert "test -s /logs/artifacts/model.patch" in text


def test_claude_code_disallows_web_tools(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = tmp_path / "abs-module-cache-flags"
    task.mkdir()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    cmd = build_pier_command(_assignment("claude-code", model="claude-sonnet-5", effort="high"),
                             tmp_path, tmp_path / "jobs", "j", tmp_path / "home")
    assert f"disallowed_tools={CLAUDE_DISALLOWED_TOOLS}" in cmd
    assert "WebSearch" in CLAUDE_DISALLOWED_TOOLS and "WebFetch" in CLAUDE_DISALLOWED_TOOLS


# --- pier's inner agent timeout must never undercut DRadar's own outer one --
# (volunteer report #4, 2026-07-15: task.toml declares a flat 5400s/90min
# agent timeout across the whole deep-swe set; DRadar's own outer watchdog
# scales up to 4x the server's estimate, but build_pier_command never told
# pier to stretch its OWN timeout to match, so pier killed long/heavy cells
# far before DRadar's watchdog ever would have).

def _task_with_toml(tmp_path, task_id="t", timeout_sec=5400.0):
    task = tmp_path / task_id
    task.mkdir()
    (task / "task.toml").write_text(f"[agent]\ntimeout_sec = {timeout_sec}\n")
    return task


def test_task_agent_timeout_sec_reads_task_toml(tmp_path):
    task = _task_with_toml(tmp_path, timeout_sec=5400.0)
    assert runner_mod._task_agent_timeout_sec(task) == 5400.0


def test_task_agent_timeout_sec_none_when_missing(tmp_path):
    task = tmp_path / "no-toml"
    task.mkdir()
    assert runner_mod._task_agent_timeout_sec(task) is None


def test_task_agent_timeout_sec_none_when_malformed(tmp_path):
    task = tmp_path / "bad-toml"
    task.mkdir()
    (task / "task.toml").write_text("this is not [ valid toml")
    assert runner_mod._task_agent_timeout_sec(task) is None


def test_multiplier_stretches_pier_to_match_drader_outer_cap(tmp_path):
    # est_minutes=68 -> outer = max(1800, 68*60*4) = 16320s; base 5400s ->
    # pier must be stretched so its own timeout is >= outer, plus slack.
    task = _task_with_toml(tmp_path, timeout_sec=5400.0)
    assignment = {"est_minutes": 68}
    m = runner_mod._agent_timeout_multiplier(assignment, task)
    assert m > 1.0
    assert m * 5400.0 >= 16320 + 60


def test_multiplier_never_shrinks_below_one(tmp_path):
    # A short-estimate cell: outer cap (1800s floor) is well under the task's
    # own 5400s default -- must NOT ask pier to shrink its own timeout.
    task = _task_with_toml(tmp_path, timeout_sec=5400.0)
    assignment = {"est_minutes": 5}
    assert runner_mod._agent_timeout_multiplier(assignment, task) == 1.0


def test_multiplier_is_one_without_task_toml(tmp_path):
    task = tmp_path / "no-toml"
    task.mkdir()
    assert runner_mod._agent_timeout_multiplier({"est_minutes": 200}, task) == 1.0


def test_build_pier_command_passes_multiplier_for_long_estimate(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = _task_with_toml(tmp_path, task_id="abs-module-cache-flags", timeout_sec=5400.0)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    a = _assignment("claude-code", model="claude-sonnet-5", effort="high")
    a["est_minutes"] = 68
    cmd = build_pier_command(a, tmp_path, tmp_path / "jobs", "j", tmp_path / "home")
    assert "--agent-timeout-multiplier" in cmd
    got = float(cmd[cmd.index("--agent-timeout-multiplier") + 1])
    assert got * 5400.0 >= 16320 + 60


def test_build_pier_command_omits_multiplier_for_short_estimate(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = _task_with_toml(tmp_path, task_id="abs-module-cache-flags", timeout_sec=5400.0)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    a = _assignment("claude-code", model="claude-sonnet-5", effort="high")
    a["est_minutes"] = 5
    cmd = build_pier_command(a, tmp_path, tmp_path / "jobs", "j", tmp_path / "home")
    assert "--agent-timeout-multiplier" not in cmd


def test_codex_command_enables_credential_free_checkpoint_metadata(tmp_path, monkeypatch):
    _stub_pier(monkeypatch)
    task = tmp_path / "task"
    task.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(auth))
    resume = tmp_path / "previous" / "checkpoint"
    a = _assignment("codex") | {
        "assignment_id": "a123", "task_id": "task",
        "resume_generation": 3,
    }
    cmd = build_pier_command(
        a, tmp_path, tmp_path / "jobs", "j", tmp_path / "home",
        resume_checkpoint=resume,
    )
    agent_values = [cmd[i + 1] for i, value in enumerate(cmd[:-1]) if value == "--ak"]
    assert "checkpoint_enabled=true" in agent_values
    assert "checkpoint_assignment_id=a123" in agent_values
    assert "checkpoint_task_id=task" in agent_values
    assert "checkpoint_resume_generation=3" in agent_values
    assert f"checkpoint_path={resume}" in agent_values
    # Auth is injected separately into the ephemeral container, never encoded
    # in the persistent checkpoint metadata.
    assert not any("auth" in value.lower() or "token" in value.lower()
                   for value in agent_values if value.startswith("checkpoint_"))


# --- self-bootstrap (ensure_pier / ensure_tasks_root) ------------------------
import subprocess
from pathlib import Path

import pytest
from dradar.runner import RunnerError, ensure_pier, ensure_tasks_root


def test_ensure_pier_noop_when_required_version_present(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: "/usr/bin/pier")
    monkeypatch.setattr(runner_mod, "_pier_version", lambda _: runner_mod.PIER_VERSION)
    called = []
    monkeypatch.setattr(runner_mod.subprocess, "run", lambda *a, **k: called.append(a))
    ensure_pier()
    assert called == []            # approved build -> never installs


def test_ensure_pier_accepts_newer_compatible_post_release(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: "/usr/bin/pier")
    monkeypatch.setattr(runner_mod, "_pier_version", lambda _: "0.3.0.post3")
    called = []
    monkeypatch.setattr(runner_mod.subprocess, "run", lambda *a, **k: called.append(a))
    ensure_pier()
    assert called == []


def test_ensure_pier_installs_via_uv_when_missing(monkeypatch):
    seen = {"pier": None}  # pier missing first, present after "install"
    def which(name):
        if name == "uv":
            return "/usr/bin/uv"
        return seen["pier"]
    monkeypatch.setattr(runner_mod.shutil, "which", which)
    monkeypatch.setattr(
        runner_mod, "_pier_version",
        lambda path: runner_mod.PIER_VERSION if path else None,
    )
    def fake_run(cmd, *a, **k):
        assert cmd == [
            "/usr/bin/uv", "tool", "install", "--force", runner_mod.PIER_SPEC,
        ]
        seen["pier"] = "/root/.local/bin/pier"   # simulate the install landing
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    ensure_pier()                  # should not raise


def test_ensure_pier_replaces_old_version(monkeypatch):
    versions = iter(["0.3.0", runner_mod.PIER_VERSION])
    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(runner_mod, "_pier_version", lambda _: next(versions))
    called = []
    monkeypatch.setattr(
        runner_mod.subprocess, "run",
        lambda cmd, *a, **k: called.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    ensure_pier()

    assert called == [[
        "/usr/bin/uv", "tool", "install", "--force", runner_mod.PIER_SPEC,
    ]]


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

    class FakePopen:
        # run_trial drives pier via Popen + a heartbeat wait loop; the fake
        # lays the artifacts down at construction ("process started and
        # finished") and reports done on the first wait().
        def __init__(self, cmd, **kw):
            trial = work_dir / "jobs" / captured["job_name"] / "task__t0"
            (trial / "artifacts").mkdir(parents=True)
            (trial / "agent").mkdir()
            if patch:
                (trial / "artifacts" / "model.patch").write_text("diff")
            if trajectory:
                (trial / "agent" / "trajectory.json").write_text("[]")
            if result is not None:
                (trial / "result.json").write_text(json.dumps(result))
            self.returncode = rc

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(runner_mod, "build_pier_command", fake_build)
    monkeypatch.setattr(runner_mod.subprocess, "Popen", FakePopen)
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
    # deadline already passed -> the very first heartbeat check aborts
    monkeypatch.setattr(runner_mod, "_trial_timeout_sec", lambda a: -1)
    killed = []

    class HungPopen:
        def __init__(self, cmd, **kw):
            # the wedged "pier" wrote its dying words to the log before hanging
            kw["stdout"].write("docker: no space left on device\n")
            kw["stdout"].flush()

        def wait(self, timeout=None):
            if killed:
                return -9
            raise subprocess.TimeoutExpired("pier", timeout)

        def terminate(self):
            pass  # the hung pier ignores TERM; the grace window must escalate

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(runner_mod.subprocess, "Popen", HungPopen)
    with pytest.raises(RunnerError) as exc:
        run_trial(_assignment("codex"), tmp_path, tmp_path)
    assert killed  # the wedged process was reaped, not left running
    assert str(tmp_path / "aa1.log") in str(exc.value)
    # the actual cause is inlined, not just the file name
    assert "docker: no space left on device" in str(exc.value)


def test_run_trial_missing_patch_raises(tmp_path, monkeypatch):
    _fake_pier(monkeypatch, tmp_path, patch=False)
    with pytest.raises(RunnerError, match="model.patch missing"):
        run_trial(_assignment("codex"), tmp_path, tmp_path)


def test_run_trial_classifies_build_failure_from_nested_result(tmp_path, monkeypatch):
    # Pier's console tail can contain only a generic teardown; the actual
    # Docker failure from the production case is preserved in result.json.
    _fake_pier(
        monkeypatch, tmp_path, patch=False,
        result={"exception_info": {
            "exception_type": "RuntimeError",
            "exception_message": "RUN apt-get update: failed to solve: exit code 100",
        }},
    )
    with pytest.raises(runner_mod.BuildFlakeError) as exc:
        run_trial(_assignment("codex"), tmp_path, tmp_path)
    assert "agent never started" in str(exc.value)
    assert "failed to solve" in str(exc.value)


def test_run_trial_missing_patch_message_includes_log_tail(tmp_path, monkeypatch):
    captured = {}
    def fake_build(assignment, tasks_root, jobs_dir, job_name, home, dev_agent=None):
        captured["job_name"] = job_name
        return ["pier"]
    class FakePopen:
        def __init__(self, cmd, **kw):
            trial = tmp_path / "jobs" / captured["job_name"] / "task__t0"
            (trial / "artifacts").mkdir(parents=True)   # no model.patch inside
            kw["stdout"].write("agent auth rejected (401)\n")
            kw["stdout"].flush()
            self.returncode = 1

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass
    monkeypatch.setattr(runner_mod, "build_pier_command", fake_build)
    monkeypatch.setattr(runner_mod.subprocess, "Popen", FakePopen)
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
    assert _trial_timeout_sec({"est_minutes": 5}) == 3600   # floor wins
    assert _trial_timeout_sec({"est_minutes": 10}) == 3600  # floor wins


def test_trial_timeout_scales_with_estimate():
    assert _trial_timeout_sec({"est_minutes": 20}) == 4800
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
