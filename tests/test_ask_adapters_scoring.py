"""Ask snapshot adapters + ETF scoring split."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot.ask import AskEngine
from bot.ask_adapters import collect_snapshot_resource_evidence
from bot.scoring import compute_scorecard, detect_instrument_type
from bot.stock_db import StockDB, StockInfo


def test_detect_instrument_type_etf_heuristics() -> None:
    assert detect_instrument_type("00929", name="復華台灣科技優息") == "etf"
    assert detect_instrument_type("0050", industry="ETF / 基金") == "etf"
    assert detect_instrument_type("2330", name="台積電", industry="半導體業") == "stock"


def test_etf_scorecard_masks_fundamental_and_consensus() -> None:
    card = compute_scorecard(
        ticker="00929",
        name="復華台灣科技優息",
        price=28.5,
        pct_change=-0.5,
        technical_snapshot={
            "has_data": True,
            "technical_score": 40.0,
            "rsi14": 45.0,
            "signals": [],
        },
        chip_summary={
            "ticker": "00929",
            "days": 5,
            "foreign_net": -1000.0,
            "investment_trust_net": 0.0,
            "dealer_net": 0.0,
            "margin_buy_change_pct": 0.0,
            "short_borrow_change_pct": 0.0,
            "block_trade_net": 0.0,
            "rows": [{"ticker": "00929"}],
        },
        fundamental={
            "valuation": {"pe_ratio": 12.0, "pb_ratio": 1.0, "dividend_yield": 6.0},
            "revenues": [{"yoy": 10.0}],
            "quarterlies": [],
        },
        consensus={"etf_count": 5, "total_weight": 12.0},
        instrument_type="etf",
    )
    day = card.timeframes["day_trade"]
    mid = card.timeframes["mid_term"]
    day_keys = {f.key: f for f in day.factors}
    mid_keys = {f.key: f for f in mid.factors}
    assert mid_keys["fundamental"].available is False
    assert mid_keys["etf_consensus"].available is False
    assert day_keys["technical"].available is True
    assert any("ETF" in n for n in day.notes)
    assert any("ETF" in n for n in card.summary_notes)


def test_collect_snapshot_adapters_emit_resource_kinds(tmp_path: Path) -> None:
    tech = SimpleNamespace(
        has_data=True,
        last_date="2026-09-15",
        last_close=28.46,
        pct_change_1d=-0.49,
        pct_change_5d=-1.5,
        pct_change_20d=-1.4,
        pct_change_60d=-11.0,
        rsi14=43.0,
        macd=-0.03,
        ma5=28.7,
        ma20=28.9,
        ma60=29.2,
        technical_score=22.0,
    )
    snap = SimpleNamespace(
        name="測試ETF",
        price=28.46,
        pct_change=-0.49,
        fetched_at="2026-09-16T12:00:00+08:00",
        technicals=tech,
        chip_summary={
            "days": 5,
            "foreign_net": -6520.0,
            "investment_trust_net": 0.0,
            "dealer_net": 0.0,
            "margin_buy_change_pct": 0.0,
            "short_borrow_change_pct": 18.0,
            "block_trade_net": 0.0,
            "rows": [{"ticker": "00929"}],
        },
        consensus={"etf_count": 0, "total_weight": 0.0},
        new_build_signal=None,
        add_signal=None,
        held_by_etfs=[],
        macro_snapshot={"asof_date": "2026-09-15", "vix": 15.0},
        fundamentals=None,
    )
    with patch("bot.ticker_view.build_snapshot", return_value=snap), patch(
        "bot.technicals.snapshot_to_dict",
        return_value={
            "last_date": "2026-09-15",
            "last_close": 28.46,
            "pct_change_1d": -0.49,
            "pct_change_5d": -1.5,
            "pct_change_20d": -1.4,
            "pct_change_60d": -11.0,
            "rsi14": 43.0,
            "macd": -0.03,
            "ma5": 28.7,
            "ma20": 28.9,
            "ma60": 29.2,
            "technical_score": 22.0,
            "signals": [{"label": "均線", "detail": "跌破"}],
        },
    ):
        items = collect_snapshot_resource_evidence(
            "00929",
            root=tmp_path,
            resource_needs=["technical", "chips", "etf", "macro", "ticker_snapshot"],
        )
    kinds = {i["kind"] for i in items}
    assert "technical" in kinds
    assert "chips" in kinds
    assert "macro" in kinds
    assert "ticker_snapshot" in kinds


def test_ask_engine_uses_snapshot_adapters(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_stock_info(StockInfo(
        symbol="00929",
        name="復華台灣科技優息",
        industry="ETF / 基金",
        quality_status="ok",
        source_record_id="company:00929",
    ))
    fake_items = [
        {
            "id": "technical:test",
            "kind": "technical",
            "source": "technicals_cache",
            "source_ref": "00929:tech",
            "title": "tech",
            "url": "",
            "observed_at": "2026-09-15",
            "excerpt": "RSI14=43",
            "quality_status": "ok",
        },
        {
            "id": "chips:test",
            "kind": "chips",
            "source": "chips_cache",
            "source_ref": "00929:chips",
            "title": "chips",
            "url": "",
            "observed_at": "2026-09-16",
            "excerpt": "外資淨買超 -1000",
            "quality_status": "ok",
        },
    ]
    with patch(
        "bot.ask_adapters.collect_snapshot_resource_evidence",
        return_value=fake_items,
    ):
        result = AskEngine(root=tmp_path, db=db).ask(
            "00929 技術面與籌碼如何？",
            ticker="00929",
        )
    kinds = {i["kind"] for i in result.evidence.usable_items}
    assert "technical" in kinds
    assert "chips" in kinds
    assert "technical" not in result.evidence.missing_resources
    assert "chips" not in result.evidence.missing_resources


def test_collect_news_adapter_reads_cache_only(tmp_path: Path) -> None:
    item = SimpleNamespace(
        news_id="n1",
        title="00929 配息公告",
        summary="將於下周除息",
        url="https://example.com/n1",
        published_at="2026-09-16T10:00:00+08:00",
        source="cnyes",
        related_tickers=["00929"],
    )
    with patch(
        "bot.news_fetcher.load_cached_news_only",
        return_value=[item],
    ) as mock_load:
        items = collect_snapshot_resource_evidence(
            "00929",
            root=tmp_path,
            resource_needs=["news"],
        )
    mock_load.assert_called_once()
    assert len(items) == 1
    assert items[0]["kind"] == "news"
    assert "配息" in items[0]["title"]


def test_news_only_skips_snapshot_build(tmp_path: Path) -> None:
    with patch("bot.ticker_view.build_snapshot") as mock_snap, patch(
        "bot.news_fetcher.load_cached_news_only",
        return_value=[],
    ):
        collect_snapshot_resource_evidence(
            "00929",
            root=tmp_path,
            resource_needs=["news"],
        )
    mock_snap.assert_not_called()
