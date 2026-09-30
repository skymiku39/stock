from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bot.answer_verifier import verify_answer_payload
from bot.evidence import EvidencePack, make_evidence
from bot.memory_store import MemoryPolicy, MemoryStore
from bot.query_plan import plan_query
from bot.stock_db import (
    EvidenceItemRow,
    ResearchChunkRow,
    ResearchDocumentRow,
    SourceDocumentRow,
    StockDB,
)
from bot.source_registry import SourceRegistry


def test_research_tables_and_exact_source_identity(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    assert db.latest_schema_version() == 5
    source = SourceRegistry(tmp_path).capture_document(
        "fixture", "原始文件 2330 10%", source_ref="fixture:1", title="fixture"
    )
    document = SourceRegistry(tmp_path).materialize_research_document(
        source.document_id, "研究正文：2330 的數值為 10%。", symbol="2330"
    )
    db.upsert_evidence(EvidenceItemRow(
        evidence_id="fixture:evidence",
        source_document_id=source.document_id,
        source="fixture",
        excerpt="2330 的數值為 10%。",
        quality_status="ok",
    ))
    db.upsert_research_chunk(ResearchChunkRow(
        chunk_id="fixture:chunk", document_id=document.document_id, text="2330 10%"
    ))
    assert db.get_evidence("fixture:evidence").source_document_id == source.document_id
    assert db.search_research_chunks("2330")[0].document_id == document.document_id
    with pytest.raises(ValueError):
        db.upsert_evidence(EvidenceItemRow(
            evidence_id="bad", source_document_id="not-an-exact-document"
        ))


def test_answer_verifier_checks_numeric_date_and_conflict() -> None:
    first = make_evidence(
        "news", source="official", source_ref="a",
        excerpt="2026-09-17 公司營收為 10%。",
    )
    second = make_evidence(
        "news", source="news", source_ref="b",
        excerpt="2026-09-17 公司營收為 12%。",
    )
    pack = EvidencePack(query="q", plan_id="p", trace_id="t", items=[first, second])
    result = verify_answer_payload({
        "answer": "我不確定，來源對數值有衝突。",
        "confidence": 0.2,
        "evidence_ids": [first["id"], second["id"]],
        "claims": [{
            "text": "2026-09-17 公司營收為 10%",
            "evidence_ids": [first["id"], second["id"]],
            "uncertainty": "來源衝突",
        }],
    }, pack)
    assert result.valid is True
    assert result.conflicts


def test_long_memory_capacity_archives_low_value_and_reports_warning(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    store = MemoryStore(db, policy=MemoryPolicy(
        max_active_long_items=10, max_active_long_chars=100,
    ))
    store.put_long("a" * 90, memory_id="long:a", importance=0.1)
    report = store.capacity_report()
    assert report["long"]["warning"] is True
    store.put_long("b" * 90, memory_id="long:b", importance=0.9)
    assert db.get_memory_item("long:a").memory_status == "archived"
    assert db.get_memory_item("long:b").memory_status == "active"


def test_follow_up_reuses_trace_and_live_is_opt_in(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    from bot.ask import AskEngine

    with patch("bot.ask.WebSearchAdapter") as adapter:
        engine = AskEngine(
            root=tmp_path, db=db, enable_live_sources=False, use_llm_planner=False,
        )
        result = engine.ask("2330 最新新聞？", ticker="2330")
    assert result.trace_id
    assert db.get_ask_run(result.trace_id)["trace_id"] == result.trace_id
    adapter.assert_not_called()
    follow = AskEngine(
        root=tmp_path, db=db, enable_live_sources=False, use_llm_planner=False,
    ).follow_up(result.trace_id)
    assert follow.trace_id == result.trace_id
