from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from bot.data_quality import (
    QUALITY_INVALID,
    QUALITY_OK,
    QUALITY_PARTIAL,
    response_to_rows,
    validate_rows,
    validate_ohlcv,
)
from bot.evidence import evidence_id, make_evidence
from bot.stock_db import StockDB


class FakeResponse:
    def __init__(self, payload, status_code=200, content=b""):
        self.payload = payload
        self.status_code = status_code
        self.content = content or b""

    def json(self):
        return self.payload


def test_response_to_rows_validates_object_list() -> None:
    raw = b'{"items":[{"Code":"2330","Date":"20260909"}]}'
    result = response_to_rows(
        FakeResponse(
            {"items": [{"Code": "2330", "Date": "20260909"}]},
            content=raw,
        ),
        source="fixture",
        url="https://example.test",
        list_key="items",
        required_keys=("Code",),
    )
    assert result.status == QUALITY_OK
    assert result.row_count == 1
    assert result.raw_hash
    assert result.schema_hash
    assert result.raw_bytes == raw
    assert "raw_bytes" not in result.to_dict()


def test_response_to_rows_rejects_schema_mismatch() -> None:
    result = response_to_rows(
        FakeResponse({"items": [{"Name": "missing code"}]}),
        source="fixture",
        list_key="items",
        required_keys=("Code",),
    )
    assert result.status == QUALITY_INVALID
    assert "Code" in result.error


def test_validate_rows_marks_small_response_partial() -> None:
    result = validate_rows(
        [{"Code": "2330"}],
        source="fixture",
        min_rows=2,
    )
    assert result.status == QUALITY_PARTIAL
    assert result.usable


def test_non_2xx_is_failed() -> None:
    result = response_to_rows(
        FakeResponse([], status_code=429),
        source="fixture",
    )
    assert result.status == "failed"
    assert result.error == "HTTP 429"


def test_validate_ohlcv_catches_inconsistent_prices() -> None:
    errors = validate_ohlcv(
        open_price=100,
        high=99,
        low=90,
        close=98,
        volume=10,
    )
    assert "OHLC invariant violated" in errors


@pytest.mark.parametrize("field", ["open_price", "high", "low", "close", "volume", "amount"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity"])
def test_ohlcv_rejects_nonfinite_values(field, value):
    values = dict(open_price=100, high=110, low=90, close=105, volume=10, amount=1000)
    values[field] = value
    assert any("not finite" in error for error in validate_ohlcv(**values))


def test_ohlcv_accepts_finite_and_explicit_all_zero_bars():
    assert validate_ohlcv(open_price=100, high=110, low=90, close=105, volume=0) == []
    assert validate_ohlcv(open_price=0, high=0, low=0, close=0, volume=0) == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_bar_cannot_overwrite_canonical_history(tmp_path, value):
    from bot.stock_db import PriceBar
    db = StockDB.open(path=tmp_path / "finite.db")
    try:
        db.upsert_price_bar(PriceBar(symbol="2330", date="2026-10-07", open=100, high=110, low=90, close=105))
        with pytest.raises(ValueError, match="OHLCV"):
            db.upsert_price_bar(PriceBar(symbol="2330", date="2026-10-07", open=100, high=110, low=90, close=value))
        assert db.fetch_all_rows("price_history")[0]["close"] == 105
    finally:
        db.close()


def test_evidence_id_is_stable_and_has_trace_fields() -> None:
    first = make_evidence(
        "news", source="fixture", source_ref="u1", title="Title", excerpt="text",
    )
    second = make_evidence(
        "news", source="fixture", source_ref="u1", title="Title", excerpt="text",
    )
    assert first["id"] == second["id"] == evidence_id(
        "news", source_ref="u1", title="Title", text="text",
    )
    assert first["quality_status"] == "ok"


def test_persist_source_result_registers_raw_rows_and_health(tmp_path: Path) -> None:
    from bot.data_quality import SourceFetchResult, persist_source_result

    result = SourceFetchResult(
        source="fixture_api",
        url="https://example.test/data",
        status=QUALITY_OK,
        fetched_at="2026-09-17T10:00:00+08:00",
        source_date="2026-09-16",
        row_count=1,
        raw_hash="raw-hash",
        schema_hash="schema-hash",
        rows=[{"symbol": "2330", "close": 100}],
    )
    persist_source_result(result, root=tmp_path)

    db = StockDB.open(root=tmp_path)
    documents = db.list_source_documents(source="fixture_api", kind="api_rows")
    runs = db.list_ingestion_runs(source="fixture_api")
    assert len(documents) == 1
    assert documents[0].content_hash
    assert len(runs) == 1
    assert runs[0].source_run_id == runs[0].run_id
    assert runs[0].status == QUALITY_OK
    health = db.get_source_health("fixture_api")
    assert health is not None
    assert health.last_status == QUALITY_OK
    assert health.consecutive_failures == 0


def test_response_result_persists_original_http_bytes(tmp_path: Path) -> None:
    from bot.data_quality import persist_source_result

    raw = b'{"items":[{"Code":"2330"}]}'
    result = response_to_rows(
        FakeResponse({"items": [{"Code": "2330"}]}, content=raw),
        source="raw_fixture",
        url="https://example.test/raw",
        list_key="items",
        required_keys=("Code",),
    )
    persist_source_result(result, root=tmp_path)

    db = StockDB.open(root=tmp_path)
    document = db.list_source_documents(source="raw_fixture", kind="http_response")[0]
    assert (tmp_path / document.content_path).read_bytes() == raw


def test_persist_source_result_retries_transient_sqlite_lock(tmp_path: Path) -> None:
    from bot.data_quality import SourceFetchResult, persist_source_result

    result = SourceFetchResult(source="locked_fixture", status=QUALITY_OK)
    with patch(
        "bot.data_quality._persist_source_result_once",
        side_effect=[sqlite3.OperationalError("database is locked"), "source-doc:fixture"],
    ) as persist_once, patch("bot.data_quality.time.sleep") as sleep:
        document_id = persist_source_result(result, root=tmp_path)

    assert document_id == "source-doc:fixture"
    assert persist_once.call_count == 2
    sleep.assert_called_once_with(0.15)
