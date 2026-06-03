from __future__ import annotations

import json

from bot.portfolio_analysis import (
    build_portfolio_analysis_bundle,
    bundle_to_json,
    data_quality,
    portfolio_totals,
)


def test_portfolio_totals_summarizes_broker_rows() -> None:
    totals = portfolio_totals([
        {"券商成本": 100_000, "券商市值": 120_000, "未實現損益": 20_000, "權重%": 60, "本工具張數": 1},
        {"券商成本": 50_000, "券商市值": 40_000, "未實現損益": -10_000, "權重%": 40, "本工具張數": 0},
    ])

    assert totals["holding_count"] == 2
    assert totals["broker_cost_twd"] == 150_000
    assert totals["broker_market_value_twd"] == 160_000
    assert totals["unrealized_pnl_twd"] == 10_000
    assert totals["unrealized_pnl_pct"] == 6.67
    assert totals["bot_marked_count"] == 1


def test_data_quality_flags_missing_evidence() -> None:
    quality = data_quality(
        "2330",
        {"代號": "2330", "名稱": "台積電", "最新價": 930, "權重%": 50},
        {
            "fundamentals": {"has_data": True},
            "technicals": {"has_data": True},
            "chip_summary": {"foreign_net": 1000},
            "llm_analysis": {"summary": "ok"},
        },
    )

    assert quality["price"] is True
    assert quality["fundamentals"] is True
    assert quality["technicals"] is True
    assert quality["chips"] is True
    assert quality["llm"] is True
    assert quality["distribution"] is False
    assert "TDCC" in quality["missing"]


def test_build_portfolio_analysis_bundle_is_json_serializable() -> None:
    bundle = build_portfolio_analysis_bundle(
        rows=[{
            "代號": "2330",
            "名稱": "台積電",
            "產業": "半導體業",
            "券商張數": 1,
            "券商均價": 900,
            "券商成本": 900_000,
            "最新價": 930,
            "券商市值": 930_000,
            "未實現損益": 30_000,
            "損益%": 3.33,
            "權重%": 100,
            "本工具標記": "本工具",
            "本工具張數": 1,
            "手動/外部張數": 0,
            "本工具均價": 900,
        }],
        snapshots={
            "2330": {
                "name": "台積電",
                "fundamentals": {"has_data": True, "latest_eps": 12.3},
                "technicals": {"has_data": True, "last_close": 930},
                "held_by_etfs": [{"symbol": "00980A"}],
            }
        },
        broker_meta={"broker": "shioaji", "asof": "2026-06-03T09:00:00+08:00"},
        warnings=["單檔集中"],
    )

    parsed = json.loads(bundle_to_json(bundle))

    assert parsed["totals"]["holding_count"] == 1
    assert parsed["industry_exposure"][0]["industry"] == "半導體業"
    assert parsed["holdings"][0]["symbol"] == "2330"
    assert parsed["holdings"][0]["ownership"]["label"] == "本工具"
    assert parsed["data_quality_summary"]["average_coverage_score"] > 0
