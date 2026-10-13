from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from bot.mops_conference_research import (
    materialize_mops_conference_calendar_slice,
)
from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB


def _payload() -> dict:
    return {
        "year": 2026,
        "month": 9,
        "fetched_at": "2026-09-20T11:00:00+08:00",
        "entries": [
            {
                "date": "2026-09-22",
                "time": "14:00",
                "ticker": "2330",
                "company": "台積電",
                "note": "法人說明會",
                "presentation_url": "https://example.test/2330.pdf",
            },
            {
                "date": "2026-09-22",
                "time": "14:00",
                "ticker": "2330",
                "company": "台積電",
                "note": "重複列",
                "presentation_url": "",
            },
            {
                "date": "2026-09-25",
                "time": "10:00",
                "ticker": "2454",
                "company": "聯發科",
                "note": "",
                "presentation_url": "",
            },
        ],
    }


def _capture(root: Path, payload: dict):
    with SourceRegistry(root) as registry:
        return registry.capture_document(
            "mops_conference_calendar",
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            source_url="mops://conference-calendar",
            source_ref="2026-09",
            title="MOPS 法說會行事曆 2026-09",
            kind="event_snapshot",
            suffix="json",
            mime_type="application/json",
            as_of="2026-09",
            retrieved_at="2026-09-20T11:00:00+08:00",
        )


def test_calendar_slice_materializes_and_reuses_exact_source(tmp_path: Path) -> None:
    document = _capture(tmp_path, _payload())
    first = materialize_mops_conference_calendar_slice(
        root=tmp_path, source_document_id=document.document_id,
    )
    second = materialize_mops_conference_calendar_slice(
        root=tmp_path, source_document_id=document.document_id,
    )

    assert first.status == "ok"
    assert first.valid_entries == 2
    assert first.invalid_entries == 0
    assert first.duplicate_entries == 1
    assert second.status == "reused"
    assert second.claim_ids == first.claim_ids

    db = StockDB.open(root=tmp_path)
    try:
        evidence = db.get_evidence(first.evidence_id)
        assert evidence is not None
        assert evidence.source_document_id == document.document_id
        assert evidence.quality_status == "ok"
        assert len(first.claim_ids) == 2
        assert all(db.get_claim(claim_id).status == "verified" for claim_id in first.claim_ids)
        assert len(db.list_evidence_links(first.evidence_id)) == 1
    finally:
        db.close()


def test_calendar_slice_rejects_wrong_source_domain(tmp_path: Path) -> None:
    with SourceRegistry(tmp_path) as registry:
        document = registry.capture_document(
            "mops_material_detail",
            b"{}",
            source_ref="wrong",
            suffix="json",
            mime_type="application/json",
        )
    result = materialize_mops_conference_calendar_slice(
        root=tmp_path, source_document_id=document.document_id,
    )
    assert result.status == "failed"
    assert "來源域不符" in result.error


def test_calendar_slice_empty_and_invalid_entries_are_not_claimed(tmp_path: Path) -> None:
    payload = _payload()
    payload["entries"] = [{"date": "not-a-date", "ticker": "2330"}]
    document = _capture(tmp_path, payload)
    result = materialize_mops_conference_calendar_slice(
        root=tmp_path, source_document_id=document.document_id,
    )
    assert result.status == "empty"
    assert result.invalid_entries == 1
    assert result.claim_ids == []
