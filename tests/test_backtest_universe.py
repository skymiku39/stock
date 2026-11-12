"""回測標的選取：解析、排除規則、可重現排序、快取。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from bot.backtest_universe import (
    build_universe_file,
    is_common_stock,
    load_universe,
    parse_tpex_daily,
    parse_twse_daily,
    select_universe,
)


def _twse(rows):
    return {"tables": [{"fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額"],
                        "data": [[c, n, "1", "1", f"{a:,}"] for c, n, a in rows]}]}


def _tpex(rows):
    return {"tables": [{"fields": ["代號", "名稱", "收盤 ", " 成交金額(元)"],
                        "data": [[c, n, "1", f"{a:,}"] for c, n, a in rows]}]}


def test_is_common_stock():
    assert is_common_stock("2317") and is_common_stock("6488")
    assert not is_common_stock("0050") and not is_common_stock("00679B")
    assert not is_common_stock("030001") and not is_common_stock("9103")
    assert not is_common_stock("2882A")


def test_parse_both_markets_and_holiday():
    assert parse_twse_daily(_twse([("2317", "鴻海", 1_000)])) == {"2317": ("鴻海", 1000.0)}
    assert parse_tpex_daily(_tpex([("6488", "環球晶", 2_500)])) == {"6488": ("環球晶", 2500.0)}
    assert parse_twse_daily({"stat": "很抱歉，沒有符合條件的資料!"}) == {}


def test_select_excludes_2330_etf_and_is_deterministic():
    totals = {"2330": 9e12, "0050": 5e12, "2317": 100.0, "2454": 100.0, "1101": 50.0}
    names = {k: k for k in totals}
    picked = select_universe(totals, names, 10, count=2)
    assert [r["symbol"] for r in picked] == ["2317", "2454"]  # 同額以代號排序
    assert picked[0]["avg_daily_turnover_twd"] == 10


def test_build_uses_year_data_and_caches(tmp_path: Path):
    calls = []

    def fake(url, session=None):
        calls.append(url)
        day = url.split("date=")[1][:10].replace("/", "").replace("&", "")[:8]
        if "twse" in url:
            if day == "20200102":
                return _twse([("2317", "鴻海", 300), ("2330", "台積電", 999)])
            if day == "20200103":
                return _twse([("2317", "鴻海", 100), ("2603", "長榮", 500)])
            return {}
        if day == "20200102":
            return _tpex([("6488", "環球晶", 200)])
        return _tpex([("6488", "環球晶", 50)])

    out = tmp_path / "u.json"
    doc = build_universe_file(2020, tmp_path / "raw", out, count=2, delay=0, fetcher=fake)
    assert doc["trading_days"] == 2
    # 2603 合計 500、2317 合計 400、6488 合計 250；2330 被排除
    assert load_universe(out) == ["2603", "2317"]
    assert doc["symbols"][0]["avg_daily_turnover_twd"] == 250
    n = len(calls)
    build_universe_file(2020, tmp_path / "raw", out, count=2, delay=0, fetcher=fake)
    assert len(calls) == n  # 原始資料已存檔，不重抓
