"""config.json safety: atomic saves (a kill mid-write must never truncate
the volunteer's only token) and an actionable message — not a traceback —
when the file on disk is corrupt."""

import json
import os
import stat

import pytest

from dradar import local_config


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(local_config, "HOME", tmp_path)
    monkeypatch.setattr(local_config, "CONFIG_PATH", tmp_path / "config.json")
    return tmp_path


def test_valid_config_still_loads(home):
    # negative control for the corrupt-config path
    local_config._save_config({"server": "https://api.example.com", "token": "drt_x"})
    assert local_config._load_config() == {
        "server": "https://api.example.com", "token": "drt_x"}


def test_missing_config_loads_empty(home):
    assert local_config._load_config() == {}


def test_corrupt_config_exits_with_recovery_guidance(home):
    (home / "config.json").write_text("{not json")
    with pytest.raises(SystemExit) as ei:
        local_config._load_config()
    msg = str(ei.value)
    assert "corrupt" in msg
    assert str(home / "config.json") in msg
    assert "login --github" in msg


def test_save_failure_leaves_prior_config_intact(home, monkeypatch):
    # atomicity invariant: os.replace is the commit point — a failure
    # anywhere before it must leave the previous config loadable.
    local_config._save_config({"token": "drt_old"})
    monkeypatch.setattr(local_config.os, "replace",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        local_config._save_config({"token": "drt_new"})
    assert local_config._load_config() == {"token": "drt_old"}


def test_saved_config_is_owner_only(home):
    local_config._save_config({"token": "drt_secret"})
    mode = stat.S_IMODE(os.stat(home / "config.json").st_mode)
    assert mode == 0o600


def test_fresh_on_corrupt_returns_empty_and_warns(tmp_path, monkeypatch, capsys):
    """`dradar login` must be able to run over a corrupt config — it IS the
    recovery command the corrupt-config error recommends."""
    monkeypatch.setattr(local_config, "HOME", tmp_path)
    monkeypatch.setattr(local_config, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text('{"serv')  # truncated by a crash
    cfg = local_config._load_config(fresh_on_corrupt=True)
    assert cfg == {}
    assert "starting fresh" in capsys.readouterr().out
