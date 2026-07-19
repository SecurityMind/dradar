"""One-command worker pool: selection happens once; children only resume."""

import argparse

import pytest

from dradar import cli, runloop


def _args(**overrides):
    values = dict(
        workers=3, yes=True, keep=False, allow_task_drift=False,
        dev_agent=None, refill=False, refill_to=None, max_tasks=None,
        max_estimated_quota_pct=None, quota_tier="plus", auto=5, pick=None,
        assignment=None, parallel=False, worker_child=False, resume=False,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_cli_parses_workers_for_go(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_go", lambda args: seen.append(args) or 0)
    assert cli.main(["go", "--auto", "5", "--workers", "3", "-y"]) == 0
    assert seen[0].workers == 3
    assert seen[0].auto == 5


@pytest.mark.parametrize("workers", [0, 33])
def test_worker_count_is_bounded_before_any_setup(workers):
    with pytest.raises(SystemExit, match="1 <= N <= 32"):
        runloop.cmd_go(_args(workers=workers))


def test_workers_cannot_mix_with_manual_parallel():
    with pytest.raises(SystemExit, match="already manages parallel"):
        runloop.cmd_go(_args(parallel=True))


def test_internal_worker_mode_cannot_be_used_as_a_normal_go():
    with pytest.raises(SystemExit, match="invalid internal worker"):
        runloop.cmd_go(_args(workers=1, worker_child=True))


def test_worker_command_never_forwards_auto_selection():
    command = runloop._worker_command(_args())
    assert command[3:6] == ["resume", "-y", "--parallel"]
    assert "--worker-child" in command
    assert "--auto" not in command
    assert "go" not in command


class _Process:
    next_pid = 100

    def __init__(self, command, env, returncode=0, **kwargs):
        self.command = command
        self.env = env
        self.returncode = returncode
        self.pid = self.next_pid
        _Process.next_pid += 1

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode


class _LiveProcess(_Process):
    def __init__(self, command, env, **kwargs):
        super().__init__(command, env, **kwargs)
        self.returncode = None
        self.signals = []

    def send_signal(self, value):
        self.signals.append(value)
        self.returncode = 130

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def _patch_pool_setup(monkeypatch, active_count=5):
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: object())
    monkeypatch.setattr(runloop, "tasks_root_from_config", lambda _cfg: object())
    monkeypatch.setattr(runloop, "acquire_run_lock", lambda _home: None)
    monkeypatch.setattr(runloop, "sweep_orphan_compose", lambda _yes: None)
    monkeypatch.setattr(runloop, "ensure_tasks_root", lambda _root: None)
    monkeypatch.setattr(runloop, "ensure_pier", lambda: None)
    monkeypatch.setattr(runloop, "_retry_pending_uploads", lambda _client: None)
    monkeypatch.setattr(
        runloop, "_prepare_batch",
        lambda _args, _client: ([{"assignment_id": str(i)} for i in range(active_count)], True),
    )


def test_pool_prepares_once_then_starts_requested_resume_workers(monkeypatch):
    _patch_pool_setup(monkeypatch)
    calls = []

    def popen(command, env, **kwargs):
        process = _Process(command, env, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)
    assert runloop._run_worker_pool(_args()) == 0
    assert len(calls) == 3
    assert [p.env["DRADAR_WORKER_INDEX"] for p in calls] == ["1", "2", "3"]
    assert all("resume" in p.command and "--auto" not in p.command for p in calls)


def test_pool_does_not_start_more_workers_than_held_tasks(monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)
    calls = []
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: calls.append(_Process(command, env, **kwargs)) or calls[-1],
    )
    assert runloop._run_worker_pool(_args(workers=5)) == 0
    assert len(calls) == 2
    assert "starting 2 worker" in capsys.readouterr().out


def test_pool_reports_child_failure_without_hiding_other_results(monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)
    returncodes = iter((0, 1))
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: _Process(command, env, next(returncodes), **kwargs),
    )
    assert runloop._run_worker_pool(_args(workers=2)) == 1
    out = capsys.readouterr().out
    assert "worker 2=exit 1" in out
    assert "completed uploads are preserved" in out


def test_declining_pool_claims_nothing(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    monkeypatch.setattr(
        runloop, "_load_config",
        lambda: pytest.fail("configuration must not be touched after decline"),
    )
    assert runloop._run_worker_pool(_args(yes=False)) == 1


def test_later_spawn_failure_stops_already_started_worker(monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=2)
    first = None

    def popen(command, env, **kwargs):
        nonlocal first
        if first is not None:
            raise OSError("process limit")
        first = _LiveProcess(command, env, **kwargs)
        return first

    monkeypatch.setattr(runloop.subprocess, "Popen", popen)
    assert runloop._run_worker_pool(_args(workers=2)) == 1
    assert first.poll() is not None
    assert first.signals
    assert "stopping those already started" in capsys.readouterr().out
