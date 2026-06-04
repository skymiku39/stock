from __future__ import annotations

from bot.intraday_live import (
    assess_tracking_status,
    build_live_tracking_rows,
    extract_llm_mentions,
)
from bot.utils import now_tw


def test_extract_llm_mentions_keeps_rankings_before_theme_only() -> None:
    report = {
        "rankings": [
            {
                "ticker": "2330",
                "name": "台積電",
                "theme": "AI",
                "day_trade_score": 82,
                "technical_score": 66,
            }
        ],
        "themes": [
            {
                "theme": "CoWoS",
                "heat": 5,
                "candidate_tickers": [
                    {"ticker": "2330", "name": "台積電", "role": "leader"},
                    {"ticker": "3661", "name": "世芯-KY", "role": "design"},
                ],
            }
        ],
    }

    rows = extract_llm_mentions(report, max_tickers=5)

    assert [r["ticker"] for r in rows] == ["2330", "3661"]
    assert rows[0]["initial_day_trade_score"] == 82
    assert "ranking" in rows[0]["sources"]
    assert "theme" in rows[0]["sources"]
    assert rows[1]["theme"] == "CoWoS"


def test_assess_tracking_status_flags_valid_and_broken_theses() -> None:
    valid = assess_tracking_status({
        "has_current_data": True,
        "initial_technical_score": 60,
        "current_technical_score": 68,
        "initial_pct_change": 1.2,
        "current_pct_change": 1.0,
    })
    broken = assess_tracking_status({
        "has_current_data": True,
        "initial_technical_score": 66,
        "current_technical_score": 38,
        "initial_pct_change": 2.1,
        "current_pct_change": -2.4,
    })

    assert valid["correctness"] == "暫時驗證"
    assert broken["correctness"] == "需要修正"


def test_build_live_tracking_rows_uses_live_quote_without_technical_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    today = now_tw().date().isoformat()

    class FakeTwseSource:
        def __init__(self, symbols, logger=None):
            self.symbols = symbols

        def get_quotes(self):
            return {
                "2330": {
                    "price": 100.5,
                    "pct_chg": 1.52,
                    "volume": 12345,
                    "quote_date": today,
                    "quote_time": "10:05:30",
                    "source": "twse_mis",
                    "fetched_at": now_tw().isoformat(timespec="seconds"),
                }
            }

    monkeypatch.setattr("bot.market_source.TwsePublicMarketSource", FakeTwseSource)
    report = {
        "rankings": [
            {
                "ticker": "2330",
                "name": "台積電",
                "theme": "AI",
                "day_trade_score": 82,
                "technical_score": 66,
                "today_pct_change": 0.4,
                "volume_ratio": 1.8,
            }
        ]
    }

    tracking = build_live_tracking_rows(
        report,
        root=tmp_path,
        max_tickers=1,
        refresh_quotes=True,
        refresh_technicals=False,
        include_news=False,
    )

    row = tracking["rows"][0]
    assert row["current_close"] == 100.5
    assert row["current_pct_change"] == 1.52
    assert row["quote_volume"] == 12345
    assert row["quote_date"] == today
    assert row["current_volume_ratio"] is None
    assert row["technical_source"] == "morning_report"
    assert row["correctness"] == "暫時驗證"


def test_assess_tracking_status_flags_non_today_quotes() -> None:
    result = assess_tracking_status({
        "has_current_data": True,
        "quote_date": "1999-01-01",
        "initial_technical_score": 66,
        "current_technical_score": 66,
        "initial_pct_change": 1.0,
        "current_pct_change": 1.2,
    })

    assert result["status"] == "非今日資料"
    assert result["correctness"] == "無法檢討"
