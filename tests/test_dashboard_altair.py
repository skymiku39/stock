"""Tests for dashboard Altair display helper."""
from __future__ import annotations

import pandas as pd

from bot.dashboard.common import (
    _VEGA_LITE_V5_SCHEMA,
    _altair_is_composite,
    _display_altair,
    _fallback_is_ohlc,
    _inline_vega_datasets,
    _prepare_vega_spec_for_streamlit,
)


def test_altair_is_composite_detects_layer_chart():
    import altair as alt

    df = pd.DataFrame({"x": [1, 2], "y": [3, 4]})
    simple = alt.Chart(df).mark_line().encode(x="x", y="y")
    layered = alt.layer(
        alt.Chart(df).mark_rule().encode(x="x", y="y"),
        alt.Chart(df).mark_bar().encode(x="x", y="y"),
    )
    assert _altair_is_composite(simple) is False
    assert _altair_is_composite(layered) is True


def test_prepare_vega_spec_downgrades_v6_schema():
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.4.1.json",
        "layer": [{"mark": "line"}],
        "data": {"name": "data-abc"},
        "datasets": {"data-abc": [{"x": 1}]},
    }
    out = _prepare_vega_spec_for_streamlit(spec)
    assert out["$schema"] == _VEGA_LITE_V5_SCHEMA
    assert out["data"] == {"values": [{"x": 1}]}


def test_inline_vega_datasets_expands_layer_data():
    spec = {
        "layer": [{"mark": "line"}, {"mark": "bar"}],
        "data": {"name": "data-abc"},
        "datasets": {"data-abc": [{"x": 1, "y": 2}]},
    }
    inlined = _inline_vega_datasets(spec)
    assert "datasets" not in inlined
    assert inlined["data"] == {"values": [{"x": 1, "y": 2}]}


def test_fallback_is_ohlc_detects_kline_df():
    df = pd.DataFrame({"date": ["2026-01-01"], "close": [100.0], "open": [99.0]})
    assert _fallback_is_ohlc(df) is True
    assert _fallback_is_ohlc(pd.DataFrame({"營收 (千元)": [1]})) is False


def test_display_altair_ohlc_uses_native_charts(monkeypatch):
    import altair as alt

    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=3),
        "open": [99.0, 101.0, 102.0],
        "high": [102.0, 103.0, 104.0],
        "low": [98.0, 100.0, 101.0],
        "close": [100.0, 101.0, 102.0],
        "volume": [1000, 1100, 1200],
    })
    chart = alt.layer(
        alt.Chart(df).mark_rule().encode(x="date:T", y="low:Q", y2="high:Q"),
        alt.Chart(df).mark_bar().encode(x="date:T", y="open:Q", y2="close:Q"),
    )
    pyplot_calls: list = []
    vega_calls: list = []

    def _pyplot(*a, **k):
        pyplot_calls.append({"args": a, **k})

    monkeypatch.setattr("streamlit.pyplot", _pyplot)
    monkeypatch.setattr("streamlit.vega_lite_chart", lambda *a, **k: vega_calls.append(k))
    _display_altair(
        chart, width=320, fallback_df=df,
        use_ohlc_native=True, include_volume=True,
    )
    assert pyplot_calls, "應以 st.pyplot 渲染 matplotlib 蠟燭圖"
    assert not vega_calls


def test_display_altair_layer_uses_vega_lite_chart(monkeypatch):
    import altair as alt

    df = pd.DataFrame({"x": [1, 2], "y": [3, 4]})
    chart = alt.layer(
        alt.Chart(df).mark_rule().encode(x="x", y="y"),
        alt.Chart(df).mark_bar().encode(x="x", y="y"),
    )
    calls: list[dict] = []

    def fake_vega_lite_chart(*_args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("streamlit.vega_lite_chart", fake_vega_lite_chart)
    _display_altair(chart, width=320)
    assert calls
    assert calls[0].get("use_container_width") is False
    assert calls[0].get("width") == 320
    assert isinstance(calls[0].get("spec"), dict)


def test_display_altair_bar_line_layer_is_composite():
    import altair as alt

    df = pd.DataFrame({"年月": ["2025/01"], "營收 (千元)": [100], "YoY %": [5.0]})
    base = alt.Chart(df).encode(x=alt.X("年月:N", title="年月"))
    bar = base.mark_bar().encode(y=alt.Y("營收 (千元):Q"))
    line = base.mark_line().encode(y=alt.Y("YoY %:Q"))
    chart = alt.layer(bar, line).resolve_scale(y="independent")
    assert _altair_is_composite(chart) is True
