"""台指期夜盤：全部用假的回應，不連網。數值取自 2026-11-25 11:31 實測的 MIS 回應。"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from bot import night_futures as nf
from bot.market_macro import _front_tx_row, _normalize_taifex_rows

TZ = dt.timezone(dt.timedelta(hours=8))


def _q(sid, last, ref, *, date="20261124", time="045957", status="TC", pct=None,
       o="40772.00", h="40900.00", lo="37730.00", vol="95627"):
    return {"SymbolID": sid, "CLastPrice": last, "CRefPrice": ref, "CDiff": "",
            "CDiffRate": pct if pct is not None else "", "COpenPrice": o, "CHighPrice": h,
            "CLowPrice": lo, "CTotalVolume": vol, "CDate": date, "CTime": time,
            "Status": status}


def _payload(quotes=None):
    if quotes is None:
        quotes = [
            _q("TXF-P", "", "40122.35", time="", status="", o="", h="", lo="", vol=""),
            _q("TXFA7-M", "38609.00", "40464.00", pct="-4.58", vol="886"),
            _q("TXFL6-M", "38482.00", "40324.00", pct="-4.57"),
            _q("TXFB7-M", "38436.00", "40606.00", pct="-5.34", vol="18"),
        ]
    return {"RtCode": "0", "RtMsg": "", "RtData": {"QuoteList": quotes}}


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        if self.exc:
            raise self.exc
        return self.resp


def test_contract_month_decoding():
    assert nf._contract_month("TXFL6-M", 2026) == "202612"
    assert nf._contract_month("TXFA7-M", 2026) == "202701"
    assert nf._contract_month("TXFA0-M", 2029) == "203001"
    assert nf._contract_month("TXFL6-F", 2026) is None  # 日盤合約
    assert nf._contract_month("TXF-P", 2026) is None


def test_parse_picks_front_month_and_spot():
    out = nf.parse_night_quotes(_payload())
    assert out["status"] == "ok"
    front = out["front"]
    assert front["symbol_id"] == "TXFL6-M"
    assert front["contract_month"] == "202612"
    assert front["last"] == 38482.0
    assert front["pct_change"] == -4.57
    assert front["low"] == 37730.0
    assert front["session_date"] == "2026-11-24"
    assert front["quote_time"] == "04:59:57"
    assert front["closed"] is True
    assert front["for_trading_day"] == "2026-11-25"
    assert out["spot_close"] == 40122.35
    assert front["vs_spot_close_pct"] == round(100 * (38482 - 40122.35) / 40122.35, 2)
    assert [c["contract_month"] for c in out["contracts"]] == ["202612", "202701", "202702"]


def test_parse_computes_pct_when_missing_and_skips_untraded():
    quotes = [_q("TXFL6-M", "", "40324.00"),
              _q("TXFA7-M", "40000.00", "40464.00", status="")]
    out = nf.parse_night_quotes(_payload(quotes))
    front = out["front"]
    assert front["symbol_id"] == "TXFA7-M"  # 近月沒成交時取下一個有成交的
    assert front["pct_change"] == round(100 * (40000 - 40464) / 40464, 2)
    assert front["closed"] is False
    assert front["vs_spot_close_pct"] is None


def test_parse_no_data():
    assert nf.parse_night_quotes({"RtCode": "0", "RtData": {}})["status"] == "no_data"


def test_friday_night_maps_to_monday():
    assert nf.next_trading_weekday("2026-11-27") == "2026-11-30"


def test_fetch_saves_files_and_raw(tmp_path):
    sess = FakeSession(FakeResp(_payload()))
    rec = nf.fetch_night_futures(root=tmp_path, session=sess,
                                 now=dt.datetime(2026, 11, 25, 8, 0, tzinfo=TZ))
    assert sess.calls[0]["MarketType"] == "1"
    assert rec["status"] == "ok" and rec["error"] == ""
    assert rec["source_document_id"]
    day = json.loads((tmp_path / "data" / "night_futures" / "2026-11-24.json").read_text("utf-8"))
    latest = json.loads((tmp_path / "data" / "night_futures" / "latest.json").read_text("utf-8"))
    assert day == latest
    assert day["front"]["last"] == 38482.0


def test_fetch_error_does_not_raise_or_save(tmp_path):
    rec = nf.fetch_night_futures(root=tmp_path, session=FakeSession(exc=OSError("down")))
    assert rec["status"] == "error" and "down" in rec["error"]
    assert not (tmp_path / "data" / "night_futures").exists()


def test_fetch_http_error(tmp_path):
    rec = nf.fetch_night_futures(root=tmp_path, session=FakeSession(FakeResp({}, status=500)))
    assert rec["status"] == "error"


def test_brief_section_warnings():
    good = nf.parse_night_quotes(_payload())
    section, warnings = nf.brief_section(lambda: good, expected_session_date="2026-11-24")
    assert section["status"] == "ok" and warnings == []

    _, warnings = nf.brief_section(lambda: good, expected_session_date="2026-11-25")
    assert any("不是預期" in w for w in warnings)

    def boom():
        raise RuntimeError("x")

    section, warnings = nf.brief_section(boom, expected_session_date="2026-11-24")
    assert section["status"] == "error" and warnings


def test_daily_report_basis_ignores_night_rows():
    # 實測 DailyMarketReportFut：同一合約有「一般」與「盤後」兩列，盤後結算價為 NULL
    rows = _normalize_taifex_rows([
        {"Contract": "TX", "ContractMonth(Week)": "202612", "Last": "39829",
         "SettlementPrice": "NULL", "Date": "20261124", "TradingSession": "盤後"},
        {"Contract": "TX", "ContractMonth(Week)": "202612", "Last": "40324",
         "SettlementPrice": "40324", "Date": "20261124", "TradingSession": "一般"},
    ])
    front = _front_tx_row(rows)
    assert front["Last"] == "40324"


def test_daily_report_without_session_field_still_works():
    rows = _normalize_taifex_rows([
        {"Contract": "TX", "ContractMonth(Week)": "202612", "Last": "40324", "Date": "20261124"},
    ])
    assert _front_tx_row(rows)["Last"] == "40324"


def test_main_no_save(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(nf, "_session", lambda: FakeSession(FakeResp(_payload())))
    assert nf.main(["--root", str(tmp_path), "--no-save"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["contract"] == "TXFL6-M"
    assert not (tmp_path / "data" / "night_futures").exists()
