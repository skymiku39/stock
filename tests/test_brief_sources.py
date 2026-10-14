import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from bot.active_etf import HoldingsSnapshot
from bot.brief_sources import BriefSources
from bot.etf_holdings_fetcher import _parse_etfinfo_holdings_html
from bot.etf_holdings_research import materialize_etf_holdings_slice
from bot.pipeline_brief import prepare_brief_metadata, validate_brief_call
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.stock_db import StockDB, LlmAnalysisRow


def macro(root):
    raw = {"indices": {"SOX": {"price": 100, "source_date": "2026-10-13"}},
           "stocks": {}, "adr_premiums": [{"adr_symbol": "TSM", "adr_source_date": "2026-10-13",
               "tw_source_date": "2026-10-14", "fx_source_date": "2026-10-14", "premium_pct": 999}]}
    with SourceRegistry(root) as reg:
        doc = reg.capture_document("market_macro", json.dumps(raw), as_of="2026-10-14")
    return {**raw, "source_document_id": doc.document_id}


def test_daily_removes_unproven_inputs_and_records_only_real_evidence(tmp_path):
    variables = {"date": "2026-10-14", "consensus_signals_json": '{"fake":999}',
                 "presentation_analyses_json": '[{"prediction":"unverified"}]',
                 "chips_summary_json": '[{"foreign_net":999}]'}
    metadata = prepare_brief_metadata(root=tmp_path, task="daily_brief", run_id="test",
                                     variables=variables, macro_snapshot=macro(tmp_path))
    assert "fake" not in variables["consensus_signals_json"]
    assert variables["presentation_analyses_json"] == "[]"
    assert variables["chips_summary_json"] == "[]"
    assert json.loads(variables["macro_summary"])["adr_premiums"] == []
    validate_brief_call(root=tmp_path, task="daily_brief", variables=variables, metadata=metadata)
    variables["presentation_analyses_json"] = '["injected after verification"]'
    with pytest.raises(ValueError, match="differs"):
        validate_brief_call(root=tmp_path, task="daily_brief", variables=variables, metadata=metadata)


def test_raw_changed_after_receipt_is_rejected(tmp_path):
    snapshot = macro(tmp_path)
    variables = {"date": "2026-10-14"}
    metadata = prepare_brief_metadata(root=tmp_path, task="daily_brief", run_id="test",
                                     variables=variables, macro_snapshot=snapshot)
    with SourceRegistry(tmp_path) as reg:
        source = reg.db.get_source_document(snapshot["source_document_id"])
        resolve_source_document_path(tmp_path, source).write_bytes(b"{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_brief_call(root=tmp_path, task="daily_brief", variables=variables, metadata=metadata)


def test_holdings_require_full_raw_replay_not_just_registered_identity(tmp_path):
    html = '''<html><head><title>00980A holdings 2026-10-13</title></head><body><table>
    <tr><th>代號 名稱</th><th>權重</th><th>股數</th></tr>
    <tr><td><a class="stock-code-link">2330</a><span class="stock-name-sub">台積電</span></td>
    <td>10.0%</td><td>1000</td></tr></table></body></html>'''
    date, rows = _parse_etfinfo_holdings_html(html, symbol="00980A")
    snapshot = HoldingsSnapshot("00980A", date, rows)
    db = StockDB.open(root=tmp_path)
    try:
        source = SourceRegistry(tmp_path, db=db).capture_document("etf_holdings_00980A", html,
            source_url="https://www.etfinfo.tw/etf/00980A/holdings", as_of="2026-10-14")
        materialize_etf_holdings_slice(root=tmp_path, db=db, snapshot=snapshot, source_document_id=source.document_id)
        verified, _ = BriefSources(tmp_path, db).holdings({"00980A": snapshot})
        assert verified["00980A"].date == dt.date(2026, 10, 13)
        changed = replace(snapshot, holdings=[replace(rows[0], weight_pct=99)])
        # Even a registered derived document cannot bless altered raw values.
        materialize_etf_holdings_slice(root=tmp_path, db=db, snapshot=changed, source_document_id=source.document_id)
        assert BriefSources(tmp_path, db).holdings({"00980A": changed})[0] == {}
    finally:
        db.close()


def test_chips_rebuilt_from_raw_ignore_forged_summary_and_unpublished_fields(tmp_path):
    raw = {"date": "20261014", "fields": ["證券代號", "證券名稱", "外資及陸資買賣超股數", "投信買賣超股數", "自營商買賣超"],
           "data": [["2330", "台積電", "1000", "-2000", "3000"]]}
    db = StockDB.open(root=tmp_path)
    try:
        source = SourceRegistry(tmp_path, db=db).capture_document("chips_twse_qfii_daily", json.dumps(raw), as_of="2026-10-14")
        result = BriefSources(tmp_path, db).chips([{"ticker": "2330", "foreign_net": 99999,
                                                  "source_document_ids": [source.document_id]}])
        values = result[0]["observations"][0]["values"]
        assert values == {"foreign_net": 1, "investment_trust_net": -2, "dealer_net": 3}
        source.as_of = "2026-10-13"
        db.upsert_source_document(source)
        assert BriefSources(tmp_path, db).chips([{"ticker": "2330", "source_document_ids": [source.document_id]}]) == []
    finally:
        db.close()


def test_old_brief_is_excluded_without_deleting_historical_output(tmp_path):
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_llm_analysis(LlmAnalysisRow(id="legacy-brief", ts="2026-10-14", prompt_id="us_market_brief", raw_output="original"))
        assert db.list_llm_analysis(prompt_id="us_market_brief") == []
        assert db.list_llm_analysis(prompt_id="us_market_brief", include_archived=True)[0].raw_output == "original"
    finally:
        db.close()


def test_gateway_refuses_unprepared_brief_before_model_call(tmp_path):
    from unittest.mock import Mock
    from bot.llm_analyzer import gemini_call
    from bot.prompt_registry import get_registry
    client = Mock(model="test", enabled=True)
    with pytest.raises(ValueError, match="verified input receipt"):
        gemini_call("daily_brief", client=client, root=tmp_path,
                    registry=get_registry(Path.cwd() / "prompts"), metadata={}, date="2026-10-14")
    client.generate_raw.assert_not_called()


def test_historical_pipeline_loader_excludes_unverified_brief_only_in_view(tmp_path):
    from bot.data_pipeline import load_pipeline_run
    path = tmp_path / "data/pipeline_runs/old/run.json"
    path.parent.mkdir(parents=True)
    original = json.dumps({"run_id": "old", "started_at": "2026-10-14", "us_brief_md": "unverified output"})
    path.write_text(original, encoding="utf-8")
    loaded = load_pipeline_run(tmp_path, "old")
    assert loaded is not None and loaded.us_brief_md == ""
    assert "unverified" in loaded.warnings[0]
    assert path.read_text(encoding="utf-8") == original


def test_futures_require_independent_raw_and_same_spot_date(tmp_path):
    db = StockDB.open(root=tmp_path)
    try:
        raw = [{"Contract": "TX", "ContractMonth(Week)": "202610", "Last": "100", "Date": "20261013"}]
        source = SourceRegistry(tmp_path, db=db).capture_document("taifex_daily_fut", json.dumps(raw))
        verifier = BriefSources(tmp_path, db)
        result = verifier.futures({"source_document_id": source.document_id}, {"price": 98, "source_date": "2026-10-13"})
        assert result["basis"] == 2
        assert result["evidence_id"]
        assert verifier.futures({"source_document_id": source.document_id}, {"price": 98, "source_date": "2026-10-14"}) == {}
    finally:
        db.close()


def test_valid_daily_gateway_persists_exact_evidence_links(tmp_path):
    from unittest.mock import Mock
    from bot.llm_analyzer import gemini_call
    from bot.llm_log import LlmCallLogger
    from bot.prompt_registry import get_registry
    variables = {"date": "2026-10-14", "presentation_analyses_json": '["BAD_UNVERIFIED_INPUT"]'}
    metadata = prepare_brief_metadata(root=tmp_path, task="daily_brief", run_id="test",
                                     variables=variables, macro_snapshot=macro(tmp_path))
    client = Mock(model="offline-test", enabled=True)
    client.generate_raw.return_value = ("offline test response", {})
    logger = LlmCallLogger(root=tmp_path)
    result, info = gemini_call("daily_brief", client=client, root=tmp_path, call_logger=logger,
        registry=get_registry(Path.cwd() / "prompts"), metadata=metadata, **variables)
    assert result == "offline test response"
    assert "BAD_UNVERIFIED_INPUT" not in client.generate_raw.call_args.args[0]
    db = StockDB.open(root=tmp_path)
    try:
        runs = db.conn.execute("SELECT run_id,metadata_json FROM llm_runs WHERE prompt_id='daily_brief'").fetchall()
        assert len(runs) == 1
        links = {r[0] for r in db.conn.execute("SELECT evidence_id FROM llm_run_evidence WHERE run_id=?", (runs[0][0],))}
        assert links == set(metadata["evidence_ids"])
        assert db.list_llm_analysis(prompt_id="daily_brief")[0].id == info["analysis_id"]
        from bot.pipeline_brief import validate_saved_brief
        db.update_llm_analysis_evidence(info["analysis_id"], metadata["evidence_ids"], quality_status="invalid")
        assert db.list_llm_analysis(prompt_id="daily_brief") == []
        assert not validate_saved_brief(tmp_path, "daily_brief", info["input_provenance"])
        db.update_llm_analysis_evidence(info["analysis_id"], metadata["evidence_ids"], quality_status="ok")
        source_id = db.get_evidence(metadata["evidence_ids"][0]).source_document_id
        resolve_source_document_path(tmp_path, db.get_source_document(source_id)).write_bytes(b"tampered")
        assert db.list_llm_analysis(prompt_id="daily_brief") == []
        assert db.list_llm_analysis(prompt_id="daily_brief", include_archived=True)
    finally:
        db.close()
