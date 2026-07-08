"""Zero-friction identity: first go auto-registers, login works tokenless."""

import pytest

from dradar import identity, local_config


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
