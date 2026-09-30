from __future__ import annotations

import json
from pathlib import Path

from bot.fundamentals_research_batch import materialize_all_fundamentals
from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB


def _register(root: Path, source: str, rows: list[dict]) -> None:
    db = StockDB.open(root=root)
    try:
        registry = SourceRegistry(root, db=db)
        registry.register_document(
            source,
            json.dumps(rows, ensure_ascii=False).encode("utf-8"),
            kind="http_response",
            url=f"https://example.test/{source}",
            retrieved_at="2026-09-20T08:00:00+08:00",
            quality_status="ok",
            suffix="json",
        )
    finally:
        db.close()


def test_materialize_all_fundamentals_builds_exact_chains(tmp_path: Path) -> None:
    _register(
        tmp_path,
        "monthly_revenue_twse",
        [{
            "公司代號": "2330",
            "公司名稱": "台積電",
            "資料年月": "11508",
            "營業收入-當月營收": "1000",
            "營業收入-去年同月營收": "800",
            "營業收入-去年同月增減(%)": "25",
            "營業收入-上月比較增減(%)": "5",
            "營業收入-當月累計營收": "7000",
            "營業收入-前期比較增減(%)": "20",
        }],
    )
    _register(
        tmp_path,
        "valuation_twse",
        [{
            "Code": "2330",
            "Name": "台積電",
            "Date": "20260918",
            "PEratio": "20",
            "PBratio": "5",
            "DividendYield": "1.2",
            "FinancialYear": "2025",
        }],
    )
    _register(
        tmp_path,
        "quarterly_financials_twse_ci",
        [{
            "公司代號": "2330",
            "年度": "115",
            "季別": "2",
            "營業收入": "2000",
            "營業毛利": "1000",
            "營業利益": "800",
            "本期淨利": "700",
            "每股盈餘": "10",
        }],
    )

    first = materialize_all_fundamentals(tmp_path, limit_per_domain=1)
    assert first["failed"] == 0
    assert first["attempted"] == 3

    db = StockDB.open(root=tmp_path)
    try:
        assert db.get_monthly_revenue("2330", "2026-08")
        assert db.get_valuation("2330", "2026-09-18")
        assert db.get_quarterly_report("2330", "2026Q2")
        assert db.conn.execute(
            "SELECT COUNT(*) FROM evidence_items WHERE source_document_id != ''"
        ).fetchone()[0] == 3
        assert db.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] >= 3
    finally:
        db.close()

    second = materialize_all_fundamentals(tmp_path, limit_per_domain=1)
    assert second["failed"] == 0
    assert second["attempted"] == 3


def test_materialize_all_fundamentals_materializes_dividend_archive(tmp_path: Path) -> None:
    _register(
        tmp_path,
        "dividend_twse",
        [
            {
                "公司代號": "2330",
                "股利年度": "115",
                "股東配發-盈餘分配之現金股利(元/股)": "2",
                "股東配發-盈餘轉增資配股(元/股)": "0",
            },
            {
                "公司代號": "2330",
                "股利年度": "115",
                "股東配發-盈餘分配之現金股利(元/股)": "3",
                "股東配發-盈餘轉增資配股(元/股)": "0",
            },
        ],
    )

    result = materialize_all_fundamentals(tmp_path, limit_per_domain=1)
    assert result["failed"] == 0
    assert result["attempted"] == 1
    assert result["dividend_history"]["rows_seen"] == 2

    db = StockDB.open(root=tmp_path)
    try:
        rows = db.get_dividend_history("2330", 2026)
        assert len(rows) == 1
        assert rows[0].cash_dividend == 5
        assert rows[0].source_date == ""
        assert rows[0].retrieved_at == "2026-09-20T08:00:00+08:00"
        assert rows[0].source_record_id.startswith("source-doc:")
        assert db.conn.execute(
            "SELECT COUNT(*) FROM evidence_items WHERE kind='dividend_history'"
        ).fetchone()[0] == 1
    finally:
        db.close()
