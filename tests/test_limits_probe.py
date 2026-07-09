"""Live-quota probe response parsing (used for price-tag calibration data,
not for gating — quota decisions are the volunteer's own to make)."""

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
