import hashlib
import json

import pytest

from bot.pipeline_brief import prepare_brief_metadata
from bot.source_registry import SourceRegistry, resolve_source_document_path


def test_us_brief_checks_actual_values_and_persists_input_before_call(tmp_path):
    snapshot = {"indices": {"SOX": {"close": 100}}, "stocks": {}, "adr_premiums": []}
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document("market_macro", json.dumps(snapshot), suffix="json")
    variables = {key + "_json": json.dumps(value) for key, value in snapshot.items()}
    variables["supply_chain_json"] = '{"configured": "mapping"}'
    metadata = prepare_brief_metadata(root=tmp_path, task="us_market_brief", run_id="test",
        variables=variables, macro_snapshot={**snapshot, "source_document_id": source.document_id})
    assert len(metadata["evidence_ids"]) == 1
    assert metadata["market_verification"] == "partial"
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
    md = prepare_brief_metadata(root=tmp_path, task="daily_brief", run_id="test",
                                variables={"consensus_signals_json": "{}"})
    assert md["input_source_document_id"]
    assert md["evidence_ids"] == []
    assert md["market_verification"] == "unverified"


def test_us_brief_missing_source_fails_before_model_call(tmp_path):
    with pytest.raises(ValueError, match="missing exact"):
        prepare_brief_metadata(root=tmp_path, task="us_market_brief", run_id="test", variables={})


def test_daily_pipeline_passes_receipt_and_project_root_to_model(tmp_path, monkeypatch):
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
    assert calls == ["daily_brief"]
    assert not run.errors
    assert any("unverified" in warning for warning in run.warnings)
