from __future__ import annotations

import json
from pathlib import Path

from bot.mops_presentation_research import materialize_mops_presentation_slice
from bot.research_quarantine_cli import main
from bot.research_repair import quarantine_research_chain
from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB


def test_research_quarantine_cli_defaults_to_dry_run(tmp_path: Path, capsys) -> None:
    assert main(["--root", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["dry_run"] is True
    assert report["status"] == "dry_run"
    assert report["source_document_id"] == ""


def test_research_quarantine_cli_list_output(tmp_path: Path, capsys) -> None:
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
                source_ref="bad-presentation-list",
                suffix="pdf",
                mime_type="application/pdf",
                as_of="2026-09-19",
            )
        sliced = materialize_mops_presentation_slice(
            root=tmp_path,
            source_document_id=source.document_id,
            canonical_text="bad presentation text",
            ticker="2330",
            conference_date="2026-09-19",
            db=db,
        )
        assert sliced.status == "ok"
        quarantine_research_chain(
            root=tmp_path,
            source_document_id=source.document_id,
            reason="fixture format mismatch",
            db=db,
        )
    finally:
        db.close()

    assert main(["--root", str(tmp_path), "--list"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["status"] == "listed"
    assert report["count"] == 1
    chain = report["quarantined_chains"][0]
    assert chain["source_document_id"] == source.document_id
    assert chain["reason"] == "fixture format mismatch"
    assert sliced.research_document_id in chain["research_document_ids"]
    assert sliced.evidence_id in chain["evidence_ids"]
    assert sliced.claim_id in chain["claim_ids"]


def test_research_quarantine_cli_apply_refuses_without_scope(
    tmp_path: Path,
    capsys,
) -> None:
    assert main(["--root", str(tmp_path), "--apply", "--reason", "no scope"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["status"] == "refused"
    assert "source-document-id" in report["error"]


