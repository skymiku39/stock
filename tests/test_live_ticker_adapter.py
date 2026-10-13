from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bot.query_plan import plan_query
from bot.resource_adapters import LiveTickerAdapter
from bot.source_registry import SourceRegistry


def test_live_ticker_adapter_preserves_exact_raw_provenance(tmp_path: Path) -> None:
    plan = plan_query("2330 最新盤中資料", ticker="2330")
    with SourceRegistry(tmp_path) as registry:
        source_document = registry.capture_document(
            "twse_mis",
            b'{"symbol":"2330","price":1000}',
            source_ref="twse-mis:2330:2026-09-18T13:30:00",
            suffix="json",
        )
    fake_source = patch("bot.market_source.TwsePublicMarketSource").start()
    try:
        fake_source.return_value.get_quotes.return_value = {
            "2330": {
                "symbol": "2330",
                "name": "台積電",
                "price": 1000.0,
                "prev_close": 990.0,
                "pct_chg": 1.01,
                "volume": 12345,
                "price_basis": "last_trade",
                "quote_date": "2026-09-18",
                "quote_time": "13:30:00",
                "fetched_at": "2026-09-20T03:30:00+08:00",
                "exchange": "tse",
                "source_document_id": source_document.document_id,
            },
        }
        result = LiveTickerAdapter(root=tmp_path, timeout=2).collect(plan)
    finally:
        fake_source.stop()

    assert result.status == "ok"
    assert result.live is True
    assert len(result.evidence) == 1
    item = result.evidence[0]
    assert item["source_document_id"] == source_document.document_id
    assert item["provenance_status"] == "exact"
    assert item["metadata"]["delayed_quote"] is True
    assert "1000" in item["excerpt"]


def test_live_ticker_adapter_does_not_turn_empty_quote_into_fact(tmp_path: Path) -> None:
    plan = plan_query("2330 最新盤中資料", ticker="2330")
    with patch("bot.market_source.TwsePublicMarketSource") as source_cls:
        source_cls.return_value.get_quotes.return_value = {}
        result = LiveTickerAdapter(root=tmp_path).collect(plan)

    assert result.status == "partial"
    assert result.evidence == []
    assert "不等於查無此事" in result.warnings[0]
