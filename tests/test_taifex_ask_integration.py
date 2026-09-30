from __future__ import annotations

import json
from pathlib import Path

from bot.ask_adapters import collect_snapshot_resource_evidence
from bot.ask import AskEngine
from bot.evidence import make_evidence, persist_evidence_items
from bot.query_plan import plan_query
from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB
from bot.taifex_research import materialize_taifex_slice


def test_taifex_query_plan_is_explicit() -> None:
    plan = plan_query("台指期最近的正逆價差與未平倉量")
    assert "taifex" in plan.resource_needs
    assert plan.planner_required is False


def test_ask_snapshot_uses_exact_taifex_evidence(tmp_path: Path) -> None:
    raw = json.dumps([
        {
            "Contract": "TX",
            "ContractMonth(Week)": "202610",
            "Last": "22100",
            "SettlementPrice": "22090",
            "OpenInterest": "90000",
            "Date": "20260918",
        }
    ], ensure_ascii=False).encode("utf-8")
    with SourceRegistry(tmp_path) as registry:
        document = registry.capture_document(
            "taifex_daily_fut", raw,
            source_ref="taifex:20260918:202610",
            title="TAIFEX TX daily futures 20260918",
            suffix="json",
            mime_type="application/json",
            as_of="20260918",
        )
    materialize_taifex_slice(
        root=tmp_path,
        source_document_id=document.document_id,
        contract_month="202610",
        futures_price=22100,
        open_interest=90000,
        asof_date="20260918",
    )

    evidence = collect_snapshot_resource_evidence(
        "", root=tmp_path, resource_needs=["taifex"],
    )
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "taifex_futures"
    assert evidence[0]["source_document_id"] == document.document_id
    assert evidence[0]["provenance_status"] == "exact"

    db = StockDB.open(root=tmp_path)
    try:
        assert db.get_source_document(document.document_id) is not None
    finally:
        db.close()


def test_ask_engine_collects_market_level_taifex_without_ticker(tmp_path: Path) -> None:
    raw = json.dumps([{
        "Contract": "TX",
        "ContractMonth(Week)": "202610",
        "Last": "22100",
        "SettlementPrice": "22090",
        "OpenInterest": "90000",
        "Date": "20260918",
    }]).encode("utf-8")
    with SourceRegistry(tmp_path) as registry:
        document = registry.capture_document(
            "taifex_daily_fut", raw,
            source_ref="taifex:20260918:202610",
            title="TAIFEX TX daily futures 20260918",
            suffix="json",
            mime_type="application/json",
            as_of="20260918",
        )
    materialize_taifex_slice(
        root=tmp_path,
        source_document_id=document.document_id,
        contract_month="202610",
        futures_price=22100,
        open_interest=90000,
        asof_date="20260918",
    )
    engine = AskEngine(root=tmp_path, client=None)
    try:
        result = engine._collect_evidence(plan_query("台指期最近的正逆價差"))
        assert "taifex" not in result.missing_resources
        assert any(
            item["kind"] == "taifex_futures"
            and item["source_document_id"] == document.document_id
            for item in result.items
        )
    finally:
        engine.close()


def test_taifex_query_does_not_recall_unrequested_cross_domain_evidence(tmp_path: Path) -> None:
    raw = json.dumps([{
        "Contract": "TX",
        "ContractMonth(Week)": "202610",
        "Last": "22100",
        "SettlementPrice": "22090",
        "OpenInterest": "90000",
        "Date": "20260918",
    }]).encode("utf-8")
    with SourceRegistry(tmp_path) as registry:
        document = registry.capture_document(
            "taifex_daily_fut", raw,
            source_ref="taifex:20260918:202610",
            title="TAIFEX TX daily futures 20260918",
            suffix="json",
            mime_type="application/json",
            as_of="20260918",
        )
    materialize_taifex_slice(
        root=tmp_path,
        source_document_id=document.document_id,
        contract_month="202610",
        futures_price=22100,
        open_interest=90000,
        asof_date="20260918",
    )
    db = StockDB.open(root=tmp_path)
    try:
        persist_evidence_items(db, [
            make_evidence(
                "macro", source="macro_cache", excerpt="台指期價差抓取失敗",
            ),
            make_evidence(
                "etf_holdings", source="etf_cache", excerpt="台指期貨權重 2%",
            ),
        ])
    finally:
        db.close()

    engine = AskEngine(root=tmp_path, client=None)
    try:
        result = engine._collect_evidence(plan_query("台指期最近的正逆價差"))
        assert {item["kind"] for item in result.items} <= {"taifex_futures"}
    finally:
        engine.close()
