from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot.answer_verifier import verify_answer_payload
from bot.ask import AskEngine, derive_ask_status, derive_collection_status
from bot.evidence import EvidencePack, make_evidence
from bot.memory_store import MemoryStore
from bot.resource_adapters import ResourceResult
from bot.stock_db import StockDB


def test_derive_collection_status_separates_failure_modes() -> None:
    assert derive_collection_status(
        [{"status": "ok"}], missing_resources=["news"], usable_count=0, item_count=0,
    ) == "no_evidence"
    assert derive_collection_status(
        [{"status": "timeout"}], missing_resources=["news"], usable_count=0, item_count=0,
    ) == "timeout"
    assert derive_collection_status(
        [{"status": "resource_failed"}],
        missing_resources=["news"], usable_count=0, item_count=0,
    ) == "resource_failed"
    assert derive_collection_status(
        [{"status": "live_disabled"}],
        missing_resources=["news"], usable_count=0, item_count=0,
    ) == "live_disabled"
    assert derive_collection_status(
        [{"status": "ok"}, {"status": "timeout"}],
        missing_resources=["news"], usable_count=1, item_count=1,
    ) == "partial"
    assert derive_collection_status(
        [{"status": "ok"}, {"status": "partial"}],
        missing_resources=[], usable_count=1, item_count=1,
    ) == "partial"


def test_ask_live_disabled_is_not_no_evidence(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    result = AskEngine(
        root=tmp_path, db=db, enable_live_sources=False, use_llm_planner=False,
    ).ask("2330 最新新聞？", ticker="2330")
    assert result.status == "live_disabled"
    assert result.evidence.collection_status == "live_disabled"
    assert any("不等於查無此事" in warning for warning in result.evidence.warnings)
    assert db.get_ask_run(result.trace_id)["status"] == "live_disabled"
    payload = result.to_dict()
    assert payload["status"] == "live_disabled"
    assert payload["collection_status"] == "live_disabled"


def test_ask_routes_professional_knowledge_to_dedicated_adapter(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    with patch("bot.ask.ProfessionalKnowledgeAdapter") as adapter_cls:
        adapter_cls.return_value.collect.return_value = ResourceResult(
            resource="professional_knowledge",
            status="partial",
            warnings=["fixture professional source"],
            live=True,
        )
        result = AskEngine(
            root=tmp_path,
            db=db,
            enable_live_sources=True,
            use_llm_planner=False,
        ).ask("台積電 專業官方文件？", ticker="2330")

    adapter_cls.assert_called_once()
    adapter_cls.return_value.collect.assert_called_once()
    assert any(
        item["resource"] == "professional_knowledge"
        for item in result.evidence.resource_outcomes
    )


def test_ask_timeout_is_not_no_evidence(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)

    def _timeout(plan):
        return ResourceResult(
            resource="news",
            warnings=["news 超過 8s 時間預算；不等於查無此事"],
            status="timeout",
            live=True,
        )

    with patch("bot.ask.WebSearchAdapter") as adapter_cls, patch("bot.ask.MopsAdapter") as mops_cls, patch("bot.ask.LiveTickerAdapter") as ticker_cls:
        adapter_cls.return_value.collect.side_effect = _timeout
        mops_cls.return_value.collect.side_effect = _timeout
        ticker_cls.return_value.collect.side_effect = lambda plan: ResourceResult(
            resource="ticker_snapshot", status="partial", live=True,
        )
        result = AskEngine(
            root=tmp_path,
            db=db,
            enable_live_sources=True,
            use_llm_planner=False,
            live_budget_seconds=1.0,
        ).ask("2330 最新新聞？", ticker="2330")
    assert result.status == "timeout"
    assert result.evidence.collection_status == "timeout"
    assert result.status != "no_evidence"


def test_ask_resource_failed_is_not_no_evidence(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)

    def _failed(plan):
        return ResourceResult(
            resource="news",
            warnings=["news 來源失敗：RuntimeError: boom"],
            status="failed",
            live=True,
        )

    with patch("bot.ask.WebSearchAdapter") as adapter_cls, patch("bot.ask.MopsAdapter") as mops_cls, patch("bot.ask.LiveTickerAdapter") as ticker_cls:
        adapter_cls.return_value.collect.side_effect = _failed
        mops_cls.return_value.collect.side_effect = _failed
        ticker_cls.return_value.collect.side_effect = lambda plan: ResourceResult(
            resource="ticker_snapshot", status="partial", live=True,
        )
        result = AskEngine(
            root=tmp_path,
            db=db,
            enable_live_sources=True,
            use_llm_planner=False,
        ).ask("2330 最新新聞？", ticker="2330")
    assert result.status == "resource_failed"
    assert result.evidence.collection_status == "resource_failed"


def test_ask_partial_when_local_ok_and_live_disabled(tmp_path: Path) -> None:
    from bot.stock_db import StockInfo

    db = StockDB.open(root=tmp_path)
    db.upsert_stock_info(StockInfo(
        symbol="2330", name="台積電", quality_status="ok", source_record_id="company:2330",
    ))
    result = AskEngine(
        root=tmp_path, db=db, enable_live_sources=False, use_llm_planner=False,
    ).ask("2330 最新新聞？", ticker="2330")
    assert result.status == "partial"
    assert result.evidence.collection_status == "partial"
    assert result.evidence.usable_items


def test_ask_no_evidence_when_sources_complete_empty(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)

    def _empty(plan):
        return ResourceResult(resource="news", status="ok", live=True, evidence=[])

    with patch("bot.ask.WebSearchAdapter") as adapter_cls, patch("bot.ask.MopsAdapter") as mops_cls, patch("bot.ask.LiveTickerAdapter") as ticker_cls:
        adapter_cls.return_value.collect.side_effect = _empty
        mops_cls.return_value.collect.side_effect = _empty
        ticker_cls.return_value.collect.side_effect = lambda plan: ResourceResult(
            resource="ticker_snapshot", status="ok", live=True, evidence=[],
        )
        result = AskEngine(
            root=tmp_path,
            db=db,
            enable_live_sources=True,
            use_llm_planner=False,
        ).ask("2330 最新新聞？", ticker="2330")
    assert result.status == "no_evidence"
    assert result.evidence.collection_status == "no_evidence"


def test_conversation_turn_cannot_support_market_claim(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    store = MemoryStore(db)
    conversation = make_evidence(
        "conversation",
        source="ask",
        source_ref="ask:prev",
        excerpt="根據我的回答，2330 五月營收年增 10%",
        quality_status="partial",
        metadata={"not_market_fact": True, "memory_type": "conversation_turn"},
    )
    store.put_short(
        json.dumps({"answer": "2330 五月營收年增 10%"}, ensure_ascii=False),
        memory_id="ask:prev-turn",
        memory_type="conversation_turn",
        subject="2330",
        summary="2330 五月營收年增 10%",
        quality_status="partial",
        evidence=[conversation],
    )
    client = MagicMock()
    client.enabled = True
    client.model = "test-model"
    payload = {
        "answer": "根據上次對話，2330 五月營收年增 10%。",
        "confidence": 0.9,
        "evidence_ids": [conversation["id"]],
        "claims": [{"text": "五月營收年增 10%", "evidence_ids": [conversation["id"]]}],
    }
    with patch("bot.ask.gemini_call", return_value=(
        json.dumps(payload, ensure_ascii=False), {"prompt_id": "ask_answer"},
    )) as mocked:
        result = AskEngine(
            root=tmp_path, db=db, client=client, use_llm_planner=False,
        ).ask("2330 五月營收如何？")
    # 市場問題不應把 conversation 正文送進 pack；即使硬塞也不應通過 verifier。
    assert conversation["id"] not in {
        item["id"] for item in result.evidence.market_usable_items
    }
    if conversation["id"] in result.evidence.evidence_ids:
        check = verify_answer_payload(payload, result.evidence)
        assert check.valid is False
    mocked.assert_not_called()


def test_historical_conversation_query_can_use_conversation_memory(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    store = MemoryStore(db)
    store.put_short(
        json.dumps({"query": "上一輪問題", "answer": "我們討論過供應鏈風險"}, ensure_ascii=False),
        memory_id="ask:hist",
        memory_type="conversation_turn",
        summary="我們討論過供應鏈風險",
        quality_status="partial",
        keywords=["供應鏈", "風險"],
    )
    result = AskEngine(
        root=tmp_path, db=db, use_llm_planner=False,
    ).ask("我們以前說過什麼供應鏈風險？")
    kinds = {item["kind"] for item in result.evidence.items}
    assert "conversation" in kinds
    assert any(
        item.get("metadata", {}).get("not_market_fact") for item in result.evidence.items
    )


def test_official_evidence_still_supports_answer(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    item = make_evidence(
        "fundamental",
        source="official",
        source_ref="rev-official",
        title="月營收公告",
        excerpt="2330 五月營收年增 10%",
        provenance_status="exact",
        source_document_id="",  # exact provenance only when ID exists; keep empty here
    )
    # Without document id, provenance falls back unless explicitly exact with empty id.
    # Explicit provenance_status=exact is allowed only when caller is sure; for this
    # test we use unresolved official excerpt which still supports lexical verify.
    item = make_evidence(
        "fundamental",
        source="official",
        source_ref="rev-official",
        title="月營收公告",
        excerpt="2330 五月營收年增 10%",
    )
    MemoryStore(db).put_long(
        "2330 五月營收年增 10%",
        memory_id="memory:official",
        summary="2330 五月營收年增",
        subject="2330",
        quality_status="ok",
        evidence=[item],
    )
    client = MagicMock()
    client.enabled = True
    client.model = "test-model"
    payload = {
        "answer": "根據公告，2330 五月營收年增 10%。",
        "confidence": 0.9,
        "evidence_ids": [item["id"]],
        "claims": [{"text": "五月營收年增 10%", "evidence_ids": [item["id"]]}],
    }
    with patch("bot.ask.gemini_call", return_value=(
        json.dumps(payload, ensure_ascii=False), {"prompt_id": "ask_answer"},
    )) as mocked_call:
        result = AskEngine(
            root=tmp_path, db=db, client=client, use_llm_planner=False,
        ).ask("2330 五月營收如何？")
    assert result.status == "answered"
    assert result.verified is True
    answer_call = next(
        call for call in mocked_call.call_args_list if call.args[0] == "ask_answer"
    )
    metadata = answer_call.kwargs["metadata"]
    assert metadata["trace_id"] == result.trace_id
    assert metadata["evidence_ids"] == result.evidence.evidence_ids
    assert item["id"] in metadata["evidence_ids"]


def test_cache_only_provenance_is_visible_and_not_fake_document(tmp_path: Path) -> None:
    item = make_evidence(
        "fundamental",
        source="fundamentals_cache",
        excerpt="營收 100",
        provenance_status="cache_only",
    )
    assert item["provenance_status"] == "cache_only"
    assert item["source_document_id"] == ""
    assert "非官方 raw document" in EvidencePack(
        query="q", plan_id="p", trace_id="t", items=[item],
    ).to_prompt_text()


def test_derive_ask_status_prefers_resource_failure_over_no_evidence() -> None:
    from bot.answer_verifier import VerificationResult

    status = derive_ask_status(
        collection_status="timeout",
        has_usable=False,
        has_items=False,
        required_missing=["news"],
        llm_enabled=False,
        verification=VerificationResult(valid=False, errors=["x"]),
        answer="我不確定",
        answered=False,
        verification_attempted=False,
    )
    assert status == "timeout"


def test_local_monthly_revenue_failure_is_not_no_evidence(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    with patch("bot.ask.MonthlyRevenueAdapter") as adapter_cls:
        adapter_cls.return_value.collect.return_value = ResourceResult(
            resource="fundamental",
            status="failed",
            warnings=["月營收 adapter 失敗：RuntimeError: boom"],
            live=False,
        )
        with patch(
            "bot.ask_adapters.collect_snapshot_resource_evidence",
            return_value=[],
        ):
            result = AskEngine(
                root=tmp_path, db=db, use_llm_planner=False, enable_live_sources=False,
            ).ask("2330 基本面？", ticker="2330")
    assert result.status == "resource_failed"
    assert result.evidence.collection_status == "resource_failed"
    assert any(
        outcome.get("resource") in {"fundamental", "monthly_revenue"}
        and outcome.get("status") in {"failed", "resource_failed"}
        for outcome in result.evidence.resource_outcomes
    )


def test_snapshot_adapter_exception_maps_to_resource_failed_outcome(
    tmp_path: Path,
) -> None:
    db = StockDB.open(root=tmp_path)
    with patch(
        "bot.ask_adapters.collect_snapshot_resource_evidence",
        side_effect=RuntimeError("adapter down"),
    ):
        result = AskEngine(
            root=tmp_path, db=db, use_llm_planner=False, enable_live_sources=False,
        ).ask("2330 技術面？", ticker="2330")
    assert any(
        outcome.get("resource") == "snapshot"
        and outcome.get("status") == "resource_failed"
        for outcome in result.evidence.resource_outcomes
    )
    assert result.evidence.collection_status == "resource_failed"
    assert result.status == "resource_failed"
