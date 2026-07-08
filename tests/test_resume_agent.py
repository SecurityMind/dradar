"""Quota-resilient codex agent: detection, wait math, command wiring."""

import pytest

from dradar import resume_agent
from dradar.resume_agent import looks_rate_limited, seconds_until_window_reset
from dradar.runner import build_pier_command


def test_rate_limit_signatures_match():
    for tail in (
        '{"type":"error","message":"You have hit your usage limit."}',
        "stream error: rate limit reached for this account",
        "HTTP 429 Too Many Requests",
    ):
        assert looks_rate_limited(tail)


def test_ordinary_failures_do_not_match():
    for tail in (
        "error: compilation failed\nexit status 2",
        "docker: no space left on device",
        "",
    ):
        assert not looks_rate_limited(tail)


def test_incidental_mention_mid_transcript_is_not_a_false_positive():
    # The process's real death is the LAST thing in the tail; an earlier
    # turn/tool-output line that happens to mention "429"/"rate limit" (e.g.
    # the model reading code that handles HTTP rate limiting) must not
    # trigger a multi-hour sleep for a trial that actually failed for an
    # unrelated reason.
    tail = "\n".join([
        'assistant: I see this handler retries on HTTP 429 rate limit errors.',
        '{"type":"tool_call","tool":"read_file","path":"client.py"}',
        '{"type":"tool_output","content":"if resp.status == 429: retry()"}',
    ] + [f'{{"type":"tool_output","content":"line {i}"}}' for i in range(20)] + [
        '{"type":"tool_call","tool":"run_build"}',
        '{"type":"tool_output","content":"building..."}',
        '{"type":"tool_output","content":"gcc: error: main.c"}',
        '{"type":"error","message":"compilation failed"}',
        "exit status 2",
    ])
    assert not looks_rate_limited(tail)


def test_genuine_death_at_the_tail_end_still_matches():
    tail = "\n".join([
        '{"type":"tool_call","tool":"run_tests"}',
        '{"type":"tool_output","content":"3 passed"}',
        'stream error: rate limit reached for this account',
    ])
    assert looks_rate_limited(tail)


def test_genuine_death_survives_a_few_trailing_shutdown_lines():
    # A real rate-limit error followed by several lines of shell/process
    # shutdown chatter (not the LAST line) must still be caught.
    tail = "\n".join([
        '{"type":"tool_output","content":"3 passed"}',
        'stream error: rate limit reached for this account',
        "cleaning up temp files...",
        "closing session...",
        "process exited",
    ])
    assert looks_rate_limited(tail)


def test_wait_uses_live_reset_instant(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(resume_agent, "read_rate_limits",
                        lambda: {"five_hour_resets_at": now + 2000})
    assert seconds_until_window_reset(now=now) == 2000 + 120


def test_wait_clamps_and_falls_back(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(resume_agent, "read_rate_limits", lambda: None)
    assert seconds_until_window_reset(now=now) == 1800.0
    monkeypatch.setattr(resume_agent, "read_rate_limits",
                        lambda: {"five_hour_resets_at": now + 10})
    assert seconds_until_window_reset(now=now) == 300.0  # min clamp
    monkeypatch.setattr(resume_agent, "read_rate_limits",
                        lambda: {"five_hour_resets_at": now + 10 * 3600})
    assert seconds_until_window_reset(now=now) == 6 * 3600.0  # max clamp


def test_real_codex_runs_use_the_resilient_agent(tmp_path, monkeypatch):
    monkeypatch.setattr("dradar.runner.shutil.which", lambda n: "/usr/bin/pier")
    (tmp_path / "t1").mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(auth))
    assignment = {"task_id": "t1", "agent": "codex", "model": "gpt-5.5",
                  "effort": "high", "assignment_id": "a1"}
    cmd = build_pier_command(assignment, tmp_path, tmp_path / "jobs", "j", tmp_path)
    joined = " ".join(cmd)
    assert "--agent-import-path dradar.resume_agent:QuotaResilientCodex" in joined
    assert "--agent codex" not in joined
    # pier's own agent wall-clock ceiling must be raised too, or pier kills
    # the trial mid-sleep before a real multi-hour resume ever gets to run.
    # (no resilient_timeout_sec passed here -> the fallback constant)
    assert "--agent-timeout-multiplier 30" in joined

    # dev-agent smoke runs keep the stock driver (no quota to protect, no
    # multi-hour sleep possible, so no need to touch pier's own timeout)
    cmd_dev = build_pier_command(assignment, tmp_path, tmp_path / "jobs", "j2",
                                 tmp_path, dev_agent="oracle")
    joined_dev = " ".join(cmd_dev)
    assert "--agent oracle" in joined_dev
    assert "--agent-timeout-multiplier" not in joined_dev


def test_agent_timeout_multiplier_is_derived_not_a_blind_guess(tmp_path, monkeypatch):
    # A fixed multiplier can be too SMALL for some est_minutes (pier's inner
    # ceiling ends up shorter than dradar's own outer timeout, so pier still
    # fires first) or needlessly huge for others. The multiplier must be
    # derived from THIS run's own outer timeout so pier's inner ceiling
    # (multiplier x the smallest possible task base, 1800s) is always >= it.
    monkeypatch.setattr("dradar.runner.shutil.which", lambda n: "/usr/bin/pier")
    (tmp_path / "t1").mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(auth))
    assignment = {"task_id": "t1", "agent": "codex", "model": "gpt-5.5",
                  "effort": "high", "assignment_id": "a1"}

    from dradar.runner import _MIN_TASK_AGENT_TIMEOUT_SEC
    for outer_timeout_sec in (7200, 59400, 66600, 162000):
        cmd = build_pier_command(assignment, tmp_path, tmp_path / "jobs", "j", tmp_path,
                                 resilient_timeout_sec=outer_timeout_sec)
        joined = " ".join(cmd)
        idx = cmd.index("--agent-timeout-multiplier")
        multiplier = int(cmd[idx + 1])
        # the core guarantee: pier's inner ceiling, even under the SMALLEST
        # possible real task base, must never be shorter than this run's own
        # outer timeout -- otherwise pier could still fire first.
        assert multiplier * _MIN_TASK_AGENT_TIMEOUT_SEC >= outer_timeout_sec, joined


def test_run_trial_derives_a_sufficient_multiplier_for_the_actual_timeout(tmp_path, monkeypatch):
    # End-to-end (through run_trial's own timeout_sec computation, not a
    # hand-picked value) property check: pier's inner ceiling must always be
    # >= dradar's own outer subprocess.run timeout for that same run.
    import subprocess as subprocess_mod
    from dradar import runner as runner_mod

    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: "/usr/bin/pier")
    (tmp_path / "t1").mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(auth))

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs["timeout"]
        raise subprocess_mod.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    assignment = {"task_id": "t1", "agent": "codex", "model": "gpt-5.5", "effort": "high",
                  "assignment_id": "a1", "nonce": "n", "est_minutes": 15}
    with pytest.raises(runner_mod.RunnerError):
        runner_mod.run_trial(assignment, tmp_path, tmp_path / "work")

    idx = captured["cmd"].index("--agent-timeout-multiplier")
    multiplier = int(captured["cmd"][idx + 1])
    assert multiplier * runner_mod._MIN_TASK_AGENT_TIMEOUT_SEC >= captured["timeout"]


def test_on_started_called_before_subprocess_launches(tmp_path, monkeypatch):
    # Confirms to the server that a free-pick claim's short initial lease
    # should be extended -- must fire before the pier subprocess (i.e. before
    # any real quota is spent), not after.
    import subprocess as subprocess_mod
    from dradar import runner as runner_mod

    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: "/usr/bin/pier")
    (tmp_path / "t1").mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(auth))

    order = []

    def fake_run(cmd, **kwargs):
        order.append("subprocess")
        raise subprocess_mod.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    assignment = {"task_id": "t1", "agent": "codex", "model": "gpt-5.5", "effort": "high",
                  "assignment_id": "a1", "nonce": "n", "est_minutes": 15}
    with pytest.raises(runner_mod.RunnerError):
        runner_mod.run_trial(assignment, tmp_path, tmp_path / "work",
                             on_started=lambda: order.append("started"))

    assert order == ["started", "subprocess"]


def test_on_started_exception_is_swallowed_not_fatal(tmp_path, monkeypatch):
    # A heartbeat ping failure (dead network, server hiccup) must never abort
    # or corrupt a real trial that's about to burn real quota.
    import subprocess as subprocess_mod
    from dradar import runner as runner_mod

    monkeypatch.setattr(runner_mod.shutil, "which", lambda n: "/usr/bin/pier")
    (tmp_path / "t1").mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(auth))

    reached_subprocess = []

    def fake_run(cmd, **kwargs):
        reached_subprocess.append(True)
        raise subprocess_mod.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    def flaky_on_started():
        raise RuntimeError("network hiccup pinging /assignment/started")

    assignment = {"task_id": "t1", "agent": "codex", "model": "gpt-5.5", "effort": "high",
                  "assignment_id": "a1", "nonce": "n", "est_minutes": 15}
    # The RunnerError below must come from the mocked subprocess timeout, not
    # from flaky_on_started's RuntimeError leaking out uncaught.
    with pytest.raises(runner_mod.RunnerError):
        runner_mod.run_trial(assignment, tmp_path, tmp_path / "work",
                             on_started=flaky_on_started)
    assert reached_subprocess == [True]
