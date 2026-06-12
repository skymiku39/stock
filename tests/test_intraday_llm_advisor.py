"""intraday_llm_advisor 單元測試。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot.config import Settings
from bot.intraday_llm_advisor import IntradayLlmAdvisor


def test_advisor_skips_when_disabled() -> None:
    settings = Settings(llm_intraday_review_enabled=False)
    advisor = IntradayLlmAdvisor(settings)
    advisor._trigger.set()
    with patch.object(advisor, "_run_cycle") as run_cycle:
        advisor.run_loop(lambda: False)
    run_cycle.assert_not_called()


def test_request_refresh_sets_trigger_when_enabled() -> None:
    settings = Settings(llm_intraday_review_enabled=True)
    advisor = IntradayLlmAdvisor(settings)
    advisor.request_refresh("fill:buy:2330")
    assert advisor._trigger.is_set()
    assert advisor._pending_reason == "fill:buy:2330"


def test_run_cycle_refreshes_symbols_only_without_intraday_report() -> None:
    settings = Settings(
        llm_intraday_review_enabled=True,
        gemini_api_key="test-key",
        symbols=["2330", "0050"],
    )
    risk = MagicMock()
    risk.open_positions_count = 0
    advisor = IntradayLlmAdvisor(settings, risk=risk)
    with patch("bot.auto_llm.auto_analyze_ticker") as refresh:
        with patch(
            "bot.intraday_pipeline.load_intraday_by_date",
            return_value=None,
        ):
            advisor._run_cycle("test")
    assert refresh.call_count == 2


def test_review_interval_shorter_when_flat() -> None:
    risk = MagicMock()
    risk.open_positions_count = 0
    advisor = IntradayLlmAdvisor(
        Settings(
            llm_intraday_review_interval_min=60,
            llm_intraday_review_interval_flat_min=10,
        ),
        risk=risk,
    )
    assert advisor._review_interval_sec() == 600


def test_review_interval_longer_when_holding() -> None:
    risk = MagicMock()
    risk.open_positions_count = 1
    advisor = IntradayLlmAdvisor(
        Settings(
            llm_intraday_review_interval_min=60,
            llm_intraday_review_interval_flat_min=10,
        ),
        risk=risk,
    )
    assert advisor._review_interval_sec() == 3600
