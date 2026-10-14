import json

from bot.historical_archive import apply_archive
from bot.source_audit_cli import audit_project
from bot.stock_db import StockDB, LlmAnalysisRow, LlmRunRow, SourceRun


def test_archiving_hides_exact_old_gaps_but_preserves_new_and_changed_records(tmp_path):
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_analysis(LlmAnalysisRow(id="old", ts="2020", prompt_id="fixture", raw_output="keep"))
    db.upsert_llm_run(LlmRunRow(run_id="old", status="ok"))
    db.record_source_run(SourceRun(run_id="old-source", source="fixture", status="failed"))
    raw = tmp_path / "data" / "raw" / "old.bin"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"keep")
    assert apply_archive(tmp_path)["counts"] == {"analysis": 1, "raw": 1, "run": 1, "source_run": 1}
    assert len(db.list_llm_analysis()) == 1
    apply_archive(tmp_path, apply=True)
    assert db.list_llm_analysis() == []
    assert db.list_llm_analysis(include_archived=True)[0].raw_output == "keep"
    audit = audit_project(tmp_path)
    assert audit["unregistered_raw_files"] == []
    assert len(audit["historical_archive"]["raw"]) == 1
    assert audit["database"]["llm_analysis_without_evidence"] == 0
    assert audit["database"]["llm_runs_without_evidence"] == 0
    assert audit["database"]["source_runs_without_documents"] == 0
    assert audit_project(tmp_path, include_archived=True)["unregistered_raw_files"]
    db.upsert_llm_analysis(LlmAnalysisRow(id="new", ts="2026", prompt_id="fixture"))
    db.upsert_llm_run(LlmRunRow(run_id="new", status="ok"))
    db.record_source_run(SourceRun(run_id="new-source", source="fixture", status="failed"))
    raw.write_bytes(b"changed")
    audit = audit_project(tmp_path)
    assert audit["unregistered_raw_files"] == ["data/raw/old.bin"]
    assert audit["database"]["llm_analysis_without_evidence"] == 1
    assert audit["database"]["llm_runs_without_evidence"] == 1
    assert audit["database"]["source_runs_without_documents"] == 1
    assert len(audit["database"]["llm_evidence_review"]["records"]) == 2
    assert [r.id for r in db.list_llm_analysis()] == ["new"]
    db.close()


def test_strict_cache_reads_keep_verified_old_sources_and_reject_missing_raw(tmp_path):
    from bot.source_registry import SourceRegistry
    from bot.evidence import make_evidence, persist_evidence_items
    from bot.auto_llm import load_cached_auto_analysis
    db = StockDB.open(root=tmp_path)
    doc = SourceRegistry(tmp_path, db).register_document("fixture", b"original", as_of="2020")
    ev = make_evidence("official", source="fixture", source_ref="original", excerpt="original",
                       source_document_id=doc.document_id, provenance_status="exact", quality_status="ok")
    persist_evidence_items(db, [ev])
    apply_archive(tmp_path, apply=True)
    path = tmp_path / "data" / "auto_llm" / "2330.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary": "keep", "evidence": [ev]}), encoding="utf-8")
    assert load_cached_auto_analysis("2330", tmp_path)["summary"] == "keep"
    (tmp_path / doc.content_path).write_bytes(b"tampered")
    assert load_cached_auto_analysis("2330", tmp_path) is None
    assert load_cached_auto_analysis("2330", tmp_path, include_archived=True)["summary"] == "keep"
    assert path.exists()
    db.close()


def test_archived_llm_memory_is_available_only_to_explicit_history_reads(tmp_path):
    from bot.memory_store import MemoryStore
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_analysis(LlmAnalysisRow(id="old", ts="2020", prompt_id="fixture"))
    memory = MemoryStore(db).put_short("historical", memory_type="llm_call", quality_status="ok",
                                      source_record_id="old", source_ref="old")
    apply_archive(tmp_path, apply=True)
    assert db.search_memory_rows("") == []
    assert any(r.memory_id == memory.memory_id for r in db.search_memory_rows("", include_non_active=True))
    db.close()


def test_archived_legacy_row_cannot_cancel_a_new_gap_count(tmp_path):
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_analysis(LlmAnalysisRow(id="old", ts="2020", prompt_id="fixture"))
    db.upsert_llm_run(LlmRunRow(run_id="legacy-analysis:old", status="legacy_unresolved"))
    apply_archive(tmp_path, apply=True)
    db.upsert_llm_analysis(LlmAnalysisRow(id="new", ts="2026", prompt_id="fixture"))
    report = audit_project(tmp_path)
    assert report["database"]["llm_analysis_without_evidence"] == 1
    assert any(g["code"] == "llm_without_evidence_registry" for g in report["gaps"])
    db.close()
