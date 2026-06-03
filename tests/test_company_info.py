from __future__ import annotations

from bot import company_info as ci
from bot.stock_db import StockInfo


def test_industry_label_maps_known_codes() -> None:
    assert ci.industry_label("24") == "半導體業"
    assert ci.industry_label("17") == "金融保險業"
    assert ci.industry_label("1") == "水泥工業"  # 補零


def test_industry_label_unknown_and_empty() -> None:
    assert ci.industry_label("99") == "產業99"  # 未知代碼仍非空，可分群
    assert ci.industry_label("") == ""
    assert ci.industry_label("－") == ""


def test_date_to_iso_handles_gregorian_and_roc() -> None:
    assert ci._date_to_iso("19940905") == "1994-09-05"  # TWSE 西元 8 碼
    assert ci._date_to_iso("1140520") == "2025-05-20"   # TPEx 民國 7 碼
    assert ci._date_to_iso("") == ""
    assert ci._date_to_iso("abc") == ""
    assert ci._date_to_iso("20259999") == ""            # 非法月日


def test_parse_twse_builds_stock_info() -> None:
    rows = [{
        "公司代號": "2330", "公司名稱": "台灣積體電路製造股份有限公司",
        "公司簡稱": "台積電", "產業別": "24", "上市日期": "19940905",
    }]
    out = ci._parse_twse(rows)
    info = out["2330"]
    assert info.short_name == "台積電"
    assert info.industry == "半導體業"
    assert info.market == "TWSE"
    assert info.listed_date == "1994-09-05"


def test_parse_tpex_builds_stock_info() -> None:
    rows = [{
        "SecuritiesCompanyCode": "6488", "CompanyName": "環球晶圓股份有限公司",
        "CompanyAbbreviation": "環球晶", "SecuritiesIndustryCode": "24",
        "DateOfListing": "1040827",
    }]
    out = ci._parse_tpex(rows)
    info = out["6488"]
    assert info.short_name == "環球晶"
    assert info.industry == "半導體業"
    assert info.market == "TPEX"
    assert info.listed_date == "2015-08-27"


def test_merge_preserves_manual_fields() -> None:
    existing = StockInfo(symbol="3017", name="奇鋐", short_name="奇鋐", industry="散熱")
    fetched = StockInfo(
        symbol="3017", name="奇鋐科技股份有限公司", short_name="奇鋐",
        industry="電子零組件業", market="TWSE", listed_date="2002-09-27",
    )
    merged = ci._merge(existing, fetched)
    assert merged.industry == "散熱"          # 人工分類保留
    assert merged.listed_date == "2002-09-27"  # 空欄位補上


class _FakeDB:
    def __init__(self) -> None:
        self.store: dict[str, StockInfo] = {}

    def get_stock_info(self, symbol: str):
        return self.store.get(symbol)

    def upsert_stock_info(self, info: StockInfo) -> None:
        self.store[info.symbol] = info


def test_backfill_fills_missing_and_keeps_manual(monkeypatch) -> None:
    fake_map = {
        "2330": StockInfo("2330", "台積電", "台積電", "TWSE", "半導體業", listed_date="1994-09-05"),
        "1101": StockInfo("1101", "台泥", "台泥", "TWSE", "水泥工業", listed_date="1962-02-09"),
    }
    monkeypatch.setattr(ci, "load_company_map", lambda **kw: fake_map)

    db = _FakeDB()
    # 既有人工分類 (與官方不同) 應保留
    db.store["2330"] = StockInfo("2330", "台積電", "台積電", "TWSE", "半導體")

    n = ci.backfill_stock_info(db, only_missing=True)
    assert n == 2
    assert db.store["2330"].industry == "半導體"      # 保留人工
    assert db.store["2330"].listed_date == "1994-09-05"  # 補上市日
    assert db.store["1101"].industry == "水泥工業"     # 新增
