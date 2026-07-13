"""The run loop: `dradar go` / `dradar resume`.

Runs the volunteer's held batch of cells serially — free-pick instances let
them claim up to a handful at once on the web, and the CLI works through
them one at a time. Menu-mode instances (no web claim) still claim a single
task from the menu. Quota is the volunteer's own to manage — dradar shows
the server's per-task estimate and lets them decide whether to proceed; if a
run doesn't finish before its lease expires, the cell just reopens for
someone else with nothing counted. Split out of cli.py to separate this from
identity (login/register) and doctor (environment checks) concerns.
"""

import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__, pending
from .api_client import ApiClient, ApiError
from .identity import _client
from .local_config import HOME, _load_config
from .runner import (
    RunnerError, check_task_content_hash, ensure_pier, ensure_tasks_root,
    local_deep_swe_commit, run_trial, summarize_result, sync_deep_swe_commit,
    trial_artifact_paths,
)
from .scrub import scan_secrets, scrub_file


def _print_assignment(a: dict) -> None:
    print(f"assignment {a['assignment_id']}: {a['task_id']}")
    print(f"  model={a['model']} effort={a['effort']} agent={a['agent']}")
    if a.get("est_minutes"):
        # Denominated in the weekly window: Codex removed the 5h rolling
        # limit (2026-07), the 7d quota is the only constraint left.
        print(f"  estimated: ~{a['est_minutes']} min, "
              f"~{a.get('est_quota_pct', '?')}% of a weekly (7d) quota window")
    print(f"  lease expires: {a['expires_at']}")


def _print_menu(menu: list[dict]) -> None:
    for i, m in enumerate(menu, 1):
        est = f"~{m['est_minutes']} min, ~{m.get('est_quota_pct', '?')}%" if m.get("est_minutes") else "?"
        print(f"  {i}. {m['task_id']}  model={m['model']} effort={m['effort']}  est={est}")


def _choose_menu_entry(menu: list[dict], yes: bool) -> dict:
    """Pick an entry from a non-empty menu. Non-interactive (-y) always takes
    the first (hungriest) pick with zero prompting, to keep automation stable.
    Empty input takes the top pick. Invalid input gets one announced re-prompt
    (the claim leases the cell immediately, so a silent fallback would point
    the volunteer's quota at a task they never chose), then falls back to the
    top pick so garbage-piping automation still terminates."""
    if yes:
        return menu[0]
    _print_menu(menu)
    for attempt in range(2):
        raw = input(f"pick a task 1 to {len(menu)}, or press enter for the top pick: ").strip()
        if not raw:
            return menu[0]
        try:
            idx = int(raw)
        except ValueError:
            idx = 0
        if 1 <= idx <= len(menu):
            return menu[idx - 1]
        if attempt == 0:
            print(f"invalid choice '{raw}'")
    print(f"taking the top pick ({menu[0]['task_id']})")
    return menu[0]


def _claim_from_menu(client: ApiClient, menu: list[dict], yes: bool) -> dict | None:
    """Claim a menu entry, retrying once with a fresh menu if it went stale.
    Returns the claimed assignment (or an already-held one, when a 409 meant
    "you already hold an active lease" and get_assignment self-heals), or
    None when no work is available."""
    for attempt in range(2):
        choice = _choose_menu_entry(menu, yes)
        try:
            data = client.claim_assignment(choice["task_id"], choice["model"], choice["effort"])
            return data.get("assignment")
        except ApiError as exc:
            if exc.status_code != 409:
                raise
            if attempt == 1:
                print("no work available right now — thank you, check back later")
                return None
            print(f"that cell went stale ({exc}); fetching a fresh menu...")
            retry = client.get_assignment()
            menu = retry.get("menu")
            if not menu:
                return retry.get("assignment")
    return None


def _exit_for(exc: ApiError) -> None:
    """Exit on a dead-end ApiError in the run flow with a next step, not just
    the raw server error. 401 means the token was reset/clobbered — recoverable
    without support. status_code None means the request never reached the
    server (DNS/connect/timeout), so any held leases are untouched. Everything
    else (e.g. 403 account suspended) carries the server's own explanation
    verbatim."""
    if exc.status_code == 401:
        sys.exit(f"{exc}\nyour token was rejected — `dradar login --github` recovers a "
                 "linked identity, otherwise grab a fresh token on the radar page")
    if exc.status_code is None:
        sys.exit(f"{exc}\ncheck your connection — held leases stay active, and "
                 "`dradar resume` continues where you left off")
    sys.exit(str(exc))


def _check_version_pin(pinned: str | None, tasks_root: Path, allow_drift: bool) -> str | None:
    """Refuse to burn real quota on a checkout the server won't grade the same
    way. The lease stays active across the exit."""
    local_commit = local_deep_swe_commit(tasks_root)
    if pinned and local_commit and local_commit != pinned:
        # Self-heal: fetch + checkout the exact commit the server grades against,
        # rather than making the volunteer do it by hand.
        print(f"deep-swe drifted (local {local_commit[:12]} != server {pinned[:12]}); "
              "syncing to the server's pinned commit...")
        if sync_deep_swe_commit(tasks_root, pinned):
            print(f"  synced to {pinned[:12]}")
            return pinned
        fix = (
            f"  git -C {tasks_root} fetch --depth 1 origin {pinned}\n"
            f"  git -C {tasks_root} checkout {pinned}"
        )
        if not allow_drift:
            sys.exit(
                "couldn't auto-sync your deep-swe checkout to the version this "
                f"server grades against:\n  local:  {local_commit}\n  server: {pinned}\n"
                f"do it by hand, then re-run (the lease stays active):\n{fix}\n"
                "or re-run with --allow-task-drift to proceed anyway (the "
                "submission will be flagged for review)"
            )
        print(
            f"warning: proceeding with task drift (local {local_commit[:12]} != "
            f"server {pinned[:12]}); the submission will be flagged for review"
        )
    return local_commit


# The trial-dir artifact layout is owned by runner (pier writes it); retry
# reconstructs paths from a bare trial_dir via the same single source of truth.
_artifacts_from_trial_dir = trial_artifact_paths


def _upload_trial(client: ApiClient, entry: dict) -> str:
    """Scrub + upload one trial's artifacts, described by a pending-ledger
    entry dict (assignment_id/nonce/task_id/trial_dir/meta/outcome/job_dir/
    keep) — the same shape the ledger round-trips, so what persists on failure
    is identical by construction to what was attempted. Shared by the normal
    post-run path and by `dradar retry-upload` (which passes loaded ledger
    entries straight through). Never exits — returns an outcome tag so
    callers (the held-batch loop, a retry scan) can carry on with the next
    item.

    The entry is recorded in the local pending-upload ledger BEFORE the
    submit attempt, so a process death mid-upload (Ctrl-C/kill/OOM during a
    large multipart POST) can't orphan a completed, quota-burning trial.
    Every exit settles it: success/409/410 remove the entry, anything else
    keeps it for retry. The raw trial_dir is never touched by scrubbing
    (which writes to a fresh tempdir), so a later retry re-scrubs from the
    same untouched originals."""
    assignment_id = entry["assignment_id"]
    task_id = entry.get("task_id", "?")
    outcome = entry.get("outcome", "completed")
    job_dir = Path(entry["job_dir"]) if entry.get("job_dir") else None

    patch, trajectory, result = trial_artifact_paths(Path(entry["trial_dir"]))
    if not patch.is_file():
        print(f"  {task_id}: local artifacts are gone, giving up on this one")
        pending.remove(HOME, assignment_id)
        return "artifacts-gone"

    leaked = scan_secrets(patch.read_bytes())
    if leaked:
        print(f"patch contains secret-shaped content ({', '.join(sorted(set(leaked)))}); "
              f"not uploaded. Inspect {patch} and scrub before resubmitting.")
        # Not retryable as-is (the patch itself needs manual attention) —
        # don't leave a ledger entry that would just fail the same way forever.
        pending.remove(HOME, assignment_id)
        return "not-uploaded"

    with tempfile.TemporaryDirectory() as td:
        scrubbed = Path(td)
        traj_scrubbed = None
        if trajectory:
            traj_scrubbed = scrubbed / "trajectory.json"
            scrub_file(trajectory, traj_scrubbed)
        result_scrubbed = None
        if result:
            result_scrubbed = scrubbed / "result.json"
            scrub_file(result, result_scrubbed)
        # Record before submitting: from here on an unacked completed trial
        # always has a ledger entry, whatever kills the upload. The server
        # dedupes replays (409 "already submitted"), so duplicates are safe.
        pending.record(HOME, entry)
        try:
            ack = client.submit(assignment_id, entry["nonce"], patch, traj_scrubbed,
                                result_scrubbed, entry["meta"], outcome=outcome)
        except ApiError as exc:
            if exc.status_code == 409:
                # Some earlier attempt actually landed server-side even
                # though THIS process never saw the response — good news.
                print(f"  {task_id}: already submitted (an earlier attempt landed) — clearing it")
                pending.remove(HOME, assignment_id)
                return "submitted"
            if exc.status_code == 410:
                print(f"  {task_id}: lease expired, unsalvageable — the cell reopened "
                      "for someone else, dropping it")
                pending.remove(HOME, assignment_id)
                return "expired"
            if exc.status_code in (404, 413, 422):
                # Definitively rejected: the exact same bytes can never
                # succeed (payload too large / unprocessable / assignment
                # unknown), so an entry here would just fail identically on
                # every future retry. 403 deliberately NOT in this list — it
                # covers both a permanent nonce mismatch and a suspension
                # that may be lifted, and dropping a suspended volunteer's
                # completed trial would destroy recoverable work.
                print(f"  {task_id}: the server rejected this upload for good ({exc}) — "
                      f"retrying can't fix it, dropping it from the retry queue "
                      f"(the local files stay in {patch.parent.parent})")
                pending.remove(HOME, assignment_id)
                return "rejected"
            print(f"  {task_id}: upload failed ({exc}) — kept for retry "
                  "(`dradar retry-upload`)")
            return "upload-failed"

    pending.remove(HOME, assignment_id)
    if ack.get("grade_status") == "invalid":
        print(f"recorded as interrupted (not graded): {ack['submission_id']} — "
              "the cell stays open for a fresh run once your quota resets")
    else:
        print(f"submitted: {ack['submission_id']} (grading happens server-side)")
    if job_dir and not entry.get("keep", False):
        shutil.rmtree(job_dir, ignore_errors=True)
    return "interrupted" if outcome == "interrupted" else "submitted"


def _run_and_submit(client: ApiClient, assignment: dict, tasks_root: Path,
                    args, local_commit: str | None) -> str:
    """Run one assignment and upload the artifacts. Returns an outcome tag —
    never exits, so the held-batch loop can carry on with the next item."""
    hash_match = check_task_content_hash(assignment, tasks_root)
    work_dir = HOME / "work"
    print("running trial (this can take a while)...")
    try:
        art = run_trial(
            assignment, tasks_root, work_dir, dev_agent=args.dev_agent,
            on_started=lambda: client.mark_started(assignment["assignment_id"]))
    except RunnerError as exc:
        print(f"trial failed: {exc}")
        return "failed"

    stats = summarize_result(art.result)
    # An interrupted/failed run (nonzero pier rc or recorded exception) is not a
    # model failure: report it so the server marks it invalid, not graded 0.
    interrupted = art.returncode != 0 or stats.get("exception_info")
    outcome = "interrupted" if interrupted else "completed"
    print(f"trial finished in {art.duration_sec/60:.1f} min (pier rc={art.returncode}, "
          f"outcome={outcome}); uploading...")

    meta = {
        "dradar_version": __version__,
        "duration_sec": round(art.duration_sec, 1),
        "pier_returncode": art.returncode,
        "dev_agent": args.dev_agent,
        "task_content_hash_match": hash_match,
        "deep_swe_commit": local_commit,
        **stats,
    }

    return _upload_trial(client, {
        "assignment_id": assignment["assignment_id"], "nonce": assignment["nonce"],
        "task_id": assignment["task_id"], "trial_dir": str(art.trial_dir),
        "meta": meta, "outcome": outcome,
        "job_dir": str(art.job_dir) if art.job_dir else None, "keep": args.keep,
    })


def _retry_pending_uploads(client: ApiClient) -> None:
    """Auto-heal at the top of every `dradar go`/`resume`: flush anything a
    previous run couldn't upload before doing anything else. Silent no-op
    when the ledger is empty — this must never surprise a volunteer who has
    nothing pending."""
    entries = pending.load(HOME)
    if not entries:
        return
    print(f"retrying {len(entries)} upload(s) left over from a previous run...")
    for e in entries:
        # _upload_trial handles the gone-artifacts case (drops the entry).
        _upload_trial(client, e)
    print()


def cmd_retry_upload(args) -> int:
    """Standalone entry point: flush the pending-upload ledger without
    grabbing any new work (e.g. you're back online and just want to clear
    the backlog before deciding whether to run more)."""
    cfg = _load_config()
    client = _client(cfg)
    entries = pending.load(HOME)
    if not entries:
        print("nothing pending — every trial you've run has been uploaded")
        return 0
    _retry_pending_uploads(client)
    remaining = pending.load(HOME)
    if remaining:
        print(f"{len(remaining)} still pending (will retry again on the next "
              "`dradar go`/`retry-upload`)")
        return 1
    print("all clear")
    return 0


def cmd_go(args) -> int:
    cfg = _load_config()
    client = _client(cfg, auto_register=True)
    tasks_root = cfg.get("tasks_root")
    if not tasks_root:
        sys.exit("tasks_root not configured; run: dradar login --tasks-root <deep-swe/tasks>")
    tasks_root = Path(tasks_root).expanduser()

    # Self-bootstrap the environment so a fresh machine needs far less manual
    # setup: clone the (public) task repo if it's missing, install pier if it's
    # missing. Docker + codex login still can't be auto-installed (privileges /
    # credentials) -- `dradar doctor` guides those.
    try:
        ensure_tasks_root(tasks_root)
        ensure_pier()
    except RunnerError as exc:
        sys.exit(str(exc))

    # Self-heal before anything else: a trial from a previous run that ran
    # but failed to upload must not just sit on disk forever.
    _retry_pending_uploads(client)

    return _go_menu(args, cfg, client, tasks_root)


def _go_menu(args, cfg: dict, client: ApiClient, tasks_root: Path) -> int:
    """Run the volunteer's held batch of cells serially. On a free-pick
    instance the batch is whatever they claimed on the web (up to the server's
    concurrent cap); on a menu-mode instance it's a single cell claimed from
    the menu here. Bundle (multi-task auto-packing) dispatch was retired
    server-side; this is the only flow now."""
    resume = getattr(args, "resume", False)
    try:
        data = client.get_assignment()
    except ApiError as exc:
        _exit_for(exc)
    # New server returns the whole held batch as `active`; fall back to the
    # older single-`assignment` shape so an older server still works.
    active = data.get("active")
    if active is None:
        one = data.get("assignment")
        active = [one] if one else []
    free_pick = data.get("free_pick", False)
    menu = data.get("menu")

    # Menu mode (non-free-pick, e.g. claude): nothing held -> claim one now.
    if not active and not free_pick and menu:
        try:
            one = _claim_from_menu(client, menu, args.yes)
        except ApiError as exc:
            _exit_for(exc)
        active = [one] if one else []

    if not active:
        if free_pick:
            print("no cells claimed — pick some on the radar page, then paste the "
                  "command it gives you (or run `dradar go` again after claiming).")
        elif resume:
            print("nothing to resume — no active lease (it may have expired). Run `dradar go`.")
        else:
            print("no work available right now — thank you, check back later")
        return 0

    # One local checkout serves the whole batch, so the version pin only needs
    # checking once (it sys.exit's on a mismatch unless --allow-task-drift).
    local_commit = _check_version_pin(active[0].get("deep_swe_commit"), tasks_root,
                                      args.allow_task_drift)

    n = len(active)
    if n > 1:
        print(f"you're holding {n} cells — running them one at a time "
              "(Ctrl-C anytime; unrun cells auto-release):")
    results = []
    for i, assignment in enumerate(active, 1):
        if n > 1:
            print(f"\n=== cell {i}/{n} ===")
        _print_assignment(assignment)
        if not args.dev_agent and assignment.get("est_quota_pct"):
            print("  it's your call whether you have room for this — dradar doesn't track "
                  "your subscription usage. If you don't finish before the lease expires, "
                  "the cell just reopens for someone else and nothing is counted.")
        if not args.yes:
            prompt = "run it now? [y/N]" + (" (or 's' to skip this one)" if n > 1 else "") + " "
            answer = input(prompt).strip().lower()
            if n > 1 and answer == "s":
                print("skipped (its lease stays active; `dradar resume` to come back to it)")
                continue
            if answer != "y":
                print("aborted (any remaining leases stay active; `dradar resume` to continue)")
                return 1
        results.append(_run_and_submit(client, assignment, tasks_root, args, local_commit))
    ok = all(o in ("submitted", "interrupted") for o in results)
    return 0 if ok else 1


__all__ = ["cmd_go", "_go_menu",
           "_run_and_submit", "_check_version_pin", "_claim_from_menu",
           "_choose_menu_entry", "_print_menu", "_print_assignment",
           "cmd_retry_upload", "_retry_pending_uploads", "_upload_trial",
           "_artifacts_from_trial_dir"]
