"""Per-machine guardrails: single-instance lock + orphan compose sweep."""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import dradar.machine as machine
from dradar.machine import acquire_run_lock, sweep_orphan_compose


def test_second_instance_refuses_to_start(tmp_path: Path):
    # A real second PROCESS must be refused (flock allows re-locking within
    # one process, so an in-process double-acquire proves nothing).
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(__file__).parent.parent / 'src')!r})
            from pathlib import Path
            from dradar.machine import acquire_run_lock
            acquire_run_lock(Path({str(tmp_path)!r}))
            print("locked", flush=True)
            time.sleep(30)
        """)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(SystemExit) as exc:
            acquire_run_lock(tmp_path)
        assert "another dradar run" in str(exc.value)
        assert "PID" in str(exc.value)
    finally:
        holder.kill()
        holder.wait()
    # holder dead -> the lock died with it, no stale-lock cleanup needed
    acquire_run_lock(tmp_path)
    machine._lock_handle.close()
    machine._lock_handle = None


def _fake_compose(monkeypatch, projects):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["docker", "compose", "ls"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"Name": p} for p in projects]), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(machine.subprocess, "run", fake_run)
    return calls


def test_sweep_downs_only_pier_shaped_projects(monkeypatch, capsys):
    calls = _fake_compose(monkeypatch, [
        "arktype-json-schema-refs-depende__ui7n6a5",   # orphan pier trial
        "boa-hierarchical-evaluation-canc__eeecwyc",   # orphan pier trial
        "my-blog",                                     # someone's real project
        "web_app-dev",                                 # underscore but not __id
    ])
    sweep_orphan_compose(assume_yes=True)
    downed = [c[3] for c in calls if c[:3] == ["docker", "compose", "-p"]]
    assert downed == ["arktype-json-schema-refs-depende__ui7n6a5",
                      "boa-hierarchical-evaluation-canc__eeecwyc"]
    out = capsys.readouterr().out
    assert "burning your quota" in out


def test_sweep_silent_when_nothing_matches(monkeypatch, capsys):
    calls = _fake_compose(monkeypatch, ["my-blog"])
    sweep_orphan_compose(assume_yes=True)
    assert len(calls) == 1                       # only the ls, no downs
    assert capsys.readouterr().out == ""


def test_sweep_survives_missing_docker(monkeypatch):
    def boom(cmd, **kw):
        raise OSError("no docker")
    monkeypatch.setattr(machine.subprocess, "run", boom)
    sweep_orphan_compose(assume_yes=True)        # must not raise


def test_sweep_asks_before_touching_anything(monkeypatch, capsys):
    calls = _fake_compose(monkeypatch, ["some-task__abc1234"])
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    sweep_orphan_compose(assume_yes=False)
    assert len(calls) == 1                       # declined -> no downs
    assert "docker compose -p" in capsys.readouterr().out
