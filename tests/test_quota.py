from dradar import quota


def test_no_estimate_never_blocks(tmp_path):
    ok, msg = quota.check(tmp_path, None)
    assert ok and msg == ""


def test_empty_ledger_allows(tmp_path):
    ok, msg = quota.check(tmp_path, 20, now=1000.0)
    assert ok and "OK to proceed" in msg


def test_accumulated_consumption_blocks(tmp_path):
    now = 100000.0
    # three 30%-window runs just now => 90% consumed
    for _ in range(3):
        quota.record_run(tmp_path, 30, now=now)
    assert quota.window_consumed_pct(tmp_path, now=now) == 90.0
    # a 20% task: 90 + 20*1.5 = 120 > 100 => blocked
    ok, msg = quota.check(tmp_path, 20, now=now)
    assert not ok and "quota guard" in msg


def test_old_entries_age_out_of_window(tmp_path):
    now = 100000.0
    quota.record_run(tmp_path, 90, now=now - quota.WINDOW_SEC - 1)  # older than 5h
    quota.record_run(tmp_path, 10, now=now)
    assert quota.window_consumed_pct(tmp_path, now=now) == 10.0
    ok, _ = quota.check(tmp_path, 30, now=now)  # 10 + 45 = 55 <= 100
    assert ok


def test_record_run_ignores_none(tmp_path):
    quota.record_run(tmp_path, None, now=1.0)
    assert quota.window_consumed_pct(tmp_path, now=1.0) == 0.0
