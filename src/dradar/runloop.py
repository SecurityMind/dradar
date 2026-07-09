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
)
from .scrub import scan_secrets, scrub_file


def _print_assignment(a: dict) -> None:
    print(f"assignment {a['assignment_id']}: {a['task_id']}")
    print(f"  model={a['model']} effort={a['effort']} agent={a['agent']}")
    if a.get("est_minutes"):
        print(f"  estimated: ~{a['est_minutes']} min, ~{a.get('est_quota_pct', '?')}% of a 5h window")
    print(f"  lease expires: {a['expires_at']}")


def _print_menu(menu: list[dict]) -> None:
    for i, m in enumerate(menu, 1):
        est = f"~{m['est_minutes']} min, ~{m.get('est_quota_pct', '?')}%" if m.get("est_minutes") else "?"
        print(f"  {i}. {m['task_id']}  model={m['model']} effort={m['effort']}  est={est}")


def _choose_menu_entry(menu: list[dict], yes: bool) -> dict:
    """Pick an entry from a non-empty menu. Non-interactive (-y) always takes
    the first (hungriest) pick with zero prompting, to keep automation stable."""
    if yes:
        return menu[0]
    _print_menu(menu)
    raw = input(f"pick a task 1 to {len(menu)}, or press enter for the top pick: ").strip()
    if not raw:
        return menu[0]
    try:
        idx = int(raw)
    except ValueError:
        return menu[0]
    if idx < 1 or idx > len(menu):
        return menu[0]
    return menu[idx - 1]


def _claim_from_menu(client: ApiClient, menu: list[dict], yes: bool) -> dict:
    """Claim a menu entry, retrying once with a fresh menu if it went stale.
    Returns {assignment: dict|None, resumed: bool} — 'resumed' is True when a
    409 turned out to mean "you already hold an active lease" (self-heal via
    get_assignment) rather than "the cell filled up", so the caller can tell
    a pre-existing lease apart from a fresh claim."""
    for attempt in range(2):
        choice = _choose_menu_entry(menu, yes)
        try:
            data = client.claim_assignment(choice["task_id"], choice["model"], choice["effort"])
            return {"assignment": data.get("assignment"), "resumed": False}
        except ApiError as exc:
            if exc.status_code != 409:
                raise
            if attempt == 1:
                print("no work available right now — thank you, check back later")
                return {"assignment": None, "resumed": False}
            print(f"that cell went stale ({exc}); fetching a fresh menu...")
            retry = client.get_assignment()
            menu = retry.get("menu")
            if not menu:
                return {"assignment": retry.get("assignment"), "resumed": retry.get("resumed", False)}
    return {"assignment": None, "resumed": False}


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


def _artifacts_from_trial_dir(trial_dir: Path) -> tuple[Path, Path | None, Path | None]:
    """Reconstruct (patch, trajectory, result) paths from a trial_dir, mirroring
    runner.run_trial's layout — used by retry, where no live TrialArtifacts
    object exists (the process that ran the trial already exited)."""
    patch = trial_dir / "artifacts" / "model.patch"
    trajectory = trial_dir / "agent" / "trajectory.json"
    result = trial_dir / "result.json"
    return patch, (trajectory if trajectory.is_file() else None), (result if result.is_file() else None)


def _upload_trial(client: ApiClient, assignment_id: str, nonce: str, task_id: str,
                  patch: Path, trajectory: Path | None, result: Path | None,
                  meta: dict, outcome: str, job_dir: Path | None, keep: bool) -> str:
    """Scrub + upload one trial's artifacts. Shared by the normal post-run
    path and by `dradar retry-upload` (which reconstructs the same arguments
    from the pending-upload ledger instead of a fresh TrialArtifacts).
    Never exits — returns an outcome tag so callers (a bundle loop, a retry
    scan) can carry on with the next item.

    On upload failure the caller's entry stays (or is freshly added) in the
    local pending-upload ledger — the raw trial_dir is never touched by
    scrubbing (which writes to a fresh tempdir), so a later retry re-scrubs
    from the same untouched originals."""
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
        try:
            ack = client.submit(assignment_id, nonce, patch, traj_scrubbed,
                                result_scrubbed, meta, outcome=outcome)
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
            print(f"  {task_id}: upload failed ({exc}) — kept for retry "
                  "(`dradar retry-upload`)")
            pending.record(HOME, {
                "assignment_id": assignment_id, "nonce": nonce, "task_id": task_id,
                "trial_dir": str(patch.parent.parent), "meta": meta, "outcome": outcome,
                "job_dir": str(job_dir) if job_dir else None, "keep": keep,
            })
            return "upload-failed"

    pending.remove(HOME, assignment_id)
    if ack.get("grade_status") == "invalid":
        print(f"recorded as interrupted (not graded): {ack['submission_id']} — "
              "the cell stays open for a fresh run once your quota resets")
    else:
        print(f"submitted: {ack['submission_id']} (grading happens server-side)")
    if job_dir and not keep:
        shutil.rmtree(job_dir, ignore_errors=True)
    return "interrupted" if outcome == "interrupted" else "submitted"


def _run_and_submit(client: ApiClient, assignment: dict, tasks_root: Path,
                    args, local_commit: str | None) -> str:
    """Run one assignment and upload the artifacts. Returns an outcome tag —
    never exits, so a bundle loop can carry on with the next item."""
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

    return _upload_trial(
        client, assignment["assignment_id"], assignment["nonce"], assignment["task_id"],
        art.patch, art.trajectory, art.result, meta, outcome, art.job_dir, args.keep,
    )


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
        patch, trajectory, result = _artifacts_from_trial_dir(Path(e["trial_dir"]))
        if not patch.is_file():
            print(f"  {e.get('task_id', '?')}: local artifacts are gone, giving up on this one")
            pending.remove(HOME, e["assignment_id"])
            continue
        _upload_trial(
            client, e["assignment_id"], e["nonce"], e.get("task_id", "?"),
            patch, trajectory, result, e["meta"], e.get("outcome", "completed"),
            Path(e["job_dir"]) if e.get("job_dir") else None, keep=e.get("keep", False),
        )
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
        sys.exit(str(exc))
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
            claimed = _claim_from_menu(client, menu, args.yes)
        except ApiError as exc:
            sys.exit(str(exc))
        one = claimed.get("assignment")
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
