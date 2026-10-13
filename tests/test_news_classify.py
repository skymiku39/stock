"""classify_news enrichment helpers."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot.news_classify import classify_headline, classify_material_batch


def test_classify_headline_requires_enabled_client() -> None:
    client = MagicMock()
    client.enabled = False
    assert classify_headline(ticker="2330", subject="公告", client=client) is None


def test_classify_headline_parses_json() -> None:
    client = MagicMock()
    client.enabled = True
    raw = '{"category": "營收", "impact": "positive", "magnitude": "medium"}'
    with patch(
        "bot.news_classify.gemini_call",
        return_value=(raw, {"prompt_id": "classify_news", "prompt_version": "1.0"}),
    ):
        out = classify_headline(
            ticker="2330",
            subject="8 月營收創新高",
            client=client,
        )
    assert out is not None
    assert out["category"] == "營收"
    assert out["_prompt"]["prompt_id"] == "classify_news"


def test_classify_material_batch_limits_and_skips_failures() -> None:
    client = MagicMock()
    client.enabled = True
    materials = [
        SimpleNamespace(date=dt.date(2026, 9, 15), subject="A", detail=""),
        SimpleNamespace(date=dt.date(2026, 9, 14), subject="B", detail=""),
        SimpleNamespace(date=dt.date(2026, 9, 13), subject="C", detail=""),
        SimpleNamespace(date=dt.date(2026, 9, 12), subject="D", detail=""),
    ]

    def _fake_call(prompt_id, **kwargs):
        subject = kwargs.get("subject", "")
        if subject == "B":
            return None, {}
        return (
            f'{{"category": "其他", "subject": "{subject}"}}',
            {"prompt_id": "classify_news", "prompt_version": "1.0"},
        )

    with patch("bot.news_classify.gemini_call", side_effect=_fake_call):
        out = classify_material_batch(
            "2330",
            materials,
            client=client,
            limit=3,
        )
    assert len(out) == 2
    assert out[0]["subject"] == "A"
    assert out[1]["subject"] == "C"
    assert out[0]["date"] == "2026-09-15"
