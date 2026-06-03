from __future__ import annotations

from pathlib import Path

from bot.intraday_pipeline import load_intraday_by_date
from bot.next_day_watch_pipeline import load_next_day_by_asof, load_next_day_by_date
from bot.stock_db import LlmDailyReportRow, StockDB


def test_load_intraday_by_date_reads_db(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="intraday",
        report_date="2026-06-02",
        asof="2026-06-02",
        market_tone="bullish",
        brief_md="brief",
        payload_json='{"asof":"2026-06-02","market_tone":"bullish"}',
    ))

    data = load_intraday_by_date(tmp_path, "2026-06-02")

    assert data is not None
    assert data["asof"] == "2026-06-02"
    assert data["market_tone"] == "bullish"
    assert data["brief_md"] == "brief"


def test_load_next_day_by_asof_prefers_update(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="next_day_watch",
        report_date="2026-06-03",
        mode="draft",
        asof="2026-06-02",
        payload_json='{"asof":"2026-06-02","target_date":"2026-06-03","mode":"draft"}',
    ))
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="next_day_watch",
        report_date="2026-06-03",
        mode="update",
        asof="2026-06-02",
        payload_json='{"asof":"2026-06-02","target_date":"2026-06-03","mode":"update"}',
    ))

    data = load_next_day_by_asof(tmp_path, "2026-06-02")
    draft = load_next_day_by_asof(tmp_path, "2026-06-02", mode="draft")

    assert data is not None
    assert data["mode"] == "update"
    assert draft is not None
    assert draft["mode"] == "draft"


def test_load_next_day_by_date_reads_target_date(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="next_day_watch",
        report_date="2026-06-03",
        mode="draft",
        asof="2026-06-02",
        payload_json='{"asof":"2026-06-02","target_date":"2026-06-03","mode":"draft"}',
    ))

    data = load_next_day_by_date(tmp_path, "2026-06-03")

    assert data is not None
    assert data["target_date"] == "2026-06-03"
