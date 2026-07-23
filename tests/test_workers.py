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


def test_cli_accepts_auto_workers(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_go", lambda args: seen.append(args) or 0)
    assert cli.main(["resume", "--workers", "auto", "-y"]) == 0
    assert seen[0].workers == "auto"


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


def test_quota_only_refill_gets_an_internal_task_safety_cap(monkeypatch):
    seen = []
    monkeypatch.setattr(
        runloop, "_run_worker_pool",
        lambda args: seen.append(args.max_tasks) or 0,
    )
    args = _args(
        refill=True, max_tasks=None, max_estimated_quota_pct=12.5,
    )
    assert runloop.cmd_go(args) == 0
    assert seen == [runloop.DEFAULT_REFILL_TASK_SAFETY_CAP]


def test_manual_workers_raise_too_small_refill_queue(monkeypatch):
    seen = []
    monkeypatch.setattr(
        runloop, "_run_worker_pool",
        lambda args: seen.append((args.workers, args.refill_to)) or 0,
    )
    args = _args(
        workers=3, refill=True, refill_to=1, max_tasks=100,
        max_estimated_quota_pct=5,
    )

    assert runloop.cmd_go(args) == 0
    assert seen == [(3, 3)]


def test_refill_without_any_limit_is_rejected_before_setup():
    with pytest.raises(SystemExit, match="requires --max-estimated-quota-pct"):
        runloop.cmd_go(_args(refill=True))


def test_worker_command_never_forwards_auto_selection():
    command = runloop._worker_command(_args())
    assert command[3:6] == ["resume", "-y", "--parallel"]
    assert "--worker-child" in command
    assert "--auto" not in command
    assert "go" not in command


class _Telemetry:
    session_id = "session-test"

    def __init__(self, _client, **_kwargs):
        self.closed = None

    def start(self):
        pass

    def set_phase(self, _phase):
        pass

    def close(self, reason):
        self.closed = reason


@pytest.mark.parametrize(
    ("worker_child", "expected_rc", "expected_checkout"),
    ((True, 0, True), (False, 1, False)),
)
def test_only_supervised_worker_skips_busy_checkpoint_and_drains_waiting_work(
        monkeypatch, capsys, worker_child, expected_rc, expected_checkout):
    """One checkpoint owner must not leave another confirmed pool slot idle."""
    checked_out = []
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda *_a, **_k: object())
    monkeypatch.setattr(runloop, "tasks_root_from_config", lambda _cfg: object())
    monkeypatch.setattr(runloop, "RunnerTelemetry", _Telemetry)
    monkeypatch.setattr(runloop, "ensure_tasks_root", lambda _root: None)
    monkeypatch.setattr(runloop, "ensure_pier", lambda: None)
    monkeypatch.setattr(runloop, "_retry_pending_uploads", lambda _client: None)
    monkeypatch.setattr(
        runloop, "_resume_local_checkpoints",
        lambda *_a, **_k: ([], True),  # every checkpoint lock was busy
    )
    monkeypatch.setattr(
        runloop, "_go_menu",
        lambda *_a, **_k: checked_out.append(True) or 0,
    )
    args = _args(
        workers=1, auto=None, parallel=True, resume=True,
        worker_child=worker_child,
    )

    assert runloop.cmd_go(args) == expected_rc
    assert bool(checked_out) is expected_checkout
    output = capsys.readouterr().out
    if worker_child:
        assert "checking for a different waiting task" in output


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


def test_auto_workers_use_capacity_recommendation(monkeypatch, capsys):
    _patch_pool_setup(monkeypatch, active_count=5)
    from dradar.capacity import CapacityReport

    report = CapacityReport(
        recommended_workers=2, docker_cpus=8, docker_memory_gib=16,
        disk_free_gib=100, account_limit=5, held_tasks=5, task_limit=5,
        cpu_limit=4, memory_limit=2, disk_limit=7,
    )
    monkeypatch.setattr("dradar.capacity.inspect_capacity", lambda *_a, **_k: report)
    monkeypatch.setattr("dradar.capacity.print_report", lambda r: print(f"auto={r.recommended_workers}"))
    calls = []
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: calls.append(_Process(command, env, **kwargs)) or calls[-1],
    )

    assert runloop._run_worker_pool(_args(workers="auto")) == 0
    assert len(calls) == 2
    assert "auto=2" in capsys.readouterr().out


def test_one_claim_auto_workers_refill_to_detected_concurrency(monkeypatch):
    seen_refill_targets = []
    _patch_pool_setup(monkeypatch, active_count=3)
    monkeypatch.setattr(
        runloop, "_prepare_batch",
        lambda args, _client: (
            seen_refill_targets.append(args.refill_to)
            or ([{"assignment_id": str(i)} for i in range(3)], True)
        ),
    )
    from dradar.capacity import CapacityReport

    report = CapacityReport(
        recommended_workers=3, docker_cpus=12, docker_memory_gib=24,
        disk_free_gib=100, account_limit=5, held_tasks=1, task_limit=4,
        cpu_limit=6, memory_limit=3, disk_limit=7,
    )
    monkeypatch.setattr("dradar.capacity.inspect_capacity", lambda *_a, **_k: report)
    monkeypatch.setattr("dradar.capacity.print_report", lambda _report: None)
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: _Process(command, env, **kwargs),
    )
    args = _args(
        workers="auto", refill=True, refill_to=1, max_tasks=100,
        max_estimated_quota_pct=5,
    )

    assert runloop._run_worker_pool(args) == 0
    assert seen_refill_targets == [3]
    assert args.refill_to == 3


def test_worker_floor_never_overrides_explicit_task_cap():
    args = _args(workers=4, refill=True, refill_to=1, max_tasks=2)

    runloop._align_refill_target_with_workers(args)

    assert args.refill_to == 2


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
    assert [p.env["DRADAR_POOL_SIZE"] for p in calls] == ["3", "3", "3"]
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
