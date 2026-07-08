"""Quota-resilient codex agent for pier: pause on a rate-limit death, resume
the SAME codex session after the window resets, finish the task.

Injected by the dradar runner via `pier run --agent-import-path
dradar.resume_agent:QuotaResilientCodex` (the volunteer's PYTHONPATH carries
the dradar package into pier's venv; this module imports only pier, stdlib,
and the stdlib-only dradar.limits probe).

Mechanism: pier's stock Codex agent issues exactly one container command
containing "codex exec " (pipefail is on, so a rate-limited codex propagates
its exit code and exec_as_agent raises RuntimeError). We intercept that one
command; on failure we read the tee'd output tail to classify the death,
sleep on the HOST until the account's real 5h window resets (app-server
probe), then `codex exec resume --last` INSIDE the still-running container —
the session state under $CODEX_HOME/sessions is intact, so the model
continues from where it stopped and no quota already spent is wasted. At
most MAX_RESUMES pauses; anything else re-raises and degrades to the
existing interrupted-upload path. Pauses are recorded to
agent/quota_pauses.json for transparent server-side annotation.
"""

import asyncio
import json
import re
import shlex
import time

from dradar.limits import read_rate_limits

try:  # pier lives in ITS venv (where this module is actually imported from);
    # the detection/wait helpers stay importable everywhere for tests.
    from pier.agents.installed.codex import Codex
    from pier.models.trial.paths import EnvironmentPaths
except ImportError:  # pragma: no cover - dev environments without pier
    Codex = None
    EnvironmentPaths = None

MAX_RESUMES = 2
_FALLBACK_WAIT_SEC = 1800.0
_MIN_WAIT_SEC = 300.0
_MAX_WAIT_SEC = 6 * 3600.0
_RATE_LIMIT_RE = re.compile(
    r"usage limit|rate.?limit|too many requests|\b429\b", re.IGNORECASE)
# codex runs with --json (one event per line); a genuine rate-limit death is
# what the process choked ON LAST, not something incidentally mentioned
# somewhere in the 6000-byte tail (a model turn or tool output discussing
# HTTP 429s / rate-limiting code would otherwise false-positive a multi-hour
# sleep for a trial that actually failed for an unrelated reason). Wide
# enough to survive some trailing shutdown/cleanup/exit chatter after the
# actual error line, while still far short of the whole 6000-byte blob.
_RATE_LIMIT_TAIL_LINES = 15

RESUME_PROMPT = (
    "The previous run stopped because the account hit its usage limit; the "
    "limit window has now reset. Continue the task from exactly where you "
    "left off and finish it."
)


def looks_rate_limited(output_tail: str) -> bool:
    lines = [line for line in (output_tail or "").strip().splitlines() if line.strip()]
    scoped = "\n".join(lines[-_RATE_LIMIT_TAIL_LINES:])
    return bool(_RATE_LIMIT_RE.search(scoped))


def seconds_until_window_reset(now: float | None = None) -> float:
    """Host-side probe for the real reset instant; conservative fallback."""
    now = now if now is not None else time.time()
    rl = read_rate_limits()
    reset = rl.get("five_hour_resets_at") if rl else None
    if not reset:
        return _FALLBACK_WAIT_SEC
    return min(_MAX_WAIT_SEC, max(_MIN_WAIT_SEC, reset - now + 120))


class QuotaResilientCodex(Codex if Codex is not None else object):
    def _resume_command(self) -> str:
        model = self._command_model_name or self.model_name.split("/")[-1]
        cli_flags = self.build_cli_flags()
        cli_flags_arg = (cli_flags + " ") if cli_flags else ""
        out = (EnvironmentPaths.agent_dir / self._OUTPUT_FILENAME).as_posix()
        return (
            "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
            "codex exec resume --last "
            "--dangerously-bypass-approvals-and-sandbox "
            "--skip-git-repo-check "
            f"--model {model} "
            "--json "
            f"{cli_flags_arg}"
            "-- "
            f"{shlex.quote(RESUME_PROMPT)} "
            f"2>&1 </dev/null | tee -a {shlex.quote(out)}"
        )

    async def _output_tail(self, environment, env) -> str:
        out = (EnvironmentPaths.agent_dir / self._OUTPUT_FILENAME).as_posix()
        try:
            result = await super().exec_as_agent(
                environment,
                command=f"tail -c 6000 {shlex.quote(out)} 2>/dev/null || true",
                env=env,
            )
        except RuntimeError:
            return ""
        return getattr(result, "stdout", None) or ""

    async def _record_pauses(self, environment, env, pauses: list[dict]) -> None:
        marker = (EnvironmentPaths.agent_dir / "quota_pauses.json").as_posix()
        payload = shlex.quote(json.dumps({"pauses": pauses}))
        try:
            await super().exec_as_agent(
                environment,
                command=f"printf '%s' {payload} > {shlex.quote(marker)}",
                env=env,
            )
        except RuntimeError:
            pass  # transparency marker is best-effort, never fail the trial

    async def exec_as_agent(self, environment, command, env=None,
                            cwd=None, timeout_sec=None):
        is_main = " codex exec " in command and " resume " not in command
        if not is_main:
            return await super().exec_as_agent(
                environment, command, env=env, cwd=cwd, timeout_sec=timeout_sec)

        pauses: list[dict] = []
        attempt_cmd = command
        while True:
            try:
                result = await super().exec_as_agent(
                    environment, attempt_cmd, env=env, cwd=cwd,
                    timeout_sec=timeout_sec)
                if pauses:
                    await self._record_pauses(environment, env, pauses)
                return result
            except RuntimeError:
                tail = await self._output_tail(environment, env)
                if not looks_rate_limited(tail) or len(pauses) >= MAX_RESUMES:
                    if pauses:
                        await self._record_pauses(environment, env, pauses)
                    raise
                wait = seconds_until_window_reset()
                resume_at = time.strftime(
                    "%H:%M", time.localtime(time.time() + wait))
                self.logger.info(
                    "codex hit a usage limit mid-task; pausing until ~%s "
                    "(%.0f min), then resuming the same session",
                    resume_at, wait / 60)
                pauses.append({"at": time.time(), "wait_sec": round(wait)})
                await asyncio.sleep(wait)
                attempt_cmd = self._resume_command()
