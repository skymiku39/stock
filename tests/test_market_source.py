"""TwsePublicMarketSource 解析測試 (使用 fixture 而非實際網路)。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bot.market_source import TwsePublicMarketSource


SAMPLE_MSG_ARRAY = [
    {
        "c": "2330",
        "z": "600.00",
        "tv": "500",
        "v": "15000",
        "y": "580.00",
        "t": "13:30:00",
        "n": "台積電",
        "ex": "tse",
    },
    {
        "c": "0050",
        "z": "140.50",
        "tv": "200",
        "v": "8000",
        "y": "138.00",
        "t": "13:30:00",
        "n": "元大台灣50",
        "ex": "tse",
    },
]


class TestGetPrevClose:
    def test_parses_prev_close(self) -> None:
        source = TwsePublicMarketSource(symbols=["2330", "0050"])
        with patch.object(source, "_fetch_raw", return_value=SAMPLE_MSG_ARRAY):
            result = source.get_prev_close(["2330", "0050"])

        assert result == {"2330": 580.0, "0050": 138.0}

    def test_skips_dash_values(self) -> None:
        items = [{"c": "9999", "z": "-", "y": "-", "ex": "tse"}]
        source = TwsePublicMarketSource(symbols=["9999"])
        with patch.object(source, "_fetch_raw", return_value=items):
            result = source.get_prev_close(["9999"])

        assert result == {}


class TestPoll:
    def test_returns_market_ticks(self) -> None:
        source = TwsePublicMarketSource(symbols=["2330", "0050"])
        with patch.object(source, "_fetch_raw", return_value=SAMPLE_MSG_ARRAY):
            ticks = source.poll()

        assert len(ticks) == 2
        t = ticks[0]
        assert t.symbol == "2330"
        assert t.price == 600.0
        assert t.prev_close == 580.0
        assert t.source == "twse_public"
        assert t.pct_chg == pytest.approx(
            100 * (600.0 - 580.0) / 580.0, rel=1e-4,
        )

    def test_deduplicates_same_volume(self) -> None:
        source = TwsePublicMarketSource(symbols=["2330"])
        with patch.object(source, "_fetch_raw", return_value=SAMPLE_MSG_ARRAY[:1]):
            first = source.poll()
            second = source.poll()

        assert len(first) == 1
        assert len(second) == 0

    def test_skips_no_trade_items(self) -> None:
        no_trade = [{"c": "2330", "z": "-", "v": "0", "y": "580.00", "ex": "tse"}]
        source = TwsePublicMarketSource(symbols=["2330"])
        with patch.object(source, "_fetch_raw", return_value=no_trade):
            ticks = source.poll()

        assert len(ticks) == 0

    def test_caches_exchange_mapping(self) -> None:
        source = TwsePublicMarketSource(symbols=["2330"])
        with patch.object(source, "_fetch_raw", return_value=SAMPLE_MSG_ARRAY[:1]):
            source.poll()

        assert source._exchange_map["2330"] == "tse"


class TestBuildExCh:
    def test_unknown_symbol_tries_both(self) -> None:
        source = TwsePublicMarketSource(symbols=["2330"])
        result = source._build_ex_ch(["2330"])
        assert "tse_2330.tw" in result
        assert "otc_2330.tw" in result

    def test_cached_symbol_uses_known_exchange(self) -> None:
        source = TwsePublicMarketSource(symbols=["2330"])
        source._exchange_map["2330"] = "tse"
        result = source._build_ex_ch(["2330"])
        assert result == "tse_2330.tw"
        assert "otc" not in result
