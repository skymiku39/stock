from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from bot import ticker_view as tv
from bot.market_macro import MacroSnapshot, fetch_macro_snapshot


def test_macro_cache_only_miss_does_not_fetch(monkeypatch, tmp_path: Path) -> None:
    def fail_import_yf():
        raise AssertionError("cache_only should not import yfinance")

    monkeypatch.setattr("bot.market_macro._import_yf", fail_import_yf)

    snap = fetch_macro_snapshot(root=tmp_path, cache_only=True)

    assert snap.indices == {}
    assert snap.stocks == {}
    assert "本地 macro 快取不存在" in snap.notes[0]


def test_macro_cache_only_hit_reads_local_cache(monkeypatch, tmp_path: Path) -> None:
    today = dt.date.today().isoformat()
    cache = tmp_path / "data" / "macro" / f"macro_{today}.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "fetched_at": f"{today}T08:00:00",
                "asof_date": today,
                "usdtwd": 31.5,
                "notes": [],
                "indices": {
                    "^GSPC": {
                        "symbol": "^GSPC",
                        "name": "S&P 500",
                        "group": "us_index",
                        "price": 5000.0,
                        "prev_close": 4990.0,
                        "pct_change": 0.2,
                        "volume": 0.0,
                        "fetched_at": f"{today}T08:00:00",
                        "source": "yfinance",
                    }
                },
                "stocks": {},
                "adr_premiums": [],
                "futures_basis": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fail_import_yf():
        raise AssertionError("cache hit should not import yfinance")

    monkeypatch.setattr("bot.market_macro._import_yf", fail_import_yf)

    snap = fetch_macro_snapshot(root=tmp_path, cache_only=True)

    assert snap.cached is True
    assert snap.indices["^GSPC"].price == 5000.0


def test_build_snapshot_local_first_does_not_auto_fill(monkeypatch, tmp_path: Path) -> None:
    fund_refresh_calls: list[bool] = []
    tech_refresh_calls: list[bool] = []
    macro_cache_only_calls: list[bool] = []

    monkeypatch.setattr(tv, "load_active_etfs", lambda root: [])
    monkeypatch.setattr(tv, "_list_runs", lambda pipeline_dir: [])
    monkeypatch.setattr(tv, "_scan_trades_for", lambda ticker, root: ([], 0.0, 0.0))
    monkeypatch.setattr(tv, "fetch_daily_chips", lambda *args, **kwargs: {})

    def fail_chip_summary(*args, **kwargs):
        raise AssertionError("local-first should not auto fetch chips")

    monkeypatch.setattr(tv, "build_chip_summary", fail_chip_summary)

    def fake_fundamental_snapshot(ticker: str, **kwargs):
        fund_refresh_calls.append(bool(kwargs.get("refresh")))
        return tv.FundamentalSnapshot(ticker=ticker)

    monkeypatch.setattr(tv, "build_fundamental_snapshot", fake_fundamental_snapshot)

    def fake_technical_snapshot(ticker: str, **kwargs):
        tech_refresh_calls.append(bool(kwargs.get("refresh")))
        return tv.TechnicalSnapshot(ticker=ticker), pd.DataFrame()

    monkeypatch.setattr(tv, "build_technical_snapshot", fake_technical_snapshot)
    monkeypatch.setattr(
        tv,
        "load_distribution_trend",
        lambda ticker, root=None: tv.DistributionTrend(ticker=ticker),
    )

    def fail_distribution_fetch(*args, **kwargs):
        raise AssertionError("local-first should not auto fetch TDCC")

    monkeypatch.setattr(tv, "build_distribution_snapshot", fail_distribution_fetch)

    def fake_fetch_macro_snapshot(**kwargs):
        macro_cache_only_calls.append(bool(kwargs.get("cache_only")))
        return MacroSnapshot(fetched_at="", asof_date="")

    monkeypatch.setattr("bot.market_macro.fetch_macro_snapshot", fake_fetch_macro_snapshot)
    monkeypatch.setattr("bot.market_macro.load_supply_chain", lambda root=None: {"us_stocks": {}})
    monkeypatch.setattr("bot.market_macro.related_us_stocks_for_tw", lambda *args, **kwargs: [])
    monkeypatch.setattr("bot.market_macro.macro_to_dict", lambda snap: {"cached": False})

    snap = tv.build_snapshot(
        "2330",
        tmp_path,
        auto_fill_missing=False,
        auto_llm=False,
        macro_cache_only=True,
    )

    assert snap.ticker == "2330"
    assert fund_refresh_calls == [False]
    assert tech_refresh_calls == [False]
    assert macro_cache_only_calls == [True]
