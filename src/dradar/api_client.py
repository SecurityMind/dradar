"""HTTP client for the dradar dispatch server."""

import json
import os
from pathlib import Path
from typing import Any

import httpx


def _env_proxies_set() -> bool:
    """Any of the proxy env vars httpx honors. Passing ANY explicit transport
    to httpx.Client disables its environment-proxy mounting entirely, so the
    connect-retry transport below must stand aside on proxied machines."""
    return any(os.environ.get(k) for k in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy"))


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
    def __init__(self, server: str, token: str,
                 transport: httpx.BaseTransport | None = None):
        self.server = server.rstrip("/")
        # write=None: large uploads over a slow tunnel must not hit a write
        # timeout; keep a bounded connect/read so a dead server fails fast.
        # No header at all when tokenless (pre-registration): an empty
        # "Bearer " is an illegal header value.
        # transport is a test seam (httpx.MockTransport); when none is
        # injected, default to connect-phase retries: httpx re-attempts only
        # failed connection ESTABLISHMENT (DNS blip, refused/reset before the
        # request is sent) and never re-sends a request whose bytes went out,
        # so this is safe for the POST claim/submit endpoints (no duplicate
        # side effects). EXCEPT on proxied machines: httpx mounts
        # HTTP(S)_PROXY/ALL_PROXY only when no explicit transport is passed,
        # so there we keep httpx's default transport (proxy correctness
        # beats a connect-retry nicety — a good chunk of the volunteer pool
        # reaches the server only through a proxy).
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if transport is None and not _env_proxies_set():
            transport = httpx.HTTPTransport(retries=2)
        self._client = httpx.Client(
            base_url=self.server,
            headers=headers,
            timeout=httpx.Timeout(30.0, write=None, read=120.0),
            transport=transport,
        )

    def _get(self, path: str) -> dict[str, Any]:
        return self._check(self._request("GET", path))

    def _post(self, path: str, **kw) -> dict[str, Any]:
        return self._check(self._request("POST", path, **kw))

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
        return self._post("/api/v1/register", data={"nickname": nickname})

    def rename(self, nickname: str) -> dict[str, Any]:
        return self._post("/api/v1/rename", data={"nickname": nickname})

    def my_submissions(self) -> dict[str, Any]:
        """Returns {nickname, points, submissions: [...]} — the volunteer's
        own recent history including grading status/flags the public pages
        hide. 404 on servers that predate this endpoint."""
        return self._get("/api/v1/my-submissions")

    def github_config(self) -> dict[str, Any]:
        return self._get("/api/v1/github/config")

    def github_link(self, access_token: str) -> dict[str, Any]:
        return self._post("/api/v1/github/link", data={"access_token": access_token})

    def github_whoami(self, access_token: str) -> dict[str, Any]:
        return self._post("/api/v1/github/whoami", data={"access_token": access_token})

    def whoami(self) -> dict[str, Any]:
        return self._get("/api/v1/whoami")

    def get_assignment(self) -> dict[str, Any]:
        """Returns {active: [dict, ...], free_pick: bool, menu: list|None, ...}
        — `active` is the whole held batch to run, in claim order. Also carries
        legacy `assignment`/`resumed` (first active lease) for older clients."""
        return self._get("/api/v1/assignment")

    def claim_assignment(self, task_id: str, model: str, effort: str) -> dict[str, Any]:
        """Returns {assignment: dict, resumed: False}. Raises ApiError (409) if
        the cell went stale or the volunteer is already at the concurrent cap."""
        return self._post(
            "/api/v1/assignment/claim",
            data={"task_id": task_id, "model": model, "effort": effort},
        )

    def suggest(self, n: int) -> dict[str, Any]:
        """Weighted-random candidate cells (server-side balanced_random_cells,
        biased toward least-tested), same primitive behind the web's 雷达随机
        推荐 button — powers `dradar go --auto` so a headless/Agent run doesn't
        need a prior web claim. Returns {cells: [menu-entry dict, ...]};
        candidates only, not yet claimed."""
        return self._get(f"/api/v1/suggest?n={n}")

    def mark_started(self, assignment_id: str) -> dict[str, Any]:
        """Confirms the trial subprocess actually started (see runner.run_trial):
        extends a free-pick claim's short initial lease out to the normal
        window. Best-effort by design — callers should swallow ApiError
        rather than let a heartbeat failure abort a real trial. 404 on
        servers that predate this endpoint or on a menu-style lease
        that never had a short window to extend in the first place."""
        return self._post(
            "/api/v1/assignment/started",
            data={"assignment_id": assignment_id},
        )

    def checkout(self, exclude_assignment_ids: set[str] | list[str] | None = None) -> dict[str, Any]:
        """Atomically check out this volunteer's next not-yet-started cell —
        the primitive that makes parallel sessions safe: N concurrent callers
        get N different cells. Returns {assignment: dict|None, held, unstarted};
        assignment None means everything held is already checked out or done.
        exclude_assignment_ids lets one CLI session avoid immediately
        re-checking-out a cell that already failed locally in that session.
        404 on servers that predate the endpoint (caller falls back to the
        legacy whole-batch flow)."""
        excluded = sorted(set(exclude_assignment_ids or ()))
        return self._post(
            "/api/v1/assignment/checkout",
            data={"exclude_assignment_ids": ",".join(excluded)},
        )

    def release_assignments(
        self,
        assignment_ids: list[str] | None = None,
        *,
        release_all: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Immediately release held cells owned by this volunteer.

        The server requires exactly one target mode: explicit IDs or
        release_all. Running cells are protected unless force=True. The
        operation is idempotent so a lost response can be retried safely.
        """
        ids = list(dict.fromkeys(assignment_ids or ()))
        return self._post(
            "/api/v1/assignments/release",
            data={
                "assignment_ids": ",".join(ids),
                "release_all": str(release_all).lower(),
                "force": str(force).lower(),
            },
        )

    def mark_stopped(self, assignment_id: str) -> dict[str, Any]:
        """The counterpart of mark_started: this trial died client-side
        (build flake, agent crash, abandonment) with nothing uploaded, so the
        server should stop showing the cell as 解题中. Same best-effort
        contract — callers swallow ApiError; a stale 'running' badge also
        self-heals server-side after est x3 with no submission."""
        return self._post(
            "/api/v1/assignment/stopped",
            # A cross-session cooldown keeps a second `--parallel` process
            # from immediately taking the same cell that just failed here.
            # Older servers ignore the extra form field harmlessly.
            data={"assignment_id": assignment_id, "defer_seconds": "300"},
        )

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
        return self._post(
            "/api/v1/submissions",
            data={
                "assignment_id": assignment_id,
                "nonce": nonce,
                "outcome": outcome,
                "client_meta": json.dumps(client_meta),
            },
            files=files,
        )
