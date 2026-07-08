"""Quota auto-sleep: exact wake-up times from the rolling-window ledger."""

from pathlib import Path

from dradar import quota


def test_fits_now_returns_zero(tmp_path: Path):
    assert quota.seconds_until_fits(tmp_path, 20, now=1000.0) == 0.0


def test_sleep_until_oldest_entry_expires(tmp_path: Path):
    now = 100_000.0
    # two runs fill the window: 60% four hours ago, 30% one hour ago
    quota.record_run(tmp_path, 60, now=now - 4 * 3600)
    quota.record_run(tmp_path, 30, now=now - 1 * 3600)
    # a 20% run needs 30% headroom (x1.5): 90 + 30 > 100 -> must wait for the
    # 60% entry to leave the 5h window, i.e. 1h from now (+60s cushion)
    wait = quota.seconds_until_fits(tmp_path, 20, now=now)
    assert wait == 3600.0 + 60.0
    # after sleeping, it fits
    assert quota.seconds_until_fits(tmp_path, 20, now=now + wait) == 0.0


def test_never_fits_flags_skip(tmp_path: Path):
    # 80% x 1.5 margin = 120% of a window: impossible even when idle
    assert quota.seconds_until_fits(tmp_path, 80, now=1000.0) == -1.0


def test_none_estimate_never_blocks(tmp_path: Path):
    assert quota.seconds_until_fits(tmp_path, None) == 0.0
