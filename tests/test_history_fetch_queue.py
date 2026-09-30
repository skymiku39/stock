"""history_fetch_queue 單元測試。"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pytest

from bot.history_fetch_queue import (
    QueueState,
    WorkItem,
    _in_cooldown,
    _month_covered,
    _set_cooldown,
    init_state,
    load_state,
    next_work_item,
    save_state,
)
from bot.stock_db import PriceBar, StockDB, default_db_path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def db(root: Path) -> StockDB:
    return StockDB.open(path=default_db_path(root))


class TestHistoryFetchQueue:
    def test_init_and_persist(self, root: Path) -> None:
        state = init_state(
            root=root,
            start_date="2026-01-01",
            end_date="2026-02-28",
            symbols=["2330"],
        )
        assert state.symbols == ["2330"]
        assert len(state.months) == 2
        loaded = load_state(root)
        assert loaded.symbols == ["2330"]

    def test_next_skips_covered_month(self, root: Path, db: StockDB) -> None:
        db.bulk_upsert_price_bars([
            PriceBar(symbol="2330", date="2026-01-05", open=1, high=2, low=1, close=2),
            PriceBar(symbol="2330", date="2026-01-06", open=2, high=3, low=2, close=3),
            PriceBar(symbol="2330", date="2026-01-07", open=3, high=4, low=3, close=4),
            PriceBar(symbol="2330", date="2026-01-08", open=4, high=5, low=4, close=5),
            PriceBar(symbol="2330", date="2026-01-09", open=5, high=6, low=5, close=6),
            PriceBar(symbol="2330", date="2026-01-10", open=6, high=7, low=6, close=7),
            PriceBar(symbol="2330", date="2026-01-13", open=7, high=8, low=7, close=8),
            PriceBar(symbol="2330", date="2026-01-14", open=8, high=9, low=8, close=9),
            PriceBar(symbol="2330", date="2026-01-15", open=9, high=10, low=9, close=10),
            PriceBar(symbol="2330", date="2026-01-16", open=10, high=11, low=10, close=11),
        ])
        state = QueueState(
            symbols=["2330"],
            months=[(2026, 1), (2026, 2)],
        )
        item = next_work_item(state, db)
        assert item == WorkItem(symbol="2330", year=2026, month=2)

    def test_cooldown(self, root: Path) -> None:
        state = QueueState()
        now = dt.datetime(2026, 6, 10, 12, 0, 0)
        _set_cooldown(state, "2330", 30, now=now)
        assert _in_cooldown(state, "2330", now=now)
        later = now + dt.timedelta(minutes=31)
        assert not _in_cooldown(state, "2330", now=later)

    def test_month_covered_threshold(self, root: Path, db: StockDB) -> None:
        assert not _month_covered(db, "9999", 2026, 1, kind="daily")
        db.bulk_upsert_price_bars([
            PriceBar(symbol="9999", date=f"2026-01-{d:02d}", open=1, high=2, low=1, close=2)
            for d in range(1, 11)
        ])
        assert _month_covered(db, "9999", 2026, 1, kind="daily")

    def test_yfinance_fallback_is_archived_as_cache_only_normalized_source(
        self, root: Path, db: StockDB, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from bot.history_fetch_queue import run_one_daily
        from bot.ask import AskEngine

        rows = [{
            "date": "2026-09-18",
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 103.0,
            "volume": 12.5,
        }]
        monkeypatch.setattr("bot.history_fetch_queue.twse_is_available", lambda *args, **kwargs: False)
        monkeypatch.setattr(
            "bot.history_fetch_queue.fetch_monthly_kline_yfinance",
            lambda *args, **kwargs: rows,
        )
        result = run_one_daily(
            WorkItem("2330", 2026, 9),
            root=root,
            db=db,
            logger=logging.getLogger("test-history"),
        )

        assert result.ok is True
        bars = db.get_price_history("2330", limit=1, ascending=False)
        assert bars[0].source == "yfinance"
        assert bars[0].quality_status == "partial"
        assert bars[0].source_record_id.startswith("source-doc:yfinance_stock_day:")
        source = db.get_source_document(bars[0].source_record_id)
        assert source is not None
        payload = json.loads((root / source.content_path).read_text(encoding="utf-8"))
        assert payload["adapter"] == "yfinance"
        assert payload["rows"] == rows
        assert json.loads(source.metadata_json)["provenance_status"] == "cache_only"

        result = AskEngine(root=root, db=db).ask("2330 最近股價？")
        price_items = [
            item for item in result.evidence.items
            if item["kind"] == "ticker_snapshot" and "日 K" in item["title"]
        ]
        assert price_items
        assert price_items[0]["provenance_status"] == "cache_only"
