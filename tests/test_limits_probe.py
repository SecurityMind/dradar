"""Live-quota probe parsing + the probe-first sleep loop's decision logic."""

from dradar import local_config, runloop
from dradar.limits import _parse


def test_parse_extracts_both_windows():
    parsed = _parse({
        "primary": {"usedPercent": 28, "windowDurationMins": 300, "resetsAt": 111},
        "secondary": {"usedPercent": 82, "windowDurationMins": 10080, "resetsAt": 222},
        "planType": "pro",
    })
    assert parsed == {
        "five_hour_used_pct": 28, "five_hour_resets_at": 111,
        "weekly_used_pct": 82, "weekly_resets_at": 222, "plan_type": "pro",
    }


def test_parse_tolerates_missing_secondary():
    parsed = _parse({"primary": {"usedPercent": 5}})
    assert parsed["five_hour_used_pct"] == 5
    assert parsed["weekly_used_pct"] is None


def _with_probe(monkeypatch, reading):
    monkeypatch.setattr(runloop.limits, "read_rate_limits", lambda: reading)


def test_sleep_loop_passes_when_live_window_has_room(monkeypatch):
    _with_probe(monkeypatch, {"five_hour_used_pct": 10, "five_hour_resets_at": None,
                              "weekly_used_pct": 50, "weekly_resets_at": None})
    assert runloop._quota_sleep_loop(20) is True  # 10 + 20x1.5 = 40 <= 100


def test_sleep_loop_stops_at_weekly_wall(monkeypatch, capsys):
    _with_probe(monkeypatch, {"five_hour_used_pct": 0, "five_hour_resets_at": None,
                              "weekly_used_pct": 96, "weekly_resets_at": None})
    assert runloop._quota_sleep_loop(5) is False
    assert "WEEKLY" in capsys.readouterr().out


def test_sleep_loop_skips_impossible_item(monkeypatch):
    _with_probe(monkeypatch, {"five_hour_used_pct": 0, "five_hour_resets_at": None,
                              "weekly_used_pct": 10, "weekly_resets_at": None})
    assert runloop._quota_sleep_loop(70) is False  # 70 x 1.5 > 100, never fits


def test_sleep_loop_falls_back_to_ledger(monkeypatch, tmp_path):
    _with_probe(monkeypatch, None)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    assert runloop._quota_sleep_loop(20) is True  # empty ledger -> fits


def test_sleep_loop_sleeps_until_live_reset(monkeypatch):
    import time as _time
    readings = iter([
        {"five_hour_used_pct": 95, "five_hour_resets_at": _time.time() + 10,
         "weekly_used_pct": 10, "weekly_resets_at": None},
        {"five_hour_used_pct": 3, "five_hour_resets_at": None,
         "weekly_used_pct": 10, "weekly_resets_at": None},
    ])
    monkeypatch.setattr(runloop.limits, "read_rate_limits", lambda: next(readings))
    slept = []
    monkeypatch.setattr(runloop.time, "sleep", lambda s: slept.append(s))
    assert runloop._quota_sleep_loop(20) is True
    assert len(slept) == 1 and slept[0] >= 60  # waited for the reset, then passed
