from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import patch

from bot.active_etf import ActiveEtf
from bot.chips_fetcher import ChipSummary
from bot.data_pipeline import PipelineConfig, run_full_pipeline
from bot.market_macro import FuturesBasis, MacroSnapshot


def _result(symbol: str, *, success: bool, error: str = "", research_status: str = ""):
    return SimpleNamespace(
        etf=ActiveEtf(symbol=symbol, name=symbol, issuer="test"),
        success=success,
        error=error,
        research_status=research_status,
        research_error="",
        holdings_count=0,
        research_document_id="",
        research_evidence_id="",
        saved_path=None,
    )


def _config(root: Path) -> PipelineConfig:
    return PipelineConfig(
        project_root=root,
        fetch_etf_holdings=True,
        fetch_chips=False,
        fetch_macro=False,
        update_calendar=False,
        auto_research_focus=False,
        run_llm_analysis=False,
        generate_brief=False,
        generate_us_brief=False,
    )


def test_pipeline_records_expected_llm_disabled_etf_as_partial_warning(tmp_path: Path) -> None:
    with patch(
        "bot.data_pipeline.fetch_all_active_etfs",
        return_value=[
            _result("2330", success=True, research_status="ok"),
            _result("00998A", success=False, error="llm_disabled"),
        ],
    ):
        run = run_full_pipeline(_config(tmp_path))

    assert run.errors == []
    assert any("00998A" in warning for warning in run.warnings)
    assert run.etf_fetch_summary["status"] == "partial"
    assert run.etf_fetch_summary["research_success"] == 1
    assert run.etf_fetch_summary["failed"] == 1


def test_pipeline_promotes_real_etf_failure_to_error(tmp_path: Path) -> None:
    with patch(
        "bot.data_pipeline.fetch_all_active_etfs",
        return_value=[_result("2330", success=False, error="fetch_failed: timeout")],
    ):
        run = run_full_pipeline(_config(tmp_path))

    assert any("2330" in error for error in run.errors)
    assert run.etf_fetch_summary["status"] == "failed"
    assert run.etf_fetch_summary["errors"]


def test_pipeline_propagates_macro_cache_and_temporal_warnings(tmp_path: Path) -> None:
    snapshot = MacroSnapshot(
        fetched_at="2026-09-20T10:00:00+08:00",
        asof_date="2026-09-20",
        notes=[
            "TAIFEX live unavailable；使用 exact raw cache (2026-09-18)，已標記 partial",
        ],
        source_dates={"^TWII": "2026-09-20", "TSM:adr": "2026-09-18"},
        source_date_status="mixed",
        futures_basis=FuturesBasis(
            asof_date="2026-09-18",
            quality_status="partial",
            cached=True,
            source_document_id="source-doc:taifex:test",
            note="exact raw cache",
        ),
    )
    config = replace(_config(tmp_path), fetch_etf_holdings=False, fetch_macro=True)

    with patch("bot.market_macro.fetch_macro_snapshot", return_value=snapshot):
        run = run_full_pipeline(config)

    assert run.errors == []
    assert run.macro_summary["status"] == "partial"
    assert run.macro_summary["source_date_status"] == "mixed"
    assert run.macro_summary["futures_basis"]["quality_status"] == "partial"
    assert run.macro_summary["futures_basis"]["cached"] is True
    assert any("exact raw cache" in warning for warning in run.warnings)
    assert any("source_date_status=mixed" in warning for warning in run.warnings)


def test_pipeline_marks_empty_chip_rows_as_warning_not_zero(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        fetch_etf_holdings=False,
        fetch_chips=True,
        focus_tickers=["2330"],
    )
    empty = ChipSummary(ticker="2330", days=5, rows=[])

    with patch("bot.data_pipeline.build_chip_summary", return_value=empty):
        run = run_full_pipeline(config)

    assert run.errors == []
    assert any("沒有可用交易日資料" in warning for warning in run.warnings)
    assert any("不等於籌碼為 0" in warning for warning in run.warnings)
