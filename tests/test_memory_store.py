from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import memory_cli
from bot.memory_store import MemoryPolicy, MemoryStore
from bot.stock_db import EvidenceItemRow, LlmAnalysisRow, LlmDailyReportRow, StockDB


def _store(tmp_path: Path, **policy_kwargs) -> MemoryStore:
    db = StockDB.open(path=tmp_path / "memory.db")
    return MemoryStore(db, policy=MemoryPolicy(**policy_kwargs))


def test_memory_cli_help_renders_percent_in_capacity_description() -> None:
    with pytest.raises(SystemExit) as exc_info:
        memory_cli.main(["--help"])
    assert exc_info.value.code == 0


def test_memory_cli_resolves_relative_db_from_root(tmp_path: Path, capsys) -> None:
    db = StockDB.open(path=tmp_path / "custom.db")
    db.close()

    assert memory_cli.main([
        "--root", str(tmp_path),
        "--db", "custom.db",
        "stats",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["events"] == 0
    assert payload["evidence_items"] == 0
    assert payload["short_active"] == 0
    assert payload["long_active"] == 0


def test_short_and_long_memory_keep_full_content_and_search_separately(tmp_path: Path) -> None:
    store = _store(tmp_path)
    short = store.put_short(
        "完整短期上下文：2330 今日法說會後需要重新確認毛利率。",
        summary="2330 法說會後確認毛利率",
        scope_type="ticker",
        scope_id="2330",
        subject="2330",
        source="test",
        quality_status="ok",
    )
    long = store.put_long(
        "長期研究摘要原文：2330 的資本支出與需求展望。",
        summary="2330 資本支出與需求展望",
        scope_type="ticker",
        scope_id="2330",
        subject="2330",
        source="test",
        quality_status="ok",
    )

    assert store.db.get_memory_item(short.memory_id).content == short.content
    assert store.db.get_memory_item(long.memory_id).content == long.content
    hits = store.search("2330", scope_type="ticker", scope_id="2330")
    assert {row.memory_id for row in hits} == {short.memory_id, long.memory_id}
    context = store.build_context("2330", scope_type="ticker", scope_id="2330")
    assert [row.memory_id for row in context.short] == [short.memory_id]
    assert [row.memory_id for row in context.long] == [long.memory_id]
    assert store.db.list_memory_events(short.memory_id)


def test_failed_memory_is_stored_but_excluded_from_normal_retrieval(tmp_path: Path) -> None:
    store = _store(tmp_path)
    failed = store.put_short(
        "failed LLM output 2330",
        summary="failed",
        subject="2330",
        quality_status="failed",
    )
    assert store.search("2330") == []
    assert store.search("2330", include_low_quality=True)[0].memory_id == failed.memory_id

    unknown = store.put_short("unknown quality 2330", subject="2330")
    assert store.search("2330") == []
    assert {row.memory_id for row in store.search("2330", include_low_quality=True)} == {
        failed.memory_id, unknown.memory_id,
    }


def test_promote_and_consolidate_archive_without_deleting_originals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _exact_evidence(store, "ev:promote-first", b"first-event")
    _exact_evidence(store, "ev:promote-second", b"second-event")
    first = store.put_short(
        "first full event 2330",
        subject="2330",
        quality_status="ok",
        evidence=["ev:promote-first"],
    )
    second = store.put_short(
        "second full event 2330",
        subject="2330",
        quality_status="ok",
        evidence=["ev:promote-second"],
    )

    promoted_outcome = store.promote(first.memory_id, summary="promoted 2330 event")
    assert promoted_outcome.ok is True
    promoted = promoted_outcome.memory
    assert promoted is not None
    assert promoted.memory_layer == "long"
    assert store.db.get_memory_item(first.memory_id).memory_status == "superseded"
    assert store.db.get_memory_item(promoted.memory_id).content == first.content

    consolidated = store.consolidate(
        [promoted.memory_id, second.memory_id],
        "two events consolidated into one long summary",
        scope_type="ticker",
        scope_id="2330",
    )
    assert consolidated is not None
    assert store.db.get_memory_item(promoted.memory_id).memory_status == "archived"
    assert store.db.get_memory_item(second.memory_id).memory_status == "archived"
    assert store.db.get_memory_item(second.memory_id).content == "second full event 2330"
    assert json.loads(consolidated.metadata_json)["children"] == [promoted.memory_id, second.memory_id]


def test_promote_rejects_unknown_failed_and_missing_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    unknown = store.put_short("unknown mem", quality_status="unknown")
    failed = store.put_short("failed mem", quality_status="failed")
    assert store.promote(unknown.memory_id).ok is False
    assert store.promote(failed.memory_id).ok is False
    assert store.db.get_memory_item(unknown.memory_id).memory_layer == "short"
    assert store.db.get_memory_item(failed.memory_id).memory_status == "active"

    empty = store.put_short("empty evidence mem", quality_status="ok")
    empty_outcome = store.promote(empty.memory_id)
    assert empty_outcome.ok is False
    assert any("缺少可升級的 evidence" in reason for reason in empty_outcome.reasons)

    missing = store.put_short(
        "missing evidence mem",
        quality_status="ok",
        evidence=["evidence:does-not-exist"],
    )
    outcome = store.promote(missing.memory_id)
    assert outcome.ok is False
    assert any("缺少 evidence" in reason for reason in outcome.reasons)


def test_promote_rejects_manifest_when_raw_archive_is_missing_or_tampered(
    tmp_path: Path,
) -> None:
    from bot.source_registry import SourceRegistry
    from bot.stock_db import EvidenceItemRow

    store = _store(tmp_path)
    document = SourceRegistry(tmp_path, store.db).register_document(
        "fixture",
        b"replayable evidence",
        suffix="txt",
    )
    store.db.upsert_evidence(EvidenceItemRow(
        evidence_id="ev:replayability",
        kind="research",
        source="fixture",
        source_document_id=document.document_id,
        excerpt="replayable evidence",
        quality_status="ok",
    ))
    item = store.put_short(
        "replayability gate",
        quality_status="ok",
        evidence=["ev:replayability"],
    )
    assert store.evaluate_promote(item.memory_id).ok is True

    raw_path = tmp_path / document.content_path
    raw_path.write_bytes(b"tampered evidence")
    outcome = store.evaluate_promote(item.memory_id)
    assert outcome.ok is False
    assert any("可回放" in reason for reason in outcome.reasons)


def test_promote_rejects_conversation_turn_and_conflicted_claims(tmp_path: Path) -> None:
    from bot.source_registry import SourceRegistry
    from bot.stock_db import ClaimEvidenceRow, ClaimRow, EvidenceItemRow, LlmRunRow

    store = _store(tmp_path)
    turn = store.put_short(
        "ask conversation",
        memory_type="conversation_turn",
        quality_status="partial",
    )
    assert store.promote(turn.memory_id).ok is False

    document = SourceRegistry(tmp_path, store.db).register_document(
        "fixture",
        b"conflict evidence",
        suffix="txt",
    )
    evidence = EvidenceItemRow(
        evidence_id="ev:conflict",
        kind="fundamental",
        source="official",
        excerpt="營收 10%",
        quality_status="ok",
        source_document_id=document.document_id,
        metadata_json='{"provenance_status":"exact"}',
    )
    store.db.upsert_evidence(evidence)
    item = store.put_short(
        "research with conflict",
        quality_status="ok",
        evidence=["ev:conflict"],
    )
    store.db.upsert_llm_run(LlmRunRow(
        run_id="run:x",
        task_type="test",
        status="ok",
    ))
    store.db.upsert_claim(ClaimRow(
        claim_id="claim:conflict",
        run_id="run:x",
        text="營收 10%",
        status="conflicted",
    ))
    store.db.link_claim_evidence(ClaimEvidenceRow(
        claim_id="claim:conflict", evidence_id="ev:conflict", ordinal=0,
    ))
    outcome = store.promote(item.memory_id)
    assert outcome.ok is False
    assert any("claim conflict" in reason for reason in outcome.reasons)
    assert store.db.get_memory_item(item.memory_id).memory_layer == "short"


def test_expiry_and_capacity_are_logical_archive_not_hard_delete(tmp_path: Path) -> None:
    store = _store(tmp_path, max_active_short_items=1, max_active_short_chars=100_000)
    expired = store.put_short("expired 1111", expires_at="2000-01-01T00:00:00+08:00")
    assert store.db.get_memory_item(expired.memory_id).memory_status == "archived"
    first = store.put_short("first active 2222", quality_status="ok")
    second = store.put_short("second active 3333", quality_status="ok")
    active = store.db.list_memory_items(memory_layer="short", memory_status="active")
    assert len(active) == 1
    assert store.db.get_memory_item(first.memory_id).content == "first active 2222"
    assert store.db.get_memory_item(second.memory_id).content == "second active 3333"


def test_expired_active_memory_is_hidden_before_maintenance_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.put_short(
        "可能已過期但尚未被 maintenance 處理的記憶 2330",
        expires_at="2099-01-01T00:00:00+08:00",
        quality_status="ok",
    )
    store.db.conn.execute(
        "UPDATE memory_items SET expires_at = ? WHERE memory_id = ?",
        ("2000-01-01T00:00:00+08:00", item.memory_id),
    )
    assert store.search("2330") == []
    assert store.db.get_memory_item(item.memory_id).memory_status == "active"


def test_capacity_report_warns_without_archiving_at_threshold(tmp_path: Path) -> None:
    store = _store(tmp_path, max_active_short_items=2, max_active_short_chars=8)
    item = store.put_short("12345678", quality_status="ok")

    report = store.capacity_report()

    assert report["short"]["active_items"] == 1
    assert report["short"]["char_ratio"] == 1.0
    assert report["short"]["warning"] is True
    assert store.db.get_memory_item(item.memory_id).memory_status == "active"


def test_llm_call_memory_preserves_prompt_and_output(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = store.record_llm_call(SimpleNamespace(
        ts="2026-09-09T12:00:00+08:00",
        prompt_id="research_ticker",
        prompt_version="1.0",
        model="test-model",
        input="完整 prompt 2330",
        output="完整 output 2330",
        metadata={"ticker": "2330"},
        success=True,
        error="",
    ))
    payload = json.loads(row.content)
    assert payload["input"] == "完整 prompt 2330"
    assert payload["output"] == "完整 output 2330"


def test_daily_report_is_mirrored_to_long_memory(tmp_path: Path) -> None:
    from bot.source_registry import SourceRegistry

    db = StockDB.open(path=tmp_path / "report.db")
    document = SourceRegistry(tmp_path, db).register_document(
        "fixture", b"daily report evidence", suffix="txt",
    )
    db.upsert_evidence(EvidenceItemRow(
        evidence_id="ev:daily-report",
        kind="research",
        source="fixture",
        source_document_id=document.document_id,
        excerpt="daily report evidence",
        quality_status="ok",
        metadata_json='{"provenance_status":"exact"}',
    ))
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="next_day_watch",
        report_date="2026-09-09",
        mode="draft",
        asof="2026-09-08",
        brief_md="2330 明日觀察",
        payload_json='{"ticker":"2330"}',
        quality_status="ok",
        evidence_json='["ev:daily-report"]',
    ))
    rows = db.list_memory_items(memory_layer="long", scope_type="global")
    assert len(rows) == 1
    assert rows[0].memory_type == "daily_report"
    assert rows[0].content == '{"ticker":"2330"}'


def test_failed_daily_report_is_not_mirrored_to_long_memory(tmp_path: Path) -> None:
    db = StockDB.open(path=tmp_path / "failed-report.db")
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="intraday",
        report_date="2026-09-09",
        asof="2026-09-09T10:00:00+08:00",
        brief_md="LLM 呼叫失敗，不應成為長期記憶",
        payload_json='{"error":"disabled"}',
        quality_status="failed",
    ))
    assert db.list_memory_items(memory_layer="long", scope_type="global") == []


def test_database_backfill_is_idempotent_for_valid_long_memory(tmp_path: Path) -> None:
    from bot.source_registry import SourceRegistry

    db = StockDB.open(path=tmp_path / "backfill.db")
    document = SourceRegistry(tmp_path, db).register_document(
        "fixture", b"analysis evidence", suffix="txt",
    )
    db.upsert_evidence(EvidenceItemRow(
        evidence_id="ev:analysis-1",
        kind="research",
        source="fixture",
        source_document_id=document.document_id,
        excerpt="analysis evidence",
        quality_status="ok",
        metadata_json='{"provenance_status":"exact"}',
    ))
    db.upsert_llm_analysis(LlmAnalysisRow(
        id="analysis-1",
        ts="2026-09-09T12:00:00+08:00",
        ticker="2330",
        summary="2330 研究摘要",
        raw_output='{"summary":"2330 研究摘要"}',
        confidence=0.8,
        success=1,
        quality_status="ok",
        evidence_json='["ev:analysis-1"]',
    ))
    store = MemoryStore(db)
    first = store.backfill_database()
    second = store.backfill_database()
    assert first["long_analyses"] == 1
    assert second["long_analyses"] == 1
    rows = db.list_memory_items(memory_layer="long", memory_type="research_summary")
    assert len(rows) == 1


def test_database_backfill_reports_failed_llm_skip_reason(tmp_path: Path) -> None:
    db = StockDB.open(path=tmp_path / "failed-backfill.db")
    db.upsert_llm_analysis(LlmAnalysisRow(
        id="failed-analysis",
        ts="2026-09-09T12:00:00+08:00",
        raw_output='{"error":"disabled"}',
        success=0,
        quality_status="unknown",
    ))

    result = MemoryStore(db).backfill_database()

    assert result["long_analyses"] == 0
    assert result["long_analysis_skipped"] == 1
    assert result["skip_reasons"] == {"llm_failed": 1}
    assert db.list_memory_items(memory_layer="long") == []


def test_evidence_packet_is_registered_and_linked_to_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    from bot.source_registry import SourceRegistry

    document = SourceRegistry(store.root, store.db).register_document(
        "fixture", "完整可驗證的原文片段".encode("utf-8"), suffix="txt",
    )
    item = store.put_long(
        "2330 研究結論完整內容",
        quality_status="ok",
        evidence=[{
            "id": "news:2330:20260909",
            "kind": "news",
            "source": "official_news",
            "source_ref": "article-1",
            "title": "官方公告",
            "url": "https://example.test/article-1",
            "observed_at": "2026-09-09T10:00:00+08:00",
            "excerpt": "完整可驗證的原文片段",
            "quality_status": "ok",
            "source_document_id": document.document_id,
            "provenance_status": "exact",
        }],
    )

    evidence = store.db.get_evidence("news:2330:20260909")
    assert evidence is not None
    assert evidence.excerpt == "完整可驗證的原文片段"
    assert [row.evidence_id for row in store.db.list_memory_evidence(item.memory_id)] == [
        "news:2330:20260909",
    ]


def test_memory_proposal_requires_approval_and_keeps_audit_trail(tmp_path: Path) -> None:
    store = _store(tmp_path, max_active_long_items=10)
    proposal = store.propose(
        "管理者核准後才可使用的頻道規則",
        memory_type="channel_rule",
        scope_type="channel",
        scope_id="demo",
        quality_status="ok",
        proposer="moderator",
    )
    assert store.db.list_memory_items(memory_status="active") == []
    assert store.db.get_memory_proposal(proposal.proposal_id).status == "pending"

    item = store.approve_proposal(proposal.proposal_id, reviewer="owner", review_note="確認")
    assert item is not None
    assert item.content == "管理者核准後才可使用的頻道規則"
    saved_proposal = store.db.get_memory_proposal(proposal.proposal_id)
    assert saved_proposal.status == "approved"
    assert saved_proposal.target_memory_id == item.memory_id
    assert any(event.event_type == "approved_from_proposal" for event in store.db.list_memory_events(item.memory_id))
    assert store.approve_proposal(proposal.proposal_id) is None


def test_memory_proposal_can_be_rejected_without_creating_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal = store.propose("不應直接進入長期記憶的內容", quality_status="ok")
    assert store.reject_proposal(proposal.proposal_id, reviewer="owner", review_note="無法驗證")
    assert store.db.get_memory_proposal(proposal.proposal_id).status == "rejected"
    assert store.db.list_memory_items(memory_status="active") == []


def test_memory_update_proposal_preserves_existing_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _exact_evidence(store, "ev:update", b"updatable evidence")
    item = store.put_long(
        "舊版研究內容",
        summary="舊摘要",
        quality_status="ok",
        evidence=["ev:update"],
    )
    proposal = store.propose(
        "更新後研究內容",
        proposal_type="update",
        target_memory_id=item.memory_id,
        summary="新摘要",
        quality_status="ok",
    )
    updated = store.approve_proposal(proposal.proposal_id, reviewer="owner")
    assert updated is not None
    assert updated.memory_id == item.memory_id
    assert updated.content == "更新後研究內容"
    assert json.loads(updated.evidence_json) == ["ev:update"]
    assert updated.revision == item.revision + 1


def test_memory_consolidate_proposal_archives_children_and_keeps_evidence(
    tmp_path: Path,
) -> None:
    from bot.source_registry import SourceRegistry

    store = _store(tmp_path)
    registry = SourceRegistry(tmp_path, store.db)
    first_doc = registry.register_document("fixture", b"first evidence", suffix="txt")
    second_doc = registry.register_document("fixture", b"second evidence", suffix="txt")
    first = store.put_long(
        "第一段研究原文 2330",
        quality_status="ok",
        evidence=[{
            "id": "ev:first",
            "kind": "official",
            "source": "test",
            "excerpt": "第一段證據",
            "quality_status": "ok",
            "source_document_id": first_doc.document_id,
            "provenance_status": "exact",
        }],
    )
    second = store.put_long(
        "第二段研究原文 2330",
        quality_status="ok",
        evidence=[{
            "id": "ev:second",
            "kind": "official",
            "source": "test",
            "excerpt": "第二段證據",
            "quality_status": "ok",
            "source_document_id": second_doc.document_id,
            "provenance_status": "exact",
        }],
    )
    proposal = store.propose(
        "合併後保留的完整研究內容",
        proposal_type="consolidate",
        target_memory_id=f"{first.memory_id},{second.memory_id}",
        summary="2330 合併研究摘要",
        quality_status="ok",
    )
    merged = store.approve_proposal(proposal.proposal_id, reviewer="owner")
    assert merged is not None
    assert merged.memory_layer == "long"
    assert json.loads(merged.evidence_json) == ["ev:first", "ev:second"]
    assert store.db.get_memory_item(first.memory_id).memory_status == "archived"
    assert store.db.get_memory_item(second.memory_id).memory_status == "archived"
    assert json.loads(merged.metadata_json)["children"] == [first.memory_id, second.memory_id]


def test_promote_rejects_cache_only_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.db.upsert_evidence(EvidenceItemRow(
        evidence_id="ev:cache-only",
        kind="technical",
        source="yfinance",
        excerpt="cache snapshot",
        quality_status="ok",
        metadata_json='{"provenance_status":"cache_only","cache_only":true}',
    ))
    item = store.put_short(
        "cache only should not promote",
        quality_status="ok",
        evidence=["ev:cache-only"],
    )
    outcome = store.evaluate_promote(item.memory_id)
    assert outcome.ok is False
    assert any("cache_only" in reason for reason in outcome.reasons)
    assert store.promote(item.memory_id).ok is False
    assert store.db.get_memory_item(item.memory_id).memory_layer == "short"


def test_promote_rejects_unresolved_and_legacy_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.db.upsert_evidence(EvidenceItemRow(
        evidence_id="ev:unresolved",
        kind="news",
        source="legacy",
        excerpt="old packet",
        quality_status="ok",
        metadata_json='{"provenance_status":"unresolved"}',
    ))
    unresolved = store.put_short(
        "unresolved evidence",
        quality_status="ok",
        evidence=["ev:unresolved"],
    )
    unresolved_outcome = store.evaluate_promote(unresolved.memory_id)
    assert unresolved_outcome.ok is False
    assert any("unresolved" in reason for reason in unresolved_outcome.reasons)

    store.db.upsert_evidence(EvidenceItemRow(
        evidence_id="ev:legacy",
        kind="news",
        source="legacy",
        excerpt="legacy unlinked",
        quality_status="ok",
        metadata_json='{"provenance_status":"legacy_unlinked","legacy":"unlinked"}',
    ))
    legacy = store.put_short(
        "legacy evidence",
        quality_status="ok",
        evidence=["ev:legacy"],
    )
    legacy_outcome = store.evaluate_promote(legacy.memory_id)
    assert legacy_outcome.ok is False
    assert any("legacy" in reason for reason in legacy_outcome.reasons)


def test_promote_accepts_composite_exact_with_replayable_document_ids(
    tmp_path: Path,
) -> None:
    from bot.source_registry import SourceRegistry

    store = _store(tmp_path)
    registry = SourceRegistry(tmp_path, store.db)
    first = registry.register_document("fixture", b"composite-a", suffix="txt")
    second = registry.register_document("fixture", b"composite-b", suffix="txt")
    store.db.upsert_evidence(EvidenceItemRow(
        evidence_id="ev:composite",
        kind="etf",
        source="official",
        excerpt="composite holdings",
        quality_status="ok",
        source_document_id=first.document_id,
        metadata_json=json.dumps({
            "provenance_status": "composite_exact",
            "source_document_ids": [first.document_id, second.document_id],
        }),
    ))
    item = store.put_short(
        "composite exact research",
        quality_status="ok",
        evidence=["ev:composite"],
    )
    outcome = store.promote(item.memory_id, summary="composite long")
    assert outcome.ok is True
    assert outcome.memory is not None
    assert outcome.memory.memory_layer == "long"
    assert json.loads(outcome.memory.evidence_json) == ["ev:composite"]
    assert [row.evidence_id for row in store.db.list_memory_evidence(outcome.memory.memory_id)] == [
        "ev:composite",
    ]


def test_consolidate_rejects_cache_only_without_archiving_children(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.db.upsert_evidence(EvidenceItemRow(
        evidence_id="ev:cache-consolidate",
        kind="technical",
        source="cache",
        excerpt="cache only",
        quality_status="ok",
        metadata_json='{"provenance_status":"cache_only","cache_only":true}',
    ))
    first = store.put_short(
        "first with cache evidence",
        quality_status="ok",
        evidence=["ev:cache-consolidate"],
    )
    second = store.put_short("second plain", quality_status="ok")
    merged = store.consolidate(
        [first.memory_id, second.memory_id],
        "should not consolidate cache_only",
    )
    assert merged is None
    assert store.db.get_memory_item(first.memory_id).memory_status == "active"
    assert store.db.get_memory_item(second.memory_id).memory_status == "active"


def _exact_evidence(store: MemoryStore, evidence_id: str, payload: bytes = b"exact") -> str:
    from bot.source_registry import SourceRegistry

    document = SourceRegistry(store.root, store.db).register_document(
        "fixture", payload, suffix="txt",
    )
    store.db.upsert_evidence(EvidenceItemRow(
        evidence_id=evidence_id,
        kind="research",
        source="fixture",
        source_document_id=document.document_id,
        excerpt=payload.decode("utf-8", errors="ignore") or evidence_id,
        quality_status="ok",
        metadata_json='{"provenance_status":"exact"}',
    ))
    return document.document_id


def test_remember_llm_analysis_rejects_empty_and_bad_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    empty = store.remember_llm_analysis(LlmAnalysisRow(
        id="a-empty",
        ts="2026-09-09T12:00:00+08:00",
        ticker="2330",
        summary="empty",
        raw_output='{"summary":"empty"}',
        confidence=0.8,
        success=1,
        quality_status="ok",
        evidence_json="[]",
    ))
    assert empty.ok is False
    assert any("缺少可升級的 evidence" in reason for reason in empty.reasons)
    assert store.db.list_memory_items(memory_layer="long") == []

    store.db.upsert_evidence(EvidenceItemRow(
        evidence_id="ev:cache-mirror",
        kind="technical",
        source="yfinance",
        excerpt="cache",
        quality_status="ok",
        metadata_json='{"provenance_status":"cache_only","cache_only":true}',
    ))
    cache_only = store.remember_llm_analysis(LlmAnalysisRow(
        id="a-cache",
        ts="2026-09-09T12:00:00+08:00",
        ticker="2330",
        summary="cache",
        raw_output='{"summary":"cache"}',
        confidence=0.8,
        success=1,
        quality_status="ok",
        evidence_json='["ev:cache-mirror"]',
    ))
    assert cache_only.ok is False
    assert any("cache_only" in reason for reason in cache_only.reasons)

    for eid, provenance in (
        ("ev:unresolved-mirror", "unresolved"),
        ("ev:legacy-mirror", "legacy_unlinked"),
    ):
        store.db.upsert_evidence(EvidenceItemRow(
            evidence_id=eid,
            kind="news",
            source="legacy",
            excerpt=provenance,
            quality_status="ok",
            metadata_json=json.dumps({"provenance_status": provenance}),
        ))
        outcome = store.remember_llm_analysis(LlmAnalysisRow(
            id=f"a-{provenance}",
            ts="2026-09-09T12:00:00+08:00",
            ticker="2330",
            summary=provenance,
            raw_output="{}",
            confidence=0.5,
            success=1,
            quality_status="ok",
            evidence_json=json.dumps([eid]),
        ))
        assert outcome.ok is False
        assert any(provenance.split("_")[0] in reason for reason in outcome.reasons)

    doc_id = _exact_evidence(store, "ev:tamper-mirror", b"original")
    document = store.db.get_source_document(doc_id)
    (tmp_path / document.content_path).write_bytes(b"tampered")
    tampered = store.remember_llm_analysis(LlmAnalysisRow(
        id="a-tamper",
        ts="2026-09-09T12:00:00+08:00",
        ticker="2330",
        summary="tamper",
        raw_output="{}",
        confidence=0.5,
        success=1,
        quality_status="ok",
        evidence_json='["ev:tamper-mirror"]',
    ))
    assert tampered.ok is False
    assert any("可回放" in reason for reason in tampered.reasons)
    assert store.db.list_memory_items(memory_layer="long") == []


def test_remember_llm_analysis_rejects_conflicted_and_accepts_exact(
    tmp_path: Path,
) -> None:
    from bot.stock_db import ClaimEvidenceRow, ClaimRow, LlmRunRow

    store = _store(tmp_path)
    _exact_evidence(store, "ev:conflict-mirror", b"conflict")
    store.db.upsert_llm_run(LlmRunRow(run_id="run:mirror", task_type="test", status="ok"))
    store.db.upsert_claim(ClaimRow(
        claim_id="claim:mirror-conflict",
        run_id="run:mirror",
        text="衝突",
        status="conflicted",
    ))
    store.db.link_claim_evidence(ClaimEvidenceRow(
        claim_id="claim:mirror-conflict", evidence_id="ev:conflict-mirror", ordinal=0,
    ))
    conflicted = store.remember_llm_analysis(LlmAnalysisRow(
        id="a-conflict",
        ts="2026-09-09T12:00:00+08:00",
        ticker="2330",
        summary="conflict",
        raw_output='{"summary":"conflict"}',
        confidence=0.7,
        success=1,
        quality_status="ok",
        evidence_json='["ev:conflict-mirror"]',
    ))
    assert conflicted.ok is False
    assert any("claim conflict" in reason for reason in conflicted.reasons)

    store.db.upsert_llm_analysis(LlmAnalysisRow(
        id="a-conflict",
        ts="2026-09-09T12:00:00+08:00",
        ticker="2330",
        summary="conflict",
        raw_output='{"summary":"conflict"}',
        confidence=0.7,
        success=1,
        quality_status="ok",
        evidence_json='["ev:conflict-mirror"]',
    ))
    assert any(row.id == "a-conflict" for row in store.db.list_llm_analysis())
    assert store.db.list_memory_items(memory_layer="long") == []

    _exact_evidence(store, "ev:ok-mirror", b"ok-analysis")
    ok = store.remember_llm_analysis(LlmAnalysisRow(
        id="a-ok",
        ts="2026-09-09T12:00:00+08:00",
        ticker="2330",
        summary="ok",
        raw_output='{"summary":"ok"}',
        confidence=0.9,
        success=1,
        quality_status="ok",
        evidence_json='["ev:ok-mirror"]',
    ))
    assert ok.ok is True
    assert ok.memory is not None
    assert ok.memory.memory_layer == "long"
    assert json.loads(ok.memory.evidence_json) == ["ev:ok-mirror"]


def test_remember_daily_report_mirror_gates_and_composite_exact(tmp_path: Path) -> None:
    from bot.source_registry import SourceRegistry

    store = _store(tmp_path)
    empty = store.remember_daily_report(LlmDailyReportRow(
        report_type="intraday",
        report_date="2026-09-09",
        brief_md="empty",
        payload_json="{}",
        quality_status="ok",
        evidence_json="[]",
    ))
    assert empty.ok is False
    assert any("缺少可升級的 evidence" in reason for reason in empty.reasons)

    store.db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="intraday",
        report_date="2026-09-09",
        brief_md="empty canonical kept",
        payload_json='{"kept":true}',
        quality_status="ok",
        evidence_json="[]",
    ))
    reports = store.db.list_llm_daily_reports()
    assert any(row.brief_md == "empty canonical kept" for row in reports)
    assert store.db.list_memory_items(memory_layer="long") == []

    registry = SourceRegistry(tmp_path, store.db)
    first = registry.register_document("fixture", b"comp-a", suffix="txt")
    second = registry.register_document("fixture", b"comp-b", suffix="txt")
    store.db.upsert_evidence(EvidenceItemRow(
        evidence_id="ev:comp-report",
        kind="research",
        source="fixture",
        source_document_id=first.document_id,
        excerpt="composite",
        quality_status="ok",
        metadata_json=json.dumps({
            "provenance_status": "composite_exact",
            "source_document_ids": [first.document_id, second.document_id],
        }),
    ))
    ok = store.remember_daily_report(LlmDailyReportRow(
        report_type="next_day_watch",
        report_date="2026-09-10",
        mode="draft",
        brief_md="composite ok",
        payload_json='{"ok":true}',
        quality_status="ok",
        evidence_json='["ev:comp-report"]',
    ))
    assert ok.ok is True
    assert ok.memory is not None
    assert ok.memory.memory_layer == "long"
    assert json.loads(ok.memory.evidence_json) == ["ev:comp-report"]


def test_put_long_gates_research_types_but_allows_preference_without_evidence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    preference = store.put_long(
        "偏好使用繁體中文摘要",
        memory_type="preference",
        quality_status="ok",
    )
    assert preference.memory_layer == "long"
    assert preference.memory_type == "preference"

    context = store.put_long(
        "非市場上下文備註",
        memory_type="context",
        quality_status="ok",
    )
    assert context.memory_type == "context"

    with pytest.raises(ValueError, match="缺少可升級的 evidence"):
        store.put_long(
            "沒有 evidence 的研究摘要",
            memory_type="research_summary",
            quality_status="ok",
        )
    with pytest.raises(ValueError, match="缺少可升級的 evidence"):
        store.put_long(
            "沒有 evidence 的決策",
            memory_type="decision",
            quality_status="ok",
        )

    store.db.upsert_evidence(EvidenceItemRow(
        evidence_id="ev:unresolved-put",
        kind="news",
        source="legacy",
        excerpt="unresolved",
        quality_status="ok",
        metadata_json='{"provenance_status":"unresolved"}',
    ))
    with pytest.raises(ValueError, match="exact／composite_exact"):
        store.put_long(
            "非 exact 研究摘要",
            memory_type="research_summary",
            quality_status="ok",
            evidence=["ev:unresolved-put"],
        )

    _exact_evidence(store, "ev:decision-ok", b"decision-evidence")
    decision = store.put_long(
        "已核對的決策紀錄",
        memory_type="decision",
        quality_status="ok",
        evidence=["ev:decision-ok"],
    )
    assert decision.memory_type == "decision"
    assert json.loads(decision.evidence_json) == ["ev:decision-ok"]


def test_proposal_approval_gates_research_but_keeps_preference_compatible(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    preference = store.propose(
        "頻道偏好：先給結論",
        memory_type="preference",
        quality_status="ok",
    )
    approved_preference = store.approve_proposal(preference.proposal_id, reviewer="owner")
    assert approved_preference is not None
    assert approved_preference.memory_type == "preference"

    empty_research = store.propose(
        "空 evidence 研究提案",
        memory_type="research_summary",
        quality_status="ok",
        evidence=[],
    )
    assert store.approve_proposal(empty_research.proposal_id, reviewer="owner") is None
    assert store.db.get_memory_proposal(empty_research.proposal_id).status == "pending"
    assert store.db.list_memory_items(memory_type="research_summary") == []

    _exact_evidence(store, "ev:proposal-research", b"proposal-research")
    ok = store.propose(
        "有 exact evidence 的研究提案",
        memory_type="research_summary",
        quality_status="ok",
        evidence=["ev:proposal-research"],
    )
    approved = store.approve_proposal(ok.proposal_id, reviewer="owner")
    assert approved is not None
    assert approved.memory_type == "research_summary"
    assert json.loads(approved.evidence_json) == ["ev:proposal-research"]
