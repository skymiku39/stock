import json

import pytest

from bot import price_crosscheck as check
from bot.stock_db import PriceBar, StockDB

DATE = "2026-11-16"


def _twse(rows):
    return {"tables": [{"fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"],
                        "data": rows}]}


def _tpex(rows):
    return {"tables": [{"fields": ["代號", "名稱", "收盤 ", "漲跌", "開盤 ", "最高 ", "最低", "成交股數  "], "data": rows}]}


def _fetcher(twse_rows, tpex_rows, calls):
    def fetch(url, **kw):
        calls.append(url)
        return _twse(twse_rows) if "twse.com.tw" in url else _tpex(tpex_rows)
    return fetch


def _db(root, bars):
    db = StockDB.open(root=root)
    try:
        db.bulk_upsert_price_bars([PriceBar(symbol=s, date=DATE, open=o, high=h, low=l, close=c, volume=v,
                                            source="twse_stock_day") for s, o, h, l, c, v in bars])
    finally:
        db.close()


def test_prices_must_match_exactly_and_volume_units_are_reconciled(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "MIN_QUOTES", 2)
    _db(tmp_path, [("2330", 2340, 2350, 2215, 2215, 52604.545),      # 一致（張 vs 股）
                   ("2303", 50, 51, 49, 50.5, 100.0),                # 收盤不符
                   ("6488", 625, 638, 575, 577, 7400.0),             # 上櫃成交量差 0.5%：容許
                   ("8299", 1620, 1620, 1515, 1515, 9999.0),         # 上櫃成交量差太多：提醒
                   ("9999", 1, 1, 1, 1, 1.0)])                       # 全市場日報沒有
    twse = [["2330", "台積電", "52,604,545", "1", "1", "2,340.00", "2,350.00", "2,215.00", "2,215.00"],
            ["2303", "聯電", "100,000", "1", "1", "50.00", "51.00", "49.00", "50.00"],
            ["2317", "鴻海", "1,000", "1", "1", "200.00", "201.00", "199.00", "200.00"]]
    tpex = [["6488", "環球晶", "577.00", "-53", "625.00", "638.00", "575.00", "7,364,000"],
            ["8299", "群聯", "1,515.00", "-125", "1,620.00", "1,620.00", "1,515.00", "3,316,000"]]
    calls = []
    result = check.run(tmp_path, DATE, expected=["2317", "2330", "NOPE"], fetcher=_fetcher(twse, tpex, calls))
    assert result["status"] == "fail" and result["matched"] == 3 and result["db_rows"] == 5
    assert [m["symbol"] for m in result["price_mismatches"]] == ["2303"]
    assert result["price_mismatches"][0]["fields"] == {"close": {"db": 50.5, "market": 50.0}}
    assert [n["symbol"] for n in result["volume_notes"]] == ["8299"]
    assert result["not_in_market_file"] == ["9999"] and result["missing_in_db"] == ["2317"]
    assert json.loads((tmp_path / check.OUTPUT_REL / f"{DATE}.json").read_text(encoding="utf-8"))["matched"] == 3
    # 原始回應已存檔，第二次不再連線。
    check.crosscheck(tmp_path, DATE, fetcher=_fetcher(twse, tpex, calls))
    assert len(calls) == 2
    assert "價格不符 1 檔" in check.describe(result)


def test_unpublished_market_file_is_not_cached_or_trusted(tmp_path):
    _db(tmp_path, [("2330", 1, 1, 1, 1, 1.0)])
    with pytest.raises(ValueError, match="尚未公布"):
        check.crosscheck(tmp_path, DATE, fetcher=lambda url, **kw: {"stat": "很抱歉，沒有符合條件的資料!"})
    assert not (tmp_path / check.CACHE_REL).exists()
