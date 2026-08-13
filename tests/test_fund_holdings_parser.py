"""共同基金持股解析（MoneyDJ）與公司名反查代號。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.company_info import lookup_symbol_by_name
from bot.etf_holdings_fetcher import fetch_and_save_holdings
from bot.fund_holdings_parser import (
    is_moneydj_fund_holdings_url,
    parse_moneydj_fund_holdings_html,
)
from bot.stock_db import StockInfo


SAMPLE_HTML = """
<html><body>
<table>
  <tr><td>安聯台灣科技基金-投資明細</td></tr>
  <tr><td>資料月份：2026/06/30</td></tr>
  <tr>
    <td>投資名稱</td><td>投資(千股)</td><td>比例</td><td>增減</td>
    <td>投資名稱</td><td>投資(千股)</td><td>比例</td><td>增減</td>
  </tr>
  <tr>
    <td>華邦電</td><td>108,514</td><td>7.44</td><td>1.11%</td>
    <td>欣銓</td><td>35,308</td><td>2.59</td><td></td>
  </tr>
  <tr>
    <td>旺矽</td><td>3,638</td><td>7.32</td><td>-0.70%</td>
    <td>國巨*</td><td>14,999</td><td>5.65</td><td></td>
  </tr>
  <tr>
    <td>台積電</td><td>7,999</td><td>6.37</td><td>-0.68%</td>
    <td>貿聯-KY</td><td>3,113</td><td>1.97</td><td></td>
  </tr>
</table>
</body></html>
"""


def _fake_map() -> dict[str, StockInfo]:
    rows = {
        "2344": ("華邦電子股份有限公司", "華邦電"),
        "3264": ("欣銓科技股份有限公司", "欣銓"),
        "6223": ("旺矽科技股份有限公司", "旺矽"),
        "2327": ("國巨股份有限公司", "國巨"),
        "2330": ("台灣積體電路製造股份有限公司", "台積電"),
        "3665": ("貿聯控股(開曼)有限公司", "貿聯-KY"),
    }
    return {
        sym: StockInfo(symbol=sym, name=full, short_name=short)
        for sym, (full, short) in rows.items()
    }


class TestUrlDetect:
    def test_moneydj_fund_url(self) -> None:
        assert is_moneydj_fund_holdings_url(
            "https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=acdd04"
        )
        assert is_moneydj_fund_holdings_url(
            "https://wwwfund.capital.com.tw/w/wr/wr04.djhtm?a=ACDD04-A003614"
        )
        assert not is_moneydj_fund_holdings_url(
            "https://www.moneydj.com/etf/x/basic/basic0007.xdjhtm?etfid=00402A.tw"
        )
        assert not is_moneydj_fund_holdings_url("https://www.etfinfo.tw/etf/00993A/holdings")


class TestLookupSymbolByName:
    def test_short_name_and_star_suffix(self) -> None:
        mp = _fake_map()
        assert lookup_symbol_by_name("華邦電", company_map=mp) == "2344"
        assert lookup_symbol_by_name("國巨*", company_map=mp) == "2327"
        assert lookup_symbol_by_name("貿聯-KY", company_map=mp) == "3665"
        assert lookup_symbol_by_name("不存在公司", company_map=mp) is None


class TestParseMoneyDjFund:
    def test_parse_dual_column_table(self) -> None:
        parsed = parse_moneydj_fund_holdings_html(
            SAMPLE_HTML, company_map=_fake_map(),
        )
        assert parsed.as_of is not None
        assert parsed.as_of.isoformat() == "2026-06-30"
        by_ticker = {h.ticker: h for h in parsed.holdings}
        assert set(by_ticker) == {"2344", "3264", "6223", "2327", "2330", "3665"}
        assert by_ticker["2344"].weight_pct == pytest.approx(7.44)
        # 千股 → 股
        assert by_ticker["2344"].shares == pytest.approx(108_514_000.0)
        assert by_ticker["2327"].name == "國巨"
        # 依權重降冪
        assert parsed.holdings[0].ticker == "2344"

    def test_unknown_name_keeps_company_as_ticker(self) -> None:
        html = SAMPLE_HTML.replace("華邦電", "未知神股")
        parsed = parse_moneydj_fund_holdings_html(html, company_map=_fake_map())
        tickers = {h.ticker for h in parsed.holdings}
        assert "未知神股" in tickers

    def test_nested_wrapper_table_not_double_counted(self) -> None:
        # 模擬 MoneyDJ：外層 table 包一層真正的持股表
        nested = f"<table><tr><td>標題</td></tr><tr><td>{SAMPLE_HTML}</td></tr></table>"
        parsed = parse_moneydj_fund_holdings_html(nested, company_map=_fake_map())
        by_ticker = {h.ticker: h for h in parsed.holdings}
        assert by_ticker["2344"].weight_pct == pytest.approx(7.44)
        assert sum(h.weight_pct for h in parsed.holdings) == pytest.approx(
            7.44 + 2.59 + 7.32 + 5.65 + 6.37 + 1.97
        )


class TestFetcherDeterministicPath:
    def test_fund_url_skips_llm(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from bot.active_etf import ActiveEtf

        class _Resp:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            text = SAMPLE_HTML
            apparent_encoding = "utf-8"
            content = SAMPLE_HTML.encode("utf-8")

            def raise_for_status(self) -> None:
                return None

        class _Sess:
            def get(self, *a, **k):
                return _Resp()

        class _Client:
            enabled = False  # 若誤走 LLM 會失敗

        monkeypatch.setattr(
            "bot.etf_holdings_fetcher.parse_moneydj_fund_holdings_html",
            lambda html, **kw: parse_moneydj_fund_holdings_html(
                html, company_map=_fake_map(),
            ),
        )

        etf = ActiveEtf(
            symbol="ALI006",
            name="安聯台灣科技基金",
            issuer="安聯投信",
            holdings_url="https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=acdd04",
        )
        result = fetch_and_save_holdings(
            etf, _Client(), root=tmp_path, session=_Sess(),  # type: ignore[arg-type]
        )
        assert result.success
        assert result.holdings_count == 6
        assert result.snapshot_date is not None
        assert result.snapshot_date.isoformat() == "2026-06-30"
        assert result.prompt_id == "fund_holdings_parser"
        saved = tmp_path / "data" / "etf_holdings" / "ALI006" / "2026-06-30.csv"
        assert saved.exists()
        text = saved.read_text(encoding="utf-8-sig")
        assert "2330" in text
        assert "2344" in text
