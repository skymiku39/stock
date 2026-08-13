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
        out = ms._fetch_material_openapi("2330", _Sess(), logging.getLogger("t"))
        assert len(out) == 1
        assert out[0].ticker == "2330"
        assert out[0].subject == "公告股利"

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
        assert len(DEFAULT_ACTIVE_ETFS) >= 27
        for e in DEFAULT_ACTIVE_ETFS:
            url = e.get("holdings_url", "")
            assert url, f"{e['symbol']} 缺 holdings_url"
            assert url.startswith("http"), f"{e['symbol']} URL 格式異常: {url}"

    def test_new_symbols_use_reachable_source(self) -> None:
        # 5 檔新掛牌與 00998A 應改走 moneydj (etfinfo 尚未收錄)
        from bot.active_etf import DEFAULT_ACTIVE_ETFS
        m = {e["symbol"]: e["holdings_url"] for e in DEFAULT_ACTIVE_ETFS}
        for sym in ("00402A", "00404A", "00405A", "00406A", "00407A", "00998A"):
            assert "moneydj" in m[sym].lower(), f"{sym} 仍指向失效來源"


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
