from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bot.ask import AskEngine
from bot.fundamentals_fetcher import fetch_monthly_revenue
from bot.monthly_revenue_research import (
    INGEST_RUN_STATUS,
    PIPELINE,
    REVENUE_UNIT,
    ingest_monthly_revenue_from_source_document,
    materialize_monthly_revenue_slice,
)
from bot.query_plan import QueryPlan
from bot.resource_adapters import MonthlyRevenueAdapter
from bot.source_registry import SourceRegistry
from bot.stock_db import MonthlyRevenue, StockDB


SAMPLE_ROWS = [
    {
        "公司代號": "1101",
        "公司名稱": "台泥",
        "資料年月": "11409",
        "營業收入-當月營收": "10,000",
        "營業收入-去年同月增減(%)": "1.0",
        "營業收入-上月比較增減(%)": "0.5",
    },
    {
        "公司代號": "2330",
        "公司名稱": "台積電",
        "資料年月": "11410",
        "營業收入-當月營收": "300,000",
        "營業收入-去年當月營收": "260,000",
        "營業收入-去年同月增減(%)": "15.4",
        "營業收入-上月比較增減(%)": "-2.1",
        "營業收入-當月累計營收": "2,400,000",
        "營業收入-前期比較增減(%)": "30.5",
    },
]


class _JsonResponse:
    status_code = 200

    def __init__(self, payload):
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def json(self):
        return json.loads(self.content.decode("utf-8"))


class _MonthlyRevenueSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        return _JsonResponse(SAMPLE_ROWS)


def _register_raw(tmp_path: Path, rows=None, *, retrieved_at: str = "2026-09-18T10:00:00+08:00"):
    payload = json.dumps(rows if rows is not None else SAMPLE_ROWS, ensure_ascii=False).encode("utf-8")
    doc = SourceRegistry(tmp_path).capture_document(
        "monthly_revenue_twse",
        payload,
        source_url="https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
        source_ref="monthly_revenue_twse:test",
        title="TWSE monthly revenue",
        suffix="json",
        mime_type="application/json",
        as_of="2025-10",
        retrieved_at=retrieved_at,
        quality_status="ok",
    )
    return doc, payload


def test_fetcher_wires_exact_raw_document_into_research_slice(tmp_path: Path) -> None:
    session = _MonthlyRevenueSession()
    rows = fetch_monthly_revenue("2330", root=tmp_path, session=session)

    assert len(rows) == 1
    assert rows[0].year == 2025
    assert rows[0].month == 10
    assert len(session.calls) == 2

    db = StockDB.open(root=tmp_path)
    try:
        canonical = db.get_monthly_revenue("2330", "2025-10")
        assert len(canonical) == 1
        assert canonical[0].source_record_id.startswith("source-doc:")
        assert db.get_source_document(canonical[0].source_record_id) is not None

        research = db.list_research_documents(symbol="2330", limit=10)
        assert research
        assert research[0].source_document_id == canonical[0].source_record_id

        evidence = [
            row for row in db.list_evidence(limit=50)
            if row.source_document_id == canonical[0].source_record_id
        ]
        assert evidence
        assert any(row.source == "monthly_revenue_twse" for row in evidence)
    finally:
        db.close()


def test_raw_archive_hash_replay(tmp_path: Path) -> None:
    doc, payload = _register_raw(tmp_path)
    digest = hashlib.sha256(payload).hexdigest()
    assert doc.content_hash == digest
    raw_path = tmp_path / doc.content_path
    assert raw_path.read_bytes() == payload

    first = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    second = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    assert first.status == "ok"
    assert second.status == "reused"
    assert second.evidence_id == first.evidence_id
    assert second.research_document_id == first.research_document_id
    assert second.run_id == first.run_id
    assert second.claim_ids == first.claim_ids


def test_exact_source_document_id_required(tmp_path: Path) -> None:
    slice_row = materialize_monthly_revenue_slice(
        root=tmp_path,
        source_document_id="source-doc:not-real",
        symbol="2330",
        year_month="2025-10",
        revenue=1.0,
        yoy_pct=1.0,
        mom_pct=1.0,
    )
    assert slice_row.status == "failed"
    assert "unknown source_document_id" in slice_row.error
    assert slice_row.provenance_status == "unresolved"


def test_url_title_text_cannot_guess_source(tmp_path: Path) -> None:
    doc, _ = _register_raw(tmp_path)
    # 即使 URL 相同，也不能用 URL 冒充 exact document id。
    slice_row = ingest_monthly_revenue_from_source_document(
        root=tmp_path,
        source_document_id=doc.url,
        ticker="2330",
    )
    assert slice_row.status == "failed"
    assert "unknown source_document_id" in slice_row.error


def test_canonical_document_and_chunks_rebuildable(tmp_path: Path) -> None:
    doc, _ = _register_raw(tmp_path)
    slice_row = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    assert slice_row.status == "ok"
    db = StockDB.open(root=tmp_path)
    try:
        canonical = db.get_monthly_revenue("2330", "2025-10")
        assert len(canonical) == 1
        assert canonical[0].revenue == 300000.0
        assert canonical[0].yoy_pct == 15.4
        assert canonical[0].source_record_id == doc.document_id
        assert REVENUE_UNIT in canonical[0].note

        research = db.get_research_document(slice_row.research_document_id)
        assert research is not None
        assert research.source_document_id == doc.document_id
        assert research.symbol == "2330"
        assert "TWD_thousand" in research.canonical_text
        chunks = db.list_research_chunks(research.document_id)
        assert chunks
        assert "".join(c.text for c in chunks) == research.canonical_text
    finally:
        db.close()


def test_numeric_unit_date_claims_verified(tmp_path: Path) -> None:
    doc, _ = _register_raw(tmp_path)
    slice_row = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    assert slice_row.status == "ok"
    db = StockDB.open(root=tmp_path)
    try:
        run = db.get_llm_run(slice_row.run_id)
        assert run is not None
        assert run.status == INGEST_RUN_STATUS
        assert run.status != "verified"
        for claim_id in slice_row.claim_ids:
            claim = db.get_claim(claim_id)
            assert claim is not None
            assert claim.status == "verified"
            assert claim.claim_type == "source_fact"
            assert "看多" not in claim.text and "看空" not in claim.text
        evidence = db.get_evidence(slice_row.evidence_id)
        assert evidence is not None
        assert evidence.source_document_id == doc.document_id
        assert "exact" in evidence.metadata_json or PIPELINE in evidence.metadata_json
    finally:
        db.close()


def test_rerun_does_not_increase_row_counts(tmp_path: Path) -> None:
    doc, _ = _register_raw(tmp_path)
    first = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    second = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    assert second.status == "reused"
    db = StockDB.open(root=tmp_path)
    try:
        assert len(db.get_monthly_revenue("2330", "2025-10")) == 1
        linked = [
            row for row in db.list_evidence(limit=50)
            if row.source_document_id == doc.document_id
        ]
        assert len(linked) == 1
        assert linked[0].evidence_id == first.evidence_id
    finally:
        db.close()


def test_source_failure_and_empty_result_are_distinct(tmp_path: Path) -> None:
    doc, _ = _register_raw(tmp_path)
    failed = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id="source-doc:missing", ticker="2330",
    )
    empty = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="9999",
    )
    assert failed.status == "failed"
    assert empty.status == "empty"
    assert "找不到 ticker" in empty.error
    assert "不等於來源失敗" in empty.error


def test_stale_data_does_not_overwrite_newer(tmp_path: Path) -> None:
    newer_doc, _ = _register_raw(
        tmp_path, retrieved_at="2026-09-18T12:00:00+08:00",
    )
    first = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=newer_doc.document_id, ticker="2330",
    )
    assert first.status == "ok"

    older_rows = json.loads(json.dumps(SAMPLE_ROWS))
    older_rows[1]["營業收入-當月營收"] = "1"
    older_doc, _ = _register_raw(
        tmp_path,
        older_rows,
        retrieved_at="2026-09-18T09:00:00+08:00",
    )
    skipped = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=older_doc.document_id, ticker="2330",
    )
    assert skipped.status == "stale_skipped"
    db = StockDB.open(root=tmp_path)
    try:
        row = db.get_monthly_revenue("2330", "2025-10")[0]
        assert row.revenue == 300000.0
        assert row.source_record_id == newer_doc.document_id
        assert row.retrieved_at.startswith("2026-09-18T12:00:00")
    finally:
        db.close()


def test_adapter_reuses_existing_evidence(tmp_path: Path) -> None:
    doc, _ = _register_raw(tmp_path)
    slice_row = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    plan = QueryPlan(
        plan_id="p1",
        trace_id="t1",
        query="2330 營收",
        normalized_query="2330 營收",
        ticker="2330",
        resource_needs=["fundamental"],
    )
    result = MonthlyRevenueAdapter(root=tmp_path).collect(plan)
    assert result.status == "ok"
    assert len(result.evidence) == 1
    assert result.evidence[0]["id"] == slice_row.evidence_id
    assert result.evidence[0]["source_document_id"] == doc.document_id
    assert result.evidence[0].get("provenance_status") == "exact"

    db = StockDB.open(root=tmp_path)
    try:
        linked = [
            row for row in db.list_evidence(limit=50)
            if row.source_document_id == doc.document_id
        ]
        assert len(linked) == 1
    finally:
        db.close()


def test_ask_uses_revenue_evidence_without_trading_path(tmp_path: Path) -> None:
    doc, _ = _register_raw(tmp_path)
    slice_row = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    assert slice_row.status == "ok"

    import sys
    import bot.ask as ask_mod

    broker_was_loaded = "bot.broker" in sys.modules
    engine = AskEngine(root=tmp_path)
    result = engine.ask("2330 最近營收如何？")
    assert any(
        item.get("id") == slice_row.evidence_id
        for item in result.evidence.items
    )
    # !ask 路徑不載入交易／下單模組
    if not broker_was_loaded:
        assert "bot.broker" not in sys.modules
    assert "place_order" not in dir(ask_mod)
    db = StockDB.open(root=tmp_path)
    try:
        longs = db.list_memory_items(memory_layer="long", limit=20)
        assert all(PIPELINE not in (m.content or "") for m in longs)
    finally:
        db.close()


def test_unresolved_legacy_canonical_not_guessed(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_monthly_revenue(MonthlyRevenue(
        symbol="2330",
        year_month="2026-08",
        revenue=100.0,
        yoy_pct=12.0,
        quality_status="ok",
        source_record_id="",  # 無法證明 raw 來源
        source_url="https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    ))
    db.close()
    # 即使有同 URL 的 exact raw，也不得依 URL 猜補 legacy 列。
    _register_raw(tmp_path)
    plan = QueryPlan(
        plan_id="p2",
        trace_id="t2",
        query="2330 營收",
        normalized_query="2330 營收",
        ticker="2330",
        resource_needs=["fundamental"],
    )
    result = MonthlyRevenueAdapter(root=tmp_path).collect(plan)
    assert result.status == "ok"
    assert result.evidence[0].get("provenance_status") == "unresolved"
    assert result.evidence[0].get("provenance_status") != "exact"
    assert not result.evidence[0].get("source_document_id")
    assert result.evidence[0].get("metadata", {}).get("cache_only") is True
    assert any("缺少 source_document_id" in w for w in result.warnings)


def test_non_ok_source_quality_rejected(tmp_path: Path) -> None:
    doc, _ = _register_raw(tmp_path)
    db = StockDB.open(root=tmp_path)
    try:
        source = db.get_source_document(doc.document_id)
        assert source is not None
        source.quality_status = "failed"
        db.upsert_source_document(source)
    finally:
        db.close()

    failed = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    assert failed.status == "failed"
    assert "quality_status=failed" in failed.error
    assert failed.provenance_status == "unresolved"

    db = StockDB.open(root=tmp_path)
    try:
        assert db.get_monthly_revenue("2330", "2025-10") == []
        assert db.list_research_documents(symbol="2330", limit=10) == []
    finally:
        db.close()


def test_raw_hash_mismatch_refuses_slice(tmp_path: Path) -> None:
    doc, _ = _register_raw(tmp_path)
    raw_path = tmp_path / doc.content_path
    raw_path.write_bytes(raw_path.read_bytes() + b"\n")

    ingest_failed = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    assert ingest_failed.status == "failed"
    assert "hash mismatch" in ingest_failed.error
    assert ingest_failed.provenance_status == "unresolved"

    direct_failed = materialize_monthly_revenue_slice(
        root=tmp_path,
        source_document_id=doc.document_id,
        symbol="2330",
        year_month="2025-10",
        revenue=300000.0,
        yoy_pct=15.4,
        mom_pct=-2.1,
    )
    assert direct_failed.status == "failed"
    assert "hash mismatch" in direct_failed.error
    assert direct_failed.provenance_status == "unresolved"

    db = StockDB.open(root=tmp_path)
    try:
        assert db.get_monthly_revenue("2330", "2025-10") == []
    finally:
        db.close()


def test_adapter_non_replayable_source_downgrades_not_exact(tmp_path: Path) -> None:
    doc, _ = _register_raw(tmp_path)
    slice_row = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    assert slice_row.status == "ok"
    assert slice_row.provenance_status == "exact"

    raw_path = tmp_path / doc.content_path
    raw_path.write_bytes(b'{"tampered": true}')

    plan = QueryPlan(
        plan_id="p-non-replay",
        trace_id="t-non-replay",
        query="2330 營收",
        normalized_query="2330 營收",
        ticker="2330",
        resource_needs=["fundamental"],
    )
    result = MonthlyRevenueAdapter(root=tmp_path).collect(plan)
    assert result.status == "ok"
    assert len(result.evidence) == 1
    packet = result.evidence[0]
    assert packet.get("provenance_status") == "unresolved"
    assert packet.get("provenance_status") != "exact"
    assert not packet.get("source_document_id")
    assert packet.get("metadata", {}).get("cache_only") is True
    assert any("無法回放" in w for w in result.warnings)
    # 不可把已毀損的 exact evidence 繼續當 exact 回傳
    assert packet.get("id") != slice_row.evidence_id


def test_single_market_materialize_does_not_overwrite_other_symbol(tmp_path: Path) -> None:
    """單一市場／單一 ticker 切片不得覆寫其他代號既有 canonical 列。"""
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_monthly_revenue(MonthlyRevenue(
            symbol="1101",
            year_month="2025-09",
            revenue=999.0,
            yoy_pct=9.0,
            mom_pct=1.0,
            quality_status="ok",
            source_record_id="source-doc:other-market-preserved",
            retrieved_at="2026-09-18T08:00:00+08:00",
            note="pre-existing other market/ticker row",
        ))
    finally:
        db.close()

    # 單一市場 raw：只含 2330；物化不得動到既有 1101
    twse_only = [SAMPLE_ROWS[1]]
    doc, _ = _register_raw(tmp_path, twse_only)
    slice_row = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    assert slice_row.status == "ok"

    # 同 raw 含多代號時，指定 ticker 仍不得順便寫入其他代號
    full_doc, _ = _register_raw(
        tmp_path,
        SAMPLE_ROWS,
        retrieved_at="2026-09-18T11:00:00+08:00",
    )
    only_2330 = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=full_doc.document_id, ticker="2330",
    )
    assert only_2330.status == "ok"
    assert only_2330.source_document_id == full_doc.document_id

    db = StockDB.open(root=tmp_path)
    try:
        other = db.get_monthly_revenue("1101", "2025-09")
        assert len(other) == 1
        assert other[0].revenue == 999.0
        assert other[0].source_record_id == "source-doc:other-market-preserved"
        assert other[0].note == "pre-existing other market/ticker row"

        target = db.get_monthly_revenue("2330", "2025-10")
        assert len(target) == 1
        assert target[0].source_record_id == full_doc.document_id
        # SAMPLE 內 1101 為 2025-09（民國 11409），不得被此次 2330-only 物化另建／覆寫
        assert db.get_monthly_revenue("1101", "2025-10") == []
        assert len(db.get_monthly_revenue("1101")) == 1
    finally:
        db.close()


def test_adapter_rejects_non_ok_source_quality_without_elevation(tmp_path: Path) -> None:
    doc, _ = _register_raw(tmp_path)
    slice_row = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    assert slice_row.status == "ok"

    db = StockDB.open(root=tmp_path)
    try:
        source = db.get_source_document(doc.document_id)
        assert source is not None
        source.quality_status = "invalid"
        db.upsert_source_document(source)
    finally:
        db.close()

    plan = QueryPlan(
        plan_id="p-quality",
        trace_id="t-quality",
        query="2330 營收",
        normalized_query="2330 營收",
        ticker="2330",
        resource_needs=["fundamental"],
    )
    result = MonthlyRevenueAdapter(root=tmp_path).collect(plan)
    assert result.status == "ok"
    packet = result.evidence[0]
    assert packet.get("provenance_status") == "unresolved"
    assert not packet.get("source_document_id")
    assert packet.get("metadata", {}).get("cache_only") is True
    assert any("quality_status=invalid" in w for w in result.warnings)


def test_monthly_revenue_published_at_does_not_fallback_to_period(tmp_path: Path) -> None:
    """缺上游發布時間時不可用 year_month／as_of 冒充 published_at。"""
    doc, _ = _register_raw(tmp_path)
    result = ingest_monthly_revenue_from_source_document(
        root=tmp_path, source_document_id=doc.document_id, ticker="2330",
    )
    assert result.status == "ok"
    db = StockDB.open(root=tmp_path)
    try:
        research = db.get_research_document(result.research_document_id)
        evidence = db.get_evidence(result.evidence_id)
        canonical = db.get_monthly_revenue("2330", "2025-10")[0]
        assert research is not None and evidence is not None
        assert research.published_at == ""
        assert evidence.published_at == ""
        assert evidence.as_of == "2025-10"
        assert evidence.retrieved_at == "2026-09-18T10:00:00+08:00"
        assert canonical.source_date == ""
        assert canonical.retrieved_at == "2026-09-18T10:00:00+08:00"
    finally:
        db.close()
