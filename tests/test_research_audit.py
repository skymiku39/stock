from __future__ import annotations

import json
from pathlib import Path

from bot.mops_presentation_research import materialize_mops_presentation_slice
from bot.research_audit import audit_research_database
from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB


def test_research_audit_reports_type_mismatch_without_rewriting(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    try:
        source = SourceRegistry(tmp_path, db=db).capture_document(
            "mops_presentation",
            b"<html>not a pdf</html>",
            source_url="https://example.invalid/presentation.pdf",
            source_ref="https://example.invalid/presentation.pdf",
            title="presentation.pdf",
            suffix="pdf",
            mime_type="text/html; charset=utf-8",
        )
        result = materialize_mops_presentation_slice(
            root=tmp_path,
            source_document_id=source.document_id,
            canonical_text="not a verified presentation",
            ticker="2330",
            db=db,
        )
        assert result.status == "failed"
        assert db.get_research_document(result.research_document_id) is None

        report = audit_research_database(tmp_path / "data" / "stock.db", root=tmp_path)
        assert report["documents_checked"] == 0
        assert report["quality_status"] == "ok"
    finally:
        db.close()


def test_research_audit_distinguishes_capture_version_from_data_period(tmp_path: Path) -> None:
    with SourceRegistry(tmp_path) as registry:
        first = registry.capture_document(
            "fixture", b"version one", source_ref="same-ref", suffix="txt",
            retrieved_at="2026-09-20T10:00:00+08:00",
        )
        second = registry.capture_document(
            "fixture", b"version two", source_ref="same-ref", suffix="txt",
            retrieved_at="2026-09-20T11:00:00+08:00",
        )
        db = registry.db
        document = registry.materialize_research_document(
            first.document_id,
            "version one",
            document_type="fixture",
            metadata={"source_document_id": first.document_id},
        )
        assert document.source_document_id == first.document_id
        report = audit_research_database(tmp_path / "data" / "stock.db", root=tmp_path)
        assert report["documents_checked"] == 1
        assert report["errors"] == 0
        assert not report["versioning"]["unversioned_groups"]
        assert report["versioning"]["capture_versioned_groups"]
        group = report["versioning"]["capture_versioned_groups"][0]
        assert group["version_basis"] == "retrieved_at"
        assert group["as_of_values"] == [""]
        assert first.document_id in group["document_ids"]
        assert second.document_id in group["document_ids"]
        assert any(item["code"] == "source_period_unpinned" for item in report["findings"])


def test_research_audit_keeps_unversioned_when_capture_time_is_not_distinct(tmp_path: Path) -> None:
    with SourceRegistry(tmp_path) as registry:
        registry.capture_document(
            "fixture", b"version one", source_ref="same-ref", suffix="txt",
            retrieved_at="2026-09-20T10:00:00+08:00",
        )
        registry.capture_document(
            "fixture", b"version two", source_ref="same-ref", suffix="txt",
            retrieved_at="2026-09-20T10:00:00+08:00",
        )
        report = audit_research_database(tmp_path / "data" / "stock.db", root=tmp_path)

    assert report["versioning"]["unversioned_groups"]
    assert not report["versioning"]["capture_versioned_groups"]
    assert report["versioning"]["unversioned_groups"][0]["version_basis"] == "unresolved"
    codes = {item["code"] for item in report["findings"]}
    assert "source_version_unpinned" in codes


def test_research_audit_detects_date_order_and_raw_hash(tmp_path: Path) -> None:
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document(
            "fixture",
            b"research bytes",
            source_ref="date-check",
            suffix="txt",
            published_at="2026-09-20T12:00:00+08:00",
            retrieved_at="2026-09-20T11:00:00+08:00",
        )
        registry.materialize_research_document(
            source.document_id,
            "research bytes",
            document_type="fixture",
        )
        raw_path = tmp_path / source.content_path
        raw_path.write_bytes(b"tampered")
    report = audit_research_database(tmp_path / "data" / "stock.db", root=tmp_path)
    codes = {item["code"] for item in report["findings"]}
    assert "source_date_order" in codes
    assert "research_raw_hash_mismatch" in codes


def test_research_audit_accepts_quarter_period(tmp_path: Path) -> None:
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document(
            "quarterly_fixture", b"quarterly bytes", source_ref="quarterly",
            suffix="json",
        )
        registry.materialize_research_document(
            source.document_id,
            json.dumps({"period": "2026Q2"}),
            document_type="quarterly_report",
            published_at="2026Q2",
        )
    report = audit_research_database(tmp_path / "data" / "stock.db", root=tmp_path)
    assert report["errors"] == 0
    assert not any(item["code"] == "research_date_unparseable" for item in report["findings"])
