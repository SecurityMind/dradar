"""Environment preflight checks (`dradar doctor`): docker/pier/agent auth,
per-platform fix hints, and a live server-login probe.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, runner
from .identity import _client
from .local_config import _load_config, tasks_root_from_config


def _check(label: str, ok: bool, hint: str = "") -> bool:
    mark = "ok " if ok else "FAIL"
    print(f"  [{mark}] {label}" + ("" if ok else f"  -> {hint}"))
    return ok


def _platform(proc_version: Path = Path("/proc/version")) -> str:
    """'macos' | 'wsl' | 'linux' | 'windows'. WSL2 is the supported way to run
    on Windows; native win32 gets a hard stop with setup guidance. Anything
    else (BSDs...) gets the linux guidance as the closest fit."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        try:
            if "microsoft" in proc_version.read_text().lower():
                return "wsl"
        except OSError:
            pass
    return "linux"


# Per-platform fix hints for the environment checks. Volunteer machines are
# macOS / Linux / WSL2; these strings are the first thing a stuck volunteer
# sees, so they name the exact command for THEIR platform.
_DOCKER_HINTS = {
    "macos": {
        "cli": "install OrbStack: brew install --cask orbstack",
        "daemon": "start OrbStack: open -a OrbStack (or daemon is wedged)",
        "compose": "ln -s /Applications/OrbStack.app/Contents/MacOS/xbin/docker-compose "
                   "~/.docker/cli-plugins/docker-compose",
    },
    "linux": {
        "cli": "install Docker Engine: https://docs.docker.com/engine/install/ "
               "then add yourself to the docker group: sudo usermod -aG docker $USER (re-login)",
        "daemon": "sudo systemctl enable --now docker "
                  "(permission denied = you're not in the docker group yet)",
        "compose": "install the compose plugin: sudo apt install docker-compose-plugin "
                   "(or your distro's equivalent)",
    },
    "wsl": {
        "cli": "enable Docker Desktop's WSL integration (Settings > Resources > WSL "
               "integration), or install Docker Engine inside WSL: "
               "https://docs.docker.com/engine/install/ubuntu/",
        "daemon": "start Docker Desktop on Windows, or inside WSL: sudo service docker start",
        "compose": "update Docker Desktop (bundles compose v2), or: "
                   "sudo apt install docker-compose-plugin",
    },
}

_CODEX_HINTS = {
    "macos": "brew install codex",
    "linux": "npm install -g @openai/codex",
    "wsl": "npm install -g @openai/codex",
}


def _probe(cmd: list[str]) -> bool:
    """Run a doctor probe; a wedged daemon must not hang doctor forever."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def cmd_doctor(args) -> int:
    cfg = _load_config()
    plat = _platform()
    print(f"dradar {__version__} doctor ({plat})")
    if plat == "windows":
        _check("platform", False,
               "native Windows is not supported — run dradar inside WSL2: "
               "PowerShell (admin): wsl --install, reboot, open Ubuntu, then "
               "re-run the install script there")
        return 1
    hints = _DOCKER_HINTS[plat]
    all_ok = True

    docker = shutil.which("docker")
    all_ok &= _check("docker CLI", bool(docker), hints["cli"])
    if docker:
        daemon = _probe([docker, "info"])
        all_ok &= _check("docker daemon", daemon, hints["daemon"])
        compose = _probe([docker, "compose", "version"])
        all_ok &= _check("docker compose plugin", compose, hints["compose"])

    # pier is auto-installed on `dradar go`; do it here too so doctor reflects
    # the ready state instead of a scary FAIL a volunteer (or an agent following
    # a runbook) then chases with the wrong fix.
    try:
        runner.ensure_pier()
    except runner.RunnerError:
        pass
    pier = shutil.which("pier")
    pier_ready = bool(
        pier and runner._pier_version_compatible(runner._pier_version(pier)))
    all_ok &= _check("pier", pier_ready, runner.PIER_INSTALL_COMMAND)

    # Agent: you only need ONE family working. If the one you're set up for is
    # ready, say so and stay quiet about the other -- don't print a FAIL for
    # claude when you use codex (that false alarm is what sends an agent down a
    # rabbit hole). Only nag about specifics when NEITHER is ready.
    codex = shutil.which("codex")
    auth = runner.codex_auth_path()
    codex_ready = bool(codex) and auth.is_file()
    claude_ready = bool(shutil.which("claude")) and bool(runner.claude_oauth_token())
    if codex_ready:
        _check("codex — agent ready", True)
    elif claude_ready:
        _check("claude — agent ready", True)
    else:
        _check("codex CLI", bool(codex), _CODEX_HINTS[plat])
        _check("codex auth.json", auth.is_file(), "run: codex login")
        _check("claude CLI (alternative to codex)", bool(shutil.which("claude")),
               "npm install -g @anthropic-ai/claude-code")
        _check("CLAUDE_CODE_OAUTH_TOKEN (alternative to codex)",
               bool(runner.claude_oauth_token()),
               "or: claude setup-token, then export CLAUDE_CODE_OAUTH_TOKEN each shell")
    all_ok &= (codex_ready or claude_ready)

    # The task repo is auto-cloned on `dradar go`; do it here too so a missing
    # checkout reports OK instead of a FAIL whose hint doesn't actually fix it.
    tasks_root = tasks_root_from_config(cfg)
    if not tasks_root.is_dir():
        try:
            runner.ensure_tasks_root(tasks_root)
        except runner.RunnerError:
            pass
    all_ok &= _check(
        "tasks_root",
        tasks_root.is_dir(),
        "run `dradar go` once — it auto-clones the task repo at "
        f"{tasks_root}",
    )

    free_gb = shutil.disk_usage(Path.home()).free / 1e9
    all_ok &= _check(f"disk free ({free_gb:.0f} GB)", free_gb > 20, "need >20GB for task images")

    if cfg.get("server") and cfg.get("token"):
        try:
            me = _client(cfg).whoami()
            all_ok &= _check(f"server login ({me['nickname']})", True)
        except Exception as exc:  # noqa: BLE001
            all_ok &= _check("server login", False, str(exc))
    else:
        all_ok &= _check("server login", False, "dradar login --server <url> --token <token>")

    print("all checks passed" if all_ok else "fix the FAIL items above, then re-run: dradar doctor")
    return 0 if all_ok else 1


__all__ = ["cmd_doctor", "_platform", "_check", "_probe", "_DOCKER_HINTS", "_CODEX_HINTS"]
