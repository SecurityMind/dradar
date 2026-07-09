"""dradar volunteer CLI: argparse wiring for login / doctor / go / resume /
rename / link-github.

The actual command implementations live in sibling modules, split by concern:
  identity.py  - login/register/rename/GitHub device-flow binding
  doctor.py    - environment preflight checks
  runloop.py   - the go/resume bundle-and-menu execution loop
  local_config.py - the shared ~/.dradar/config.json + constants

This file re-exports their public names (so `dradar.cli.whatever` keeps
working for anything scripted against the old single-file layout) and owns
only the argparse tree + `main()`, which is this package's console-script
entry point (see pyproject.toml).
"""

import argparse
import sys

from . import __version__
from .api_client import ApiClient, ApiError
from .doctor import (
    _CODEX_HINTS, _DOCKER_HINTS, _check, _probe, _platform, cmd_doctor,
)
from .identity import (
    TIERS, _auto_register, _client, _github_device_token, cmd_link_github,
    cmd_login, cmd_rename, cmd_status,
)
from .local_config import CONFIG_PATH, HOME, _load_config, _save_config
from .runloop import (
    _check_version_pin, _choose_menu_entry, _claim_from_menu, _print_assignment,
    _print_menu, _run_and_submit,
    _go_menu, cmd_go, cmd_retry_upload,
)

__all__ = [
    "ApiClient", "ApiError", "HOME", "CONFIG_PATH", "TIERS",
    "cmd_login", "cmd_rename", "cmd_link_github", "cmd_doctor", "cmd_go",
    "cmd_retry_upload", "main",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dradar", description="DRadar crowdtest client")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="save server + token (or register with --nickname)")
    p_login.add_argument("--server")
    p_login.add_argument("--token")
    p_login.add_argument("--nickname", help="register a new account instead of using a token")
    p_login.add_argument("--tasks-root", help="path to deep-swe/tasks checkout")
    p_login.add_argument("--tier", choices=TIERS, help="subscription tier (asked on first go otherwise)")
    p_login.add_argument("--github", action="store_true",
                         help="recover your identity via GitHub (device flow)")
    p_login.set_defaults(func=cmd_login)

    p_doc = sub.add_parser("doctor", help="preflight checks")
    p_doc.set_defaults(func=cmd_doctor)

    p_ren = sub.add_parser("rename", help="change your leaderboard name (points stay)")
    p_ren.add_argument("nickname")
    p_ren.set_defaults(func=cmd_rename)

    p_st = sub.add_parser("status", help="see your own recent submissions, points, and flags")
    p_st.set_defaults(func=cmd_status)

    p_gh = sub.add_parser("link-github",
                          help="bind your GitHub account (name + avatar on the board, cross-machine recovery)")
    p_gh.set_defaults(func=cmd_link_github)

    p_retry = sub.add_parser(
        "retry-upload",
        help="flush any trials that ran but failed to upload (also runs automatically before `go`)")
    p_retry.set_defaults(func=cmd_retry_upload)

    for name, help_, is_resume in (
        ("go", "fetch an assignment and run it", False),
        ("resume", "continue the active assignment (no-op if none)", True),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
        p.add_argument("--keep", action="store_true", help="keep local job dir after upload")
        p.add_argument(
            "--allow-task-drift", action="store_true",
            help="run even if your deep-swe checkout differs from the server's pinned version",
        )
        p.add_argument("--dev-agent", help=argparse.SUPPRESS)  # oracle/nop for pipeline tests
        p.set_defaults(func=cmd_go, resume=is_resume)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
