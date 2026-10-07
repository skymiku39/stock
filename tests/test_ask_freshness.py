"""Issue #4：估值／股利 evidence 應以 retrieved_at 判斷嚴格 freshness。

與 tests/test_ask.py 隔離，避免與 issue #3 進行中的變更互相干擾。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot.ask import AskEngine
from bot.evidence import is_evidence_fresh, make_evidence
from bot.query_plan import plan_query
from bot.stock_db import DividendHistory, StockDB, ValuationDaily
from bot.utils import TW_TZ


REFERENCE = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)


def test_valuation_daily_uses_retrieved_at_for_strict_freshness() -> None:
    fresh_retrieval = make_evidence(
        "valuation_daily",
        source="official",
        observed_at="2026-01-15",
        as_of="2026-01-15",
        retrieved_at="2026-09-10T11:00:00+08:00",
        excerpt="PER 20",
    )
    stale_retrieval = make_evidence(
        "valuation_daily",
        source="official",
        observed_at="2026-01-15",
        as_of="2026-01-15",
        retrieved_at="2026-09-08T11:00:00+08:00",
        excerpt="PER 20",
    )
    missing_retrieval = make_evidence(
        "valuation_daily",
        source="official",
        observed_at="2026-01-15",
        as_of="2026-01-15",
        excerpt="PER 20",
    )

    assert is_evidence_fresh(fresh_retrieval, "24h", now=REFERENCE)
    assert not is_evidence_fresh(stale_retrieval, "24h", now=REFERENCE)
    assert not is_evidence_fresh(missing_retrieval, "24h", now=REFERENCE)
    assert is_evidence_fresh(stale_retrieval, "as_available", now=REFERENCE)
    assert is_evidence_fresh(missing_retrieval, "as_available", now=REFERENCE)
    assert is_evidence_fresh(stale_retrieval, "unbounded", now=REFERENCE)


def test_dividend_history_uses_retrieved_at_for_strict_freshness() -> None:
    fresh_retrieval = make_evidence(
        "dividend_history",
        source="official",
        observed_at="2025-06-01",
        as_of="2025",
        retrieved_at="2026-09-10T11:00:00+08:00",
        excerpt="現金股利 5",
    )
    stale_retrieval = make_evidence(
        "dividend_history",
        source="official",
        observed_at="2025-06-01",
        as_of="2025",
        retrieved_at="2026-09-08T11:00:00+08:00",
        excerpt="現金股利 5",
    )
    missing_retrieval = make_evidence(
        "dividend_history",
        source="official",
        observed_at="2025-06-01",
        as_of="2025",
        excerpt="現金股利 5",
    )

    assert is_evidence_fresh(fresh_retrieval, "24h", now=REFERENCE)
    assert not is_evidence_fresh(stale_retrieval, "24h", now=REFERENCE)
    assert not is_evidence_fresh(missing_retrieval, "24h", now=REFERENCE)
    assert is_evidence_fresh(stale_retrieval, "as_available", now=REFERENCE)
    assert is_evidence_fresh(missing_retrieval, "as_available", now=REFERENCE)
    assert is_evidence_fresh(stale_retrieval, "unbounded", now=REFERENCE)


def test_ask_accepts_freshly_retrieved_period_valuation_for_latest_query(
    tmp_path: Path,
) -> None:
    """本地快取估值：資料期間較早但剛擷取，嚴格「最新」查詢應可回答。"""
    db = StockDB.open(root=tmp_path)
    retrieved_at = dt.datetime.now(TW_TZ).isoformat(timespec="seconds")
    db.upsert_valuation(ValuationDaily(
        symbol="2330",
        as_of="2026-01-15",
        pe_ratio=20.0,
        pb_ratio=5.0,
        dividend_yield=2.5,
        source_date="2026-01-15",
        retrieved_at=retrieved_at,
        quality_status="ok",
        source_record_id="valuation:2330:2026-01-15",
    ))

    engine = AskEngine(
        root=tmp_path,
        db=db,
        client=None,
        use_llm_planner=False,
    )
    pack = engine._collect_evidence(plan_query("2330 最新本益比？"))
    valuation_items = [
        item for item in pack.usable_items
        if item.get("kind") == "valuation_daily"
    ]
    assert valuation_items, "預期 freshly retrieved valuation_daily 通過 freshness=24h"
    evidence_ids = [item["id"] for item in valuation_items if item.get("id")]

    client = MagicMock()
    client.enabled = True
    client.model = "test-model"
    payload = {
        "answer": "根據本地估值，2330 PER 20。",
        "confidence": 0.9,
        "evidence_ids": evidence_ids,
        "claims": [{"text": "PER 20", "evidence_ids": evidence_ids}],
        "uncertainty": "",
    }
    with patch(
        "bot.ask.gemini_call",
        return_value=(
            json.dumps(payload, ensure_ascii=False),
            {"prompt_id": "ask_answer", "prompt_version": "1.0"},
        ),
    ) as mocked_call:
        result = AskEngine(
            root=tmp_path,
            db=db,
            client=client,
            use_llm_planner=False,
        ).ask("2330 最新本益比？")

    assert result.plan.freshness == "24h"
    assert any(
        item["kind"] == "valuation_daily" for item in result.evidence.usable_items
    )
    assert "fundamental" not in result.evidence.missing_resources
    assert result.status == "answered"
    assert result.verified is True
    assert any(call.args[0] == "ask_answer" for call in mocked_call.call_args_list)


def test_ask_accepts_freshly_retrieved_period_dividend_for_latest_query(
    tmp_path: Path,
) -> None:
    db = StockDB.open(root=tmp_path)
    retrieved_at = dt.datetime.now(TW_TZ).isoformat(timespec="seconds")
    db.upsert_dividend_history(DividendHistory(
        symbol="2330",
        year=2025,
        cash_dividend=5.0,
        stock_dividend=0.0,
        source_date="2025-06-01",
        retrieved_at=retrieved_at,
        quality_status="ok",
        source_record_id="dividend:2330:2025",
    ))

    engine = AskEngine(
        root=tmp_path,
        db=db,
        client=None,
        use_llm_planner=False,
    )
    pack = engine._collect_evidence(plan_query("2330 最新股利？"))
    dividend_items = [
        item for item in pack.usable_items
        if item.get("kind") == "dividend_history"
    ]
    assert dividend_items, "預期 freshly retrieved dividend_history 通過 freshness=24h"
    evidence_ids = [item["id"] for item in dividend_items if item.get("id")]

    client = MagicMock()
    client.enabled = True
    client.model = "test-model"
    payload = {
        "answer": "根據本地股利資料，2330 現金股利 5 元。",
        "confidence": 0.9,
        "evidence_ids": evidence_ids,
        "claims": [{"text": "現金股利 5 元", "evidence_ids": evidence_ids}],
        "uncertainty": "",
    }
    with patch(
        "bot.ask.gemini_call",
        return_value=(
            json.dumps(payload, ensure_ascii=False),
            {"prompt_id": "ask_answer", "prompt_version": "1.0"},
        ),
    ) as mocked_call:
        result = AskEngine(
            root=tmp_path,
            db=db,
            client=client,
            use_llm_planner=False,
        ).ask("2330 最新股利？")

    assert result.plan.freshness == "24h"
    assert any(
        item["kind"] == "dividend_history" for item in result.evidence.usable_items
    )
    assert "fundamental" not in result.evidence.missing_resources
    assert result.status == "answered"
    assert result.verified is True
    assert any(call.args[0] == "ask_answer" for call in mocked_call.call_args_list)
