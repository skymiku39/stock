"""Offline contracts for call identity, input provenance and publication."""

import datetime as dt
import json
from types import SimpleNamespace
import pytest

from bot import llm_log
from bot.llm_log import LlmCallLogger, LlmCallRecord, analysis_record_id, research_run_id
from bot.provenance_backfill import backfill_llm_provenance
from bot.stock_db import StockDB, EvidenceItemRow


def record(logger, **kwargs):
    args = dict(prompt_id="research_ticker", prompt_version="1", model="fixture",
                input_text="fixture input", output_text='{"summary":"fixture result"}')
    args.update(kwargs)
    return logger.record(**args)


def test_same_second_calls_and_pipeline_stages_keep_distinct_records(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_log, "now_tw", lambda: dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc))
    logger = LlmCallLogger(root=tmp_path)
    records = [record(logger, prompt_id=task, metadata={"run_id": "pipeline:1", "task": task})
               for task in ("daily_brief", "daily_brief", "us_market_brief")]
    db = StockDB.open(root=tmp_path)
    try:
        assert len({rec.call_id for rec in records}) == 3
        assert len(db.list_llm_analysis()) == 3
        assert db.conn.execute("SELECT count(*) FROM llm_runs").fetchone()[0] == 3
        assert db.conn.execute("SELECT count(*) FROM llm_outputs").fetchone()[0] == 3
        assert [r.call_id for r in logger.read()] == [r.call_id for r in records]
        for rec in records:
            md = json.loads(db.get_llm_run(research_run_id(rec)).metadata_json)
            assert md["call_id"] == rec.call_id
            assert md["pipeline_run_id"] == "pipeline:1"
            assert md["analysis_id"] == analysis_record_id(rec)
    finally:
        db.close()


def test_replaying_current_jsonl_reuses_runs_without_legacy_duplicates(tmp_path):
    logger = LlmCallLogger(root=tmp_path)
    rec = record(logger, metadata={"run_id": "pipeline:1"})
    db = StockDB.open(root=tmp_path)
    try:
        for _ in range(2):
            backfill_llm_provenance(root=tmp_path, db=db, apply=True)
        assert db.get_llm_run(research_run_id(rec)) is not None
        assert db.conn.execute("SELECT count(*) FROM llm_runs").fetchone()[0] == 1
        assert len(db.list_llm_analysis()) == 1
    finally:
        db.close()


def test_historical_record_ids_remain_compatible():
    old = LlmCallRecord(ts="2026-10-01T00:00:00Z", prompt_id="legacy",
                        prompt_version="1", model="fixture", input="input", output="",
                        metadata={"run_id": "old-run", "ticker": "2330"})
    assert research_run_id(old) == "old-run"
    assert analysis_record_id(old) == "2026-10-01T00:00:00Z_legacy_2330"


@pytest.mark.parametrize("ids, expected_state", [
    ([], "missing"), (["ev:missing"], "missing"),
    (["ev:known", "ev:missing"], "partial"), (["ev:known"], "linked"),
    ("ev:known", "linked"), ({"ev:known": True}, "missing"),
])
def test_log_and_replay_never_publish_unverified_output(tmp_path, ids, expected_state):
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_evidence(EvidenceItemRow(evidence_id="ev:known", excerpt="fixture", quality_status="ok"))
        rec = record(LlmCallLogger(root=tmp_path), metadata={"evidence_ids": ids})
        for replay in (False, True):
            if replay:
                backfill_llm_provenance(root=tmp_path, db=db, apply=True)
            run = db.get_llm_run(research_run_id(rec))
            assert run.status == "ok"
            receipt = json.loads(run.metadata_json)["input_evidence"]
            assert receipt["state"] == expected_state
            row = db.conn.execute("SELECT * FROM llm_outputs WHERE run_id=?", (run.run_id,)).fetchone()
            assert row["is_publishable"] == 0
            assert "business verification" in row["limitation"]
    finally:
        db.close()


def test_model_output_only_ids_are_not_input_evidence(tmp_path):
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_evidence(EvidenceItemRow(evidence_id="ev:known", excerpt="fixture"))
        rec = record(LlmCallLogger(root=tmp_path), metadata={"evidence_ids": []},
                     output_text=json.dumps({"answer": "made up", "evidence_ids": ["ev:known"],
                                             "claims": [{"text": "made up", "evidence_ids": ["ev:known"]}]}))
        backfill_llm_provenance(root=tmp_path, db=db, apply=True)
        assert db.list_llm_run_evidence(research_run_id(rec)) == []
        assert db.conn.execute("SELECT count(*) FROM claim_evidence").fetchone()[0] == 0
        assert db.get_claim(f"{research_run_id(rec)}:claim:0").status == "unverified"
    finally:
        db.close()


@pytest.mark.parametrize("failure_point", ["link", "output"])
def test_mirror_write_failure_rolls_back_all_tables_but_preserves_jsonl(tmp_path, monkeypatch, failure_point):
    db = StockDB.open(root=tmp_path)
    db.upsert_evidence(EvidenceItemRow(evidence_id="ev:known", excerpt="fixture"))
    def fail(*args, **kwargs):
        raise RuntimeError("injected write failure")
    method = "link_llm_run_evidence" if failure_point == "link" else "upsert_llm_output"
    with monkeypatch.context() as patched:
        patched.setattr(StockDB, method, fail)
        logger = LlmCallLogger(root=tmp_path)
        rec = record(logger, metadata={"evidence_ids": ["ev:known"]})
    try:
        assert logger.read()[0].call_id == rec.call_id
        for table in ("llm_analysis_history", "llm_runs", "llm_run_evidence", "llm_outputs"):
            assert db.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        backfill_llm_provenance(root=tmp_path, db=db, apply=True)
        assert len(db.list_llm_analysis()) == 1
        assert len(db.list_llm_run_evidence(research_run_id(rec))) == 1
    finally:
        db.close()


def test_nested_dao_savepoint_does_not_commit_outer_transaction(tmp_path):
    db = StockDB.open(root=tmp_path)
    try:
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.upsert_evidence(EvidenceItemRow(evidence_id="ev:rollback", excerpt="fixture"))
                raise RuntimeError("outer rollback")
        assert db.get_evidence("ev:rollback") is None
        with db.transaction():
            db.upsert_evidence(EvidenceItemRow(evidence_id="ev:keep", excerpt="fixture"))
            try:
                with db.transaction():
                    db.upsert_evidence(EvidenceItemRow(evidence_id="ev:inner", excerpt="fixture"))
                    raise RuntimeError("inner rollback")
            except RuntimeError:
                pass
        assert db.get_evidence("ev:keep") is not None
        assert db.get_evidence("ev:inner") is None
    finally:
        db.close()


@pytest.mark.parametrize("failure_point", [None, "link_llm_run_evidence", "link_claim_evidence"])
def test_verified_ask_publication_requires_all_link_writes(tmp_path, monkeypatch, failure_point):
    from bot.ask import AskEngine
    from bot.query_plan import plan_query
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_evidence(EvidenceItemRow(evidence_id="ev:known", excerpt="fixture"))
        engine = AskEngine(root=tmp_path, db=db, client=None)
        plan = plan_query("2330 新聞", ticker="2330")
        verification = SimpleNamespace(valid=True, cited_evidence_ids=["ev:known"])
        payload = {"answer": "fixture", "claims": [{"text": "fixture", "evidence_ids": ["ev:known"]}]}
        if failure_point:
            def fail(*args, **kwargs):
                raise RuntimeError("injected link failure")
            monkeypatch.setattr(db, failure_point, fail)
            with pytest.raises(RuntimeError):
                engine._persist_verified_answer(plan, None, payload, verification)
            for table in ("llm_runs", "llm_outputs", "llm_run_evidence", "claims", "claim_evidence"):
                assert db.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        else:
            engine._persist_verified_answer(plan, None, payload, verification)
            assert db.conn.execute("SELECT is_publishable FROM llm_outputs").fetchone()[0] == 1
            assert len(db.list_llm_run_evidence(plan.trace_id)) == 1
    finally:
        db.close()


def test_audit_separates_failed_planner_legacy_and_unassessed_calls(tmp_path):
    from bot.source_audit_cli import audit_project
    from bot.stock_db import LlmAnalysisRow, LlmRunRow
    db = StockDB.open(root=tmp_path)
    try:
        logger = LlmCallLogger(root=tmp_path)
        record(logger, metadata={"evidence_ids": []})
        record(logger, success=False, error="fixture failure", metadata={"evidence_ids": []})
        record(logger, prompt_id="query_plan", metadata={"task": "query_plan"})
        record(logger)  # No input declaration remains unassessed.
        db.upsert_llm_analysis(LlmAnalysisRow(id="old", ts="2026-10-01T00:00:00Z", prompt_id="legacy"))
        db.upsert_llm_run(LlmRunRow(run_id="legacy-analysis:old", status="legacy_unresolved"))
        before = db.conn.total_changes
        report = audit_project(tmp_path)["database"]
        assert db.conn.total_changes == before
        assert report["llm_run_provenance"]["counts"] == {
            "linked": 0, "partial": 0, "successful_without_evidence": 1,
            "failed": 1, "not_required": 1, "legacy_unresolved": 1, "unassessed": 1,
        }
        assert report["llm_runs_without_evidence"] == 2
        assert report["llm_analysis_without_evidence"] == 2
        assert report["llm_analysis_failed"] == 1
        assert report["llm_analysis_evidence_not_required"] == 1
        assert report["llm_analysis_legacy_unresolved"] == 1
        assert sum(row["count"] for row in report["llm_run_provenance"]["by_task"]) == 5
        json.dumps(report)  # The public report must remain JSON serializable.
    finally:
        db.close()


def test_audit_checks_actual_links_instead_of_trusting_stored_receipt(tmp_path):
    from bot.source_audit_cli import audit_project
    db = StockDB.open(root=tmp_path)
    try:
        for evidence_id in ("ev:one", "ev:two"):
            db.upsert_evidence(EvidenceItemRow(evidence_id=evidence_id, excerpt="fixture"))
        rec = record(LlmCallLogger(root=tmp_path), metadata={"evidence_ids": ["ev:one", "ev:two"]})
        with db.transaction() as conn:
            conn.execute("DELETE FROM llm_run_evidence WHERE evidence_id='ev:two'")
        assert json.loads(db.get_llm_run(research_run_id(rec)).metadata_json)["input_evidence"]["state"] == "linked"
        report = audit_project(tmp_path)
        assert report["database"]["llm_run_provenance"]["counts"]["partial"] == 1
        assert any(gap["code"] == "research_llm_run_incomplete_evidence" for gap in report["gaps"])
    finally:
        db.close()


def test_backfill_preview_counts_empty_and_undeclared_inputs_as_unresolved(tmp_path):
    logger = LlmCallLogger(root=tmp_path)
    record(logger, metadata={"evidence_ids": []})
    record(logger)
    record(logger, prompt_id="query_plan", metadata={"task": "query_plan"})
    db = StockDB.open(root=tmp_path)
    try:
        before = list(db.conn.execute("SELECT * FROM llm_runs").fetchall())
        report = backfill_llm_provenance(root=tmp_path, db=db)
        assert report["unresolved_records"] == 2
        assert report["runs_upserted"] == 0
        assert list(db.conn.execute("SELECT * FROM llm_runs").fetchall()) == before
    finally:
        db.close()
