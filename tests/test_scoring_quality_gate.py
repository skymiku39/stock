from __future__ import annotations

from bot.scoring import compute_scorecard, factor_chips, factor_fundamental


def test_low_factor_coverage_blocks_buy_action() -> None:
    scorecard = compute_scorecard(
        ticker="2330",
        price=100.0,
        pct_change=5.0,
    )
    day = scorecard.timeframes["day_trade"]
    assert day.raw_action in {"BUY", "STRONG_BUY"}
    assert day.action == "HOLD"
    assert day.decision_status == "blocked_insufficient_data"
    assert any("覆蓋率" in note for note in day.notes)


def test_empty_fundamental_snapshot_is_unavailable() -> None:
    factor = factor_fundamental({"revenues": [], "quarterlies": [], "valuation": None})
    assert factor.available is False


def test_empty_chip_summary_is_unavailable() -> None:
    factor = factor_chips({"ticker": "2330", "days": 5, "rows": []})
    assert factor.available is False
