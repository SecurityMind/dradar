"""Read-only full-board browsing and filtering for ``dradar cells``."""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable

from .api_client import ApiClient, ApiError
from .local_config import _load_config


_TEXT_SORTS = {"task", "model", "effort", "state"}
_SORT_FIELDS = {
    "task": "task_id",
    "model": "model",
    "effort": "effort",
    "state": "st",
    "multiplier": "mult",
    "tests": "total_n",
    "pass-rate": "rate",
    "minutes": "min",
    "cost": "cost",
    "priority": "suggest_priority",
}


def _selected(values: Iterable[str] | None) -> set[str] | None:
    """Accept both repeatable flags and convenient comma-separated values."""
    if not values:
        return None
    selected = {
        item.strip().lower()
        for value in values
        for item in value.split(",")
        if item.strip()
    }
    return selected or None


def _rows(table: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, value in (table.get("cells") or {}).items():
        try:
            task_id, model, effort = key.split("|", 2)
        except ValueError:
            continue
        row = {"task_id": task_id, "model": model, "effort": effort}
        if isinstance(value, dict):
            row.update(value)
        rows.append(row)
    return rows


def _filter_rows(rows: list[dict[str, Any]], args) -> list[dict[str, Any]]:
    models = _selected(args.model)
    efforts = _selected(args.effort)
    states = {"open"} if args.available else _selected(args.state)
    task_query = args.task.lower() if args.task else None

    filtered = []
    for row in rows:
        if models and str(row["model"]).lower() not in models:
            continue
        if efforts and str(row["effort"]).lower() not in efforts:
            continue
        if states and str(row.get("st", "")).lower() not in states:
            continue
        if task_query and task_query not in str(row["task_id"]).lower():
            continue
        mult = row.get("mult")
        tests = row.get("total_n")
        priority = row.get("suggest_priority", 0)
        if args.min_multiplier is not None and (
                mult is None or mult < args.min_multiplier):
            continue
        if args.min_tests is not None and (tests is None or tests < args.min_tests):
            continue
        if args.max_tests is not None and (tests is None or tests > args.max_tests):
            continue
        if args.min_priority is not None and priority < args.min_priority:
            continue
        filtered.append(row)
    return filtered


def _sort_rows(rows: list[dict[str, Any]], field: str, reverse: bool) -> None:
    key = _SORT_FIELDS[field]
    populated = [row for row in rows if row.get(key) is not None]
    missing = [row for row in rows if row.get(key) is None]
    # Natural order: names A-Z, numeric signals highest-first. --reverse flips
    # that useful default.  Keep missing measurements at the end either way.
    descending = field not in _TEXT_SORTS
    if reverse:
        descending = not descending
    # Sort ties predictably A-Z even when the primary numeric field is
    # descending; reversing one compound tuple would reverse task IDs too.
    populated.sort(key=lambda row: (row["task_id"], row["model"], row["effort"]))
    populated.sort(
        key=lambda row: str(row[key]).lower() if field in _TEXT_SORTS else row[key],
        reverse=descending,
    )
    rows[:] = populated + missing


def _fmt_number(value: Any, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    else:
        rendered = str(value)
    return rendered + suffix


def _print_rows(rows: list[dict[str, Any]]) -> None:
    print(f"{'STATE':8s} {'TASK':38s} {'MODEL':16s} {'EFFORT':7s} "
          f"{'MULT':>6s} {'PRI':>4s} {'TESTS':>5s} {'PASS':>6s} "
          f"{'MIN':>5s} {'COST':>7s}")
    for row in rows:
        task = str(row["task_id"])
        if len(task) > 38:
            task = task[:35] + "..."
        rate = row.get("rate")
        pass_rate = "-" if rate is None else f"{rate * 100:.0f}%"
        print(
            f"{str(row.get('st', '?')):8.8s} {task:38s} "
            f"{str(row['model']):16.16s} {str(row['effort']):7.7s} "
            f"{_fmt_number(row.get('mult'), 'x'):>6s} "
            f"{_fmt_number(row.get('suggest_priority', 0)):>4s} "
            f"{_fmt_number(row.get('total_n')):>5s} {pass_rate:>6s} "
            f"{_fmt_number(row.get('min')):>5s} "
            f"{('-' if row.get('cost') is None else '$' + _fmt_number(row['cost'])):>7s}"
        )


def cmd_cells(args) -> int:
    """Fetch, filter and display the public board without claiming cells."""
    cfg = _load_config()
    if not cfg.get("server"):
        sys.exit("not configured — run: dradar login --server <url>")
    client = ApiClient(cfg["server"], cfg.get("token", ""))
    try:
        table = client.table()
    except ApiError as exc:
        if exc.status_code == 404:
            sys.exit("this server doesn't support `dradar cells` yet")
        sys.exit(f"could not load cells: {exc}")

    all_rows = _rows(table)
    rows = _filter_rows(all_rows, args)
    _sort_rows(rows, args.sort, args.reverse)
    matched = len(rows)
    shown = rows if args.all else rows[:args.limit]

    if args.json:
        print(json.dumps({
            "total": len(all_rows),
            "matched": matched,
            "shown": len(shown),
            "cells": shown,
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"{len(all_rows)} total cells; {matched} matched; showing {len(shown)}")
    if shown:
        _print_rows(shown)
    else:
        print("no cells match these filters")
    if not args.all and matched > len(shown):
        print(f"... {matched - len(shown)} more; use --all or --limit N")
    print("read-only snapshot — a cell can be claimed by someone else before you pick it")
    return 0


__all__ = ["cmd_cells"]
