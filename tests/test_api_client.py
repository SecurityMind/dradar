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
        return httpx.Response(409, json={
            "detail": "cell went stale", "code": "cell_unavailable",
        })

    with pytest.raises(ApiError) as ei:
        _client(handler).get_assignment()
    assert ei.value.status_code == 409
    assert ei.value.code == "cell_unavailable"
    assert "cell went stale" in str(ei.value)


def test_legacy_http_error_without_code_remains_compatible():
    def handler(request):
        return httpx.Response(409, json={"detail": "legacy conflict"})

    with pytest.raises(ApiError) as ei:
        _client(handler).get_assignment()
    assert ei.value.status_code == 409
    assert ei.value.code is None
    assert "legacy conflict" in str(ei.value)


def test_transport_failure_has_no_status_code():
    def handler(request):
        raise httpx.ConnectError("name resolution failed")

    with pytest.raises(ApiError) as ei:
        _client(handler).whoami()
    assert ei.value.status_code is None
    assert ei.value.code is None
    assert "cannot reach" in str(ei.value)


def test_suggest_passes_n_and_returns_cells():
    seen = {}

    def handler(request):
        seen["path"] = str(request.url)
        return httpx.Response(200, json={"cells": [{"task_id": "t1"}]})

    got = _client(handler).suggest(3)
    assert seen["path"].endswith("/api/v1/suggest?n=3")
    assert got == {"cells": [{"task_id": "t1"}]}


def test_table_fetches_public_full_board():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json={"cells": {"t1|m|low": {"st": "open"}}})

    got = _client(handler).table()
    assert seen["path"] == "/api/v1/table"
    assert got["cells"]["t1|m|low"]["st"] == "open"


def test_checkout_sends_failed_cell_exclusions():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"assignment": None, "held": 1, "unstarted": 0})

    _client(handler).checkout(
        exclude_assignment_ids={"a2", "a1"}, session_id="session-123")
    assert b"exclude_assignment_ids=a1%2Ca2" in seen["body"]
    assert b"session_id=session-123" in seen["body"]


def test_mark_started_sends_runner_session_id():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"ok": True})

    _client(handler).mark_started("a1", session_id="session-123")
    assert b"assignment_id=a1" in seen["body"]
    assert b"session_id=session-123" in seen["body"]


def test_release_sends_bulk_target_and_force_flags():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={
            "released": [], "skipped": [], "already_released": [], "held": 0})

    _client(handler).release_assignments(["a1", "a2"], force=True)
    assert seen["path"] == "/api/v1/assignments/release"
    assert b"assignment_ids=a1%2Ca2" in seen["body"]
    assert b"release_all=false" in seen["body"]
    assert b"force=true" in seen["body"]


def test_mark_stopped_requests_cross_session_cooldown():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"ok": True, "retry_after": "later"})

    _client(handler).mark_stopped("a1")
    assert b"assignment_id=a1" in seen["body"]
    assert b"defer_seconds=300" in seen["body"]


def test_checkpoint_protocol_sends_id_generation_and_runner_session():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.read()))
        if request.url.path.endswith("/resume"):
            return httpx.Response(200, json={"assignment": {"assignment_id": "a1"}})
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    client.checkpoint_pause("a1", "checkpoint-123", 2)
    client.checkpoint_resume("a1", "checkpoint-123", 2, session_id="session-123")
    client.checkpoint_discard("a1", "checkpoint-123", 3, reason="invalid")
    assert [path for path, _ in seen] == [
        "/api/v1/assignment/checkpoint/pause",
        "/api/v1/assignment/checkpoint/resume",
        "/api/v1/assignment/checkpoint/discard",
    ]
    assert all(b"checkpoint_id=checkpoint-123" in body for _, body in seen)
    assert b"resume_generation=2" in seen[0][1]
    assert b"session_id=session-123" in seen[1][1]
    assert b"reason=invalid" in seen[2][1]


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


def test_submit_sends_multi_agent_trajectory_bundle(tmp_path):
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"submission_id": "s1", "grade_status": "pending"})

    patch = tmp_path / "model.patch"
    patch.write_text("diff")
    bundle = tmp_path / "trajectory_bundle.json"
    bundle.write_text('{"schema_version":"dradar-codex-trajectory-bundle-v1"}')
    _client(handler).submit(
        "a1", "nonce", patch, None, None, {}, trajectory_bundle=bundle,
    )
    body = seen["body"]
    assert b'name="trajectory_bundle"' in body
    assert b'filename="trajectory_bundle.json"' in body


def test_submit_sends_resume_generation_when_fenced(tmp_path):
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"submission_id": "s1", "grade_status": "pending"})

    patch = tmp_path / "model.patch"
    patch.write_text("diff")
    _client(handler).submit(
        "a1", "nonce", patch, None, None, {}, resume_generation=7,
    )
    assert b"resume_generation" in seen["body"]
    assert b"7" in seen["body"]


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
