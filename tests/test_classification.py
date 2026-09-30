from __future__ import annotations

from bot.classification import normalize_theme_payload, normalize_ticker


def test_normalize_ticker_rejects_free_text() -> None:
    assert normalize_ticker(" 2330 ") == "2330"
    assert normalize_ticker("00998a") == "00998A"
    assert normalize_ticker("台積電") == ""


def test_normalize_theme_payload_is_bounded() -> None:
    payload, warnings = normalize_theme_payload({
        "market_tone": "unknown",
        "themes": [{
            "theme": "AI",
            "heat": 99,
            "candidate_tickers": [
                {"ticker": "2330", "role": "龍頭", "reason": "x"},
                {"ticker": "not-a-ticker"},
                {"ticker": "2330", "role": "leader"},
            ],
        }],
    }, known_tickers={"2330"})
    assert payload["market_tone"] == "neutral"
    assert payload["themes"][0]["heat"] == 5
    assert payload["themes"][0]["candidate_tickers"] == [{
        "ticker": "2330",
        "name": "",
        "role": "leader",
        "role_raw": "龍頭",
        "reason": "x",
    }]
    assert warnings
