"""GitHub identity binding: device-flow poller (client side). The
link/whoami server endpoints this feeds are tested in the server repo."""

import pytest

from dradar import github_device


def _flow_responses(monkeypatch, responses):
    it = iter(responses)
    monkeypatch.setattr(github_device, "_post_form", lambda url, data: next(it))


def test_poll_returns_token_after_pending(monkeypatch):
    _flow_responses(monkeypatch, [
        {"error": "authorization_pending"},
        {"error": "authorization_pending"},
        {"access_token": "gho_xyz"},
    ])
    tok = github_device.poll("cid", "dc", interval=1, expires_in=100,
                             sleep=lambda s: None, clock=lambda: 0)
    assert tok == "gho_xyz"


def test_poll_honors_slow_down(monkeypatch):
    waits = []
    _flow_responses(monkeypatch, [
        {"error": "slow_down", "interval": 7},
        {"access_token": "gho_ok"},
    ])
    tok = github_device.poll("cid", "dc", interval=1, expires_in=100,
                             sleep=lambda s: waits.append(s), clock=lambda: 0)
    assert tok == "gho_ok"
    assert 7 in waits  # backoff applied


def test_poll_raises_on_denial(monkeypatch):
    _flow_responses(monkeypatch, [{"error": "access_denied"}])
    with pytest.raises(github_device.DeviceFlowError, match="access_denied"):
        github_device.poll("cid", "dc", interval=1, expires_in=100,
                           sleep=lambda s: None, clock=lambda: 0)


def test_poll_gives_up_at_expiry(monkeypatch):
    _flow_responses(monkeypatch, [{"error": "authorization_pending"}] * 50)
    t = [0]
    def clock():
        t[0] += 40
        return t[0]
    with pytest.raises(github_device.DeviceFlowError, match="expired"):
        github_device.poll("cid", "dc", interval=1, expires_in=100,
                           sleep=lambda s: None, clock=clock)
