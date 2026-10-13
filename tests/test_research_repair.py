from __future__ import annotations

from pathlib import Path

from bot.mops_presentation_research import materialize_mops_presentation_slice
from bot.research_repair import (
    list_quarantined_research_chains,
    plan_quarantine_research_chain,
    quarantine_research_chain,
    repair_evidence_source_links,
)
from bot.source_audit_cli import audit_project
from bot.source_registry import SourceRegistry
from bot.stock_db import (
    EvidenceItemRow,
    LlmRunEvidenceRow,
    LlmRunRow,
    StockDB,
)


def test_quarantine_preserves_rows_but_blocks_invalid_evidence(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    try:
        with SourceRegistry(tmp_path, db=db) as registry:
            source = registry.capture_document(
                "mops_presentation",
                (
                    b"%PDF-1.4\n"
                    b"1 0 obj<<>>endobj\n"
                    b"trailer<<>>\n"
                    b"%%EOF\n"
                ),
                source_ref="bad-presentation",
                suffix="pdf",
                mime_type="application/pdf",
                as_of="2026-09-19",
            )
        # Seed a normal evidence/claim chain, then quarantine it explicitly as
        # a known bad derived source.
        sliced = materialize_mops_presentation_slice(
            root=tmp_path,
            source_document_id=source.document_id,
            canonical_text="bad presentation text",
            ticker="2330",
            conference_date="2026-09-19",
            db=db,
        )
        assert sliced.status == "ok"
        planned = plan_quarantine_research_chain(
            root=tmp_path,
            source_document_id=source.document_id,
            reason="fixture format mismatch",
            db=db,
        )
        assert planned["dry_run"] is True
        assert planned["status"] == "dry_run"
        assert planned["research_document_ids"] == [sliced.research_document_id]
        assert planned["evidence_ids"] == [sliced.evidence_id]
        assert planned["claim_ids"] == [sliced.claim_id]
        assert db.get_source_document(source.document_id).quality_status != "invalid"

        result = quarantine_research_chain(
            root=tmp_path,
            source_document_id=source.document_id,
            reason="fixture format mismatch",
            db=db,
        )
        assert result["status"] == "quarantined"
        assert result["dry_run"] is False
        assert result["research_documents"] == 1
        assert result["research_document_ids"] == [sliced.research_document_id]
        assert result["evidence_ids"] == [sliced.evidence_id]
        assert result["claim_ids"] == [sliced.claim_id]
        assert db.get_source_document(source.document_id).quality_status == "invalid"
        assert db.get_research_document(sliced.research_document_id).status == "unresolved"
        assert db.get_research_document(sliced.research_document_id).canonical_text == "bad presentation text"
        assert db.get_evidence(sliced.evidence_id).quality_status == "invalid"
        assert db.get_claim(sliced.claim_id).status == "unverified"

        listed = list_quarantined_research_chains(root=tmp_path, db=db)
        assert listed["count"] == 1
        assert listed["quarantined_chains"][0]["source_document_id"] == source.document_id
        assert listed["quarantined_chains"][0]["research_document_ids"] == [
            sliced.research_document_id
        ]
    finally:
        db.close()


def test_repair_evidence_source_links_is_exact_and_idempotent(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    try:
        with SourceRegistry(tmp_path, db=db) as registry:
            source = registry.capture_document(
                "fixture",
                b"immutable research source",
                source_ref="fixture-source",
                suffix="json",
                mime_type="application/json",
            )
        evidence_id = "evidence:repair-fixture"
        db.upsert_evidence(EvidenceItemRow(
            evidence_id=evidence_id,
            source="fixture",
            source_ref="fixture-source",
            excerpt="可核對原文",
            quality_status="ok",
            source_document_id=source.document_id,
        ))
        db.upsert_llm_run(LlmRunRow(
            run_id="run:repair-fixture",
            task_type="fixture",
            status="source_ingested",
            metadata_json='{"source_document_id":"%s"}' % source.document_id,
        ))
        db.link_llm_run_evidence(LlmRunEvidenceRow(
            run_id="run:repair-fixture",
            evidence_id=evidence_id,
        ))

        dry_run = repair_evidence_source_links(root=tmp_path, db=db, apply=False)
        assert dry_run["dry_run"] is True
        assert dry_run["links_added"] == 1
        assert db.list_evidence_links(evidence_id) == []
        before_audit = audit_project(tmp_path)
        assert before_audit["database"]["research_missing_source_links"] == 1

        applied = repair_evidence_source_links(root=tmp_path, db=db, apply=True)
        assert applied["links_added"] == 1
        assert len(db.list_evidence_links(evidence_id)) == 1
        after_audit = audit_project(tmp_path)
        assert after_audit["database"]["research_missing_source_links"] == 0

        repeated = repair_evidence_source_links(root=tmp_path, db=db, apply=True)
        assert repeated["links_added"] == 0
        assert repeated["already_linked"] >= 1
    finally:
        db.close()
