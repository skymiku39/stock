from __future__ import annotations

import datetime as dt
from pathlib import Path

from bot.kline_backfill import compare_snapshots, coverage_report, month_gaps, run
from bot.stock_db import PriceBar, StockDB, default_db_path


def test_month_gaps():
    assert month_gaps(["2020-01-02", "2020-04-01"], dt.date(2020, 4, 30)) == ["2020-02", "2020-03"]
    assert month_gaps([], dt.date(2020, 4, 30)) == []


def test_compare_detects_change_and_loss():
    a = {("1", "d1"): (1, 1, 1, 1, 1), ("1", "d2"): (2, 2, 2, 2, 2)}
    assert compare_snapshots(a, dict(a))["unchanged"]
    b = {("1", "d1"): (9, 1, 1, 1, 1)}
    res = compare_snapshots(a, b)
    assert res["changed"] == [["1", "d1"]] and res["missing_after"] == [["1", "d2"]]


def test_run_keeps_existing_and_reports_gaps(tmp_path: Path):
    db = StockDB.open(path=default_db_path(tmp_path))
    db.bulk_upsert_price_bars([PriceBar(symbol="1101", date="2020-01-02", open=1, high=2, low=1, close=2, volume=5)])
    db.close()

    def fake_fetch(sym, *, start_date, end_date, root, request_delay_sec, direction):
        d = StockDB.open(path=default_db_path(root))
        d.bulk_upsert_price_bars([PriceBar(symbol=sym, date="2020-03-02", open=1, high=2, low=1, close=2, volume=5)])
        d.close()

    report = run(tmp_path, ["1101", "9999"], start=dt.date(2020, 1, 1),
                 end=dt.date(2020, 3, 31), delay=0, fetch=fake_fetch)
    assert report["integrity"]["unchanged"]
    cov = {c["symbol"]: c for c in report["coverage"]}
    assert cov["1101"]["missing_months"] == ["2020-02"]
    assert cov["9999"]["rows"] == 1 and cov["9999"]["starts_after_range_start"]
