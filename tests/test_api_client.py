"""Wire contract of ApiClient over httpx.MockTransport: the status_code
attached to ApiError (the 409/410 pending-ledger pruning branches on it),
the submit() multipart shape, and the Authorization header rules."""

import json

import httpx
import pytest

from dradar.api_client import ApiClient, ApiError


def _client(handler, token="drt_test"):
    return ApiClient("https://api.example.com", token,
                     transport=httpx.MockTransport(handler))


def test_http_error_attaches_status_code_and_detail():
    def handler(request):
        return httpx.Response(409, json={"detail": "cell went stale"})

    with pytest.raises(ApiError) as ei:
        _client(handler).get_assignment()
    assert ei.value.status_code == 409
    assert "cell went stale" in str(ei.value)


def test_transport_failure_has_no_status_code():
    def handler(request):
        raise httpx.ConnectError("name resolution failed")

    with pytest.raises(ApiError) as ei:
        _client(handler).whoami()
    assert ei.value.status_code is None
    assert "cannot reach" in str(ei.value)


def _do_submit(handler, tmp_path, with_optional):
    patch = tmp_path / "model.patch"
    patch.write_bytes(b"diff --git a/f b/f\n")
    trajectory = result = None
    if with_optional:
        trajectory = tmp_path / "trajectory.json"
        trajectory.write_text("[]")
        result = tmp_path / "result.json"
        result.write_text("{}")
    return _client(handler).submit(
        "a1", "nonce1", patch, trajectory, result,
        {"dradar_version": "0.test"}, outcome="completed")


def test_submit_sends_only_patch_part_when_optionals_absent(tmp_path):
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"submission_id": "s1", "grade_status": "pending"})

    _do_submit(handler, tmp_path, with_optional=False)
    body = seen["body"]
    assert b'name="patch"' in body
    assert b'name="trajectory"' not in body
    assert b'name="result"' not in body


def test_submit_sends_three_parts_and_client_meta_as_json_string(tmp_path):
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"submission_id": "s1", "grade_status": "pending"})

    _do_submit(handler, tmp_path, with_optional=True)
    body = seen["body"]
    for part in (b'name="patch"', b'name="trajectory"', b'name="result"'):
        assert part in body
    # client_meta travels as a single JSON-encoded string form field
    assert json.dumps({"dradar_version": "0.test"}).encode() in body


def test_tokenless_client_sends_no_authorization_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"nickname": "n", "token": "t"})

    _client(handler, token="").register("n")
    assert seen["auth"] is None


def test_token_becomes_bearer_authorization_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"nickname": "n"})

    _client(handler, token="drt_test").whoami()
    assert seen["auth"] == "Bearer drt_test"


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def _clear_proxy_env(monkeypatch):
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)


def test_env_proxy_is_still_honored(monkeypatch):
    """Passing ANY explicit transport makes httpx skip HTTP(S)_PROXY mounting
    entirely — the retrying default must stand aside on proxied machines or
    every proxied volunteer hard-breaks on upgrade."""
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    client = ApiClient("https://radar.example", "tok")
    assert client._client._mounts, "env proxy was not mounted"


def test_connect_retries_default_when_unproxied(monkeypatch):
    _clear_proxy_env(monkeypatch)
    client = ApiClient("https://radar.example", "tok")
    assert client._client._mounts == {}
    # httpcore internal, pinned deliberately: this is the only observable
    # evidence that the connect-phase retry default is actually in effect.
    assert client._client._transport._pool._retries == 2
