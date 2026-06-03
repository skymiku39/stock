from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import bot.next_day_watch_pipeline as pipeline


def test_run_next_day_watch_scans_llm_discovered_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "now_tw",
        lambda: dt.datetime(2026, 6, 2, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
    )
    monkeypatch.setattr(pipeline, "fetch_today_news", lambda **_kwargs: [])
    monkeypatch.setattr(pipeline, "news_to_compact_text", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(pipeline, "fetch_macro_snapshot", lambda **_kwargs: object())
    monkeypatch.setattr(pipeline, "macro_to_dict", lambda _snap: {"adr_premiums": []})
    monkeypatch.setattr(pipeline, "_macro_summary_text", lambda _macro: "macro")
    monkeypatch.setattr(pipeline, "_tomorrow_event_tickers", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(pipeline, "_watchlist_tickers", lambda _root: [])
    monkeypatch.setattr(pipeline, "_consensus_tickers_today", lambda _root: [])
    monkeypatch.setattr(
        "bot.conference_calendar.ensure_calendar_fresh",
        lambda *_args, **_kwargs: None,
    )

    scan_calls: list[list[str]] = []

    def fake_scan(tickers, **_kwargs):
        pool = list(tickers)
        scan_calls.append(pool)
        if "2301" not in pool:
            return {}
        return {
            "2301": {
                "close": 120.0,
                "pct_1d": 4.2,
                "vol_ratio": 2.1,
                "strength_score": 82.0,
                "foreign_net": 1300.0,
                "investment_trust_net": 200.0,
            }
        }

    monkeypatch.setattr(pipeline, "_scan_today_strength", fake_scan)

    class FakeGeminiClient:
        enabled = True

        def __init__(self, **_kwargs) -> None:
            pass

    monkeypatch.setattr(pipeline, "GeminiClient", FakeGeminiClient)

    def fake_gemini_call(prompt_id: str, **_kwargs):
        if prompt_id == "next_day_radar":
            return (
                json.dumps({
                    "market_tone": "risk_on",
                    "overall_brief": "brief",
                    "carry_themes": [{
                        "theme": "AI伺服器",
                        "heat": 5,
                        "candidate_tickers": [{
                            "ticker": "2301",
                            "name": "光寶科",
                            "entry_logic": "強勢承接",
                        }],
                    }],
                    "event_focus": [],
                    "strong_carry": [],
                }),
                {},
            )
        return ("brief md", {"prompt_id": prompt_id, "prompt_version": "test"})

    monkeypatch.setattr(pipeline, "gemini_call", fake_gemini_call)

    report = pipeline.run_next_day_watch(
        project_root=tmp_path,
        settings=SimpleNamespace(gemini_api_key="key", gemini_model="model"),
        force_refresh_technicals=False,
    )

    assert scan_calls == [[], ["2301"]]
    assert report.rankings[0].ticker == "2301"
    assert report.rankings[0].strength_score == 82.0
    assert report.rankings[0].today_pct_change == 4.2


def test_run_next_day_watch_mirrors_report_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        pipeline,
        "now_tw",
        lambda: dt.datetime(2026, 6, 2, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
    )
    monkeypatch.setattr(pipeline, "fetch_today_news", lambda **_kwargs: [])
    monkeypatch.setattr(pipeline, "news_to_compact_text", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(pipeline, "fetch_macro_snapshot", lambda **_kwargs: object())
    monkeypatch.setattr(pipeline, "macro_to_dict", lambda _snap: {"adr_premiums": []})
    monkeypatch.setattr(pipeline, "_macro_summary_text", lambda _macro: "macro")
    monkeypatch.setattr(pipeline, "_tomorrow_event_tickers", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(pipeline, "_watchlist_tickers", lambda _root: [])
    monkeypatch.setattr(pipeline, "_consensus_tickers_today", lambda _root: [])
    monkeypatch.setattr(pipeline, "_scan_today_strength", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        "bot.conference_calendar.ensure_calendar_fresh",
        lambda *_args, **_kwargs: None,
    )

    class FakeGeminiClient:
        enabled = False

        def __init__(self, **_kwargs) -> None:
            pass

    monkeypatch.setattr(pipeline, "GeminiClient", FakeGeminiClient)
    mirrored: list = []
    monkeypatch.setattr(
        pipeline,
        "mirror_file_to_cloud",
        lambda path, *, root=None: mirrored.append(Path(path)) or True,
    )

    pipeline.run_next_day_watch(
        project_root=tmp_path,
        settings=SimpleNamespace(gemini_api_key="", gemini_model="model"),
        force_refresh_technicals=False,
    )

    assert any(p.name == "report.json" for p in mirrored)
