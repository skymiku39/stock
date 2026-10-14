import datetime as dt
import json
from unittest.mock import Mock

from bot import chips_fetcher as chips
from bot.source_registry import SourceRegistry


def test_tpex_repeated_headers_use_net_columns_and_correct_units():
    fields = ["代號", "名稱"] + ["買進股數", "賣出股數", "買賣超股數"] * 7 + ["三大法人買賣超股數合計"]
    table = {"fields": fields, "data": [["3081", "聯亞"] + [str(n * 1000) for n in range(2, 25)]]}
    row = chips._parse_tpex_insti(table)["3081"]
    assert (row.foreign_net, row.investment_trust_net, row.dealer_net) == (10, 13, 22)
    table["fields"][5] = "unexpected"
    assert chips._parse_tpex_insti(table) == {}


def test_tpex_margin_uses_today_not_previous_balance():
    table = {"fields": ["代號", "名稱", "前資餘額(張)", "資餘額", "前券餘額(張)", "券餘額"],
             "data": [["3081", "聯亞", "900", "123", "800", "45"]]}
    assert chips._parse_tpex_margin(table) == {"3081": (123, 45)}


def test_twse_margin_expands_groups_without_reading_market_totals():
    data = {"tables": [
        {"fields": ["項目", "今日餘額"], "data": [["融資", "99999"]]},
        {"fields": ["代號", "名稱", "前日餘額", "今日餘額", "前日餘額", "今日餘額"],
         "groups": [{"title": "股票", "span": 2}, {"title": "融資", "span": 2}, {"title": "融券", "span": 2}],
         "data": [["2330", "台積電", "100", "120", "200", "210"]]},
    ]}
    assert chips._parse_margin(data) == {"2330": (120, 210)}


def test_empty_tpex_cache_retries_but_read_only_does_not(tmp_path, monkeypatch):
    day = dt.date(2026, 10, 14)
    path = chips._cache_path("tpex_margin", day, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"fields": ["代號"], "data": []}), encoding="utf-8")
    table = {"fields": ["代號"], "data": [["3081"]]}
    payload = json.dumps({"tables": [table]}).encode()
    session = Mock()
    session.get.return_value.status_code = 200
    session.get.return_value.content = payload
    session.get.return_value.json.return_value = {"tables": [table]}
    monkeypatch.setattr(chips.time, "sleep", lambda _: None)
    assert chips._fetch_tpex_chip("tpex_margin", day, root=tmp_path, session=session, cache_only=True) is None
    session.get.assert_not_called()
    assert chips._fetch_tpex_chip("tpex_margin", day, root=tmp_path, session=session) == table
    assert chips.cached_chip_source_document_ids([day], root=tmp_path)
    # A matching source/ref/date must not endorse altered cache contents.
    path.write_text(json.dumps({**table, "data": [["wrong"]]}), encoding="utf-8")
    assert chips.cached_chip_source_document_ids([day], root=tmp_path) == []


def test_chip_source_rejects_tampered_raw(tmp_path):
    day = dt.date(2026, 10, 14)
    data = {"fields": ["代號"], "data": [["2330"]]}
    path = chips._cache_path("qfii_daily", day, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    with SourceRegistry(tmp_path) as registry:
        row = registry.capture_document("chips_twse_qfii_daily", json.dumps(data).encode(), source_ref="qfii_daily:2026-10-14")
        assert chips.cached_chip_source_document_ids([day], root=tmp_path) == [row.document_id]
        chips.resolve_source_document_path(tmp_path, row).write_bytes(b"{}")
    assert chips.cached_chip_source_document_ids([day], root=tmp_path) == []


def test_empty_summary_has_unknown_date(tmp_path, monkeypatch):
    monkeypatch.setattr(chips, "fetch_daily_chips", lambda *a, **kw: {})
    monkeypatch.setattr(chips, "_fetch_endpoint", lambda *a, **kw: None)
    summary = chips.build_chip_summary("2330", root=tmp_path)
    assert summary.source_date_status == "unknown"
    assert summary.source_document_ids == []
    data = chips.summary_to_dict(summary)
    assert data["foreign_net"] is None
    assert data["margin_buy_change_pct"] is None


def test_twse_dealer_header_without_share_suffix():
    row = chips._parse_t86({"fields": ["證券代號", "證券名稱", "外資及陸資買賣超股數(不含外資自營商)",
                          "投信買賣超股數", "自營商買賣超"],
                          "data": [["2330", "台積電", "2000", "3000", "-4000"]]})["2330"]
    assert row.dealer_net == -4
    assert "dealer_net" in row.available_fields


def test_raw_registration_failure_does_not_publish_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(chips, "_capture_chip_response", lambda **kw: False)
    session = Mock()
    session.get.return_value.status_code = 200
    session.get.return_value.content = b"{}"
    session.get.return_value.json.return_value = {"fields": ["代號"], "data": [["2330"]]}
    assert chips._fetch_endpoint("qfii_daily", dt.date(2026, 10, 14), root=tmp_path, session=session) is None
    assert not chips._cache_path("qfii_daily", dt.date(2026, 10, 14), tmp_path).exists()
