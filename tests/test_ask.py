from __future__ import annotations

from dataclasses import replace

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot.ask import AskEngine, UNCERTAIN_ANSWER, _evidence_matches_resource
from bot.answer_verifier import verify_answer_payload
from bot.evidence import (
    EvidencePack,
    is_evidence_fresh,
    is_evidence_temporally_acceptable,
    make_evidence,
)
from bot.memory_store import MemoryStore
from bot.query_plan import VALID_RESOURCES, normalize_planner_payload, plan_query
from bot.resource_adapters import ResourceResult, WebSearchAdapter
from bot.stock_db import (
    DividendHistory,
    MonthlyRevenue,
    QuarterlyReport,
    StockDB,
    StockInfo,
    ValuationDaily,
)


def test_ask_engine_closes_only_database_handles_it_owns(tmp_path: Path) -> None:
    owned = AskEngine(root=tmp_path / "owned", client=None)
    owned_db = owned.db
    assert owned_db._local.conn is not None
    owned.close()
    assert owned_db._local.conn is None

    supplied_db = StockDB.open(root=tmp_path / "supplied")
    supplied = AskEngine(root=tmp_path / "supplied", db=supplied_db, client=None)
    supplied.close()
    assert supplied_db._local.conn is not None
    supplied_db.close()


def test_plan_is_always_created_and_marks_ambiguous_query_for_planner() -> None:
    plan = plan_query("這個頻道以前主要討論什麼？")
    assert plan.planner_required is False
    assert "channel_knowledge" in plan.resource_needs
    assert "historical_evidence" in plan.resource_needs
    assert plan.freshness == "unbounded"

    unknown = plan_query("請幫我看看這件事情")
    assert unknown.planner_required is True
    assert "web_search" in unknown.resource_needs


def test_query_plan_prompt_declares_every_registered_resource() -> None:
    from bot.prompt_registry import PromptRegistry

    prompts = PromptRegistry(Path(__file__).resolve().parents[1] / "prompts")
    template = prompts.get("query_plan")
    assert template is not None
    assert all(resource in template.template for resource in VALID_RESOURCES)


def test_recent_word_uses_seven_day_freshness_window() -> None:
    assert plan_query("2330 最近消息").freshness == "7d"


def test_major_announcements_route_to_news_and_live_mops() -> None:
    plan = plan_query("台積電最近的月營收和重大訊息？", ticker="2330")
    assert "fundamental" in plan.resource_needs
    assert "news" in plan.resource_needs
    assert "mops" in plan.resource_needs
    assert "重大訊息" in plan.keywords
    assert plan.planner_required is False


def test_dividend_query_routes_to_fundamental_resource() -> None:
    plan = plan_query("2330 近年股利與現金股利是多少？")
    assert plan.ticker == "2330"
    assert "fundamental" in plan.resource_needs
    assert plan.planner_required is False
    assert "股利" in plan.keywords


def test_web_search_adapter_compacts_conversational_query(tmp_path: Path) -> None:
    plan = plan_query(
        "台積電最近的營收、估值、股利、季報、新聞、法說會與總經背景，"
        "請分別標示資料日期、抓取時間、來源與不確定性",
        ticker="2330",
    )
    with patch("bot.web_search.search_news", return_value=[]) as search:
        result = WebSearchAdapter(
            root=tmp_path, resource_name="news", timeout=1,
        ).collect(plan)
    assert result.status == "partial"
    query = str(search.call_args.args[0])
    assert len(query) < len(plan.query)
    assert "2330" in query
    assert "營收" in query
    assert "法說會" in query
    assert " OR " in query
    assert "請分別標示" not in query
    assert "資料日期" not in query
    assert "when:7d" in query
    assert result.metadata["search_query"] == query


def test_evidence_quota_keeps_news_when_local_evidence_fills_first(
    tmp_path: Path,
) -> None:
    engine = AskEngine(root=tmp_path, client=None, enable_live_sources=True)
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(
        timespec="seconds",
    )
    news = make_evidence(
        "news",
        source="fixture-news",
        observed_at=now,
        excerpt="台積電重大訊息 fixture",
    )
    local = [
        make_evidence(
            "ticker_snapshot",
            source=f"local-{index}",
            observed_at=now,
            excerpt=f"local {index}",
        )
        for index in range(8)
    ]
    plan = plan_query("2330 最近的重大訊息", ticker="2330")
    with patch.object(engine, "_collect_local_ticker_evidence", return_value=(local, [], [])), \
            patch("bot.ask.WebSearchAdapter") as web_cls, \
            patch("bot.ask.MopsAdapter") as mops_cls, \
            patch("bot.ask.LiveTickerAdapter") as ticker_cls:
        web_cls.return_value.collect.return_value = ResourceResult(
            resource="news", evidence=[news], live=True, status="ok",
        )
        mops_cls.return_value.collect.return_value = ResourceResult(
            resource="mops", evidence=[], live=True, status="partial",
        )
        ticker_cls.return_value.collect.return_value = ResourceResult(
            resource="ticker_snapshot", evidence=[], live=True, status="partial",
        )
        pack = engine._collect_evidence(plan)
    assert any(item["kind"] == "news" for item in pack.items)
    assert "news" not in pack.missing_resources


def test_live_evidence_wins_same_resource_quota(tmp_path: Path) -> None:
    engine = AskEngine(root=tmp_path, client=None, enable_live_sources=True)
    local = make_evidence(
        "ticker_snapshot",
        source="local-cache",
        observed_at="2026-09-18T13:30:00+08:00",
        excerpt="2330 舊快取 900",
    )
    live = make_evidence(
        "ticker_snapshot",
        source="twse_mis",
        observed_at="2026-09-20T09:01:00+08:00",
        excerpt="2330 live 1000",
        metadata={"live": True},
    )
    plan = plan_query("2330 最新盤中資料", ticker="2330")
    plan = replace(plan, resource_needs=["ticker_snapshot"])
    with patch.object(engine, "_collect_local_ticker_evidence", return_value=([local], [], [])), \
            patch("bot.ask.LiveTickerAdapter") as ticker_cls:
        ticker_cls.return_value.collect.return_value = ResourceResult(
            resource="ticker_snapshot", evidence=[live], live=True, status="ok",
        )
        pack = engine._collect_evidence(plan)
    ticker_items = [item for item in pack.items if item["kind"] == "ticker_snapshot"]
    assert ticker_items
    assert ticker_items[0]["source"] == "twse_mis"


def test_ask_infers_ticker_from_local_company_name(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_stock_info(StockInfo(
        symbol="2330",
        name="台灣積體電路製造",
        short_name="台積電",
        quality_status="ok",
    ))
    result = AskEngine(root=tmp_path, db=db).ask("台積電最近消息")
    assert result.plan.ticker == "2330"
    assert result.plan.freshness == "7d"


def test_llm_planner_payload_cannot_inject_unknown_resource() -> None:
    fallback = plan_query("2330 最新基本面")
    normalized = normalize_planner_payload({
        "resource_needs": ["fundamental", "shell_command", "web_search"],
        "keywords": ["2330", "營收"],
        "freshness": "24h",
        "max_sources": "bad",
    }, fallback=fallback)
    assert normalized.resource_needs == [
        "memory", "fundamental", "ticker_snapshot", "web_search",
    ]
    assert normalized.ticker == "2330"
    assert normalized.freshness == "24h"
    assert normalized.citation_required is True


def test_etf_query_plan_and_llm_normalization_stay_within_registered_resources() -> None:
    plan = plan_query("2330 ETF 持股與新建倉")
    assert "etf" in plan.resource_needs
    assert all(resource in VALID_RESOURCES for resource in plan.resource_needs)
    normalized = normalize_planner_payload(
        {"resource_needs": ["etf", "etf_live_fetch", "unknown_tool"]},
        fallback=plan,
    )
    assert "etf" in normalized.resource_needs
    assert "etf_live_fetch" not in normalized.resource_needs
    assert "unknown_tool" not in normalized.resource_needs
    assert all(resource in VALID_RESOURCES for resource in normalized.resource_needs)


def test_llm_planner_cannot_remove_deterministic_freshness_or_forecast_policy() -> None:
    fallback = plan_query("2330 最新基本面")
    normalized = normalize_planner_payload({
        "resource_needs": ["memory"],
        "freshness": "as_available",
        "allow_forecast": True,
    }, fallback=fallback)
    assert normalized.freshness == "24h"
    assert normalized.allow_forecast is False
    assert "fundamental" in normalized.resource_needs


def test_llm_planner_can_replace_only_speculative_unknown_fallback() -> None:
    fallback = plan_query("請幫我看看這件事情")
    normalized = normalize_planner_payload({
        "resource_needs": ["professional_knowledge"],
    }, fallback=fallback)
    assert normalized.resource_needs == ["memory", "professional_knowledge"]
    assert "news" not in normalized.resource_needs
    assert "web_search" not in normalized.resource_needs


def test_llm_planner_parses_false_without_python_truthiness_bug() -> None:
    fallback = plan_query("2330 未來展望")
    normalized = normalize_planner_payload({"allow_forecast": "false"}, fallback=fallback)
    assert normalized.allow_forecast is False


def test_temporal_policy_is_strict_when_user_requires_same_date() -> None:
    fallback = plan_query("2330 ADR 與台股同一交易日比較")
    assert fallback.temporal_policy == "same_date"
    normalized = normalize_planner_payload({"temporal_policy": "allow_mixed"}, fallback=fallback)
    assert normalized.temporal_policy == "same_date"


def test_temporal_evidence_rejects_mixed_sources_only_in_strict_mode() -> None:
    item = make_evidence(
        "macro",
        source="macro_cache",
        excerpt="ADR=2026-09-16; TW=2026-09-17",
        metadata={"temporal_status": "mixed"},
    )
    assert is_evidence_temporally_acceptable(item, "warn_mixed")
    assert not is_evidence_temporally_acceptable(item, "same_date")


def test_evidence_freshness_is_conservative_for_strict_windows() -> None:
    reference = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)
    fresh = make_evidence(
        "news", source="official", observed_at="2026-09-10T17:00:00+08:00", excerpt="最新公告",
    )
    stale = make_evidence(
        "news", source="official", observed_at="2026-09-08T17:00:00+08:00", excerpt="舊公告",
    )
    missing_time = make_evidence("news", source="official", excerpt="無時間公告")
    period = make_evidence(
        "fundamental", source="official", observed_at="2026-08",
        retrieved_at="2026-09-10T11:00:00+08:00", excerpt="月營收",
    )
    assert is_evidence_fresh(fresh, "24h", now=reference)
    assert not is_evidence_fresh(stale, "24h", now=reference)
    assert not is_evidence_fresh(missing_time, "24h", now=reference)
    assert is_evidence_fresh(period, "24h", now=reference)

    period_without_retrieval = make_evidence(
        "fundamental", source="official", observed_at="2026-08", excerpt="月營收",
    )
    assert not is_evidence_fresh(period_without_retrieval, "24h", now=reference)

    quarterly = make_evidence(
        "quarterly_report",
        source="official",
        as_of="2026Q2",
        observed_at="2026Q2",
        retrieved_at="2026-09-10T11:00:00+08:00",
        excerpt="季報",
    )
    assert is_evidence_fresh(quarterly, "24h", now=reference)
    quarterly_without_retrieval = make_evidence(
        "quarterly_report",
        source="official",
        as_of="2026Q2",
        observed_at="2026Q2",
        excerpt="季報",
    )
    assert not is_evidence_fresh(quarterly_without_retrieval, "24h", now=reference)


def test_valuation_and_dividend_strict_freshness_uses_retrieved_at_only() -> None:
    """valuation_daily／dividend_history 嚴格 freshness 只看 retrieved_at。"""
    reference = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)
    recent_observed = "2026-09-10T11:55:00+08:00"
    old_period = "2026-08-01"
    fresh_retrieved = "2026-09-10T11:00:00+08:00"
    stale_retrieved = "2026-09-08T11:00:00+08:00"

    for kind in ("valuation_daily", "dividend_history"):
        missing_retrieval = make_evidence(
            kind,
            source="official",
            observed_at=recent_observed,
            as_of=old_period,
            excerpt=kind,
        )
        assert not is_evidence_fresh(missing_retrieval, "24h", now=reference)

        fresh_retrieval_old_period = make_evidence(
            kind,
            source="official",
            observed_at=old_period,
            as_of=old_period,
            retrieved_at=fresh_retrieved,
            excerpt=kind,
        )
        assert is_evidence_fresh(fresh_retrieval_old_period, "24h", now=reference)

        stale_retrieval = make_evidence(
            kind,
            source="official",
            observed_at=recent_observed,
            as_of=old_period,
            retrieved_at=stale_retrieved,
            excerpt=kind,
        )
        assert not is_evidence_fresh(stale_retrieval, "24h", now=reference)

        assert is_evidence_fresh(missing_retrieval, "as_available", now=reference)
        assert is_evidence_fresh(missing_retrieval, "unbounded", now=reference)

    # 其他期間型仍維持 retrieved_at → observed_at fallback。
    other_period = make_evidence(
        "fundamental",
        source="official",
        observed_at=recent_observed,
        excerpt="月營收",
    )
    assert is_evidence_fresh(other_period, "24h", now=reference)


def test_evidence_keeps_distinct_time_semantics() -> None:
    item = make_evidence(
        "news",
        source="official",
        observed_at="2026-09-10T12:00:00+08:00",
        published_at="2026-09-10T10:00:00+08:00",
        retrieved_at="2026-09-10T12:05:00+08:00",
        excerpt="公告",
    )
    assert item["published_at"].startswith("2026-09-10T10:00")
    assert item["retrieved_at"].startswith("2026-09-10T12:05")


def test_evidence_pack_deduplicates_and_limits_prompt_text() -> None:
    item = make_evidence(
        "news",
        source="official",
        source_ref="n-1",
        title="公告",
        excerpt="2330 營收成長",
    )
    pack = EvidencePack(
        query="2330 營收",
        plan_id="plan:test",
        trace_id="trace:test",
        items=[item, item],
    )
    assert pack.evidence_ids == [item["id"]]
    assert "2330 營收成長" in pack.to_prompt_text()


def test_ask_without_llm_returns_uncertain_and_keeps_short_audit(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    store = MemoryStore(db)
    store.put_long(
        "2330 最近營收成長，來源為官方公告。",
        memory_id="memory:2330:revenue",
        summary="2330 營收成長",
        subject="2330",
        quality_status="ok",
        source="official",
    )

    result = AskEngine(root=tmp_path, db=db).ask("2330 最近營收如何？")
    assert result.status == "uncertain"
    assert result.answer == UNCERTAIN_ANSWER
    assert result.evidence.usable_items
    assert db.get_memory_item(f"ask:{result.trace_id}") is not None


def test_ask_reads_local_canonical_ticker_and_fundamental_evidence(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_stock_info(StockInfo(
        symbol="2330",
        name="台積電",
        industry="半導體業",
        quality_status="ok",
        source_record_id="company:2330",
    ))
    db.upsert_monthly_revenue(MonthlyRevenue(
        symbol="2330",
        year_month="2026-08",
        revenue=100.0,
        yoy_pct=12.0,
        quality_status="ok",
        source_record_id="revenue:2330:2026-08",
    ))

    result = AskEngine(root=tmp_path, db=db).ask("2330 最新營收？")
    kinds = {item["kind"] for item in result.evidence.usable_items}
    assert "ticker_snapshot" in kinds
    assert "monthly_revenue" in kinds
    assert result.evidence.missing_resources == []
    registered = db.list_evidence(limit=20)
    assert {item.evidence_id for item in registered}.issuperset(
        set(result.evidence.evidence_ids)
    )


def test_ask_keeps_multiple_dividend_years_for_historical_query(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    for year, cash in ((2026, 14.0), (2025, 6.0), (2024, 4.0)):
        db.upsert_dividend_history(DividendHistory(
            symbol="2330",
            year=year,
            cash_dividend=cash,
            quality_status="ok",
            source_record_id=f"source-doc:dividend:{year}",
        ))

    result = AskEngine(root=tmp_path, db=db, use_llm_planner=False).ask(
        "2330 近年股利與現金股利是多少？",
    )
    dividend_items = [
        item for item in result.evidence.items
        if item["kind"] == "dividend_history"
    ]
    assert len(dividend_items) == 3
    assert {item["as_of"] for item in dividend_items} == {"2026", "2025", "2024"}


def test_ask_does_not_call_missing_manifest_ids_exact_provenance(
    tmp_path: Path,
) -> None:
    """canonical row 遺留的不存在 manifest ID 不得偽裝成 exact evidence。"""
    db = StockDB.open(root=tmp_path)
    missing_id = "source-doc:historical-missing"
    db.upsert_valuation(ValuationDaily(
        symbol="2330",
        as_of="2026-09-18",
        pe_ratio=20.0,
        quality_status="ok",
        source_record_id=missing_id,
    ))
    db.upsert_dividend_history(DividendHistory(
        symbol="2330",
        year=2025,
        cash_dividend=5.0,
        quality_status="ok",
        source_record_id=missing_id,
    ))
    db.upsert_quarterly_report(QuarterlyReport(
        symbol="2330",
        period="2026Q1",
        eps=13.0,
        quality_status="ok",
        source_record_id=missing_id,
    ))

    result = AskEngine(root=tmp_path, db=db, use_llm_planner=False).ask(
        "2330 基本面？",
    )
    rows = [
        item for item in result.evidence.items
        if item["kind"] in {"valuation_daily", "dividend_history", "fundamental"}
    ]
    assert {item["kind"] for item in rows} == {
        "valuation_daily", "dividend_history", "fundamental",
    }
    assert all(item["source_document_id"] == "" for item in rows)
    assert all(item["provenance_status"] == "unresolved" for item in rows)


def test_ask_excludes_stale_evidence_from_answer_context(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    stale = make_evidence(
        "news",
        source="official",
        source_ref="news:old",
        observed_at="2020-01-01T09:00:00+08:00",
        excerpt="2330 舊新聞",
    )
    MemoryStore(db).put_long(
        "2330 舊新聞",
        memory_id="memory:2330:old-news",
        summary="2330 舊新聞",
        subject="2330",
        quality_status="ok",
        evidence=[stale],
    )

    result = AskEngine(root=tmp_path, db=db).ask("2330 最新新聞？")
    assert result.status == "live_disabled"
    assert result.evidence.usable_items == []
    assert any("freshness=24h" in warning for warning in result.evidence.warnings)


def test_ask_does_not_use_ticker_snapshot_as_news_substitute(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_stock_info(StockInfo(
        symbol="2330",
        name="台積電",
        industry="半導體業",
        quality_status="ok",
        source_record_id="company:2330",
    ))
    client = MagicMock()
    client.enabled = True
    client.model = "test-model"

    with patch("bot.ask.gemini_call") as mocked_call:
        result = AskEngine(
            root=tmp_path,
            db=db,
            client=client,
            use_llm_planner=False,
        ).ask("2330 最新新聞？")

    assert result.status == "partial"
    assert result.evidence.collection_status == "partial"
    assert result.answer == UNCERTAIN_ANSWER
    assert "news" in result.verification.errors[0]
    mocked_call.assert_not_called()
    assert any(
        outcome.get("status") == "live_disabled"
        for outcome in result.evidence.resource_outcomes
    )


def test_ask_surfaces_snapshot_adapter_failure_as_warning(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    with patch(
        "bot.ask_adapters.collect_snapshot_resource_evidence",
        side_effect=RuntimeError("adapter down"),
    ):
        result = AskEngine(root=tmp_path, db=db, use_llm_planner=False).ask(
            "2330 基本面？"
        )
    assert any("snapshot adapter 發生例外" in warning for warning in result.evidence.warnings)
    assert any(
        outcome.get("resource") == "snapshot"
        and outcome.get("status") == "resource_failed"
        for outcome in result.evidence.resource_outcomes
    )
    assert result.evidence.collection_status == "resource_failed"


def test_ask_monthly_revenue_adapter_failure_adds_resource_outcome(
    tmp_path: Path,
) -> None:
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
                root=tmp_path, db=db, use_llm_planner=False,
            ).ask("2330 基本面？", ticker="2330")

    assert any("月營收來源失敗" in warning for warning in result.evidence.warnings)
    failed = [
        outcome for outcome in result.evidence.resource_outcomes
        if outcome.get("resource") in {"fundamental", "monthly_revenue"}
        and outcome.get("status") in {"failed", "resource_failed"}
    ]
    assert failed
    assert failed[0].get("live") is False
    assert result.evidence.collection_status == "resource_failed"


def test_ask_quarterly_evidence_uses_canonical_retrieved_at(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_quarterly_report(QuarterlyReport(
        symbol="2330",
        period="2026Q1",
        eps=13.0,
        quality_status="ok",
        retrieved_at="2026-09-10T11:00:00+08:00",
        source_record_id="quarterly:2330:2026Q1",
    ))
    with patch(
        "bot.ask_adapters.collect_snapshot_resource_evidence",
        return_value=[],
    ):
        result = AskEngine(root=tmp_path, db=db, use_llm_planner=False).ask(
            "2330 基本面？",
        )
    quarterly_items = [
        item for item in result.evidence.items if item["kind"] == "fundamental"
    ]
    assert quarterly_items
    assert quarterly_items[0]["retrieved_at"].startswith("2026-09-10T11:00")


def test_ask_quarterly_evidence_blank_retrieved_at_when_canonical_missing(
    tmp_path: Path,
) -> None:
    db = StockDB.open(root=tmp_path)
    # StockDB upsert 會補上 retrieved_at；此案例直接餵空值列給 Ask 讀取路徑。
    blank_row = QuarterlyReport(
        symbol="2330",
        period="2026Q1",
        eps=13.0,
        quality_status="ok",
        retrieved_at="",
        source_record_id="quarterly:2330:2026Q1",
    )
    fabricated = "2026-09-22T20:00:00+08:00"
    with patch(
        "bot.ask.now_tw",
        return_value=dt.datetime(
            2026, 9, 22, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)),
        ),
    ):
        with patch.object(db, "get_quarterly_report", return_value=[blank_row]):
            with patch(
                "bot.ask_adapters.collect_snapshot_resource_evidence",
                return_value=[],
            ):
                result = AskEngine(
                    root=tmp_path, db=db, use_llm_planner=False,
                ).ask("2330 基本面？")
    quarterly_items = [
        item for item in result.evidence.items if item["kind"] == "fundamental"
    ]
    assert quarterly_items
    assert quarterly_items[0]["retrieved_at"] == ""
    assert quarterly_items[0]["retrieved_at"] != fabricated


def test_ask_accepts_only_answers_linked_to_pack_evidence(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    item = make_evidence(
        "fundamental",
        source="official",
        source_ref="rev-1",
        title="月營收公告",
        excerpt="2330 五月營收年增 10%",
    )
    MemoryStore(db).put_long(
        "2330 五月營收年增 10%",
        memory_id="memory:2330:official-revenue",
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
        "uncertainty": "",
    }
    with patch("bot.ask.gemini_call", return_value=(
        json.dumps(payload, ensure_ascii=False),
        {"prompt_id": "ask_answer", "prompt_version": "1.0"},
    )) as mocked_call:
        result = AskEngine(root=tmp_path, db=db, client=client).ask("2330 五月營收如何？")

    assert result.status == "answered"
    assert result.verified is True
    assert result.verification.valid is True
    assert [call.args[0] for call in mocked_call.call_args_list] == [
        "query_plan", "ask_answer",
    ]


def test_ask_repairs_one_invalid_answer_then_stops_at_bound(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    item = make_evidence(
        "fundamental",
        source="official",
        source_ref="rev-repair",
        title="月營收公告",
        excerpt="2330 五月營收年增 10%",
    )
    MemoryStore(db).put_long(
        "2330 五月營收年增 10%",
        memory_id="memory:2330:repair",
        summary="2330 五月營收年增",
        subject="2330",
        quality_status="ok",
        evidence=[item],
    )
    client = MagicMock()
    client.enabled = True
    client.model = "test-model"
    invalid = {
        "answer": "這是結論",
        "confidence": 0.9,
        "evidence_ids": ["missing"],
        "claims": [],
    }
    valid = {
        "answer": "根據公告，2330 五月營收年增 10%。",
        "confidence": 0.9,
        "evidence_ids": [item["id"]],
        "claims": [{"text": "五月營收年增 10%", "evidence_ids": [item["id"]]}],
        "uncertainty": "",
    }
    with patch("bot.ask.gemini_call", side_effect=[
        (json.dumps(invalid, ensure_ascii=False), {"attempt": 1}),
        (json.dumps(valid, ensure_ascii=False), {"attempt": 2}),
    ]) as mocked_call:
        result = AskEngine(
            root=tmp_path,
            db=db,
            client=client,
            use_llm_planner=False,
        ).ask("2330 五月營收如何？")

    assert result.status == "answered"
    assert result.verified is True
    assert mocked_call.call_count == 2
    assert result.llm_info["answer_repair"]["attempt"] == 2

    repair_kwargs = mocked_call.call_args_list[1].kwargs
    assert "repair_feedback" in repair_kwargs
    assert "previous_answer_json" in repair_kwargs
    assert "verification_errors" not in repair_kwargs
    assert "previous_output_json" not in repair_kwargs
    assert "不存在" in repair_kwargs["repair_feedback"] or "missing" in repair_kwargs[
        "previous_answer_json"
    ]
    assert '"evidence_ids": ["missing"]' in repair_kwargs["previous_answer_json"] or (
        '"missing"' in repair_kwargs["previous_answer_json"]
    )

    from bot.prompt_registry import get_registry

    rendered = get_registry().render(
        "ask_answer",
        query="2330 五月營收如何？",
        plan_json="{}",
        evidence_text="evidence",
        missing_resources="(無)",
        repair_feedback=repair_kwargs["repair_feedback"],
        previous_answer_json=repair_kwargs["previous_answer_json"],
    )
    assert "驗證錯誤：" in rendered
    assert repair_kwargs["repair_feedback"] in rendered
    assert "上一版 JSON：" in rendered
    assert "missing" in rendered

    for call in mocked_call.call_args_list:
        metadata = call.kwargs.get("metadata") or {}
        assert metadata.get("trace_id") == result.trace_id
        assert metadata.get("evidence_ids") == result.evidence.evidence_ids


def test_ask_warn_mixed_retains_temporal_mixed_with_explicit_warning(
    tmp_path: Path,
) -> None:
    db = StockDB.open(root=tmp_path)
    item = make_evidence(
        "fundamental",
        source="official",
        source_ref="rev-mixed",
        title="月營收公告",
        excerpt="2330 五月營收年增 10%",
        metadata={"temporal_status": "mixed"},
    )
    MemoryStore(db).put_long(
        "2330 五月營收年增 10%",
        memory_id="memory:2330:mixed",
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
        "uncertainty": "",
    }
    with patch("bot.ask.gemini_call", return_value=(
        json.dumps(payload, ensure_ascii=False),
        {"prompt_id": "ask_answer"},
    )):
        result = AskEngine(
            root=tmp_path,
            db=db,
            client=client,
            use_llm_planner=False,
        ).ask("2330 五月營收如何？")

    assert item["id"] in result.evidence.evidence_ids
    usable_ids = [str(row["id"]) for row in result.evidence.usable_items]
    assert item["id"] in usable_ids
    assert any(
        "temporal_status=mixed" in warning and "跨日" in warning
        for warning in result.evidence.warnings
    )


def test_answer_verifier_rejects_unknown_evidence_id() -> None:
    pack = EvidencePack(
        query="q",
        plan_id="p",
        trace_id="t",
        items=[make_evidence("news", source="x", excerpt="可用證據")],
    )
    result = verify_answer_payload({
        "answer": "結論",
        "confidence": 0.5,
        "evidence_ids": ["not-in-pack"],
    }, pack)
    assert result.valid is False
    assert any("不存在" in error for error in result.errors)


def test_answer_verifier_requires_claims_for_confident_answer() -> None:
    item = make_evidence("news", source="x", excerpt="可用證據")
    pack = EvidencePack(
        query="q",
        plan_id="p",
        trace_id="t",
        items=[item],
    )
    result = verify_answer_payload({
        "answer": "這是結論",
        "confidence": 0.5,
        "evidence_ids": [item["id"]],
    }, pack)
    assert result.valid is False
    assert any("claims" in error for error in result.errors)


def test_answer_verifier_rejects_cited_but_unrelated_claim() -> None:
    item = make_evidence(
        "fundamental",
        source="official",
        source_ref="rev-2",
        title="月營收公告",
        excerpt="2330 五月營收年增 10%",
    )
    pack = EvidencePack(
        query="2330 營收",
        plan_id="p",
        trace_id="t",
        items=[item],
    )
    result = verify_answer_payload({
        "answer": "公司即將大幅下跌。",
        "confidence": 0.9,
        "evidence_ids": [item["id"]],
        "claims": [{
            "text": "公司即將大幅下跌",
            "evidence_ids": [item["id"]],
        }],
    }, pack)
    assert result.valid is False
    assert result.claim_support[0]["supported"] is False
    assert any("無法在列出的 evidence" in error for error in result.errors)


def test_answer_verifier_rejects_unsupported_number_in_answer_body() -> None:
    item = make_evidence(
        "fundamental",
        source="official",
        source_ref="rev-3",
        title="月營收公告",
        excerpt="2330 五月營收年增 10%",
    )
    pack = EvidencePack(
        query="2330 營收",
        plan_id="p",
        trace_id="t",
        items=[item],
    )
    result = verify_answer_payload({
        "answer": "根據公告，2330 五月營收年增 10%；股價為 999 元。",
        "confidence": 0.8,
        "evidence_ids": [item["id"]],
        "claims": [{
            "text": "五月營收年增 10%",
            "evidence_ids": [item["id"]],
        }],
    }, pack)
    assert result.valid is False
    assert any("回答正文的數值" in error for error in result.errors)
    assert result.numeric_checks[0]["missing_numbers"] == ["999"]


def test_evidence_matches_mops_material_and_legacy_mops_news() -> None:
    mops_material = make_evidence(
        "mops_material",
        source="mops",
        excerpt="官方 MOPS 材料",
    )
    legacy_mops_news = make_evidence(
        "news",
        source="mops",
        excerpt="既有 MOPS 新聞格式",
    )
    generic_news = make_evidence(
        "news",
        source="fixture-news",
        excerpt="一般新聞",
    )

    assert _evidence_matches_resource(mops_material, "mops") is True
    assert _evidence_matches_resource(legacy_mops_news, "mops") is True
    assert _evidence_matches_resource(generic_news, "mops") is False
    assert _evidence_matches_resource(generic_news, "news") is True
    assert _evidence_matches_resource(mops_material, "news") is False


def test_collect_evidence_rejects_non_mops_news_for_mops_resource(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = AskEngine(root=tmp_path, client=None, enable_live_sources=True)
    observed = "2026-09-28T10:00:00+08:00"
    monkeypatch.setattr("bot.evidence.now_tw", lambda: dt.datetime.fromisoformat(observed))
    generic_news = make_evidence(
        "news",
        source="fixture-news",
        observed_at=observed,
        published_at=observed,
        retrieved_at=observed,
        excerpt="2330 一般新聞 fixture",
        quality_status="ok",
    )
    plan = plan_query("2330 最近重大訊息", ticker="2330")
    with patch.object(
        engine, "_collect_local_ticker_evidence", return_value=([], [], []),
    ), patch("bot.ask.WebSearchAdapter") as web_cls, \
            patch("bot.ask.MopsAdapter") as mops_cls, \
            patch("bot.ask.LiveTickerAdapter") as ticker_cls:
        web_cls.return_value.collect.return_value = ResourceResult(
            resource="news", evidence=[generic_news], live=True, status="ok",
        )
        mops_cls.return_value.collect.return_value = ResourceResult(
            resource="mops", evidence=[], live=True, status="partial",
        )
        ticker_cls.return_value.collect.return_value = ResourceResult(
            resource="ticker_snapshot", evidence=[], live=True, status="partial",
        )
        pack = engine._collect_evidence(plan)

    assert "news" not in pack.missing_resources
    assert "mops" in pack.missing_resources


def test_ask_official_mops_material_prevents_false_missing_mops(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = StockDB.open(root=tmp_path)
    observed = "2026-09-28T10:00:00+08:00"
    monkeypatch.setattr("bot.evidence.now_tw", lambda: dt.datetime.fromisoformat(observed))
    mops_item = make_evidence(
        "mops_material",
        source="mops",
        source_ref="mops:2330:fixture-material",
        title="MOPS 官方重大訊息",
        observed_at=observed,
        published_at=observed,
        retrieved_at=observed,
        excerpt="2330 公布重大訊息 fixture",
        quality_status="ok",
    )
    # Use news_cache so the news resource is met without the legacy kind=news
    # false-positive that currently also clears the mops requirement.
    news_item = make_evidence(
        "news_cache",
        source="fixture-news",
        source_ref="news:2330:fixture",
        title="相關新聞",
        observed_at=observed,
        published_at=observed,
        retrieved_at=observed,
        excerpt="2330 相關新聞 fixture",
        quality_status="ok",
    )
    MemoryStore(db).put_long(
        "2330 重大訊息",
        memory_id="memory:2330:mops-material",
        summary="2330 官方重大訊息",
        subject="2330",
        quality_status="ok",
        evidence=[mops_item, news_item],
    )
    client = MagicMock()
    client.enabled = True
    client.model = "test-model"
    payload = {
        "answer": "根據 MOPS，2330 公布重大訊息 fixture。",
        "confidence": 0.9,
        "evidence_ids": [mops_item["id"]],
        "claims": [{
            "text": "2330 公布重大訊息 fixture",
            "evidence_ids": [mops_item["id"]],
        }],
        "uncertainty": "",
    }
    with patch("bot.ask.gemini_call", return_value=(
        json.dumps(payload, ensure_ascii=False),
        {"prompt_id": "ask_answer", "prompt_version": "1.0"},
    )) as mocked_call:
        result = AskEngine(
            root=tmp_path,
            db=db,
            client=client,
            use_llm_planner=False,
        ).ask("2330 最近重大訊息", ticker="2330")

    assert "mops" not in result.evidence.missing_resources
    assert "mops" not in result.pending_resources
    assert not any("尚未取得必要資源" in err for err in result.verification.errors)
    assert result.status == "answered"
    assert result.verified is True
    assert any(call.args[0] == "ask_answer" for call in mocked_call.call_args_list)
