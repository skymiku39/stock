"""ETF Ask：規劃契約與 snapshot evidence provenance（不打外網）。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.active_etf import Holding, HoldingsSnapshot, save_holdings
from bot.ask_adapters import collect_snapshot_resource_evidence
from bot.query_plan import VALID_RESOURCES, normalize_planner_payload, plan_query
from bot.source_registry import SourceRegistry


def _etf_snap(*, fetched_at: str = "2026-09-20T12:00:00+08:00") -> SimpleNamespace:
    return SimpleNamespace(
        name="台積電",
        price=1000.0,
        pct_change=1.0,
        fetched_at=fetched_at,
        technicals=None,
        chip_summary=None,
        consensus={"etf_count": 1, "total_weight": 10.5},
        new_build_signal=None,
        add_signal=None,
        held_by_etfs=[
            {"etf_symbol": "00980A", "etf_name": "主動野村臺灣優選", "weight_pct": 10.5},
        ],
        macro_snapshot=None,
        fundamentals=None,
    )


def test_deterministic_etf_plan_requests_only_registered_resources() -> None:
    plan = plan_query("2330 被哪些 ETF 持股？成分股權重多少？")
    assert "etf" in plan.resource_needs
    assert plan.planner_required is False
    assert all(resource in VALID_RESOURCES for resource in plan.resource_needs)


def test_llm_normalization_keeps_registered_etf_and_drops_unknown_tools() -> None:
    fallback = plan_query("2330 ETF 新建倉與加碼")
    assert "etf" in fallback.resource_needs
    normalized = normalize_planner_payload(
        {
            "resource_needs": ["etf", "etf_scraper", "shell_command", "web_search"],
            "keywords": ["2330", "ETF"],
        },
        fallback=fallback,
    )
    assert "etf" in normalized.resource_needs
    assert "etf_scraper" not in normalized.resource_needs
    assert "shell_command" not in normalized.resource_needs
    assert all(resource in VALID_RESOURCES for resource in normalized.resource_needs)


def test_etf_evidence_is_cache_only_without_replayable_holdings_docs(
    tmp_path: Path,
) -> None:
    save_holdings(
        HoldingsSnapshot(
            symbol="00980A",
            date=dt.date(2026, 9, 19),
            holdings=[Holding(ticker="2330", name="台積電", weight_pct=10.5)],
        ),
        root=tmp_path,
    )
    snap = _etf_snap()
    with patch("bot.ticker_view.build_snapshot", return_value=snap):
        items = collect_snapshot_resource_evidence(
            "2330", root=tmp_path, resource_needs=["etf"],
        )

    etf = next(item for item in items if item["kind"] == "etf")
    assert etf["provenance_status"] == "cache_only"
    assert etf["metadata"]["cache_only"] is True
    assert etf["metadata"]["source_document_ids"] == []
    assert not str(etf.get("source_document_id") or "").startswith("source-doc:")
    assert etf["metadata"]["holdings_dates"] == {"00980A": "2026-09-19"}
    assert etf["as_of"] == "2026-09-19"
    assert etf["observed_at"] == "2026-09-19"
    assert etf["retrieved_at"] == snap.fetched_at
    assert etf["metadata"]["temporal_status"] == "aligned"


def test_etf_evidence_is_composite_exact_only_with_replayable_holdings(
    tmp_path: Path,
) -> None:
    save_holdings(
        HoldingsSnapshot(
            symbol="00980A",
            date=dt.date(2026, 9, 19),
            holdings=[Holding(ticker="2330", name="台積電", weight_pct=10.5)],
        ),
        root=tmp_path,
    )
    with SourceRegistry(tmp_path) as registry:
        document = registry.capture_document(
            "etf_holdings_00980A",
            "<html>2330 台積電 weight 10.50%</html>".encode("utf-8"),
            source_ref="00980A:2026-09-19",
            title="00980A holdings 2026-09-19",
            suffix="html",
            mime_type="text/html",
            as_of="2026-09-19",
        )

    snap = _etf_snap()
    with patch("bot.ticker_view.build_snapshot", return_value=snap):
        items = collect_snapshot_resource_evidence(
            "2330", root=tmp_path, resource_needs=["etf"],
        )

    etf = next(item for item in items if item["kind"] == "etf")
    assert etf["kind"] == "etf"
    assert etf["provenance_status"] == "composite_exact"
    assert etf["metadata"]["cache_only"] is False
    assert etf["metadata"]["source_document_ids"] == [document.document_id]
    assert etf["metadata"]["holdings_dates"] == {"00980A": "2026-09-19"}
    assert etf["as_of"] == "2026-09-19"
    assert etf["metadata"]["temporal_status"] == "aligned"
    # 共識證據不把單一 document_id 升格成假装的單一 exact 來源。
    assert not str(etf.get("source_document_id") or "").startswith("source-doc:")


def test_etf_evidence_does_not_invent_source_ids_for_partial_holdings_raw(
    tmp_path: Path,
) -> None:
    """多檔 CSV 中只有部分有 raw → 不可升級 composite_exact，也不可臆造缺漏 ID。"""
    save_holdings(
        HoldingsSnapshot(
            symbol="00980A",
            date=dt.date(2026, 9, 19),
            holdings=[Holding(ticker="2330", name="台積電", weight_pct=10.5)],
        ),
        root=tmp_path,
    )
    save_holdings(
        HoldingsSnapshot(
            symbol="00982A",
            date=dt.date(2026, 9, 18),
            holdings=[Holding(ticker="2330", name="台積電", weight_pct=8.0)],
        ),
        root=tmp_path,
    )
    with SourceRegistry(tmp_path) as registry:
        registry.capture_document(
            "etf_holdings_00980A",
            b"<html>2330 weight 10.50%</html>",
            source_ref="00980A:2026-09-19",
            title="00980A holdings",
            suffix="html",
            mime_type="text/html",
            as_of="2026-09-19",
        )

    snap = SimpleNamespace(
        name="台積電",
        price=1000.0,
        pct_change=1.0,
        fetched_at="2026-09-20T12:00:00+08:00",
        technicals=None,
        chip_summary=None,
        consensus={"etf_count": 2, "total_weight": 18.5},
        new_build_signal=None,
        add_signal=None,
        held_by_etfs=[
            {"etf_symbol": "00980A", "weight_pct": 10.5},
            {"etf_symbol": "00982A", "weight_pct": 8.0},
        ],
        macro_snapshot=None,
        fundamentals=None,
    )
    with patch("bot.ticker_view.build_snapshot", return_value=snap):
        items = collect_snapshot_resource_evidence(
            "2330", root=tmp_path, resource_needs=["etf"],
        )

    etf = next(item for item in items if item["kind"] == "etf")
    assert etf["provenance_status"] == "cache_only"
    assert etf["metadata"]["cache_only"] is True
    assert etf["metadata"]["source_document_ids"] == []
    assert etf["metadata"]["holdings_dates"] == {
        "00980A": "2026-09-19",
        "00982A": "2026-09-18",
    }
    assert etf["metadata"]["temporal_status"] == "mixed"
    assert etf["as_of"] == "2026-09-19"


