"""dradar volunteer CLI: argparse wiring for login / doctor / go / resume /
rename / link-github.

The actual command implementations live in sibling modules, split by concern:
  identity.py  - login/register/rename/GitHub device-flow binding
  doctor.py    - environment preflight checks
  runloop.py   - the go/resume held-batch-and-menu execution loop
  local_config.py - the shared ~/.dradar/config.json + constants

This file owns only the argparse tree + `main()`, this package's
console-script entry point (see pyproject.toml). Import everything else from
the module that defines it — the single-file-era courtesy re-exports were
dropped once a grep of every known consumer (this repo's tests and the ds0
pipeline scripts) showed nothing reaching through `dradar.cli`.
"""

import argparse
import sys

from . import __version__
from .doctor import cmd_doctor
from .identity import cmd_link_github, cmd_login, cmd_rename, cmd_status
from .runloop import cmd_go, cmd_retry_upload

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dradar", description="DRadar crowdtest client")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="save server + token (or register with --nickname)")
    p_login.add_argument("--server")
    p_login.add_argument("--token")
    p_login.add_argument("--nickname", help="register a new account instead of using a token")
    p_login.add_argument("--tasks-root", help="path to deep-swe/tasks checkout")
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
    p_retry.set_defaults(func=cmd_retry_upload, lease_hint=True)

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
        p.set_defaults(func=cmd_go, resume=is_resume, lease_hint=True)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (KeyboardInterrupt, EOFError):
        # single choke point for every command: Ctrl-C during a run (the
        # batch banner promises it's safe) or EOF from piped/non-tty stdin
        # hitting input() must not dump a raw traceback. 130 = SIGINT.
        # The lease reassurance is only true of the lease-touching commands —
        # cancelling `dradar login`'s device flow must not send a brand-new
        # volunteer chasing `dradar resume` on an unconfigured machine.
        if getattr(args, "lease_hint", False):
            print("\ninterrupted — any held leases stay active; `dradar resume` "
                  "continues where you left off (or the lease expires on its own)")
        else:
            print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
