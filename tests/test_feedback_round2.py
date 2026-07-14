"""Second volunteer-feedback round (2026-07-13): git identity inside the task
container, heartbeat log parsing, and free-pick batches picking up cells
claimed on the web while an earlier batch was still running."""
from pathlib import Path

import dradar.runloop as runloop
import dradar.runner as runner_mod
from dradar.runner import _last_activity, build_pier_command

from test_go_menu import FakeClient, _args, _patch_run


def test_pier_command_injects_git_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _: "/usr/bin/pier")
    (tmp_path / "t").mkdir()
    monkeypatch.setenv("CODEX_AUTH_JSON_PATH", str(tmp_path / "auth.json"))
    (tmp_path / "auth.json").write_text("{}")
    home = tmp_path / "home"
    home.mkdir()
    a = {"assignment_id": "a1", "task_id": "t", "agent": "codex",
         "model": "gpt-5.6-sol", "effort": "low"}
    cmd = build_pier_command(a, tmp_path, tmp_path / "jobs", "j", home)
    for var in ("GIT_AUTHOR_NAME=dradar-trial", "GIT_COMMITTER_NAME=dradar-trial",
                "GIT_AUTHOR_EMAIL=trial@dradar.invalid",
                "GIT_COMMITTER_EMAIL=trial@dradar.invalid"):
        assert var in cmd
        assert cmd[cmd.index(var) - 1] == "--ae"


def test_last_activity_unwraps_progress_bar_redraws(tmp_path: Path):
    log = tmp_path / "pier.log"
    log.write_text("cmd=pier run\n1/1 Mean: 0.000 ━━ 0:00:30\r1/1 Mean: 0.000 ━━ 0:07:10\n")
    assert "0:07:10" in _last_activity(log)


def test_last_activity_survives_empty_log(tmp_path: Path):
    log = tmp_path / "pier.log"
    log.write_text("")
    assert "still running" in _last_activity(log)


def _cell(aid):
    return {"assignment_id": aid, "task_id": f"task-{aid}", "agent": "codex",
            "model": "gpt-5.6-sol", "effort": "low", "nonce": "n",
            "expires_at": "2099-01-01T00:00:00+00:00", "est_minutes": 2,
            "est_quota_pct": 0.5}


def test_free_pick_continues_with_cells_claimed_mid_run(monkeypatch, capsys, tmp_path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient([
        {"active": [_cell("a1")], "free_pick": True},                 # startup snapshot
        {"active": [_cell("a2"), _cell("a3")], "free_pick": True},    # claimed mid-run
        {"active": [], "free_pick": True},                            # drained
    ])
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1", "a2", "a3"]
    assert "claimed while that batch ran" in capsys.readouterr().out


def test_free_pick_still_held_cells_do_not_loop(monkeypatch, tmp_path):
    # A cell the volunteer deliberately skipped stays leased and keeps coming
    # back from GET /assignment — it must not re-prompt in an endless loop.
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient({"active": [_cell("a1")], "free_pick": True})  # repeats forever
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1"]  # ran exactly once


def test_menu_mode_keeps_single_run_contract(monkeypatch, tmp_path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient([
        {"active": [_cell("a1")], "free_pick": False, "menu": None},
        {"active": [_cell("a2")], "free_pick": False, "menu": None},
    ])
    rc = runloop._go_menu(_args(), {}, client, tmp_path)
    assert rc == 0
    assert ran == ["a1"]  # no auto-continuation on menu-mode instances


# --- quota-share denomination (owner report 2026-07-14) ----------------------

WINDOWS = {"plus": 91.37, "pro-5x": 456.86, "pro-20x": 1827.42}


def test_quota_share_converts_per_tier():
    line = runloop._quota_share_line(
        {"est_quota_pct": 0.5, "tier_windows_usd": WINDOWS})
    # 0.5% of Plus = 0.10% of 5x = 0.025% of 20x (adaptive precision, same
    # as the radar page's tags; 0.025 renders 0.02 under binary float)
    assert "Plus ~0.50%" in line
    assert "5x Pro ~0.10%" in line
    assert "20x Pro ~0.02%" in line


def test_quota_share_without_windows_labels_denomination():
    line = runloop._quota_share_line({"est_quota_pct": 0.5})
    assert "Plus" in line and "0.5" in line  # old server: say what 0.5% means


def test_quota_share_tiny_pct_never_shows_zero():
    line = runloop._quota_share_line(
        {"est_quota_pct": 0.05, "tier_windows_usd": WINDOWS})
    assert "20x Pro ~<0.01%" in line
    assert "0.00%" not in line
