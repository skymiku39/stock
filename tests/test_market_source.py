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

    def test_poll_carries_source_document_id_when_rooted(self, tmp_path) -> None:
        from pathlib import Path

        import json
        from bot.stock_db import StockDB

        raw = json.dumps({"msgArray": SAMPLE_MSG_ARRAY[:1]}).encode("utf-8")

        class _Response:
            content = raw
            headers = {"Content-Type": "application/json"}
            status_code = 200
            url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

            def raise_for_status(self) -> None:
                return None

            def json(self):
                return json.loads(self.content.decode("utf-8"))

        class _Session:
            def get(self, _url, **_kwargs):
                return _Response()

        source = TwsePublicMarketSource(symbols=["2330"], root=tmp_path)
        source._session = _Session()
        source._initialized = True
        ticks = source.poll()
        assert len(ticks) == 1
        assert ticks[0].source_document_id.startswith("source-doc:")
        docs = StockDB.open(root=Path(tmp_path)).list_source_documents(
            source="twse_mis_quote",
        )
        assert any(doc.document_id == ticks[0].source_document_id for doc in docs)

    def test_poll_source_document_id_empty_without_root(self) -> None:
        class _Response:
            content = b'{"msgArray":[{"c":"2330","z":"600.00","v":"15000","y":"580.00","t":"13:30:00","ex":"tse"}]}'
            headers = {"Content-Type": "application/json"}
            status_code = 200
            url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

            def raise_for_status(self) -> None:
                return None

            def json(self):
                import json
                return json.loads(self.content.decode("utf-8"))

        class _Session:
            def get(self, _url, **_kwargs):
                return _Response()

        source = TwsePublicMarketSource(symbols=["2330"], root=None)
        source._session = _Session()
        source._initialized = True
        ticks = source.poll()
        assert len(ticks) == 1
        assert ticks[0].source_document_id == ""


class TestGetQuotes:
    def test_uses_last_trade_when_available(self) -> None:
        source = TwsePublicMarketSource(symbols=["2330"])
        source._last_source_document_id = "source-doc:twse-mis:fixture"
        with patch.object(source, "_fetch_raw", return_value=SAMPLE_MSG_ARRAY[:1]):
            quotes = source.get_quotes()

        quote = quotes["2330"]
        assert quote["price"] == 600.0
        assert quote["price_basis"] == "last_trade"
        assert quote["pct_chg"] == pytest.approx(3.45, abs=0.01)
        assert quote["source_document_id"] == "source-doc:twse-mis:fixture"

    def test_uses_bid_ask_mid_when_last_trade_missing(self) -> None:
        items = [{
            "c": "2382",
            "z": "-",
            "pz": "-",
            "b": "405.0000_404.5000_",
            "a": "406.0000_406.5000_",
            "v": "38143",
            "y": "417.0000",
            "d": "20260604",
            "t": "11:13:36",
            "n": "廣達",
            "ex": "tse",
        }]
        source = TwsePublicMarketSource(symbols=["2382"])
        with patch.object(source, "_fetch_raw", return_value=items):
            quotes = source.get_quotes()

        quote = quotes["2382"]
        assert quote["price"] == 405.5
        assert quote["best_bid"] == 405.0
        assert quote["best_ask"] == 406.0
        assert quote["price_basis"] == "bid_ask_mid"
        assert quote["pct_chg"] == pytest.approx(-2.76, abs=0.01)

    @staticmethod
    def _book_item(b: str, a: str, z: str = "-", pz: str = "-") -> dict:
        return {
            "c": "1234", "z": z, "pz": pz, "b": b, "a": a,
            "v": "100", "y": "70.0000", "d": "20261125",
            "t": "10:00:00", "n": "測試", "ex": "tse",
        }

    def _quote(self, item: dict) -> dict:
        source = TwsePublicMarketSource(symbols=["1234"])
        with patch.object(source, "_fetch_raw", return_value=[item]):
            return source.get_quotes()["1234"]

    def test_zero_bid_is_not_a_book_price(self) -> None:
        quote = self._quote(self._book_item("0_76.4000_", "76.4000_76.5000_"))
        assert quote["best_bid"] is None
        assert quote["best_ask"] == 76.4
        assert quote["price"] != 38.2
        assert quote["price"] == 76.4
        assert quote["price_basis"] == "best_ask"

    def test_normal_two_sided_book_mid_unchanged(self) -> None:
        quote = self._quote(self._book_item("76.3000_76.2000_", "76.4000_76.5000_"))
        assert quote["price"] == pytest.approx(76.35)
        assert quote["price_basis"] == "bid_ask_mid"

    def test_both_sides_zero_gives_no_price(self) -> None:
        quote = self._quote(self._book_item("0_", "0.0000_"))
        assert quote["best_bid"] is None
        assert quote["best_ask"] is None
        assert quote["price"] is None
        assert quote["price_basis"] == ""
        assert quote["pct_chg"] is None

    def test_zero_last_and_previous_trade_fall_through(self) -> None:
        quote = self._quote(self._book_item(
            "76.3000_", "76.4000_", z="0.0000", pz="0",
        ))
        assert quote["price"] == pytest.approx(76.35)
        assert quote["price_basis"] == "bid_ask_mid"

    def test_open_and_last_trade_are_extra_fields(self) -> None:
        quote = self._quote({**self._book_item("72.1000_", "72.2000_", z="72.3000"),
                             "o": "72.2000"})
        assert quote["open"] == 72.2
        assert quote["last_trade"] == 72.3
        assert quote["price"] == 72.3 and quote["price_basis"] == "last_trade"  # 既有欄位不變

    def test_missing_or_zero_open_is_none(self) -> None:
        quote = self._quote(self._book_item("76.3000_", "76.4000_"))
        assert quote["open"] is None and quote["last_trade"] is None
        assert quote["price_basis"] == "bid_ask_mid"
        quote = self._quote({**self._book_item("76.3000_", "76.4000_"), "o": "0.0000"})
        assert quote["open"] is None


class TestPollZeroPrice:
    def test_zero_last_trade_is_skipped(self) -> None:
        items = [{"c": "1234", "z": "0.0000", "v": "100", "y": "70.00", "ex": "tse"}]
        source = TwsePublicMarketSource(symbols=["1234"])
        with patch.object(source, "_fetch_raw", return_value=items):
            assert source.poll() == []


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
