"""HTTP client for the dradar dispatch server."""

import json
from pathlib import Path
from typing import Any

import httpx


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        # None means "never got a real HTTP response" (DNS/connect/timeout) —
        # callers that need to branch on a specific status (e.g. 409 vs 410)
        # must check this instead of grepping the message, which can contain
        # a server URL/port or arbitrary error text that happens to embed the
        # same digits as a status code.
        super().__init__(message)
        self.status_code = status_code


class ApiClient:
    def __init__(self, server: str, token: str):
        self.server = server.rstrip("/")
        # write=None: large uploads over a slow tunnel must not hit a write
        # timeout; keep a bounded connect/read so a dead server fails fast.
        # No header at all when tokenless (pre-registration): an empty
        # "Bearer " is an illegal header value.
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(
            base_url=self.server,
            headers=headers,
            timeout=httpx.Timeout(30.0, write=None, read=120.0),
        )

    def _get(self, path: str) -> dict[str, Any]:
        return self._check(self._request("GET", path))

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            return self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:  # transport-level: connect/timeout/etc.
            raise ApiError(f"cannot reach {self.server}: {exc}") from exc

    def _check(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            detail: Any = resp.text
            try:
                body = resp.json()
                if isinstance(body, dict):
                    detail = body.get("detail", resp.text)
            except (json.JSONDecodeError, ValueError):
                pass
            raise ApiError(f"server returned {resp.status_code}: {detail}", status_code=resp.status_code)
        return resp.json()

    def register(self, nickname: str) -> dict[str, Any]:
        """Self-serve signup; returns {nickname, token}. No auth required."""
        resp = self._request("POST", "/api/v1/register", data={"nickname": nickname})
        return self._check(resp)

    def rename(self, nickname: str) -> dict[str, Any]:
        resp = self._request("POST", "/api/v1/rename", data={"nickname": nickname})
        return self._check(resp)

    def my_submissions(self) -> dict[str, Any]:
        """Returns {nickname, points, submissions: [...]} — the volunteer's
        own recent history including grading status/flags the public pages
        hide. 404 on servers that predate this endpoint."""
        return self._get("/api/v1/my-submissions")

    def github_config(self) -> dict[str, Any]:
        return self._get("/api/v1/github/config")

    def github_link(self, access_token: str) -> dict[str, Any]:
        resp = self._request("POST", "/api/v1/github/link",
                             data={"access_token": access_token})
        return self._check(resp)

    def github_whoami(self, access_token: str) -> dict[str, Any]:
        resp = self._request("POST", "/api/v1/github/whoami",
                             data={"access_token": access_token})
        return self._check(resp)

    def whoami(self) -> dict[str, Any]:
        return self._get("/api/v1/whoami")

    def get_assignment(self) -> dict[str, Any]:
        """Returns {assignment: dict|None, resumed: bool, menu: list|None}."""
        return self._get("/api/v1/assignment")

    def claim_assignment(self, task_id: str, model: str, effort: str) -> dict[str, Any]:
        """Returns {assignment: dict, resumed: False}. Raises ApiError (409) if
        the cell went stale or the volunteer already holds an active lease."""
        resp = self._request(
            "POST", "/api/v1/assignment/claim",
            data={"task_id": task_id, "model": model, "effort": effort},
        )
        return self._check(resp)

    def mark_started(self, assignment_id: str) -> dict[str, Any]:
        """Confirms the trial subprocess actually started (see runner.run_trial):
        extends a free-pick claim's short initial lease out to the normal
        window. Best-effort by design — callers should swallow ApiError
        rather than let a heartbeat failure abort a real trial. 404 on
        servers that predate this endpoint or on a menu/bundle-style lease
        that never had a short window to extend in the first place."""
        resp = self._request(
            "POST", "/api/v1/assignment/started",
            data={"assignment_id": assignment_id},
        )
        return self._check(resp)

    def submit(
        self,
        assignment_id: str,
        nonce: str,
        patch: Path,
        trajectory: Path | None,
        result: Path | None,
        client_meta: dict[str, Any],
        outcome: str = "completed",
    ) -> dict[str, Any]:
        files: list[tuple[str, tuple[str, bytes]]] = [
            ("patch", ("model.patch", patch.read_bytes())),
        ]
        if trajectory and trajectory.exists():
            files.append(("trajectory", ("trajectory.json", trajectory.read_bytes())))
        if result and result.exists():
            files.append(("result", ("result.json", result.read_bytes())))
        resp = self._request(
            "POST", "/api/v1/submissions",
            data={
                "assignment_id": assignment_id,
                "nonce": nonce,
                "outcome": outcome,
                "client_meta": json.dumps(client_meta),
            },
            files=files,
        )
        return self._check(resp)
