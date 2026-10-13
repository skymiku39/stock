from __future__ import annotations

import hashlib
import json

from bot.evidence import persist_evidence_items
from bot.source_audit_cli import audit_project
from bot.source_audit import audit_rows
from bot.source_registry import SourceRegistry
from bot.stock_db import (
    EvidenceItemRow,
    LlmAnalysisRow,
    LlmDailyReportRow,
    LlmRunRow,
    SourceRun,
    StockDB,
    StockInfo,
)


def test_audit_accepts_aliases_and_records_schema() -> None:
    result = audit_rows(
        [
            {"公司代號": "2330", "資料年月": "202608", "殖利率": "2.5"},
            {"公司代號": "2317", "資料年月": "202608", "殖利率": "3.1"},
        ],
        source="fixture",
        required_fields=(("公司代號", "Code"), ("資料年月", "Date")),
        key_fields=(("公司代號", "Code"), ("資料年月", "Date")),
        numeric_fields=(("殖利率", "DividendYield"),),
    )
    assert result.usable
    assert result.quality_status == "ok"
    assert result.rows == 2
    assert result.schema_hash


def test_audit_parses_roc_compact_date() -> None:
    result = audit_rows(
        [{"symbol": "2330", "date": "1150909"}],
        source="twse-fixture",
        required_fields=("symbol", "date"),
        key_fields=("symbol", "date"),
        date_fields=("date",),
    )

    assert result.usable
    assert result.quality_status == "ok"
    assert result.observed_date == "2026-09-09"


def test_audit_accepts_explicit_missing_numeric_value() -> None:
    result = audit_rows(
        [{"symbol": "2330", "pe": "N/A", "pbr": "1.8"}],
        source="twse-fixture",
        required_fields=("symbol",),
        numeric_fields=("pe", "pbr"),
    )

    assert result.quality_status == "ok"


def test_audit_rejects_duplicate_key_and_bad_number() -> None:
    result = audit_rows(
        [
            {"symbol": "2330", "date": "2026-08-01", "close": "bad"},
            {"symbol": "2330", "date": "2026-08-01", "close": "900"},
        ],
        source="fixture",
        required_fields=("symbol", "date"),
        key_fields=("symbol", "date"),
        date_fields=("date",),
        numeric_fields=("close",),
    )
    assert result.quality_status == "invalid"
    checks = {finding.check for finding in result.findings}
    assert {"duplicate_key", "numeric_format"}.issubset(checks)


def test_audit_marks_future_dates_as_partial() -> None:
    result = audit_rows(
        [{"symbol": "2330", "date": "2099-01-01"}],
        source="fixture",
        required_fields=("symbol", "date"),
        date_fields=("date",),
    )
    assert result.quality_status == "partial"
    assert any(f.check == "future_date" for f in result.findings)


def test_audit_does_not_silently_drop_non_mapping_rows() -> None:
    result = audit_rows([{"symbol": "2330"}, "not-a-row"], source="fixture")
    assert result.quality_status == "invalid"
    assert any(f.check == "row_type" for f in result.findings)


def test_project_audit_reports_provenance_gaps_without_fetching(tmp_path) -> None:
    db = StockDB.open(root=tmp_path)
    db.record_source_run(SourceRun(
        run_id="source-run-1",
        source="fixture",
        status="ok",
        row_count=1,
    ))
    raw = tmp_path / "data" / "raw" / "fixture" / "orphan.json"
    raw.parent.mkdir(parents=True)
    raw.write_text("{}", encoding="utf-8")

    report = audit_project(tmp_path)

    assert report["database"]["schema_version"] == 1
    assert report["database"]["source_runs_without_documents"] == 1
    assert report["database"]["unresolved_source_runs"][0]["run_id"] == "source-run-1"
    assert report["database"]["unresolved_source_runs"][0]["reason_code"] == (
        "source_document_link_missing"
    )
    assert report["database"]["unresolved_source_runs"][0]["raw_hash_present"] is False
    source_gap = next(
        gap for gap in report["gaps"] if gap["code"] == "source_runs_without_raw_manifest"
    )
    assert source_gap["details"]["policy"] == "exact_only"
    assert source_gap["details"]["unresolved_source_runs"][0]["run_id"] == "source-run-1"
    assert report["database"]["integrity"]["orphan_counts"]["memory_evidence"] == 0
    assert report["database"]["integrity"]["fts"]["memory_items"]["consistent"] is True
    assert report["database"]["integrity"]["sync_conflicts"] == {
        "total": 0,
        "open": 0,
    }
    assert report["database"]["integrity"]["sqlite"]["status"] == "ok"
    codes = {gap["code"] for gap in report["gaps"]}
    assert "source_runs_without_raw_manifest" in codes
    assert "unregistered_raw_files" in codes


def test_project_audit_distinguishes_unresolved_document_reference(tmp_path) -> None:
    db = StockDB.open(root=tmp_path)
    db.record_source_run(SourceRun(
        run_id="source-run-with-missing-document",
        source="fixture",
        status="ok",
        row_count=2,
        details_json='{"document_ids":["source-doc:missing"],"warnings":[]}',
    ))

    report = audit_project(tmp_path)

    unresolved = report["database"]["unresolved_source_runs"][0]
    assert unresolved["reason_code"] == "source_document_reference_unresolved"
    assert unresolved["linked_document_ids"] == ["source-doc:missing"]
    assert unresolved["details_keys"] == ["document_ids", "warnings"]


def test_project_audit_reports_exact_raw_hash_candidate_without_linking(tmp_path) -> None:
    raw_content = b"exact raw fixture"
    with SourceRegistry(tmp_path) as registry:
        document = registry.register_document("fixture", raw_content, suffix="json")

    db = StockDB.open(root=tmp_path)
    db.record_source_run(SourceRun(
        run_id="source-run-with-hash-only",
        source="fixture",
        status="ok",
        row_count=1,
        raw_hash=hashlib.sha256(raw_content).hexdigest(),
    ))

    report = audit_project(tmp_path)

    unresolved = report["database"]["unresolved_source_runs"][0]
    assert unresolved["reason_code"] == "source_document_link_missing"
    assert unresolved["raw_hash_exact_document_ids"] == [document.document_id]
    assert report["database"]["source_runs_without_documents"] == 1


def test_project_audit_verifies_registered_raw_archive_hash(tmp_path) -> None:
    with SourceRegistry(tmp_path) as registry:
        document = registry.register_document(
            "fixture", b"immutable raw", suffix="json",
        )

    raw_path = tmp_path / document.content_path
    raw_path.write_bytes(b"changed after registration")

    report = audit_project(tmp_path)

    assert report["raw_archive"]["checked"] == 1
    assert report["raw_archive"]["mismatched"] == [
        document.content_path.replace("\\", "/")
    ]
    assert any(
        gap["code"] == "raw_archive_hash_mismatch"
        for gap in report["gaps"]
    )


def test_project_audit_reports_canonical_source_record_orphan(tmp_path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_stock_info(StockInfo(
        symbol="2330",
        name="台積電",
        source_record_id="source-doc:missing-canonical-source",
    ))

    report = audit_project(tmp_path)

    assert report["database"]["integrity"]["orphan_counts"]["stock_info_source_records"] == 1
    assert any(gap["code"] == "relational_orphan_links" for gap in report["gaps"])


def test_project_audit_counts_llm_rows_without_evidence(tmp_path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_analysis(LlmAnalysisRow(
        id="analysis-without-evidence",
        ts="2026-09-17T10:00:00+08:00",
        prompt_id="fixture",
        raw_output="{}",
    ))
    db.upsert_llm_analysis(LlmAnalysisRow(
        id="analysis-with-evidence",
        ts="2026-09-17T10:01:00+08:00",
        prompt_id="fixture",
        evidence_json='["evidence:1"]',
    ))
    db.upsert_evidence(EvidenceItemRow(
        evidence_id="evidence:1",
        source="fixture",
        excerpt="fixture evidence",
        quality_status="ok",
    ))

    report = audit_project(tmp_path)

    assert report["database"]["llm_analysis_without_evidence"] == 1
    assert report["database"]["llm_analysis_with_unresolved_evidence"] == 0
    gap = next(gap for gap in report["gaps"] if gap["code"] == "llm_without_evidence_registry")
    assert "1 筆" in gap["message"]


def test_project_audit_separates_marked_legacy_llm_limit(tmp_path) -> None:
    db = StockDB.open(root=tmp_path)
    legacy_id = "legacy-analysis:old-analysis"
    db.upsert_llm_analysis(LlmAnalysisRow(
        id="old-analysis",
        ts="2026-09-17T10:00:00+08:00",
        prompt_id="legacy",
        raw_output="legacy output",
    ))
    db.upsert_llm_run(LlmRunRow(
        run_id=legacy_id,
        task_type="legacy",
        status="legacy_unresolved",
        metadata_json='{"legacy_analysis_id":"old-analysis"}',
    ))

    report = audit_project(tmp_path)

    assert report["database"]["llm_analysis_without_evidence"] == 0
    assert report["database"]["llm_analysis_legacy_unresolved"] == 1
    assert report["database"]["llm_runs_without_evidence"] == 0
    assert report["known_limits"][0]["code"] == "legacy_llm_without_provable_evidence"


def test_project_audit_accepts_exact_multi_source_daily_report_without_row_source_id(
    tmp_path,
) -> None:
    db = StockDB.open(root=tmp_path)
    with SourceRegistry(tmp_path, db=db) as registry:
        document = registry.register_document(
            "fixture", b"daily report raw", suffix="json",
        )
    db.upsert_evidence(EvidenceItemRow(
        evidence_id="evidence:daily:1",
        kind="news",
        source="fixture",
        excerpt="daily evidence",
        quality_status="ok",
        source_document_id=document.document_id,
    ))
    packet = {
        "id": "evidence:daily:1",
        "kind": "news",
        "source": "fixture",
        "excerpt": "daily evidence",
        "source_document_id": document.document_id,
        "provenance_status": "exact",
    }
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="intraday",
        report_date="2026-09-20",
        quality_status="ok",
        payload_json='{"provenance_status":"composite_exact"}',
        evidence_json=json.dumps([packet]),
        # Aggregate reports may contain several source documents; this field
        # must remain blank instead of pretending one source represents all.
        source_record_id="",
    ))

    report = audit_project(tmp_path)

    assert report["database"]["llm_daily_reports_with_unresolved_evidence"] == 0
    assert report["database"]["llm_daily_reports_without_evidence"] == 0
    assert "llm_daily_report_unresolved_evidence" not in {
        gap["code"] for gap in report["gaps"]
    }


def test_project_audit_keeps_historical_source_link_when_evidence_id_is_reused(
    tmp_path,
) -> None:
    db = StockDB.open(root=tmp_path)
    with SourceRegistry(tmp_path, db=db) as registry:
        first_document = registry.register_document(
            "fixture", b"daily report raw v1", suffix="json",
        )
        second_document = registry.register_document(
            "fixture", b"daily report raw v2", suffix="json",
        )
    first_packet = {
        "id": "evidence:versioned",
        "kind": "news",
        "source": "fixture",
        "source_ref": "same-article",
        "excerpt": "same article text",
        "source_document_id": first_document.document_id,
        "provenance_status": "exact",
    }
    second_packet = dict(first_packet)
    second_packet["source_document_id"] = second_document.document_id
    persist_evidence_items(db, [first_packet, second_packet])
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="intraday",
        report_date="2026-09-20",
        quality_status="ok",
        payload_json=json.dumps({"provenance_status": "exact"}),
        evidence_json=json.dumps([first_packet]),
    ))

    report = audit_project(tmp_path)

    assert len(db.list_evidence_links("evidence:versioned")) == 2
    assert report["database"]["llm_daily_reports_with_unresolved_evidence"] == 0


def test_project_audit_reports_daily_report_unresolved_evidence(tmp_path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="next_day_watch",
        report_date="2026-09-21",
        mode="draft",
        quality_status="ok",
        payload_json='{"provenance_status":"exact"}',
        evidence_json=json.dumps([{
            "id": "news:legacy",
            "source": "legacy",
            "excerpt": "not registered",
        }]),
    ))

    report = audit_project(tmp_path)

    assert report["database"]["llm_daily_reports_with_unresolved_evidence"] == 1
    assert report["database"]["llm_daily_report_unresolved_evidence_items"] == 1
    gap = next(
        gap for gap in report["gaps"]
        if gap["code"] == "llm_daily_report_unresolved_evidence"
    )
    assert gap["details"]["policy"] == "exact_only"
    unresolved_reasons = gap["details"]["findings"][0]["unresolved_items"][0]["reasons"]
    assert "evidence_item_missing" in unresolved_reasons
    assert "source_document_id_missing" in unresolved_reasons


def test_project_audit_does_not_double_count_malformed_daily_report_evidence(
    tmp_path,
) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="intraday",
        report_date="2026-09-20",
        quality_status="ok",
        evidence_json="{malformed",
    ))

    report = audit_project(tmp_path)

    assert report["database"]["llm_daily_reports_with_unresolved_evidence"] == 1
    assert report["database"]["llm_daily_reports_without_evidence"] == 0
