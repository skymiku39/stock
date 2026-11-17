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
    # 觀測日混合只需註明；使用快取的台指期仍代表沒有拿到即時資料。
    from bot.data_pipeline import blocking_warnings
    blocking = blocking_warnings(run)
    assert "macro: source_date_status=mixed" not in blocking
    assert any("exact raw cache" in warning for warning in blocking)


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


def test_pipeline_skips_foreign_holdings_for_chips(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        fetch_etf_holdings=False,
        fetch_chips=True,
        focus_tickers=["2330", "NVDA US", "8306 JT"],
    )
    called: list[str] = []

    def summary(ticker, **kwargs):
        called.append(ticker)
        return ChipSummary(ticker=ticker, days=5, rows=[], market_days=0)

    with patch("bot.data_pipeline.build_chip_summary", side_effect=summary):
        run = run_full_pipeline(config)

    assert called == ["2330"]
    assert run.chip_skipped_foreign == 2
    # 全市場在回顧期間都沒有資料（例如連假），不算個股缺資料。
    assert not any("沒有可用交易日資料" in warning for warning in run.warnings)


def test_pipeline_partial_chips_compare_with_market_days(tmp_path: Path) -> None:
    from bot.chips_fetcher import ChipDailyRow
    from bot.data_pipeline import blocking_warnings

    config = replace(
        _config(tmp_path),
        fetch_etf_holdings=False,
        fetch_chips=True,
        focus_tickers=["2330", "1101"],
    )
    rows = [ChipDailyRow(ticker="x") for _ in range(4)]

    def summary(ticker, **kwargs):
        # 當天籌碼盤後才公布：5 個平日中全市場只有 4 天有資料。
        market_days = 4 if ticker == "2330" else 5
        return ChipSummary(ticker=ticker, days=5, rows=list(rows), market_days=market_days)

    with patch("bot.data_pipeline.build_chip_summary", side_effect=summary):
        run = run_full_pipeline(config)

    partial = [w for w in run.warnings if "只有部分回顧日資料" in w]
    assert len(partial) == 1 and "1101" in partial[0] and "2330" not in partial[0]
    # 部分資料只需註明限制，不代表收集失敗。
    assert partial[0] in run.advisory_warnings
    assert partial[0] not in blocking_warnings(run)


def test_persist_run_replaces_non_finite_values(tmp_path: Path) -> None:
    import json
    import logging
    from bot.data_pipeline import PipelineRun, _persist_run

    run = PipelineRun(run_id="r", started_at="2026-11-17T10:00:00+08:00")
    run.macro_summary = {"top_movers": [{"symbol": "^TWII", "pct_change": float("nan")}]}
    with patch("bot.data_pipeline.mirror_file_to_cloud"):
        _persist_run(run, tmp_path, logging.getLogger("test"))
    saved = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert saved["macro_summary"]["top_movers"][0]["pct_change"] is None
    assert any("非有限數值" in w and "top_movers[0].pct_change" in w for w in saved["warnings"])


def _price_calendar(root: Path, dates: list[str]) -> None:
    import sqlite3
    (root / "data").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "data/stock.db")
    conn.execute("CREATE TABLE price_history (symbol TEXT, date TEXT)")
    conn.executemany("INSERT INTO price_history VALUES ('2330', ?)", [(d,) for d in dates])
    conn.commit()
    conn.close()


def test_pipeline_blocks_when_chip_source_is_down_on_trading_days(tmp_path: Path) -> None:
    from bot.data_pipeline import blocking_warnings

    _price_calendar(tmp_path, ["2020-01-02", "2020-01-03"])
    config = replace(_config(tmp_path), fetch_etf_holdings=False, fetch_chips=True,
                     focus_tickers=["2330"])
    # 籌碼端點全斷：全市場每天都沒資料，market_days=0，個股不會被列為缺資料。
    down = ChipSummary(ticker="2330", days=5, rows=[], market_days=0,
                       market_missing_dates=["2020-01-02", "2020-01-03", "2020-01-06"])
    with patch("bot.data_pipeline.build_chip_summary", return_value=down):
        run = run_full_pipeline(config)

    outage = [w for w in run.warnings if "來源可能中斷" in w]
    assert len(outage) == 1 and "2 個交易日" in outage[0]       # 01-06 不是交易日（日 K 沒有）
    assert outage[0] in blocking_warnings(run)


def test_pipeline_ignores_market_gaps_on_holidays(tmp_path: Path) -> None:
    _price_calendar(tmp_path, ["2020-01-02"])
    config = replace(_config(tmp_path), fetch_etf_holdings=False, fetch_chips=True,
                     focus_tickers=["2330"])
    holiday = ChipSummary(ticker="2330", days=5, rows=[], market_days=0,
                          market_missing_dates=["2020-01-20", "2020-01-21"])
    with patch("bot.data_pipeline.build_chip_summary", return_value=holiday):
        run = run_full_pipeline(config)
    assert not any("來源可能中斷" in w for w in run.warnings)
