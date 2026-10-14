import json
import sqlite3

import pytest

from bot.llm_evidence_review import review_history
from bot.llm_log import LlmCallLogger, research_run_id
from bot.stock_db import StockDB, LlmAnalysisRow, LlmRunRow, LlmOutputRow, EvidenceItemRow


def test_review_preserves_history_separates_failures_and_blocks_publication(tmp_path):
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_analysis(LlmAnalysisRow(id="old", ts="2020", prompt_id="fixture", raw_output="original text"))
    for status in ("ok", "failed"):
        db.upsert_llm_run(LlmRunRow(run_id=status, status=status, task_type="fixture"))
        db.upsert_llm_output(LlmOutputRow(output_id=status, run_id=status, markdown="original", is_publishable=1))
    original = [tuple(r) for r in db.conn.execute("SELECT * FROM llm_analysis_history")]
    runs = [tuple(r) for r in db.conn.execute("SELECT * FROM llm_runs")]
    assert review_history(tmp_path)["marked"] == 0
    assert db.conn.execute("SELECT count(*) FROM llm_evidence_reviews").fetchone()[0] == 0
    report = review_history(tmp_path, apply=True)
    assert report["applied"] == report["marked"] == 3
    assert {r["execution_status"] for r in report["by_path"]} == {"ok", "failed"}
    assert review_history(tmp_path, apply=True)["applied"] == 0
    assert original == [tuple(r) for r in db.conn.execute("SELECT * FROM llm_analysis_history")]
    assert runs == [tuple(r) for r in db.conn.execute("SELECT * FROM llm_runs")]
    assert all(r[0] == 0 and r[1] == "original" for r in db.conn.execute("SELECT is_publishable,markdown FROM llm_outputs"))
    from bot.source_audit_cli import audit_project
    assert audit_project(tmp_path)["database"]["llm_evidence_review"]["marked"] == 3
    db.close()


def test_new_successful_call_persists_inputs_and_is_not_marked(tmp_path):
    db = StockDB.open(root=tmp_path)
    db.upsert_evidence(EvidenceItemRow(evidence_id="ev:input", excerpt="input"))
    logger = LlmCallLogger(root=tmp_path)
    rec = logger.record(prompt_id="research_ticker", prompt_version="1", model="fixture",
                        input_text="input", output_text="output", metadata={"evidence_ids": ["ev:input"]})
    logger.record(prompt_id="query_plan", prompt_version="1", model="fixture",
                  input_text="question", output_text="plan", metadata={"task": "query_plan"})
    assert [r.evidence_id for r in db.list_llm_run_evidence(research_run_id(rec))] == ["ev:input"]
    assert review_history(tmp_path, apply=True)["unresolved"] == 0
    db.close()


def test_markers_expire_when_record_or_evidence_changes(tmp_path):
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_analysis(LlmAnalysisRow(id="old", ts="2020", prompt_id="fixture", evidence_json='["ev:missing"]'))
    assert review_history(tmp_path, apply=True)["marked"] == 1
    with db.transaction() as conn:
        conn.execute("UPDATE llm_analysis_history SET raw_output='changed' WHERE id='old'")
    assert review_history(tmp_path)["marked"] == 0
    db.upsert_evidence(EvidenceItemRow(evidence_id="ev:missing", excerpt="input"))
    assert review_history(tmp_path)["unresolved"] == 0
    db.close()


def test_marker_and_publication_failure_roll_back_together(tmp_path):
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_run(LlmRunRow(run_id="old", status="ok"))
    db.upsert_llm_output(LlmOutputRow(output_id="old", run_id="old", is_publishable=1))
    db.conn.execute("CREATE TRIGGER fail_output BEFORE UPDATE ON llm_outputs BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):
        review_history(tmp_path, apply=True)
    assert db.conn.execute("SELECT count(*) FROM llm_evidence_reviews").fetchone()[0] == 0
    assert db.conn.execute("SELECT is_publishable FROM llm_outputs").fetchone()[0] == 1
    db.close()


def test_preview_does_not_create_database(tmp_path):
    assert review_history(tmp_path)["records"] == []
    assert not (tmp_path / "data").exists()
