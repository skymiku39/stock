from __future__ import annotations

import pytest

from bot.scoring_calibration import (
    action_from_score,
    build_calibration_samples,
    evaluate_calibration,
    main,
)


def test_build_samples_uses_future_trading_rows_without_leakage() -> None:
    signals = [{"ticker": "2330", "ts": "2026-01-02T18:00:00", "score": 80}]
    prices = [
        {"symbol": "2330", "date": "2026-01-02", "close": 100},
        {"symbol": "2330", "date": "2026-01-05", "close": 101},
        {"symbol": "2330", "date": "2026-01-06", "close": 104},
        {"symbol": "2330", "date": "2026-01-07", "close": 105},
    ]
    built = build_calibration_samples(signals, prices, horizon_days=2)
    assert len(built.samples) == 1
    sample = built.samples[0]
    assert sample.entry_close == 100
    assert sample.exit_date == "2026-01-06"
    assert sample.forward_return_pct == pytest.approx(4.0)
    assert sample.action == "BUY"
    assert sample.label == "positive"


def test_calibration_report_explicitly_marks_small_sample() -> None:
    signals = [
        {"ticker": "2330", "date": "2026-01-02", "score": 80},
        {"ticker": "2330", "date": "2026-01-05", "score": 20},
    ]
    prices = [
        {"symbol": "2330", "date": "2026-01-02", "close": 100},
        {"symbol": "2330", "date": "2026-01-05", "close": 104},
        {"symbol": "2330", "date": "2026-01-06", "close": 96},
    ]
    built = build_calibration_samples(signals, prices, horizon_days=1)
    report = evaluate_calibration(built, horizon_days=1, minimum_samples=30)
    assert report.status == "insufficient"
    assert report.sample_count == 2
    assert report.directional_accuracy == 1.0
    assert report.notes


def test_action_from_score_is_deterministic() -> None:
    assert action_from_score(80) == "BUY"
    assert action_from_score(50) == "HOLD"
    assert action_from_score(20) == "SELL"


def test_calibration_skips_failed_or_low_quality_llm_rows() -> None:
    built = build_calibration_samples(
        [
            {"ticker": "2330", "success": 0, "score": 80},
            {"ticker": "2330", "quality_status": "unknown", "score": 80},
        ],
        [],
    )
    assert built.samples == []
    assert built.skip_reasons == {"llm_failed": 1, "low_quality": 1}


def test_calibration_cli_resolves_db_and_output_from_root(tmp_path) -> None:
    assert main([
        "--root", str(tmp_path),
        "--minimum-samples", "1",
        "--json", "reports/calibration.json",
    ]) == 0
    assert (tmp_path / "data" / "stock.db").is_file()
    assert (tmp_path / "reports" / "calibration.json").is_file()


def test_calibration_cli_resolves_relative_db_from_root(tmp_path) -> None:
    assert main([
        "--root", str(tmp_path),
        "--db", "data/custom.db",
        "--minimum-samples", "1",
    ]) == 0
    assert (tmp_path / "data" / "custom.db").is_file()
