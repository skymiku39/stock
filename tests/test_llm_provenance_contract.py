"""Offline contracts for call identity, input provenance and publication."""

import datetime as dt
import json
import logging

from bot import llm_log
from bot.llm_log import LlmCallLogger, LlmCallRecord, analysis_record_id, research_run_id
from bot.provenance_backfill import backfill_llm_provenance
from bot.stock_db import StockDB


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
