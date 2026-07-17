"""Run one benchmark trial locally via pier and collect submission artifacts.

The volunteer client runs agent-only (`--disable-verification`); grading is
server-side. model.patch is produced inside the container by the task's own
pre_artifacts.sh, then downloaded by pier into the trial dir.
"""

import glob
import json
import math
import os
import shutil
import subprocess
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .manifest import task_content_hash

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

CODEX_SUBMISSION_PROMPT = """{{ instruction }}

Before finishing, after the implementation is complete and committed, create
the submission artifact required by this benchmark. Run these commands inside
the task container and do not finish until the final check succeeds:

    bash /tests/pre_artifacts.sh
    test -s /logs/artifacts/model.patch
"""


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


class BuildFlakeError(RunnerError):
    """The trial died while BUILDING the task/proxy image — the agent never
    started, so zero quota was consumed. Distinct from RunnerError because
    the caller may retry it once for free; a Chinese-network ARM Mac hitting
    ports.ubuntu.com is the canonical case (volunteer report, 2026-07-14:
    this used to surface as 'model.patch missing (agent likely failed)',
    blaming the agent for a mirror hiccup)."""


# Signatures (in the pier log tail) of an image build / infra failure that
# happened before any agent ran. Deliberately specific: a false positive here
# would auto-retry a run that DID burn quota.
_BUILD_FLAKE_MARKERS = (
    "ports.ubuntu.com", "archive.ubuntu.com", "failed to solve",
    "apt-get update", "Temporary failure resolving", "proxyconnect",
    "TLS handshake timeout", "error getting credentials",
)


def _looks_like_build_flake(log_tail: str) -> bool:
    return any(m in log_tail for m in _BUILD_FLAKE_MARKERS)


def _result_exception_text(result_path: Path | None) -> str:
    """The Pier console tail can end before Docker's actual build error.

    Pier preserves the full setup exception in result.json, so inspect that
    structured source as well. This is diagnostic-only: classification still
    requires one of the deliberately narrow build markers above.
    """
    if not result_path or not result_path.is_file():
        return ""
    try:
        data = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    info = data.get("exception_info") or {}
    if not isinstance(info, dict):
        return ""
    return "\n".join(str(info.get(key) or "") for key in (
        "exception_type", "exception_message", "exception_traceback"))


def _diagnostic_tail(text: str, max_chars: int = 4000) -> str:
    return text[-max_chars:]


def codex_auth_path() -> Path:
    """Where codex keeps its auth (CODEX_AUTH_JSON_PATH overrides the default).
    Shared with doctor so its "agent ready" verdict tests the exact condition
    `dradar go` enforces."""
    return Path(os.environ.get("CODEX_AUTH_JSON_PATH", Path.home() / ".codex" / "auth.json"))


def claude_oauth_token() -> str | None:
    """The Claude Code readiness signal — same sharing rationale as
    codex_auth_path()."""
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")


def _ensure_allowlist(home: Path) -> Path:
    path = home / "codex-chatgpt-allowlist.toml"
    path.write_text(ALLOWLIST_TOML)
    return path


def _ensure_codex_submission_prompt(home: Path) -> Path:
    path = home / "codex-submission-prompt.j2"
    path.write_text(CODEX_SUBMISSION_PROMPT)
    return path


def _task_agent_timeout_sec(task_path: Path) -> float | None:
    """The task's own declared agent watchdog (task.toml's [agent].timeout_sec
    -- currently 5400.0/90min flat across the whole deep-swe set). None if the
    file is missing or malformed; caller must not guess a number in that case."""
    try:
        with (task_path / "task.toml").open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return data.get("agent", {}).get("timeout_sec")


def _agent_timeout_multiplier(assignment: dict, task_path: Path) -> float:
    """pier's own inner agent watchdog must never fire before DRadar's outer
    one (_trial_timeout_sec, which scales with the server's per-cell
    estimate) -- otherwise a long, healthy trial gets killed by pier itself
    well before DRadar's watchdog would ever trigger (volunteer report,
    2026-07-15: a 68-min-estimated ultra cell, outer-allowed ~272 min, killed
    by pier's flat 90-min inner timeout because build_pier_command never told
    pier to stretch it). Only ever stretches pier's timeout, never shrinks it
    below the task's own declared default; +60s of slack keeps DRadar's outer
    watchdog the one that actually fires on a truly wedged run, not a
    same-instant race with pier's."""
    base = _task_agent_timeout_sec(task_path)
    if not base:
        return 1.0
    raw = (_trial_timeout_sec(assignment) + 60) / base
    if raw <= 1.0:
        return 1.0
    # Round UP to 3 decimals: --agent-timeout-multiplier is formatted to the
    # same precision, and rounding to nearest/down could shave the product
    # back under (outer + 60), silently reopening the exact race this exists
    # to close.
    return math.ceil(raw * 1000) / 1000


def build_pier_command(
    assignment: dict,
    tasks_root: Path,
    jobs_dir: Path,
    job_name: str,
    home: Path,
    dev_agent: str | None = None,
    resume_checkpoint: Path | None = None,
) -> list[str]:
    pier = shutil.which("pier")
    if not pier:
        raise RunnerError("pier not found on PATH (run: uv tool install datacurve-pier)")
    task_path = tasks_root / assignment["task_id"]
    if not task_path.is_dir():
        raise RunnerError(f"task not found locally: {task_path}")

    agent = dev_agent or assignment["agent"]
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
    multiplier = _agent_timeout_multiplier(assignment, task_path)
    if multiplier > 1.0:
        cmd += ["--agent-timeout-multiplier", f"{multiplier:.3f}"]
    # Task containers ship no git identity, so an agent's final `git commit`
    # dies with "Author identity unknown" unless the model thinks to configure
    # one (volunteer report, 2026-07-13). These ride pier's --ae into the
    # agent's process env, which every git it spawns inherits. .invalid TLD:
    # never routable, per RFC 2606.
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        cmd += ["--ae", f"{var}=dradar-trial"]
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        cmd += ["--ae", f"{var}=trial@dradar.invalid"]
    if agent == "codex":
        auth = codex_auth_path()
        if not auth.is_file():
            raise RunnerError(f"codex auth not found: {auth} (run `codex login` first)")
        allowlist = _ensure_allowlist(home)
        submission_prompt = _ensure_codex_submission_prompt(home)
        cmd += [
            "--model", assignment["model"],
            "--ak", f"reasoning_effort={assignment['effort']}",
            "--ak", f"config_toml_file={allowlist}",
            "--ak", f"prompt_template_path={submission_prompt}",
            "--ak", "checkpoint_enabled=true",
            "--ak", f"checkpoint_assignment_id={assignment['assignment_id']}",
            "--ak", f"checkpoint_task_id={assignment['task_id']}",
            "--ak", f"checkpoint_effort={assignment['effort']}",
            "--ak", f"checkpoint_resume_generation={assignment.get('resume_generation', 0)}",
            "--ae", f"CODEX_AUTH_JSON_PATH={auth}",
        ]
        if resume_checkpoint is not None:
            cmd += ["--ak", f"checkpoint_path={resume_checkpoint}"]
        # Server-pinned agent version. pier bakes `npm install -g
        # @openai/codex@latest` into a Docker layer, so "latest" freezes at
        # whenever THIS machine first built the image — stale images then
        # fail hard on newer models (400 "requires a newer version of
        # Codex"). Pinning changes the install command string, which busts
        # that cached layer on every volunteer machine automatically, and
        # puts the whole fleet on one known-good version chosen server-side.
        if assignment.get("agent_version"):
            cmd += ["--ak", f"version={assignment['agent_version']}"]
    elif agent == "claude-code":
        oauth_token = claude_oauth_token()
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


def trial_artifact_paths(trial_dir: Path) -> tuple[Path, Path | None, Path | None]:
    """The (patch, trajectory, result) paths inside a trial_dir — the single
    source of truth for pier's artifact layout. Used by run_trial right after
    a run, and by the retry-upload path, which reconstructs the paths from a
    bare trial_dir long after the process that ran the trial exited. The
    optional files are None when absent; the patch path is returned either
    way (callers decide whether a missing patch is fatal)."""
    patch = trial_dir / "artifacts" / "model.patch"
    trajectory = trial_dir / "agent" / "trajectory.json"
    result = trial_dir / "result.json"
    return patch, (trajectory if trajectory.is_file() else None), (result if result.is_file() else None)


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


# The (public) task repo. Self-bootstrap clones it so a volunteer never has to.
DEEP_SWE_REPO = "https://github.com/datacurve-ai/deep-swe"

# Temporary SecurityMind Pier build containing datacurve-ai/pier#23 plus
# persistent workspace/Codex-session checkpoints. Keep the immutable commit
# pin until both fixes are released upstream, then follow the official tag.
PIER_VERSION = "0.3.0.post3"
PIER_COMMIT = "acd1d94a53c9ada225187e4b73206970f14ba415"
PIER_SPEC = (
    "datacurve-pier @ git+https://github.com/SecurityMind/pier.git@"
    f"{PIER_COMMIT}"
)
PIER_INSTALL_COMMAND = f"uv tool install --force '{PIER_SPEC}'"


def _pier_version(pier: str) -> str | None:
    try:
        proc = subprocess.run(
            [pier, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def ensure_pier() -> None:
    """Ensure the pinned Pier build with persistent-resume support is active."""
    pier = shutil.which("pier")
    installed_version = _pier_version(pier) if pier else None
    if installed_version == PIER_VERSION:
        return
    uv = shutil.which("uv")
    if not uv:
        raise RunnerError(
            f"Pier {PIER_VERSION} is required but uv is missing -- install uv first: "
            "curl -LsSf https://astral.sh/uv/install.sh | sh")
    if pier:
        print(f"Pier {installed_version or 'unknown'} lacks persistent resume — "
              f"installing SecurityMind build {PIER_VERSION}...")
    else:
        print(f"pier not found — installing SecurityMind build {PIER_VERSION}...")
    proc = subprocess.run([uv, "tool", "install", "--force", PIER_SPEC])
    active_pier = shutil.which("pier")
    active_version = _pier_version(active_pier) if active_pier else None
    if proc.returncode != 0 or active_version != PIER_VERSION:
        raise RunnerError(
            f"couldn't activate Pier {PIER_VERSION}; run `{PIER_INSTALL_COMMAND}` "
            "yourself and make sure ~/.local/bin precedes other Pier installs on PATH")


def ensure_tasks_root(tasks_root: Path) -> None:
    """Auto-clone the public deep-swe task repo if the configured tasks_root
    doesn't exist yet (magic-command convention: tasks_root is <repo>/tasks), so
    a fresh volunteer doesn't have to clone it by hand. No-op if it's already
    there; bails quietly if the path doesn't fit the convention (leave it to
    the user rather than clobber something)."""
    if tasks_root.is_dir():
        return
    if tasks_root.name != "tasks":
        raise RunnerError(
            f"tasks_root {tasks_root} doesn't exist and doesn't look like a "
            f"deep-swe/tasks path; clone {DEEP_SWE_REPO} and point --tasks-root at its tasks/")
    repo_dir = tasks_root.parent
    if repo_dir.exists() and any(repo_dir.iterdir()):
        raise RunnerError(
            f"{repo_dir} exists but has no tasks/ dir; not touching it — "
            f"make sure it's a clean {DEEP_SWE_REPO} checkout")
    print(f"deep-swe task repo not found; cloning {DEEP_SWE_REPO} → {repo_dir} (one-time)...")
    proc = subprocess.run(["git", "clone", DEEP_SWE_REPO, str(repo_dir)])
    if proc.returncode != 0 or not tasks_root.is_dir():
        raise RunnerError(f"failed to clone deep-swe into {repo_dir}")
    print(f"  cloned; tasks at {tasks_root}")


def sync_deep_swe_commit(tasks_root: Path, pinned: str) -> bool:
    """Fetch + checkout the exact commit the server grades against, so a drifted
    checkout self-heals instead of hard-failing. Returns True on success."""
    for cmd in (["git", "-C", str(tasks_root), "fetch", "--depth", "1", "origin", pinned],
                ["git", "-C", str(tasks_root), "checkout", pinned]):
        try:
            if subprocess.run(cmd, capture_output=True, text=True, timeout=120).returncode != 0:
                return False
        except (OSError, subprocess.TimeoutExpired):
            return False
    return local_deep_swe_commit(tasks_root) == pinned


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


def _tail(log_path: Path, n: int = 15) -> str:
    """Last n lines of the pier log, for inlining into trial-failure messages:
    after a 30-120 min run the actual cause (docker pull failure, auth
    rejection, rate-limit death) sits at the end of that log, and just naming
    the file makes the volunteer go hunt for it. Local-terminal only — never
    uploaded — so no scrub concern."""
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


HEARTBEAT_SEC = 60


def _last_activity(log_path: Path) -> str:
    """The newest meaningful chunk of the pier log, for heartbeat lines.
    pier redraws its progress bar with carriage returns inside one physical
    line, so split on \\r as well and skip pure control/blank chunks."""
    raw = _tail(log_path, 1)
    chunks = [c.strip() for c in raw.replace("\r", "\n").splitlines() if c.strip()]
    return (chunks[-1][:120] if chunks else "still running (no new log output)")


def _trial_timeout_sec(assignment: dict) -> int:
    """Cap for one trial: a generous multiple of the server's estimate, with
    a floor for image pull/build."""
    est_min = assignment.get("est_minutes") or 30
    return max(1800, int(est_min) * 60 * 4)


def run_trial(
    assignment: dict,
    tasks_root: Path,
    work_dir: Path,
    dev_agent: str | None = None,
    on_started: Callable[[], None] | None = None,
    resume_checkpoint: Path | None = None,
) -> TrialArtifacts:
    work_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir = work_dir / "jobs"
    job_name = f"a{assignment['assignment_id']}"
    # A fresh lease re-run must not collide with a stale job dir.
    if (jobs_dir / job_name).exists():
        job_name = f"{job_name}-{int(time.time())}"

    log_path = work_dir / f"{job_name}.log"
    # Cap the run so a wedged docker/agent can't hang the CLI forever.
    # A mid-task rate-limit death just ends the run (no sleep-and-resume) --
    # it surfaces as a nonzero pier rc, which _run_and_submit reports as
    # `interrupted` -> the server marks it invalid and the cell reopens.
    timeout_sec = _trial_timeout_sec(assignment)

    if resume_checkpoint is None:
        cmd = build_pier_command(
            assignment, tasks_root, jobs_dir, job_name, work_dir, dev_agent)
    else:
        cmd = build_pier_command(
            assignment, tasks_root, jobs_dir, job_name, work_dir,
            dev_agent, resume_checkpoint=resume_checkpoint,
        )
    env = dict(os.environ)
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
        # Heartbeat loop instead of a blocking run: image build + a long
        # agent turn can be silent for many minutes, and volunteers couldn't
        # tell "working" from "wedged" without docker-exec'ing into the
        # container (volunteer report, 2026-07-13). Once a minute, print
        # elapsed time plus the newest pier log line.
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                cwd=work_dir, env=env)
        try:
            next_beat = started + HEARTBEAT_SEC
            while True:
                try:
                    proc.wait(timeout=min(30, HEARTBEAT_SEC))
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.time()
                if now - started > timeout_sec:
                    log.flush()
                    raise RunnerError(
                        f"trial exceeded {timeout_sec // 60} min and was aborted "
                        f"(see {log_path}); docker/agent likely wedged\n"
                        f"last lines of the log:\n{_tail(log_path)}")
                if now >= next_beat:
                    next_beat = now + HEARTBEAT_SEC
                    print(f"  … {int((now - started) / 60)} min elapsed — "
                          f"{_last_activity(log_path)}")
        except BaseException:
            # Same contract subprocess.run had: no exception (timeout, Ctrl-C,
            # anything) leaves a pier process running detached. TERM first
            # with a grace window: a SIGKILLed pier can never `docker compose
            # down`, and its orphaned task container keeps the agent alive —
            # burning quota with nobody left to harvest the result.
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise
    duration = time.time() - started

    tail = _tail(log_path)
    try:
        job_dir, trial_dir = locate_artifacts(jobs_dir, job_name)
    except RunnerError:
        if _looks_like_build_flake(tail):
            raise BuildFlakeError(
                f"the task environment failed to BUILD (mirror/network flake) — "
                f"the agent never started and no quota was used.\n"
                f"last lines of the log:\n{tail}")
        raise
    patch, trajectory, result = trial_artifact_paths(trial_dir)
    if not patch.is_file():
        # No patch at all means the agent never produced anything — usually
        # the environment died under it. Say which, instead of blaming the
        # agent for a mirror hiccup.
        result_exception = _result_exception_text(result)
        diagnostic = "\n".join(x for x in (tail, result_exception) if x)
        if _looks_like_build_flake(diagnostic):
            raise BuildFlakeError(
                f"the task environment failed to BUILD (mirror/network flake) — "
                f"the agent never started and no quota was used.\n"
                f"build diagnostic:\n{_diagnostic_tail(diagnostic)}")
        raise RunnerError(
            f"model.patch missing (agent likely failed; see {log_path} and {trial_dir})\n"
            f"last lines of the log:\n{tail}"
        )
    return TrialArtifacts(
        job_dir=job_dir,
        trial_dir=trial_dir,
        patch=patch,
        trajectory=trajectory,
        result=result,
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
    exc = data.get("exception_info") or {}
    return {
        "n_input_tokens": agent.get("n_input_tokens"),
        "n_cache_tokens": agent.get("n_cache_tokens"),
        "n_output_tokens": agent.get("n_output_tokens"),
        "n_agent_steps": agent.get("n_agent_steps"),
        "exception_info": bool(exc),
        # the WHY, not just the whether: rides client_meta to the server so
        # `dradar status` / operators can tell rate-limit from stale-agent
        # from auth failure without opening the uploaded result.json
        "exception_type": exc.get("exception_type"),
    }


def diagnose_exception(result_path: Path | None) -> dict:
    """Classify a trial's recorded exception for honest console reporting:
    {} when there is none, else {type, tail, kind} where kind is one of
    stale-agent | rate-limit | auth | None (unrecognized). The message tail
    matters most: pier's exception_message embeds the agent's actual output,
    which for codex includes the API error JSON naming the real cause."""
    if not result_path or not result_path.is_file():
        return {}
    try:
        data = json.loads(result_path.read_text())
    except json.JSONDecodeError:
        return {}
    info = data.get("exception_info") or {}
    if not info:
        return {}
    msg = info.get("exception_message") or ""
    low = msg.lower()
    kind = None
    if "requires a newer version of codex" in low:
        kind = "stale-agent"
    elif any(s in low for s in ("rate limit", "rate_limit", "usage_limit",
                                "too many requests", "429")):
        kind = "rate-limit"
    elif any(s in low for s in ("401", "unauthorized", "authentication failed",
                                "invalid api key", "token expired")):
        kind = "auth"
    elif "at capacity" in low:
        # codex treats a momentary "Selected model is at capacity" as a fatal
        # turn.failed, which pier reports as a plain nonzero exit -- this is
        # not a real failure of the agent's work (volunteer report #3,
        # 2026-07-15: caught mid-run after 1,327 passing tests). The pinned
        # SecurityMind Pier build resumes the root session with bounded
        # retries; reaching this diagnostic means those retries were
        # exhausted. Keep the distinct classification for honest reporting.
        kind = "model-capacity"
    tail = [ln.strip() for ln in msg.splitlines() if ln.strip()][-6:]
    return {"type": info.get("exception_type"), "kind": kind, "tail": tail}


# Targeted advice per diagnose_exception kind. Only the rate-limit case may
# mention quota — an unrecognized failure gets the artifact paths, not a
# guess (a volunteer bug report proved "wait for your quota to reset" was
# actively misleading for a version error).
DIAG_ADVICE = {
    "stale-agent": (
        "the codex CLI baked into your pier container image is too old for "
        "this model. Update dradar (add --refresh to your uvx command) and "
        "re-run: the server now pins the agent version, which rebuilds the "
        "image automatically. If this repeats on the latest dradar, tell the "
        "radar operators — the server-side pin may need a bump."),
    "rate-limit": (
        "this looks like a genuine rate/usage limit — wait for your quota "
        "window to reset, then claim again."),
    "auth": (
        "the agent could not authenticate inside the container — run "
        "`codex login` again and re-check `dradar doctor`."),
    "model-capacity": (
        "the model stayed at capacity after Pier retried the original Codex "
        "session with bounded backoff. This is not a problem with your setup "
        "or work; the automatic recovery was attempted but could not finish "
        "within its retry budget. Claim the cell again later."),
}
