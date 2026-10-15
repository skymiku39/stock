import hashlib
import json

import pytest

from bot.pipeline_brief import prepare_brief_metadata
from bot.source_registry import SourceRegistry, resolve_source_document_path


def test_us_brief_checks_actual_values_and_persists_input_before_call(tmp_path):
    snapshot = {"indices": {"SOX": {"price": 100, "source_date": "2026-10-13"}}, "stocks": {}, "adr_premiums": []}
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document("market_macro", json.dumps(snapshot), suffix="json")
    variables = {key + "_json": json.dumps(value) for key, value in snapshot.items()}
    variables["supply_chain_json"] = '{"configured": "mapping"}'
    metadata = prepare_brief_metadata(root=tmp_path, task="us_market_brief", run_id="test",
        variables=variables, macro_snapshot={**snapshot, "source_document_id": source.document_id})
    assert len(metadata["evidence_ids"]) == 1
    assert metadata["market_verification"] == "replayed_inputs_only"
    assert variables["supply_chain_json"] == "{}"
    with SourceRegistry(tmp_path) as registry:
        receipt = registry.db.get_source_document(metadata["input_source_document_id"])
        raw = resolve_source_document_path(tmp_path, receipt).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == metadata["input_content_hash"]
        assert json.loads(raw)["variables"] == variables
        assert receipt.quality_status == "partial"
    variables["stocks_json"] = '{"invented": 42}'
    with pytest.raises(ValueError, match="differs"):
        prepare_brief_metadata(root=tmp_path, task="us_market_brief", run_id="bad",
            variables=variables, macro_snapshot={"source_document_id": source.document_id})


def test_daily_receipt_is_not_invented_market_evidence(tmp_path):
    with pytest.raises(ValueError, match="no verifiable evidence"):
        prepare_brief_metadata(root=tmp_path, task="daily_brief", run_id="test",
                               variables={"consensus_signals_json": "{}"})


def test_us_brief_missing_source_fails_before_model_call(tmp_path):
    with pytest.raises(ValueError, match="no verifiable macro"):
        prepare_brief_metadata(root=tmp_path, task="us_market_brief", run_id="test", variables={"indices_json": "null", "stocks_json": "null", "adr_premiums_json": "null"})


def test_daily_pipeline_refuses_model_when_no_verified_inputs(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from bot import data_pipeline
    calls = []
    monkeypatch.setattr(data_pipeline, "GeminiClient", lambda **kw: SimpleNamespace(enabled=True))

    def call(task, *, root, metadata, **kwargs):
        assert root == tmp_path
        with SourceRegistry(root) as registry:
            assert registry.db.get_source_document(metadata["input_source_document_id"])
        calls.append(task)
        return "test brief", {}

    monkeypatch.setattr(data_pipeline, "gemini_call", call)
    run = data_pipeline.run_full_pipeline(data_pipeline.PipelineConfig(
        project_root=tmp_path, fetch_etf_holdings=False, fetch_chips=False,
        fetch_macro=False, update_calendar=False, auto_research_focus=False,
        run_llm_analysis=False, generate_brief=True, generate_us_brief=False,
    ))
    assert calls == []
    assert any("no verifiable evidence" in error for error in run.errors)


def test_nan_quote_is_excluded_without_rejecting_other_verified_quotes(tmp_path):
    snapshot = {"indices": {"SOX": {"price": 100, "source_date": "2026-10-14"}},
                "stocks": {"META": {"price": float("nan"), "source_date": "2026-10-14"}},
                "adr_premiums": []}
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document("market_macro", json.dumps(snapshot), suffix="json")
    variables = {key + "_json": json.dumps(value) for key, value in snapshot.items()}
    metadata = prepare_brief_metadata(root=tmp_path, task="us_market_brief", run_id="nan",
        variables=variables, macro_snapshot={**snapshot, "source_document_id": source.document_id})
    assert json.loads(variables["stocks_json"]) == {}
    assert json.loads(variables["indices_json"])["SOX"]["price"] == 100
    assert any(e["component"] == "macro:META" for e in json.loads(variables["input_provenance_json"])["excluded"])
    assert "NaN" not in json.dumps(variables)
    assert metadata["evidence_ids"]


def test_nan_cannot_mask_modified_quote(tmp_path):
    snapshot = {"indices": {}, "stocks": {"META": {"price": float("nan")}}, "adr_premiums": []}
    variables = {key + "_json": json.dumps(value) for key, value in snapshot.items()}
    variables["stocks_json"] = '{"META": {"price": 42}}'
    with pytest.raises(ValueError, match="differs"):
        prepare_brief_metadata(root=tmp_path, task="us_market_brief", run_id="bad",
            variables=variables, macro_snapshot=snapshot)
