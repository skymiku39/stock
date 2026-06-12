from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from bot.config import Settings
from bot.watch_symbol_pool import (
    SOURCE_INTRADAY_PREV,
    SOURCE_INTRADAY_TODAY,
    SOURCE_LIVE_OPEN,
    SOURCE_MANUAL,
    SOURCE_NEXTDAY_PREV,
    prev_trading_day,
    resolve_watch_symbol_pool,
)


def _write_intraday(root: Path, day: str, tickers: list[str]) -> None:
    out = root / "data" / "intraday" / day
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "asof": day,
        "rankings": [
            {
                "ticker": t,
                "name": f"N{t}",
                "day_trade_score": 80 - i,
                "today_close": 100.0 + i,
            }
            for i, t in enumerate(tickers)
        ],
    }
    (out / "report.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_nextday(root: Path, target: str, tickers: list[str]) -> None:
    out = root / "data" / "next_day_watch" / target
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "asof": prev_trading_day(dt.date.fromisoformat(target)).isoformat(),
        "target_date": target,
        "rankings": [
            {
                "ticker": t,
                "name": f"N{t}",
                "next_day_score": 70 - i,
                "today_close": 50.0 + i,
            }
            for i, t in enumerate(tickers)
        ],
    }
    (out / "report_update.json").write_text(json.dumps(payload), encoding="utf-8")


def test_prev_trading_day_skips_weekend() -> None:
    monday = dt.date(2026, 6, 8)
    assert prev_trading_day(monday) == dt.date(2026, 6, 5)


def test_resolve_watch_symbol_pool_merges_four_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_day = dt.date(2026, 6, 10)
    prev_day = prev_trading_day(trading_day)

    _write_intraday(tmp_path, prev_day.isoformat(), ["1111", "2222"])
    _write_intraday(tmp_path, trading_day.isoformat(), ["3333", "4444"])
    _write_nextday(tmp_path, trading_day.isoformat(), ["5555", "6666"])

    class FakeTwseSource:
        def __init__(self, symbols, logger=None):
            self.symbols = symbols

        def get_quotes(self):
            return {
                t: {
                    "price": 80.0,
                    "pct_chg": 1.0,
                    "quote_date": trading_day.isoformat(),
                }
                for t in self.symbols
            }

    monkeypatch.setattr("bot.market_source.TwsePublicMarketSource", FakeTwseSource)

    settings = Settings(
        _env_file=None,
        symbols=["7777"],
        symbols_auto_merge=True,
        symbols_merge_top_n=5,
        symbols_merge_max_total=20,
        daily_fund_budget=10000,
        use_odd_lot=True,
        symbols_merge_budget_filter=True,
    )

    noon = dt.datetime(2026, 6, 10, 10, 0)
    result = resolve_watch_symbol_pool(
        settings,
        tmp_path,
        asof=noon,
        include_live=True,
        refresh_live_quotes=True,
    )

    assert "7777" in result.symbols
    assert SOURCE_MANUAL in result.sources_by_symbol["7777"]
    assert "3333" in result.symbols
    assert SOURCE_INTRADAY_TODAY in result.sources_by_symbol["3333"]
    assert "5555" in result.symbols
    assert SOURCE_NEXTDAY_PREV in result.sources_by_symbol["5555"]
    assert "1111" in result.symbols
    assert SOURCE_INTRADAY_PREV in result.sources_by_symbol["1111"]
    assert any(SOURCE_LIVE_OPEN in tags for tags in result.sources_by_symbol.values())
    assert result.source_counts[SOURCE_MANUAL] == 1


def test_budget_filter_drops_expensive_lot_only_stock(tmp_path: Path) -> None:
    trading_day = dt.date(2026, 6, 10)
    out = tmp_path / "data" / "intraday" / trading_day.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({
            "asof": trading_day.isoformat(),
            "rankings": [{
                "ticker": "2330",
                "day_trade_score": 90,
                "today_close": 600000.0,
            }],
        }),
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        symbols=[],
        symbols_auto_merge=True,
        symbols_merge_top_n=5,
        daily_fund_budget=10000,
        use_odd_lot=False,
        symbols_merge_budget_filter=True,
    )
    result = resolve_watch_symbol_pool(
        settings,
        tmp_path,
        asof=dt.datetime.combine(trading_day, dt.time(8, 0)),
        include_live=False,
    )

    assert "2330" not in result.symbols
    assert "2330" in result.skipped_budget
