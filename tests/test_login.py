"""cmd_login first-contact paths: nickname registration, github recovery,
server-only config, and whoami verification failure."""

import argparse
import json

import pytest

from dradar import identity, local_config
from dradar.api_client import ApiError


def _args(**kw):
    base = dict(server=None, token=None, tasks_root=None, nickname=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(local_config, "HOME", tmp_path)
    monkeypatch.setattr(local_config, "CONFIG_PATH", tmp_path / "config.json")
    return tmp_path


def test_login_nickname_registers_and_stores_token(home, monkeypatch, capsys):
    class FakeClient:
        def __init__(self, server, token):
            self.server, self.token = server, token
        def register(self, nickname):
            return {"nickname": nickname, "token": "drt_new"}
        def whoami(self):
            return {"nickname": "alice"}

    monkeypatch.setattr(identity, "ApiClient", FakeClient)
    rc = identity.cmd_login(_args(server="https://api.example.com", nickname="alice"))
    assert rc == 0
    saved = json.loads((home / "config.json").read_text())
    assert saved["token"] == "drt_new"
    assert saved["tasks_root"] == str(home / "deep-swe" / "tasks")
    out = capsys.readouterr().out
    assert "registered as alice" in out
    assert "logged in as alice" in out


def test_login_github_unlinked_account_gets_guidance(home, monkeypatch):
    class FakeClient:
        def __init__(self, server, token):
            pass
        def github_whoami(self, access_token):
            raise ApiError("server returned 404: not linked", 404)

    monkeypatch.setattr(identity, "ApiClient", FakeClient)
    monkeypatch.setattr(identity, "_github_device_token", lambda client: "gh_tok")
    with pytest.raises(SystemExit) as ei:
        identity.cmd_login(_args(server="https://api.example.com", github=True))
    # points at linking first on the original machine, not a dead end
    assert "link-github" in str(ei.value)


def test_login_server_only_saves_and_notes_auto_create(home, capsys):
    rc = identity.cmd_login(_args(server="https://api.example.com"))
    assert rc == 0
    saved = json.loads((home / "config.json").read_text())
    assert saved["server"] == "https://api.example.com"
    assert saved["tasks_root"] == str(home / "deep-swe" / "tasks")
    assert not saved.get("token")
    assert "auto-created" in capsys.readouterr().out


def test_login_preserves_existing_explicit_tasks_root(home, monkeypatch):
    legacy = home.parent / "deep-swe" / "tasks"
    (home / "config.json").write_text(json.dumps({
        "server": "https://api.example.com",
        "tasks_root": str(legacy),
    }))
    rc = identity.cmd_login(_args())
    assert rc == 0
    saved = json.loads((home / "config.json").read_text())
    assert saved["tasks_root"] == str(legacy)


def test_login_explicit_tasks_root_overrides_hidden_default(home, monkeypatch, tmp_path):
    chosen = tmp_path / "chosen" / "tasks"
    rc = identity.cmd_login(_args(
        server="https://api.example.com", tasks_root=str(chosen)))
    assert rc == 0
    saved = json.loads((home / "config.json").read_text())
    assert saved["tasks_root"] == str(chosen.resolve())


def test_login_whoami_failure_exits(home, monkeypatch):
    class FakeClient:
        def __init__(self, server, token):
            pass
        def whoami(self):
            raise ApiError("server returned 401: invalid token", 401)

    monkeypatch.setattr(identity, "ApiClient", FakeClient)
    with pytest.raises(SystemExit) as ei:
        identity.cmd_login(_args(server="https://api.example.com", token="drt_bad"))
    assert "login failed" in str(ei.value)
