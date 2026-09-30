"""資料源修復後的回歸測試 (全程不發 HTTP，網路以 mock 取代)。

覆蓋：
* 股利 t187ap45 欄位解析 + 同年度彙總 + 上櫃 CSV 解析
* MOPS 安全性阻擋偵測 + 民國日期 + 重大訊息 OpenAPI 解析
* ETF 清單 URL 格式 (link 層)
* 台指期正逆價差 state 判定
* validate_sources 報表 (summarize / markdown / 失敗判定)
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest


# ----------------------------------------------------------------------
# 股利 (t187ap45_L / t187ap45_O)
# ----------------------------------------------------------------------


def _div_row(code: str, year_roc: str, cash: str, stock: str = "0.0") -> dict:
    return {
        "公司代號": code,
        "公司名稱": "測試",
        "股利年度": year_roc,
        "股東配發-盈餘分配之現金股利(元/股)": cash,
        "股東配發-法定盈餘公積發放之現金(元/股)": "0.0",
        "股東配發-資本公積發放之現金(元/股)": "0.0",
        "股東配發-盈餘轉增資配股(元/股)": stock,
        "股東配發-法定盈餘公積轉增資配股(元/股)": "0.0",
        "股東配發-資本公積轉增資配股(元/股)": "0.0",
    }


class TestDividendParse:
    def test_parse_new_fields(self) -> None:
        from bot.fundamentals_fetcher import _parse_dividend
        rec = _parse_dividend(_div_row("2330", "114", "16.0", "0.0"), "2330")
        assert rec is not None
        assert rec.year == 2025  # 民國 114 → 西元 2025
        assert rec.cash_dividend == pytest.approx(16.0)
        assert rec.stock_dividend == pytest.approx(0.0)

    def test_parse_sums_cash_components(self) -> None:
        from bot.fundamentals_fetcher import _parse_dividend
        raw = _div_row("2317", "114", "5.0")
        raw["股東配發-資本公積發放之現金(元/股)"] = "2.2"
        rec = _parse_dividend(raw, "2317")
        assert rec is not None
        assert rec.cash_dividend == pytest.approx(7.2)

    def test_parse_wrong_ticker_returns_none(self) -> None:
        from bot.fundamentals_fetcher import _parse_dividend
        assert _parse_dividend(_div_row("2330", "114", "1.0"), "9999") is None

    def test_fetch_dividends_aggregates_quarterly_by_year(self, tmp_path: Path) -> None:
        """同一股利年度多筆 (季配) → 現金股利加總。"""
        from bot import fundamentals_fetcher as ff
        rows = [
            _div_row("2330", "114", "3.0"),  # 第1季
            _div_row("2330", "114", "3.5"),  # 第2季
            _div_row("2330", "114", "4.0"),  # 第3季
            _div_row("2330", "113", "11.0"),  # 前一年度
        ]
        with patch.object(ff, "fetch_dividend_all", return_value=rows):
            recs = ff.fetch_dividends("2330", root=tmp_path)
        by_year = {r.year: r for r in recs}
        assert by_year[2025].cash_dividend == pytest.approx(10.5)  # 3+3.5+4
        assert by_year[2024].cash_dividend == pytest.approx(11.0)


def test_daily_chips_cache_only_never_fetches_missing_cache(tmp_path: Path) -> None:
    from bot.chips_fetcher import fetch_daily_chips

    with patch("bot.chips_fetcher.requests.Session.get") as get:
        rows = fetch_daily_chips(
            dt.date(2026, 9, 17), root=tmp_path, cache_only=True,
        )

    assert rows == {}
    get.assert_not_called()


def test_daily_chips_cache_only_falls_back_to_latest_saved_trading_day(
    tmp_path: Path,
) -> None:
    from bot.chips_fetcher import fetch_daily_chips

    cache = tmp_path / "data" / "chips" / "2026-09-17"
    cache.mkdir(parents=True)
    (cache / "qfii_daily.json").write_text(
        '{"fields":["證券代號","證券名稱","外資及陸資買賣超股數",'
        '"投信買賣超股數","自營商買賣超股數"],'
        '"data":[["2330","台積電","1000","2000","-500"]]}',
        encoding="utf-8",
    )

    with patch("bot.chips_fetcher.requests.Session.get") as get:
        rows = fetch_daily_chips(
            dt.date(2026, 9, 19), root=tmp_path, cache_only=True,
        )

    assert rows["2330"].foreign_net == pytest.approx(1.0)
    assert rows["2330"].investment_trust_net == pytest.approx(2.0)
    get.assert_not_called()


def test_chips_endpoint_failure_is_not_retried_for_same_session_and_date(
    tmp_path: Path,
) -> None:
    """同一次研究執行不應為每個 ticker 重複打相同失敗端點。"""
    from bot.chips_fetcher import _fetch_endpoint

    class _Response:
        status_code = 200
        content = b'{"stat":"no data"}'

        def json(self):
            return {"stat": "很抱歉，查無資料"}

    class _Session:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return _Response()

    session = _Session()
    day = dt.date(2026, 9, 21)
    assert _fetch_endpoint(
        "qfii_daily", day, session=session, root=tmp_path, use_cache=False,
    ) is None
    assert _fetch_endpoint(
        "qfii_daily", day, session=session, root=tmp_path, use_cache=False,
    ) is None
    assert session.calls == 1  # 第二次呼叫命中 process cache，不再重試


def test_chips_endpoint_failure_is_suppressed_across_sessions(
    tmp_path: Path,
) -> None:
    """不同 ticker/session 也不應在同一 process 重打明確失敗的端點。"""
    from bot.chips_fetcher import _fetch_endpoint

    class _Response:
        status_code = 200
        content = b'{"stat":"no data"}'

        def json(self):
            return {"stat": "很抱歉，查無資料"}

    class _Session:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return _Response()

    day = dt.date(2026, 9, 21)
    first = _Session()
    second = _Session()
    assert _fetch_endpoint(
        "margin_balance", day, session=first, root=tmp_path, use_cache=False,
    ) is None
    assert _fetch_endpoint(
        "margin_balance", day, session=second, root=tmp_path, use_cache=False,
    ) is None

    assert first.calls == 1
    assert second.calls == 0
    assert not list((tmp_path / "data" / "chips").rglob("*.json"))


def test_chips_endpoint_failure_cache_expires(monkeypatch, tmp_path: Path) -> None:
    """短期 failure cache 到期後仍允許重新確認上游。"""
    from bot import chips_fetcher as module

    class _Response:
        status_code = 200
        content = b'{"stat":"no data"}'

        def json(self):
            return {"stat": "很抱歉，查無資料"}

    class _Session:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return _Response()

    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    day = dt.date(2026, 9, 20)
    first = _Session()
    second = _Session()
    assert module._fetch_endpoint(
        "block_trade", day, session=first, root=tmp_path, use_cache=False,
    ) is None
    assert module._fetch_endpoint(
        "block_trade", day, session=second, root=tmp_path, use_cache=False,
    ) is None
    assert second.calls == 0

    clock[0] += module._PROCESS_FAILURE_CACHE_TTL_SECONDS + 1
    third = _Session()
    assert module._fetch_endpoint(
        "block_trade", day, session=third, root=tmp_path, use_cache=False,
    ) is None
    assert third.calls == 1

    def test_tpex_csv_parser(self) -> None:
        from bot.fundamentals_fetcher import _parse_tpex_dividend_csv
        csv_text = (
            '"公司代號","股利年度","股東配發-盈餘分配之現金股利(元/股)"\n'
            '"6488","114","8.5"\n'
        )
        rows = _parse_tpex_dividend_csv(csv_text)
        assert len(rows) == 1
        assert rows[0]["公司代號"] == "6488"
        assert rows[0]["股東配發-盈餘分配之現金股利(元/股)"] == "8.5"


# ----------------------------------------------------------------------
# MOPS
# ----------------------------------------------------------------------


class TestMopsHelpers:
    def test_security_block_detected(self) -> None:
        from bot.mops_scraper import _is_security_block
        assert _is_security_block("FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED")
        assert not _is_security_block("<table><tr><td>1101</td></tr></table>")

    def test_roc_to_date(self) -> None:
        from bot.mops_scraper import _roc_to_date
        assert _roc_to_date("1150601") == dt.date(2026, 6, 1)   # 民國 7 碼
        assert _roc_to_date("20260601") == dt.date(2026, 6, 1)  # 西元 8 碼
        assert _roc_to_date("") is None

    def test_material_openapi_filters_ticker(self) -> None:
        from bot import mops_scraper as ms

        class _Resp:
            status_code = 200

            def json(self):
                return [
                    {"公司代號": "2330", "公司名稱": "台積電", "發言日期": "1150601",
                     "發言時間": "08:00", "主旨 ": "公告股利"},
                    {"公司代號": "2317", "公司名稱": "鴻海", "發言日期": "1150601",
                     "主旨": "其他"},
                ]

        class _Sess:
            def get(self, *a, **k):
                return _Resp()

        import logging
        stats = {}
        out = ms._fetch_material_openapi(
            "2330", _Sess(), logging.getLogger("t"), stats=stats,
        )
        assert len(out) == 1
        assert out[0].ticker == "2330"
        assert out[0].subject == "公告股利"
        assert stats["openapi_valid"] is True
        assert stats["openapi_row_count"] == 2

    def test_material_openapi_failure_is_not_empty_success(self) -> None:
        from bot import mops_scraper as ms

        class _Sess:
            def get(self, *a, **k):
                raise TimeoutError("test timeout")

        import logging
        stats = {}
        out = ms.fetch_material_info(
            "2330",
            session=_Sess(),
            logger=logging.getLogger("t"),
            merge_sources=False,
            stats=stats,
        )

        assert out == []
        assert stats["openapi_valid"] is False
        assert stats["openapi_row_count"] == 0
        assert "TimeoutError" in stats["openapi_error"]

    def test_host_is_overridable(self) -> None:
        # 預設應走 mopsov，且 URL 由 MOPS_HOST 組成
        from bot.mops_scraper import MOPS_CONF_URL, MOPS_HOST
        assert MOPS_CONF_URL.startswith(MOPS_HOST)
        assert "ajax_t100sb02_1" in MOPS_CONF_URL


# ----------------------------------------------------------------------
# ETF 清單 (link 層)
# ----------------------------------------------------------------------


class TestActiveEtfUrls:
    def test_all_default_urls_present_and_valid(self) -> None:
        from bot.active_etf import DEFAULT_ACTIVE_ETFS
        assert len(DEFAULT_ACTIVE_ETFS) >= 28
        for e in DEFAULT_ACTIVE_ETFS:
            url = e.get("holdings_url", "")
            assert url, f"{e['symbol']} 缺 holdings_url"
            assert url.startswith("http"), f"{e['symbol']} URL 格式異常: {url}"

    def test_new_symbols_use_reachable_source(self) -> None:
        # 這些新掛牌 ETF 現在已有 etfinfo.tw 的完整 SSR holdings payload。
        from bot.active_etf import DEFAULT_ACTIVE_ETFS
        m = {e["symbol"]: e["holdings_url"] for e in DEFAULT_ACTIVE_ETFS}
        for sym in ("00402A", "00404A", "00405A", "00406A", "00407A", "00998A"):
            assert "etfinfo.tw" in m[sym].lower(), f"{sym} 未指向完整 holdings 來源"

    def test_allianz_taiwan_tech_fund_listed(self) -> None:
        from bot.active_etf import DEFAULT_ACTIVE_ETFS
        m = {e["symbol"]: e for e in DEFAULT_ACTIVE_ETFS}
        assert "ALI006" in m
        assert "安聯台灣科技" in m["ALI006"]["name"]
        assert m["ALI006"]["issuer"] == "安聯投信"
        url = m["ALI006"]["holdings_url"].lower()
        assert "moneydj.com" in url
        assert "yp013000" in url
        assert "acdd04" in url

    def test_sync_metadata_is_config_only_and_preserves_external_rows(self, tmp_path) -> None:
        from bot.active_etf import ActiveEtf, sync_etf_meta_to_db
        from bot.stock_db import EtfMeta, StockDB

        db = StockDB.open(root=tmp_path)
        try:
            result = sync_etf_meta_to_db(
                tmp_path,
                db=db,
                etfs=[ActiveEtf(
                    "00999A", "測試 ETF", "測試投信",
                    holdings_url="https://example.test/holdings",
                )],
            )
            assert result["created"] == 1
            row = db.get_etf_meta("00999A")
            assert row is not None
            assert row.quality_status == "unknown"
            assert row.source_record_id == ""
            assert row.source_url == "config://default_active_etfs"

            again = sync_etf_meta_to_db(
                tmp_path,
                db=db,
                etfs=[ActiveEtf(
                    "00999A", "測試 ETF", "測試投信",
                    holdings_url="https://example.test/holdings",
                )],
            )
            assert again["unchanged"] == 1

            db.upsert_etf_meta(EtfMeta(
                symbol="00999A",
                name="官方名稱",
                manager="官方投信",
                source_url="https://official.example/etf",
                source_record_id="source-doc-1",
                quality_status="ok",
            ))
            preserved = sync_etf_meta_to_db(
                tmp_path,
                db=db,
                etfs=[ActiveEtf("00999A", "設定名稱", "設定投信")],
            )
            assert preserved["preserved_external"] == 1
            assert db.get_etf_meta("00999A").name == "官方名稱"
        finally:
            db.close()


# ----------------------------------------------------------------------
# 台指期正逆價差
# ----------------------------------------------------------------------


class TestFuturesBasis:
    def _fake_get(self, rows):
        class _Resp:
            status_code = 200

            def json(self_inner):
                return rows
        def _get(*a, **k):
            return _Resp()
        return _get

    def test_premium_state(self) -> None:
        from bot import market_macro as mm
        rows = [
            {"Contract": "TX", "ContractMonth(Week)": "202606", "Last": "22100",
             "SettlementPrice": "22090", "OpenInterest": "90000"},
            {"Contract": "TX", "ContractMonth(Week)": "202607", "Last": "22050",
             "SettlementPrice": "22040", "OpenInterest": "5000"},
            {"Contract": "TX", "ContractMonth(Week)": "202606W2", "Last": "22120",
             "SettlementPrice": "22110", "OpenInterest": "1000"},  # 週合約應被排除
        ]
        with patch("requests.get", side_effect=self._fake_get(rows)):
            fb = mm.fetch_tx_futures_basis(22000.0)
        assert fb is not None
        assert fb.contract_month == "202606"   # 近月
        assert fb.futures_price == pytest.approx(22100.0)
        assert fb.basis == pytest.approx(100.0)
        assert fb.state == "正價差"

    def test_discount_state(self) -> None:
        from bot import market_macro as mm
        rows = [{"Contract": "TX", "ContractMonth(Week)": "202606", "Last": "21900",
                 "SettlementPrice": "21900", "OpenInterest": "90000"}]
        with patch("requests.get", side_effect=self._fake_get(rows)):
            fb = mm.fetch_tx_futures_basis(22000.0)
        assert fb is not None
        assert fb.basis == pytest.approx(-100.0)
        assert fb.state == "逆價差"

    def test_no_tx_rows_returns_none(self) -> None:
        from bot import market_macro as mm
        rows = [{"Contract": "MTX", "ContractMonth(Week)": "202606", "Last": "22000"}]
        with patch("requests.get", side_effect=self._fake_get(rows)):
            assert mm.fetch_tx_futures_basis(22000.0) is None

    def test_utf8_csv_response_is_supported(self) -> None:
        from bot import market_macro as mm

        class _Resp:
            status_code = 200
            content = (
                "\ufeff日期,契約代號,到期月份(週別),最後成交價,結算價,未沖銷契約數\n"
                "20260918,TX,202610,22100,22090,90000\n"
                "20260918,TX,202610W2,22120,22110,1000\n"
            ).encode("utf-8")

            def json(self):
                raise ValueError("CSV response")

        with patch("requests.get", return_value=_Resp()):
            fb = mm.fetch_tx_futures_basis(22000.0)
        assert fb is not None
        assert fb.contract_month == "202610"
        assert fb.futures_price == pytest.approx(22100.0)
        assert fb.open_interest == pytest.approx(90000.0)

    def test_csv_response_keeps_exact_source_document(self, tmp_path) -> None:
        from bot import market_macro as mm
        from bot.stock_db import StockDB

        class _Resp:
            status_code = 200
            headers = {"content-type": "application/octet-stream"}
            content = (
                "\ufeff日期,契約代號,到期月份(週別),最後成交價,結算價,未沖銷契約數\n"
                "20260918,TX,202610,22100,22090,90000\n"
            ).encode("utf-8")

            def json(self):
                raise ValueError("CSV response")

        with patch("requests.get", return_value=_Resp()):
            fb = mm.fetch_tx_futures_basis(22000.0, root=tmp_path)
        assert fb is not None
        assert fb.source_document_id
        db = StockDB.open(root=tmp_path)
        try:
            documents = db.list_source_documents(
                source="taifex_daily_fut", kind="http_response", limit=10,
            )
            assert any(row.document_id == fb.source_document_id for row in documents)
        finally:
            db.close()

    def test_403_replays_hash_verified_exact_cache_as_partial(self, tmp_path) -> None:
        from bot import market_macro as mm
        from bot.source_registry import SourceRegistry
        from bot.stock_db import StockDB

        raw = (
            "\ufeff日期,契約代號,到期月份(週別),最後成交價,結算價,未沖銷契約數\n"
            "20260918,TX,202610,22100,22090,90000\n"
        ).encode("utf-8")
        with SourceRegistry(tmp_path) as registry:
            document = registry.capture_document(
                "taifex_daily_fut",
                raw,
                source_url=mm.URL_TAIFEX_DAILY_FUT,
                source_ref="taifex:20260918:202610",
                title="TAIFEX TX daily futures 20260918",
                kind="http_response",
                suffix="csv",
                mime_type="text/csv; charset=utf-8",
                as_of="20260918",
            )

        class _Forbidden:
            status_code = 403
            content = b"cloudflare"
            headers = {"content-type": "text/html"}

        with patch("requests.get", return_value=_Forbidden()):
            basis = mm.fetch_tx_futures_basis(22000.0, root=tmp_path)

        assert basis is not None
        assert basis.cached is True
        assert basis.quality_status == "partial"
        assert basis.source_document_id == document.document_id
        assert basis.asof_date == "20260918"
        assert basis.futures_price == pytest.approx(22100.0)
        assert "exact local raw archive" in basis.note

        db = StockDB.open(root=tmp_path)
        try:
            assert any(
                row.source_document_id == document.document_id
                for row in db.list_research_documents(limit=20)
            )
        finally:
            db.close()


class TestMacroFreshness:
    def test_source_date_classification_is_explicit(self) -> None:
        from bot.market_macro import classify_source_dates

        assert classify_source_dates({}) == "unknown"
        assert classify_source_dates({"adr": "2026-09-16", "tw": "2026-09-16"}) == "aligned"
        assert classify_source_dates({"adr": "2026-09-16", "tw": "2026-09-17"}) == "mixed"

    def test_yfinance_quote_keeps_actual_history_date(self) -> None:
        from bot import market_macro as mm

        class _Ticker:
            def history(self, **_kwargs):
                return pd.DataFrame(
                    {"Close": [100.0, 101.5], "Volume": [10, 20]},
                    index=pd.to_datetime(["2026-09-15", "2026-09-16"]),
                )

        class _YF:
            def Ticker(self, _symbol):
                return _Ticker()

        quote = mm._safe_quote(_YF(), "^TWII", mm.get_logger("test-macro"))

        assert quote is not None
        assert quote["source_date"] == "2026-09-16"
        assert quote["pct_change"] == pytest.approx(1.5)

    def test_empty_twse_close_response_is_retryable(self, monkeypatch) -> None:
        from bot import market_macro as mm

        class _Resp:
            status_code = 200

            def __init__(self, rows):
                self._rows = rows

            def json(self):
                return self._rows

        responses = iter([_Resp([]), _Resp([{"Code": "2330", "ClosingPrice": "100"}])])
        monkeypatch.setattr("requests.get", lambda *args, **kwargs: next(responses))
        monkeypatch.setattr(mm, "_TW_CLOSE_CACHE", {})
        monkeypatch.setattr(mm, "_TW_CLOSE_CACHE_LOADED", False)

        logger = mm.get_logger("test-macro")
        assert mm._load_twse_close_map(logger) == {}
        assert mm._TW_CLOSE_CACHE_LOADED is False
        assert mm._load_twse_close_map(logger) == {"2330": 100.0}
        assert mm._TW_CLOSE_CACHE_LOADED is True

    def test_adr_spot_quote_keeps_source_and_observation_date(self) -> None:
        from bot import market_macro as mm

        class _Ticker:
            def history(self, **_kwargs):
                return pd.DataFrame(
                    {"Close": [100.0, 101.0], "Volume": [10, 20]},
                    index=pd.to_datetime(["2026-09-15", "2026-09-16"]),
                )

        class _YF:
            def Ticker(self, _symbol):
                return _Ticker()

        with patch.object(mm, "_import_yf", return_value=_YF()):
            price, source_date, source = mm._fetch_tw_close_with_meta(
                "2330", mm.get_logger("test-macro"),
            )

        assert price == pytest.approx(101.0)
        assert source_date == "2026-09-16"
        assert source == "yfinance:2330.TW"


# ----------------------------------------------------------------------
# TWSE 籌碼欄位映射
# ----------------------------------------------------------------------


class TestBorrowParser:
    def test_uses_current_balance_not_previous_balance(self) -> None:
        from bot.chips_fetcher import ENDPOINTS, _parse_borrow

        fields = [
            "代號", "名稱", "前日餘額", "賣出", "買進", "現券", "今日餘額",
            "次一營業日限額", "前日餘額", "當日賣出", "當日還券", "當日調整",
            "當日餘額", "次一營業日可限額", "備註",
        ]
        data = {
            "fields": fields,
            "data": [[
                "2330", "台積電", "1000", "2", "3", "4", "1001", "0",
                "2000", "10", "1", "0", "2009", "0", "",
            ]],
        }

        result = _parse_borrow(data)

        assert "marginTrading/TWT93U" in ENDPOINTS["borrow_balance"]
        assert result["2330"] == pytest.approx(2.009)


def test_quarterly_fetch_drops_blank_footer_rows(tmp_path: Path) -> None:
    from bot import fundamentals_fetcher as ff

    class _Resp:
        status_code = 200

        def json(self):
            return [
                {"公司代號": "2330", "年度": "115", "季別": "2"},
                {},
            ]

    class _Sess:
        def get(self, *args, **kwargs):
            return _Resp()

    rows = ff.fetch_quarterly_financials_all(
        root=tmp_path,
        session=_Sess(),
        use_cache=False,
    )

    assert len(rows) == 12
    assert all(row.get("公司代號") == "2330" for row in rows)
    assert all(row.get("年度") == "115" for row in rows)


# ----------------------------------------------------------------------
# validate_sources 報表
# ----------------------------------------------------------------------


class TestValidateReport:
    def test_summarize_counts(self) -> None:
        from bot.validate_sources import CheckResult, summarize
        results = [
            CheckResult(name="a", ok=True),
            CheckResult(name="b", ok=False, error="boom"),         # fail
            CheckResult(name="c", ok=False, note="未啟用"),         # warn
        ]
        s = summarize(results)
        assert s["total"] == 3
        assert s["ok"] == 1
        assert s["fail"] == 1
        assert s["warn"] == 1

    def test_markdown_has_rows(self) -> None:
        from bot.validate_sources import CheckResult, summarize, to_markdown
        md = to_markdown(summarize([CheckResult(name="x", ok=True, rows=5)]))
        assert "資料源驗證報告" in md
        assert "| x |" in md

    def test_main_returns_nonzero_on_failure(self) -> None:
        # mock run_all 回一個失敗結果，main 應回 1
        from bot import validate_sources as vs
        from bot.validate_sources import CheckResult
        with patch.object(vs, "run_all", return_value=[CheckResult(name="x", ok=False, error="e")]):
            rc = vs.main([])
        assert rc == 1

    def test_main_creates_parent_directories_for_reports(self, tmp_path: Path) -> None:
        from bot import validate_sources as vs
        from bot.validate_sources import CheckResult

        json_path = tmp_path / "nested" / "reports" / "source.json"
        md_path = tmp_path / "nested" / "reports" / "source.md"
        with patch.object(vs, "run_all", return_value=[CheckResult(name="x", ok=True)]):
            rc = vs.main(["--json", str(json_path), "--md", str(md_path)])

        assert rc == 0
        assert json_path.is_file()
        assert md_path.is_file()


def test_company_info_check_requires_both_markets() -> None:
    from types import SimpleNamespace

    from bot import company_info as ci
    from bot.validate_sources import check_company_info

    rows = {
        "2330": SimpleNamespace(symbol="2330", name="台積電", market="TWSE"),
        "6488": SimpleNamespace(symbol="6488", name="環球晶", market="TPEX"),
    }
    with patch.object(ci, "_build_company_map", return_value=rows):
        result = check_company_info()

    assert result.ok
    assert result.quality_status == "ok"
    assert result.details["market_counts"] == {"TWSE": 1, "TPEX": 1}


def test_daily_kline_check_validates_twse_and_tpex() -> None:
    from bot import technicals
    from bot.validate_sources import check_daily_kline

    def fake_fetch(*args, **kwargs):
        return [{
            "date": "2000-01-03",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 100,
        }]

    with patch.object(technicals, "fetch_monthly_kline", side_effect=fake_fetch):
        result = check_daily_kline()

    assert result.ok
    assert result.rows == 2
    assert result.quality_status == "ok"


def test_validate_run_all_forwards_isolated_root_to_collectors(tmp_path: Path) -> None:
    """驗證 smoke test 不會把 raw/provenance 寫回正式工作樹。"""
    from bot import validate_sources as vs
    from bot.validate_sources import CheckResult

    root_aware = (
        "check_company_info",
        "check_daily_kline",
        "check_monthly_revenue",
        "check_valuation",
        "check_dividends",
        "check_quarterly",
        "check_mops_conference",
        "check_global_events",
        "check_mops_material",
        "check_chips",
        "check_distribution",
        "check_news",
        "check_web_search",
        "check_macro",
        "check_futures_basis",
        "check_etf_urls",
    )
    patches = []
    mocks = []
    try:
        for name in root_aware:
            fn = patch.object(
                vs,
                name,
                side_effect=(
                    (lambda root: [CheckResult(name=name, ok=True)])
                    if name == "check_etf_urls"
                    else (lambda root: CheckResult(name=name, ok=True))
                ),
            )
            patches.append(fn)
            mocks.append(fn.start())
        with patch.object(vs, "check_shioaji", return_value=[]), \
             patch.object(vs, "check_cloud", return_value=CheckResult(name="cloud", ok=True)):
            vs.run_all(root=tmp_path)
        for mock in mocks:
            mock.assert_called_once_with(tmp_path)
    finally:
        for fn in reversed(patches):
            fn.stop()


def test_validate_run_all_cleans_only_its_own_temporary_root(tmp_path: Path) -> None:
    from bot import validate_sources as vs

    generated_root = tmp_path / "generated-validate-root"
    with patch.object(vs.tempfile, "mkdtemp", return_value=str(generated_root)) as mkdtemp, \
         patch.object(vs.shutil, "rmtree") as rmtree, \
         patch.object(vs, "_run_all_checks", return_value=[]):
        assert vs.run_all() == []

    mkdtemp.assert_called_once_with(prefix="validate_sources_")
    rmtree.assert_called_once_with(generated_root, ignore_errors=True)

    with patch.object(vs.tempfile, "mkdtemp") as mkdtemp, \
         patch.object(vs, "_run_all_checks", return_value=[]):
        assert vs.run_all(root=tmp_path) == []
    mkdtemp.assert_not_called()


def test_dividend_fetch_uses_stale_cache_on_source_block(tmp_path: Path) -> None:
    import json

    from bot import fundamentals_fetcher as ff

    cache = tmp_path / "data" / "fundamentals" / "dividends_all.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps([{"Code": "2330"}]), encoding="utf-8")

    class _Resp:
        status_code = 429
        text = "\u60a8\u7684\u700f\u89bd\u91cf\u7570\u5e38"

        def json(self):
            return []

    class _Sess:
        def get(self, *args, **kwargs):
            return _Resp()

    rows = ff.fetch_dividend_all(
        root=tmp_path,
        session=_Sess(),
        use_cache=True,
        cache_ttl=-1,
    )

    assert rows == [{"Code": "2330"}]
    state = json.loads((tmp_path / "data" / "fundamentals" / "dividends_source_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["next_attempt_at"]


def test_dividend_fetch_backoff_skips_http(tmp_path: Path) -> None:
    import json

    from bot import fundamentals_fetcher as ff
    from bot.utils import now_tw

    base = tmp_path / "data" / "fundamentals"
    base.mkdir(parents=True)
    (base / "dividends_all.json").write_text(json.dumps([{"Code": "2330"}]), encoding="utf-8")
    (base / "dividends_source_state.json").write_text(
        json.dumps({
            "status": "failed",
            "fail_count": 1,
            "next_attempt_at": (now_tw() + dt.timedelta(hours=1)).isoformat(timespec="seconds"),
        }),
        encoding="utf-8",
    )

    class _Sess:
        calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("HTTP should not be called during backoff")

    sess = _Sess()
    rows = ff.fetch_dividend_all(
        root=tmp_path,
        session=sess,
        use_cache=True,
        cache_ttl=-1,
    )

    assert rows == [{"Code": "2330"}]
    assert sess.calls == 0
