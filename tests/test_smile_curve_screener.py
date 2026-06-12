"""微笑曲線複合選股測試。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from bot.active_etf import Holding, HoldingsSnapshot, save_holdings
from bot.config import Settings
from bot.smile_curve_screener import (
    compute_price_metrics,
    screen_smile_candidates,
)
from bot.stock_db import PriceBar, StockDB


def _volatile_bars(symbol: str, base: float = 100.0) -> list[PriceBar]:
    """震盪序列，適合微笑曲線。"""
    pattern = [0, -2, -5, -8, -4, 0, 3, 6, 2, -1, -4, -7, -3, 1, 5, 8, 4, 0, -3]
    bars: list[PriceBar] = []
    for i, pct in enumerate(pattern * 4):
        c = base * (1.0 + pct / 100.0)
        d = f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
        bars.append(PriceBar(
            symbol=symbol, date=d,
            open=c, high=c + 1, low=c - 1, close=c,
        ))
    return bars


def _flat_bars(symbol: str) -> list[PriceBar]:
    bars: list[PriceBar] = []
    for i in range(80):
        c = 100.0 + i * 0.1
        d = f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
        bars.append(PriceBar(
            symbol=symbol, date=d,
            open=c, high=c + 0.5, low=c - 0.5, close=c,
        ))
    return bars


@pytest.fixture
def root(tmp_path: Path) -> Path:
    etf_dir = tmp_path / "data" / "etf_holdings" / "00980A"
    etf_dir.mkdir(parents=True)
    snap = HoldingsSnapshot(
        symbol="00980A",
        date=dt.date(2026, 6, 1),
        holdings=[
            Holding("GOOD", "好標的", 10.0),
            Holding("FLAT", "平盤股", 8.0),
            Holding("EXP", "太貴股", 5.0),
        ],
    )
    save_holdings(snap, root=tmp_path)
    return tmp_path


@pytest.fixture
def db(tmp_path: Path) -> StockDB:
    database = StockDB.open(path=tmp_path / "screen.db")
    database.bulk_upsert_price_bars(_volatile_bars("GOOD", 150.0))
    database.bulk_upsert_price_bars(_flat_bars("FLAT"))
    database.bulk_upsert_price_bars(_volatile_bars("EXP", 500.0))
    return database


class TestSmileCurveScreener:
    def test_compute_price_metrics_volatile(self) -> None:
        metrics = compute_price_metrics(_volatile_bars("GOOD", 150.0), budget_per_symbol=60_000)
        assert metrics.ann_vol_pct > 10
        assert metrics.dip_5pct_days > 0
        assert metrics.odd_shares_per_budget >= 300

    def test_screen_prefers_volatile_affordable(self, root: Path, db: StockDB) -> None:
        report = screen_smile_candidates(
            db,
            root=root,
            start="2024-01-01",
            end="2024-12-31",
            total_fund=300_000,
            top_n=2,
            min_etf_count=1,
            max_price=300.0,
            min_ann_vol=5.0,
            use_odd_lot=True,
            settings=Settings(max_fund=60_000, use_odd_lot=True, _env_file=None),
        )
        assert report.universe_size == 3
        assert len(report.candidates) >= 1
        top = report.selected[0]
        assert top.symbol == "GOOD"
        assert top.composite_score >= report.candidates[-1].composite_score

    def test_max_price_filters_expensive(self, root: Path, db: StockDB) -> None:
        report = screen_smile_candidates(
            db,
            root=root,
            start="2024-01-01",
            end="2024-12-31",
            total_fund=300_000,
            top_n=5,
            min_etf_count=1,
            max_price=300.0,
            min_ann_vol=0.0,
            use_odd_lot=True,
            settings=Settings(max_fund=60_000, use_odd_lot=True, _env_file=None),
        )
        symbols = {c.symbol for c in report.candidates}
        assert "EXP" not in symbols

    def test_extra_symbols_without_etf(self, db: StockDB, tmp_path: Path) -> None:
        report = screen_smile_candidates(
            db,
            root=tmp_path,
            start="2024-01-01",
            end="2024-12-31",
            total_fund=300_000,
            top_n=1,
            min_etf_count=2,
            max_price=0,
            min_ann_vol=0.0,
            extra_symbols=["GOOD"],
            settings=Settings(max_fund=60_000, use_odd_lot=True, _env_file=None),
        )
        assert len(report.selected) == 1
        assert report.selected[0].symbol == "GOOD"
