"""Run one benchmark trial locally via pier and collect submission artifacts.

The volunteer client runs agent-only (`--disable-verification`); grading is
server-side. model.patch is produced inside the container by the task's own
pre_artifacts.sh, then downloaded by pier into the trial dir.
"""

import glob
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .manifest import task_content_hash
from .resume_agent import MAX_RESUMES, _MAX_WAIT_SEC

# The egress allowlist alone does NOT stop the agent from searching the web:
# codex/Claude web tools execute server-side (at OpenAI/Anthropic), riding the
# same allowed API channel. So we also disable the web tool at the agent-config
# layer. Codex's key is a TOP-LEVEL string `web_search = "disabled"` (verified
# behaviourally: with it, codex makes zero web_search calls and reports no web
# tool). It MUST come before any [table] header or TOML nests it into that
# table; pier appends this block first into an otherwise-empty config.toml.
# Server-side trajectory audit is the backstop if a client tampers with this.
ALLOWLIST_TOML = (
    'web_search = "disabled"\n'
    '[__pier_allowlist]\n'
    'url = "https://chatgpt.com"\n'
)

# Claude Code: deny the web tools (and keep pier's default EnterPlanMode deny).
CLAUDE_DISALLOWED_TOOLS = "WebSearch WebFetch EnterPlanMode"


@dataclass
class TrialArtifacts:
    job_dir: Path
    trial_dir: Path
    patch: Path
    trajectory: Path | None
    result: Path | None
    returncode: int
    duration_sec: float
    log_path: Path


class RunnerError(RuntimeError):
    pass


def _ensure_allowlist(home: Path) -> Path:
    path = home / "codex-chatgpt-allowlist.toml"
    path.write_text(ALLOWLIST_TOML)
    return path


# The smaller of the two values every deep-swe task.toml declares for
# [agent] timeout_sec (the other is 5400). pier's own --agent-timeout-
# multiplier multiplies whichever base the ACTUAL task happens to declare —
# dividing by the smallest possible base (below) when deriving a multiplier
# guarantees the result is large enough regardless of which base a given
# task really has (it only ever over-shoots for 5400s-base tasks, which has
# no real cost — see build_pier_command's docstring).
_MIN_TASK_AGENT_TIMEOUT_SEC = 1800


def build_pier_command(
    assignment: dict,
    tasks_root: Path,
    jobs_dir: Path,
    job_name: str,
    home: Path,
    dev_agent: str | None = None,
    resilient_timeout_sec: int | None = None,
) -> list[str]:
    """resilient_timeout_sec: this run's OWN outer subprocess.run timeout
    (only meaningful on the resilient-codex path) -- required so pier's
    inner --agent-timeout-multiplier can be derived to guarantee pier's own
    watchdog never fires before dradar's outer one does, for THIS specific
    run's actual timeout, rather than trusting a fixed constant to happen to
    be large enough for every est_minutes/task combination."""
    pier = shutil.which("pier")
    if not pier:
        raise RunnerError("pier not found on PATH (run: uv tool install datacurve-pier)")
    task_path = tasks_root / assignment["task_id"]
    if not task_path.is_dir():
        raise RunnerError(f"task not found locally: {task_path}")

    agent = dev_agent or assignment["agent"]
    if agent == "codex" and not dev_agent:
        # Quota-resilient wrapper: same pier codex driver, but a mid-task
        # rate-limit pauses and resumes the session instead of dying (see
        # dradar/resume_agent.py). Import path resolves inside pier's venv
        # via the PYTHONPATH the runner injects.
        #
        # pier enforces its OWN agent wall-clock ceiling independently of
        # this process's subprocess.run timeout (asyncio.wait_for around the
        # whole agent.run() coroutine -> AgentTimeoutError) — every deep-swe
        # task.toml sets [agent] timeout_sec to 1800 or 5400. A resume sleep
        # can run up to MAX_RESUMES x 6h; without raising pier's own ceiling
        # too, pier kills the trial (and the sleeping resume coroutine with
        # it) long before a real multi-hour rate-limit window resets, and the
        # sleep-and-resume mechanism never actually gets to resume anything.
        # The multiplier is DERIVED from this run's own outer timeout (see
        # run_trial) rather than a fixed guess: a fixed constant can be too
        # SMALL for some est_minutes (pier's inner ceiling ends up shorter
        # than dradar's own outer one, so pier still fires first defeating
        # the fix) or needlessly huge for others. Deriving it against the
        # smallest possible task base guarantees pier's inner ceiling is
        # always >= this run's outer one, so dradar's own subprocess.run
        # timeout below remains the true, GUARANTEED ceiling in every case.
        multiplier = 30
        if resilient_timeout_sec:
            multiplier = max(1, -(-int(resilient_timeout_sec) // _MIN_TASK_AGENT_TIMEOUT_SEC))
        agent_args = ["--agent-import-path", "dradar.resume_agent:QuotaResilientCodex",
                     "--agent-timeout-multiplier", str(multiplier)]
    else:
        agent_args = ["--agent", agent]
    cmd = [
        pier, "run",
        "-p", str(task_path),
        *agent_args,
        "--jobs-dir", str(jobs_dir),
        "--job-name", job_name,
        "--n-concurrent", "1",
        "--max-retries", "0",
        "--disable-verification",
        "--yes",
    ]
    if agent == "codex":
        auth = Path(os.environ.get("CODEX_AUTH_JSON_PATH", Path.home() / ".codex" / "auth.json"))
        if not auth.is_file():
            raise RunnerError(f"codex auth not found: {auth} (run `codex login` first)")
        allowlist = _ensure_allowlist(home)
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", f"config_toml_file={allowlist}",
            "--ae", f"CODEX_AUTH_JSON_PATH={auth}",
        ]
    elif agent == "claude-code":
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if not oauth_token:
            raise RunnerError(
                "CLAUDE_CODE_OAUTH_TOKEN not set (run: claude setup-token, "
                "then export CLAUDE_CODE_OAUTH_TOKEN before dradar go)"
            )
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", "version=2.1.197",
            "--ak", f"disallowed_tools={CLAUDE_DISALLOWED_TOOLS}",
            "--ae", f"CLAUDE_CODE_OAUTH_TOKEN={oauth_token}",
            "--ae", "API_TIMEOUT_MS=3000000",
            "--ae", "CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000",
            "--ae", "CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000",
        ]
    return cmd


def locate_artifacts(jobs_dir: Path, job_name: str) -> tuple[Path, Path]:
    job_dir = jobs_dir / job_name
    trials = [Path(p) for p in glob.glob(str(job_dir / "*__*")) if Path(p).is_dir()]
    if not trials:
        raise RunnerError(f"no trial dir under {job_dir}")
    return job_dir, trials[0]


def local_deep_swe_commit(tasks_root: Path) -> str | None:
    """HEAD commit of the volunteer's deep-swe checkout, or None when git is
    unavailable or tasks_root isn't inside a work tree (e.g. a plain tarball
    download — the per-task content hash still covers that case)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(tasks_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def check_task_content_hash(assignment: dict, tasks_root: Path) -> bool | None:
    """Compare the server's task_content_hash against this volunteer's local
    checkout. Returns None when the assignment carries no hash to compare
    against (older server). A mismatch is a detection signal for the server,
    not a client-side hard stop — the caller should warn but keep running."""
    expected = assignment.get("task_content_hash")
    if not expected:
        return None
    actual = task_content_hash(tasks_root, assignment["task_id"])
    match = actual == expected
    if not match:
        print(
            "warning: your local deep-swe checkout does not match the server "
            "copy for this task, pull the latest deep-swe repo"
        )
    return match


def run_trial(
    assignment: dict,
    tasks_root: Path,
    work_dir: Path,
    dev_agent: str | None = None,
    on_started: Callable[[], None] | None = None,
) -> TrialArtifacts:
    work_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir = work_dir / "jobs"
    job_name = f"a{assignment['assignment_id']}"
    # A fresh lease re-run must not collide with a stale job dir.
    if (jobs_dir / job_name).exists():
        job_name = f"{job_name}-{int(time.time())}"

    log_path = work_dir / f"{job_name}.log"
    # Cap the run so a wedged docker/agent can't hang the CLI past the lease.
    # Generous multiple of the estimate (+ a floor for image pull/build).
    est_min = assignment.get("est_minutes") or 30
    timeout_sec = max(1800, int(est_min) * 60 * 4)
    resilient = not dev_agent and (assignment.get("agent") == "codex")
    if resilient:
        # Worst case the resilient agent can legitimately need: every resume
        # attempt hits the window wall again (MAX_RESUMES x up to
        # _MAX_WAIT_SEC of sleep), PLUS real working time across the initial
        # attempt and each resume (bounded generously by the larger observed
        # per-task agent timeout, 5400s, x (MAX_RESUMES+1) attempts). This
        # value also drives pier's own inner timeout multiplier below, so
        # dradar's own subprocess.run timeout stays the guaranteed ceiling.
        timeout_sec += MAX_RESUMES * _MAX_WAIT_SEC + (MAX_RESUMES + 1) * 5400

    cmd = build_pier_command(assignment, tasks_root, jobs_dir, job_name, work_dir, dev_agent,
                             resilient_timeout_sec=timeout_sec if resilient else None)
    env = dict(os.environ)
    if resilient:
        import dradar
        pkg_parent = str(Path(dradar.__file__).resolve().parent.parent)
        env["PYTHONPATH"] = (pkg_parent + os.pathsep + env["PYTHONPATH"]
                             if env.get("PYTHONPATH") else pkg_parent)
    if on_started is not None:
        # Best-effort by design: this only confirms to the server that a
        # free-pick claim's short initial lease should be extended (see
        # app.py's assignment_started endpoint) -- a network hiccup here must
        # never abort a real trial that's about to burn real quota.
        try:
            on_started()
        except Exception:
            pass
    started = time.time()
    with log_path.open("w") as log:
        log.write("cmd=" + " ".join(cmd) + "\n")
        log.flush()
        try:
            proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                                  cwd=work_dir, env=env, timeout=timeout_sec)
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(
                f"trial exceeded {timeout_sec // 60} min and was aborted "
                f"(see {log_path}); docker/agent likely wedged"
            ) from exc
    duration = time.time() - started

    job_dir, trial_dir = locate_artifacts(jobs_dir, job_name)
    patch = trial_dir / "artifacts" / "model.patch"
    if not patch.is_file():
        raise RunnerError(
            f"model.patch missing (agent likely failed; see {log_path} and {trial_dir})"
        )
    trajectory = trial_dir / "agent" / "trajectory.json"
    result = trial_dir / "result.json"
    return TrialArtifacts(
        job_dir=job_dir,
        trial_dir=trial_dir,
        patch=patch,
        trajectory=trajectory if trajectory.is_file() else None,
        result=result if result.is_file() else None,
        returncode=proc.returncode,
        duration_sec=duration,
        log_path=log_path,
    )


def summarize_result(result_path: Path | None) -> dict:
    """Extract token/cost stats from a trial result.json for client_meta."""
    if not result_path or not result_path.is_file():
        return {}
    try:
        data = json.loads(result_path.read_text())
    except json.JSONDecodeError:
        return {}
    agent = data.get("agent_result") or {}
    return {
        "n_input_tokens": agent.get("n_input_tokens"),
        "n_cache_tokens": agent.get("n_cache_tokens"),
        "n_output_tokens": agent.get("n_output_tokens"),
        "n_agent_steps": agent.get("n_agent_steps"),
        "exception_info": bool(data.get("exception_info")),
    }
