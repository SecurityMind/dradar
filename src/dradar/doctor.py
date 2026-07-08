"""Environment preflight checks (`dradar doctor`): docker/pier/agent auth,
per-platform fix hints, and a live server-login probe.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .identity import _client
from .local_config import _load_config


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

    all_ok &= _check("pier", bool(shutil.which("pier")), "uv tool install datacurve-pier")

    codex = shutil.which("codex")
    codex_ok = _check("codex CLI", bool(codex), _CODEX_HINTS[plat])
    auth = Path(os.environ.get("CODEX_AUTH_JSON_PATH", Path.home() / ".codex" / "auth.json"))
    codex_auth_ok = _check("codex auth.json", auth.is_file(), "run: codex login")

    claude = shutil.which("claude")
    claude_ok = _check("claude CLI", bool(claude), "npm install -g @anthropic-ai/claude-code")
    claude_token_ok = _check(
        "CLAUDE_CODE_OAUTH_TOKEN", bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
        "run: claude setup-token, then export CLAUDE_CODE_OAUTH_TOKEN before dradar go "
        "(this is a per-session env var, unlike codex's auth.json file — re-export it in "
        "every new shell)",
    )
    # Volunteers only need ONE agent family fully working; don't fail doctor
    # just because the other one isn't configured, but still show both blocks.
    all_ok &= (codex_ok and codex_auth_ok) or (claude_ok and claude_token_ok)

    tasks_root = cfg.get("tasks_root")
    all_ok &= _check(
        "tasks_root configured", bool(tasks_root and Path(tasks_root).is_dir()),
        "dradar login --tasks-root /path/to/deep-swe/tasks",
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
