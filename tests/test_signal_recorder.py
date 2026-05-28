"""SignalRecorder report generation tests."""

from __future__ import annotations

import datetime

import pandas as pd

from bot.models import MarketTick, SignalEvent
from bot.signal_recorder import SignalRecorder


def _tick(symbol: str, price: float) -> MarketTick:
    return MarketTick(
        ts=datetime.datetime(2026, 5, 13, 9, 0),
        symbol=symbol,
        price=price,
        volume=100,
        pct_chg=0.0,
        prev_close=100.0,
        source="twse_public",
    )


def _signal(action: str, price: float, reason: str) -> SignalEvent:
    return SignalEvent(
        ts=datetime.datetime(2026, 5, 13, 9, 5),
        symbol="2330",
        action=action,
        price=price,
        quantity=1,
        reason=reason,
        pct_chg=0.0,
        pnl_pct=0.0,
        mode="report",
        source="twse_public",
    )


def test_report_high_low_use_observed_market_ticks(tmp_path) -> None:
    recorder = SignalRecorder(output_dir=str(tmp_path))
    recorder.record_tick(_tick("2330", 590.0))
    recorder.record_tick(_tick("2330", 620.0))
    recorder.record_tick(_tick("2330", 580.0))
    recorder.record(_signal("would-buy", 600.0, "enter"))
    recorder.record(_signal("would-sell", 610.0, "trail"))

    path = recorder.export_report()

    assert path is not None
    df = pd.read_csv(path)
    row = df.iloc[0]
    assert row["symbol"] == 2330
    assert row["high"] == 620.0
    assert row["low"] == 580.0
