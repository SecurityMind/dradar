"""Zero-friction identity: first go auto-registers, login works tokenless."""

import pytest

from dradar import identity, local_config
from dradar.api_client import ApiError


def test_client_auto_registers_when_tokenless(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(local_config, "HOME", tmp_path)
    monkeypatch.setattr(local_config, "CONFIG_PATH", tmp_path / "config.json")
    registered = {}

    class FakeClient:
        def __init__(self, server, token):
            self.server, self.token = server, token
        def register(self, nickname):
            registered["nickname"] = nickname
            return {"nickname": nickname, "token": "drt_fake"}

    monkeypatch.setattr(identity, "ApiClient", FakeClient)
    cfg = {"server": "https://api.example.com"}
    client = identity._client(cfg, auto_register=True)
    assert client.token == "drt_fake"
    assert registered["nickname"].startswith("vol-")
    assert cfg["token"] == "drt_fake"
    assert "auto-registered" in capsys.readouterr().out
    # persisted for next runs
    assert (tmp_path / "config.json").exists()


def test_client_without_flag_still_demands_token():
    with pytest.raises(SystemExit):
        identity._client({"server": "https://api.example.com"}, auto_register=False)


def _scripted_client(monkeypatch, tmp_path, responses):
    """FakeClient whose register() pops one scripted response per call —
    an ApiError instance raises, anything else acks with a token."""
    monkeypatch.setattr(local_config, "HOME", tmp_path)
    monkeypatch.setattr(local_config, "CONFIG_PATH", tmp_path / "config.json")
    tried = []

    class FakeClient:
        def __init__(self, server, token):
            self.server, self.token = server, token
        def register(self, nickname):
            tried.append(nickname)
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return {"nickname": nickname, "token": "drt_fake"}

    monkeypatch.setattr(identity, "ApiClient", FakeClient)
    return tried


def test_auto_register_retries_a_fresh_handle_on_409(monkeypatch, tmp_path, capsys):
    tried = _scripted_client(monkeypatch, tmp_path,
                             [ApiError("server returned 409: taken", 409), "ok"])
    cfg = {"server": "https://api.example.com"}
    identity._auto_register(cfg)
    assert cfg["token"] == "drt_fake"
    assert len(tried) == 2
    assert all(n.startswith("vol-") for n in tried)
    assert tried[0] != tried[1]  # a fresh handle, not the same one again
    assert "auto-registered" in capsys.readouterr().out


def test_auto_register_non_409_error_exits(monkeypatch, tmp_path):
    tried = _scripted_client(monkeypatch, tmp_path,
                             [ApiError("server returned 500: boom", 500)])
    with pytest.raises(SystemExit) as ei:
        identity._auto_register({"server": "https://api.example.com"})
    assert "auto-registration failed" in str(ei.value)
    assert len(tried) == 1  # no retry on a non-409


def test_auto_register_gives_up_after_three_409s(monkeypatch, tmp_path):
    tried = _scripted_client(monkeypatch, tmp_path,
                             [ApiError("server returned 409: taken", 409)] * 3)
    with pytest.raises(SystemExit) as ei:
        identity._auto_register({"server": "https://api.example.com"})
    assert "could not find a free handle" in str(ei.value)
    assert len(tried) == 3
