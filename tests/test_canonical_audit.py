from __future__ import annotations

from bot.canonical_audit import audit_canonical_rows
from bot.source_audit_cli import audit_project
from bot.stock_db import PriceBar, StockDB


def test_canonical_audit_accepts_valid_price_and_period_rows() -> None:
    price = audit_canonical_rows(
        "price_history",
        [{
            "symbol": "2330", "date": "2026-09-18",
            "open": 2400, "high": 2450, "low": 2390,
            "close": 2440, "volume": 100,
        }],
    )
    revenue = audit_canonical_rows(
        "monthly_revenue",
        [{"symbol": "2330", "year_month": "2026-08", "revenue": 100}],
    )
    price_with_month_source = audit_canonical_rows(
        "price_history",
        [{
            "symbol": "2330", "date": "2026-09-18", "source_date": "2026-09",
            "open": 2400, "high": 2450, "low": 2390, "close": 2440,
            "volume": 100,
        }],
    )
    assert price.quality_status == "ok"
    assert revenue.quality_status == "ok"
    assert price_with_month_source.quality_status == "ok"


def test_canonical_audit_rejects_ohlcv_and_period_errors() -> None:
    result = audit_canonical_rows(
        "price_history",
        [{
            "symbol": "2330", "date": "2026-09-18",
            "open": 100, "high": 90, "low": 80,
            "close": 95, "volume": 1,
        }],
    )
    period = audit_canonical_rows(
        "monthly_revenue",
        [{"symbol": "2330", "year_month": "2026/08", "revenue": 100}],
    )
    assert result.quality_status == "invalid"
    assert any(item.check == "ohlcv_invariant" for item in result.findings)
    assert period.quality_status == "invalid"
    assert any(item.check == "period_format" for item in period.findings)


def test_canonical_audit_rejects_unparseable_provenance_time() -> None:
    result = audit_canonical_rows(
        "monthly_revenue",
        [{
            "symbol": "2330", "year_month": "2026-08", "revenue": 100,
            "source_date": "not-a-period", "retrieved_at": "not-a-timestamp",
        }],
    )
    assert result.quality_status == "invalid"
    assert {item.check for item in result.findings} >= {
        "source_date_format", "timestamp_format",
    }


def test_canonical_audit_preserves_source_defined_financial_extremes() -> None:
    revenue = audit_canonical_rows(
        "monthly_revenue",
        [{"symbol": "7777", "year_month": "2026-08", "revenue": -51703}],
    )
    quarterly = audit_canonical_rows(
        "quarterly_report",
        [{
            "symbol": "1340", "period": "2026Q2", "revenue": 28726,
            "gross_margin": -141.43, "op_margin": -309.20,
            "net_margin": -186.24,
        }],
    )

    # 退貨／重編與小分母造成的極端比率仍是可追溯的來源事實；
    # 只有不可解析、主鍵或期間結構錯誤才應阻擋 canonical table。
    assert revenue.quality_status == "ok"
    assert quarterly.quality_status == "ok"


def test_canonical_audit_rejects_cross_domain_provenance_link() -> None:
    result = audit_canonical_rows(
        "monthly_revenue",
        [{
            "symbol": "2330",
            "year_month": "2026-08",
            "revenue": 100,
            "source_record_id": "source-doc:valuation:1",
        }],
        source_documents={"source-doc:valuation:1": "valuation_twse"},
    )
    assert result.quality_status == "invalid"
    assert any(item.check == "provenance_domain" for item in result.findings)


def test_project_audit_exposes_canonical_semantic_gap(tmp_path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_price_bar(PriceBar(
        symbol="2330", date="2026-09-18",
        open=100, high=90, low=80, close=95, volume=1,
    ))
    report = audit_project(tmp_path)
    canonical = report["database"]["canonical"]
    assert canonical["tables"]["price_history"]["quality_status"] == "invalid"
    assert any(gap["code"] == "canonical_semantic_invalid" for gap in report["gaps"])
