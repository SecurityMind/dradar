import argparse
import json

from dradar import cells


TABLE = {
    "cells": {
        "alpha-task|gpt-a|low": {
            "st": "open", "mult": 1.2, "total_n": 3, "rate": 0.5,
            "min": 10, "cost": 1.5,
        },
        "beta-task|gpt-a|high": {
            "st": "cooldown", "mult": 3.0, "total_n": 8, "rate": 1.0,
            "min": 20, "cost": 2.5, "suggest_priority": 100,
        },
        "gamma-task|gpt-b|high": {
            "st": "open", "mult": 2.5, "total_n": 1, "rate": None,
            "min": None, "cost": None,
        },
        "delta-task|gpt-b|medium": {
            "st": "leased", "total_n": 0, "rate": None,
        },
    }
}


class FakeClient:
    def __init__(self, server, token):
        self.server = server
        self.token = token

    def table(self):
        return TABLE


def _args(**overrides):
    values = dict(
        model=None, effort=None, available=False, state=None, task=None,
        min_multiplier=None, min_tests=None, max_tests=None, min_priority=None,
        sort="multiplier", reverse=False, limit=20, all=False, json=True,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _run(monkeypatch, capsys, **overrides):
    monkeypatch.setattr(cells, "_load_config", lambda: {
        "server": "https://api.example.com"
    })
    monkeypatch.setattr(cells, "ApiClient", FakeClient)
    assert cells.cmd_cells(_args(**overrides)) == 0
    return json.loads(capsys.readouterr().out)


def test_cells_filters_model_effort_and_availability(monkeypatch, capsys):
    result = _run(
        monkeypatch, capsys, model=["GPT-B"], effort=["high,ultra"],
        available=True,
    )
    assert result["total"] == 4
    assert result["matched"] == 1
    assert result["cells"][0]["task_id"] == "gamma-task"


def test_cells_numeric_filters_and_multiplier_sort(monkeypatch, capsys):
    result = _run(
        monkeypatch, capsys, min_multiplier=1.1, max_tests=3,
        sort="multiplier",
    )
    assert [row["task_id"] for row in result["cells"]] == [
        "gamma-task", "alpha-task",
    ]


def test_cells_missing_sort_values_stay_last_when_reversed(monkeypatch, capsys):
    result = _run(monkeypatch, capsys, sort="minutes", reverse=True)
    assert [row["task_id"] for row in result["cells"]] == [
        "alpha-task", "beta-task", "gamma-task", "delta-task",
    ]


def test_cells_limit_reports_full_match_count(monkeypatch, capsys):
    result = _run(monkeypatch, capsys, sort="task", limit=2)
    assert result["matched"] == 4
    assert result["shown"] == 2
    assert [row["task_id"] for row in result["cells"]] == [
        "alpha-task", "beta-task",
    ]
