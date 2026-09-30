from __future__ import annotations

from pathlib import Path

from bot.mops_presentation_research import materialize_mops_presentation_slice
from bot.mops_scraper import download_presentation
from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB
import json


_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<<>>endobj\n"
    b"trailer<<>>\n"
    b"%%EOF\n"
)


def test_mops_presentation_slice_is_exact_and_idempotent(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    try:
        document = SourceRegistry(tmp_path, db=db).capture_document(
            "mops_presentation",
            _MINIMAL_PDF,
            source_url="https://example.invalid/2330.pdf",
            source_ref="https://example.invalid/2330.pdf",
            title="2330_2026-09-19.pdf",
            suffix="pdf",
            mime_type="application/pdf",
            as_of="2026-09-19",
        )
        first = materialize_mops_presentation_slice(
            root=tmp_path,
            source_document_id=document.document_id,
            canonical_text="Revenue outlook\nCapex plan",
            ticker="2330",
            conference_date="2026-09-19",
            pages=2,
            db=db,
        )
        assert first.status == "ok"
        assert db.get_research_document(first.research_document_id).source_document_id == document.document_id
        assert db.get_evidence(first.evidence_id).source_document_id == document.document_id
        assert db.get_claim(first.claim_id).status == "verified"

        second = materialize_mops_presentation_slice(
            root=tmp_path,
            source_document_id=document.document_id,
            canonical_text="Revenue outlook\nCapex plan",
            ticker="2330",
            conference_date="2026-09-19",
            pages=2,
            db=db,
        )
        assert second.status == "reused"
        assert second.research_document_id == first.research_document_id
        assert second.evidence_id == first.evidence_id
    finally:
        db.close()


def test_mops_presentation_rejects_hash_mismatch_and_missing_raw(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    try:
        document = SourceRegistry(tmp_path, db=db).capture_document(
            "mops_presentation",
            _MINIMAL_PDF,
            source_url="https://example.invalid/2330.pdf",
            source_ref="https://example.invalid/2330.pdf",
            title="2330_2026-09-19.pdf",
            suffix="pdf",
            mime_type="application/pdf",
            as_of="2026-09-19",
        )
        raw_path = tmp_path / document.content_path
        raw_path.write_bytes(_MINIMAL_PDF + b"\n#tampered")
        mismatched = materialize_mops_presentation_slice(
            root=tmp_path,
            source_document_id=document.document_id,
            canonical_text="Revenue outlook\nCapex plan",
            ticker="2330",
            conference_date="2026-09-19",
            pages=2,
            db=db,
        )
        assert mismatched.status == "failed"
        assert "hash mismatch" in mismatched.error
        assert db.list_research_documents() == []
        assert db.list_evidence() == []

        raw_path.write_bytes(_MINIMAL_PDF)
        first = materialize_mops_presentation_slice(
            root=tmp_path,
            source_document_id=document.document_id,
            canonical_text="Revenue outlook\nCapex plan",
            ticker="2330",
            conference_date="2026-09-19",
            pages=2,
            db=db,
        )
        assert first.status == "ok"
        evidence_before = first.evidence_id
        raw_path.unlink()
        missing = materialize_mops_presentation_slice(
            root=tmp_path,
            source_document_id=document.document_id,
            canonical_text="Revenue outlook\nCapex plan",
            ticker="2330",
            conference_date="2026-09-19",
            pages=2,
            db=db,
        )
        assert missing.status == "failed"
        assert "raw archive missing" in missing.error
        assert missing.status != "reused"
        assert db.get_evidence(evidence_before) is not None
    finally:
        db.close()


def test_download_presentation_connects_existing_raw_manifest(tmp_path: Path) -> None:
    payload = b"Revenue outlook\nCapex plan\n" * 8

    class Response:
        def __init__(self) -> None:
            self.content = payload
            self.headers = {"Content-Type": "text/plain; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, _chunk_size: int):
            yield self.content

    class Session:
        def get(self, _url: str, **_kwargs):
            return Response()

    dest = tmp_path / "data" / "mops_downloads" / "2330_2026-09-19.txt"
    first = download_presentation(
        "https://example.invalid/2330.txt",
        dest,
        session=Session(),
        root=tmp_path,
    )
    assert first is not None
    assert first.source_document_id.startswith("source-doc:")
    assert first.research_status == "ok"
    assert first.research_document_id
    assert first.evidence_id

    second = download_presentation(
        "https://example.invalid/2330.txt",
        dest,
        session=Session(),
        root=tmp_path,
    )
    assert second is not None
    assert second.research_status == "reused"
    assert second.research_document_id == first.research_document_id
    db = StockDB.open(root=tmp_path)
    try:
        source = db.get_source_document(first.source_document_id)
        assert source.as_of == "2026-09-19"
        research = db.get_research_document(first.research_document_id)
        assert json.loads(research.metadata_json)["conference_date"] == "2026-09-19"
    finally:
        db.close()


def test_download_presentation_does_not_materialize_html_as_pdf(tmp_path: Path) -> None:
    payload = "<html><body>investor relations index</body></html>".encode("utf-16")

    class Response:
        content = payload
        headers = {"Content-Type": "text/html; charset=utf-16"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, _chunk_size: int):
            yield self.content

    class Session:
        def get(self, _url: str, **_kwargs):
            return Response()

    result = download_presentation(
        "https://example.invalid/index.pdf",
        tmp_path / "data" / "mops_downloads" / "2330_index.pdf",
        session=Session(),
        root=tmp_path,
    )
    assert result is not None
    assert result.research_status == "failed"
    assert "HTML" in result.research_error or "不是" in result.research_error
