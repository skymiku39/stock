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
    out = ci._parse_twse(rows, source_record_id="source-doc:twse:test")
    info = out["2330"]
    assert info.short_name == "台積電"
    assert info.industry == "半導體業"
    assert info.market == "TWSE"
    assert info.listed_date == "1994-09-05"
    assert info.source_record_id == "source-doc:twse:test"


def test_parse_tpex_builds_stock_info() -> None:
    rows = [{
        "SecuritiesCompanyCode": "6488", "CompanyName": "環球晶圓股份有限公司",
        "CompanyAbbreviation": "環球晶", "SecuritiesIndustryCode": "24",
        "DateOfListing": "1040827",
    }]
    out = ci._parse_tpex(rows, source_record_id="source-doc:tpex:test")
    info = out["6488"]
    assert info.short_name == "環球晶"
    assert info.industry == "半導體業"
    assert info.market == "TPEX"
    assert info.listed_date == "2015-08-27"
    assert info.source_record_id == "source-doc:tpex:test"


def test_twse_csv_fallback_preserves_exact_response_and_source_url(tmp_path):
    import json
    from types import SimpleNamespace
    from bot.source_registry import SourceRegistry, resolve_source_document_path
    raw = "\ufeff公司代號,公司名稱,公司簡稱,產業別\n2330,台灣積體電路,台積電,24\n".encode("utf-8")
    class Session:
        def get(self, url, **kwargs):
            if url == ci.URL_TWSE_COMPANY:
                def blocked():
                    raise ValueError("not JSON")
                return SimpleNamespace(status_code=200, content=b"<html>unavailable</html>", json=blocked)
            if url == ci.URL_TWSE_COMPANY_CSV:
                return SimpleNamespace(status_code=200, content=raw, raise_for_status=lambda: None)
            rows = [{"SecuritiesCompanyCode": "6488", "CompanyName": "環球晶"}]
            return SimpleNamespace(status_code=200, content=json.dumps(rows).encode(), json=lambda: rows)
    result = ci._build_company_map(root=tmp_path, session=Session())
    assert {i.market for i in result.values()} == {"TWSE", "TPEX"}
    assert result["2330"].source_url == ci.URL_TWSE_COMPANY_CSV
    with SourceRegistry(tmp_path) as registry:
        source = registry.db.get_source_document(result["2330"].source_record_id)
        assert source.url == ci.URL_TWSE_COMPANY_CSV
        assert resolve_source_document_path(tmp_path, source).read_bytes() == raw


def test_stale_company_cache_keeps_failed_market_and_refreshes_after_midnight(tmp_path, monkeypatch):
    import datetime as dt
    import json
    from dataclasses import asdict
    from bot.utils import now_tw
    clock = [now_tw()]
    yesterday = clock[0] - dt.timedelta(days=1)
    path = tmp_path / "data/meta/company_info.json"
    path.parent.mkdir(parents=True)
    old = {s: StockInfo(s, name=s, market=m, retrieved_at=yesterday.isoformat())
           for s, m in (("2330", "TWSE"), ("6488", "TPEX"))}
    path.write_text(json.dumps({"date": yesterday.date().isoformat(),
        "map": {s: asdict(v) for s, v in old.items()}}), encoding="utf-8")
    calls = []
    def build(**kwargs):
        calls.append(True)
        return {"6488": StockInfo("6488", name="updated", market="TPEX", retrieved_at=clock[0].isoformat())}
    monkeypatch.setattr(ci, "now_tw", lambda: clock[0])
    monkeypatch.setattr(ci, "_build_company_map", build)
    result = ci.load_company_map(root=tmp_path)
    assert result["2330"].retrieved_at == yesterday.isoformat()
    assert result["6488"].name == "updated"
    ci.load_company_map(root=tmp_path)
    assert len(calls) == 1
    clock[0] += dt.timedelta(days=1)
    ci.load_company_map(root=tmp_path)
    assert len(calls) == 2


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


def test_company_update_cli_honors_explicit_root(monkeypatch, tmp_path) -> None:
    from bot import company_update

    seen: dict[str, object] = {}
    fake_map = {
        "2330": StockInfo("2330", "台積電", "台積電", "TWSE", "半導體業"),
    }

    def fake_load_company_map(**kwargs):
        seen["load_root"] = kwargs.get("root")
        return fake_map

    def fake_backfill(db, **kwargs):
        seen["backfill_root"] = kwargs.get("root")
        return 0

    monkeypatch.setattr(company_update, "load_company_map", fake_load_company_map)
    monkeypatch.setattr(company_update, "backfill_stock_info", fake_backfill)

    assert company_update.main(["--root", str(tmp_path), "--refresh"]) == 0
    assert seen["load_root"] == tmp_path.resolve()
    assert seen["backfill_root"] == tmp_path.resolve()


def test_english_short_name_twse_and_tpex() -> None:
    twse = ci._parse_twse([{
        "公司代號": "2330", "公司名稱": "台灣積體電路製造股份有限公司",
        "公司簡稱": "台積電", "英文簡稱": "\u3000TSMC \u3000", "產業別": "24",
    }])
    assert twse["2330"].english_short_name == "TSMC"
    tpex = ci._parse_tpex([{
        "SecuritiesCompanyCode": "6488", "CompanyName": "環球晶圓股份有限公司",
        "CompanyAbbreviation": "環球晶", "Symbol": " GlobalWafers\u3000",
        "SecuritiesIndustryCode": "24",
    }])
    assert tpex["6488"].english_short_name == "GlobalWafers"


def test_english_name_for_reads_cache_and_legacy(tmp_path) -> None:
    import json

    meta = tmp_path / "data" / "meta"
    meta.mkdir(parents=True)
    (meta / "company_info.json").write_text(json.dumps({"date": "2024-01-01", "map": {
        "2330": {"symbol": "2330", "name": "台積電", "english_short_name": "TSMC"},
        "2317": {"symbol": "2317", "name": "鴻海"},  # legacy cache: no field
    }}, ensure_ascii=False), encoding="utf-8")
    assert ci.english_name_for("2330", tmp_path) == "TSMC"
    assert ci.english_name_for("2317", tmp_path) == ""
    assert ci.english_name_for("9999", tmp_path) == ""
    assert ci.english_name_for("2330", tmp_path / "nope") == ""
