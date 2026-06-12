"""history_fetch_queue 單元測試。"""

from __future__ import annotations

import datetime as dt
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
