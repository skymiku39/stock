"""Tests for SVG candlestick renderer."""
from __future__ import annotations

import pandas as pd

from bot.dashboard.common import (
    _build_candlestick_image,
    _build_candlestick_svg,
    _ohlc_bars_from_df,
)


def test_build_candlestick_svg_contains_candles():
    bars = [
        {"open": 100, "high": 105, "low": 98, "close": 103},
        {"open": 103, "high": 104, "low": 99, "close": 100},
    ]
    svg = _build_candlestick_svg(bars, width=200, height=80)
    assert "<svg" in svg
    assert 'stroke="#d64545"' in svg or 'fill="#d64545"' in svg
    assert "<rect" in svg


def test_build_candlestick_image_non_empty():
    bars = [
        {"open": 100, "high": 105, "low": 98, "close": 103},
        {"open": 103, "high": 104, "low": 99, "close": 100},
    ]
    img = _build_candlestick_image(bars, width=200, height=80)
    assert img is not None
    assert img.size == (200, 80)


def test_ohlc_bars_from_df():
    df = pd.DataFrame({
        "date": ["2026-01-01", "2026-01-02"],
        "open": [10.0, 11.0],
        "high": [12.0, 13.0],
        "low": [9.0, 10.0],
        "close": [11.0, 10.5],
    })
    bars = _ohlc_bars_from_df(df)
    assert len(bars) == 2
    assert bars[0]["high"] == 12.0
