"""除權息資料：解析、保存、與日 K 前收盤價交叉檢查。"""
from __future__ import annotations

from pathlib import Path

from bot.ex_rights import (
    fetch_year,
    load_events,
    parse_tpex,
    parse_twse,
    roc_to_iso,
    save_events,
    verify_against_price_history,
)
from bot.stock_db import PriceBar, StockDB

TWSE = {
    "fields": ["資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價",
               "權值+息值", "權/息"],
    "data": [
        ["109年06月15日", "2317", "鴻海", "80.00", "78.50", "1.500000", "息"],
        ["109年07月01日", "1101", "台泥", "40.00", "36.00", "4.000000", "權息"],
        ["109年08月01日", "2002", "中鋼", "20.00", "19.00", "1.000000", "權"],
    ],
}
TPEX = {"tables": [{
    "fields": ["除權息日期", "代號", "名稱", "除權息前收盤價", "除權息參考價", "權值", "息值",
               "權值+息值", "權/息", "漲停價", "跌停價", "開始交易基準價", "減除股利參考價",
               "現金股利", "每仟股無償配股"],
    "data": [["109/01/02", "5324", "士開", "9.80", "9.73", "0.07", "0", "0.065993", "除權",
              "10.75", "8.76", "9.80", "9.80", "0.5", "30"]],
}]}


def test_roc_to_iso():
    assert roc_to_iso("109年01月30日") == "2020-01-30"
    assert roc_to_iso("109/01/02") == "2020-01-02"
    assert roc_to_iso("") == ""


def test_parse_twse_kinds_and_cash():
    rows = {r["symbol"]: r for r in parse_twse(TWSE)}
    assert rows["2317"]["cash_dividend"] == 1.5 and rows["2317"]["ex_date"] == "2020-06-15"
    assert rows["1101"]["kind"] == "權息" and rows["1101"]["cash_dividend"] is None
    assert rows["2002"]["cash_dividend"] == 0.0


def test_parse_tpex_has_cash_and_stock():
    (row,) = parse_tpex(TPEX)
    assert (row["market"], row["cash_dividend"], row["stock_per_1000"]) == ("tpex", 0.5, 30.0)
    assert parse_tpex({}) == []


def test_fetch_year_merges_both_markets():
    def fake(url, session=None):
        return TWSE if "twse" in url else TPEX
    assert len(fetch_year(2020, fetcher=fake, now="t")) == 4


def test_save_load_and_verify(tmp_path: Path):
    db = StockDB.open(path=tmp_path / "x.db")
    save_events(db, parse_twse(TWSE))
    save_events(db, parse_twse(TWSE))  # 冪等
    assert len(load_events(db, "2317")) == 1
    assert load_events(db, "2317", start="2021-01-01") == []
    db.bulk_upsert_price_bars([
        PriceBar(symbol="2317", date="2020-06-12", open=80, high=81, low=79, close=80.0),
        PriceBar(symbol="1101", date="2020-06-30", open=41, high=41, low=39, close=41.0),
    ])
    (ok,) = verify_against_price_history(db, "2317")
    assert ok["ok"] and ok["status"] == "ok"
    (bad,) = verify_against_price_history(db, "1101")
    assert bad["status"] == "mismatch"
    (none,) = verify_against_price_history(db, "2002")
    assert none["status"] == "no_price"
