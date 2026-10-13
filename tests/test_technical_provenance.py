import json
from pathlib import Path

import pandas as pd
import requests

from bot.ask import AskEngine
from bot.ask_adapters import _price_source_document_ids
from bot.stock_db import PriceBar, StockDB, StockInfo, default_db_path
from bot.technicals import (
    _coerce_df,
    _save_df_to_db,
    fetch_monthly_kline,
    fetch_monthly_kline_with_meta,
    fetch_recent_kline,
)


PAYLOAD = {
    "stat": "OK",
    "fields": ["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"],
    "data": [
        ["115/08/01", "1000", "100000", "100", "105", "99", "103"],
    ],
}


class FakeResponse:
    status_code = 200

    def __init__(self):
        self.content = json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")

    def json(self):
        return PAYLOAD


class FakeSession:
    def get(self, url, timeout=20):
        return FakeResponse()


class RedirectSession:
    def get(self, url, timeout=20, allow_redirects=False):
        raise requests.TooManyRedirects("redirect loop")


def test_monthly_kline_raw_document_and_price_bar_are_linked(tmp_path: Path):
    result = fetch_monthly_kline_with_meta(
        "2330", 2026, 8, session=FakeSession(), root=tmp_path,
    )

    assert len(result.rows) == 1
    assert result.source == "twse_stock_day"
    assert result.source_document_id.startswith("source-doc:twse_stock_day:")

    db = StockDB.open(path=default_db_path(tmp_path))
    document = db.get_source_document(result.source_document_id)
    assert document is not None
    assert document.quality_status == "ok"
    assert (tmp_path / document.content_path).exists()

    rows = pd.DataFrame(result.rows)
    _save_df_to_db(
        "2330",
        rows,
        root=tmp_path,
        provenance_by_date={
            result.rows[0]["date"]: {
                "source": result.source,
                "source_document_id": result.source_document_id,
                "source_url": result.source_url,
                "source_date": result.source_date,
                "retrieved_at": result.retrieved_at,
                "quality_status": result.quality_status,
            },
        },
    )
    bar = db.get_price_history("2330", limit=1, ascending=False)[0]
    assert bar.source == "twse_stock_day"
    assert bar.source_record_id == result.source_document_id
    assert bar.quality_status == "ok"


def test_public_monthly_kline_keeps_list_contract_and_metadata(tmp_path: Path):
    rows = fetch_monthly_kline(
        "2330", 2026, 8, session=FakeSession(), root=tmp_path,
    )

    assert isinstance(rows, list)
    assert rows[0]["date"] == "2026-08-01"
    assert rows.source_document_id.startswith("source-doc:")


def test_monthly_kline_redirect_failure_is_controlled(tmp_path: Path):
    result = fetch_monthly_kline_with_meta(
        "6669", 2026, 9, session=RedirectSession(), root=tmp_path,
    )

    assert result.rows == []
    assert result.quality_status == "invalid"
    assert result.error.startswith("too_many_redirects:")


def test_empty_or_malformed_kline_frame_is_safe():
    assert list(_coerce_df(pd.DataFrame()).columns) == [
        "date", "open", "high", "low", "close", "volume",
    ]
    assert _coerce_df(pd.DataFrame({"date": ["2026-09-01"]})).empty


def test_recent_kline_source_failure_returns_empty_frame(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("bot.technicals._resolve_market", lambda *args, **kwargs: "twse")
    frame = fetch_recent_kline(
        "6669",
        months=1,
        root=tmp_path,
        session=RedirectSession(),
        incremental=False,
        save_to_db=False,
    )

    assert frame.empty
    assert list(frame.columns) == ["date", "open", "high", "low", "close", "volume"]


def test_ask_marks_exact_canonical_price_provenance(tmp_path: Path):
    from bot.source_registry import SourceRegistry

    with SourceRegistry(tmp_path) as registry:
        document = registry.capture_document(
            "twse_stock_day",
            b'{"data":[{"Date":"20260918"}]}',
            source_ref="2330:2026-09",
            suffix="json",
            mime_type="application/json",
            as_of="2026-09-18",
        )

    db = StockDB.open(root=tmp_path)
    document_id = document.document_id
    db.upsert_stock_info(StockInfo(
        symbol="2330",
        name="台積電",
        market="TWSE",
        quality_status="ok",
        source_record_id="source-doc:twse_company_info:verified",
    ))
    db.upsert_price_bar(PriceBar(
        symbol="2330",
        date="2026-09-18",
        open=2460,
        high=2460,
        low=2435,
        close=2460,
        volume=40892.7,
        source="twse_stock_day",
        source_date="2026-09",
        quality_status="ok",
        source_record_id=document_id,
    ))

    result = AskEngine(root=tmp_path, db=db).ask("2330 最近股價？")
    price_items = [
        item for item in result.evidence.items
        if item["kind"] == "ticker_snapshot" and "日 K" in item["title"]
    ]
    assert price_items
    assert price_items[0]["source_document_id"] == document_id
    assert price_items[0]["provenance_status"] == "exact"


def test_yfinance_replayable_rows_do_not_upgrade_price_provenance(tmp_path: Path):
    """可重播不等於官方來源；yfinance 不可成為 composite_exact。"""
    from bot.source_registry import SourceRegistry

    with SourceRegistry(tmp_path) as registry:
        document = registry.capture_document(
            "yfinance",
            b'{"symbol":"00929","date":"2026-09-15"}',
            source_ref="00929:2026-09-15",
            suffix="json",
            mime_type="application/json",
            as_of="2026-09-15",
        )

    db = StockDB.open(root=tmp_path)
    db.upsert_price_bar(PriceBar(
        symbol="00929",
        date="2026-09-15",
        open=28.0,
        high=28.5,
        low=27.8,
        close=28.2,
        source="yfinance",
        quality_status="partial",
        source_record_id=document.document_id,
    ))
    try:
        assert _price_source_document_ids(
            "00929", tmp_path, last_date="2026-09-15", limit=1,
        ) == []
    finally:
        db.close()
