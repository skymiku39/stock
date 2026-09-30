from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import bot.intraday_pipeline as pipeline
from bot.news_fetcher import NewsItem
from bot.source_registry import SourceRegistry


def test_score_candidate_uses_technical_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "bot.chips_fetcher.build_chip_summary",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "bot.chips_fetcher.summary_to_dict",
        lambda _summary: {},
    )
    monkeypatch.setattr(
        pipeline,
        "related_us_stocks_for_tw",
        lambda *_args, **_kwargs: [],
    )

    tech_snapshot = SimpleNamespace(
        has_data=True,
        rows=80,
        last_date="2026-06-02",
        last_close=120.5,
        pct_change_1d=4.25,
        volume_last=24000.0,
        vol_ma20=12000.0,
        technical_score=76.0,
    )
    tech_dict = {
        "rows": 80,
        "last_date": "2026-06-02",
        "last_close": 120.5,
        "pct_change_1d": 4.25,
        "volume_last": 24000.0,
        "vol_ma20": 12000.0,
        "technical_score": 76.0,
    }
    monkeypatch.setattr(
        "bot.technicals.build_technical_snapshot",
        lambda *_args, **_kwargs: (tech_snapshot, None),
    )
    monkeypatch.setattr(
        "bot.technicals.snapshot_to_dict",
        lambda _snap: tech_dict,
    )

    captured: dict[str, object] = {}

    def fake_scorecard(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            timeframes={
                "day_trade": SimpleNamespace(
                    total=88.8,
                    action="BUY",
                    factors=[SimpleNamespace(key="us_market", score=55.0)],
                )
            }
        )

    monkeypatch.setattr(pipeline, "compute_scorecard", fake_scorecard)

    row = pipeline.CandidateRow(ticker="2301", name="光寶科")
    pipeline._score_candidate(
        row,
        macro={},
        supply_chain={},
        project_root=tmp_path,
        today=dt.date(2026, 6, 2),
        refresh_technicals=False,
        log=SimpleNamespace(debug=lambda *_args, **_kwargs: None),
    )

    assert captured["price"] == 120.5
    assert captured["pct_change"] == 4.25
    assert captured["volume"] == 24000.0
    assert captured["technical_snapshot"] == tech_dict
    assert row.today_close == 120.5
    assert row.today_pct_change == 4.25
    assert row.volume_ratio == 2.0
    assert row.technical_score == 76.0
    assert row.day_trade_score == 88.8


def test_run_intraday_persists_explicit_report_evidence(tmp_path: Path, monkeypatch) -> None:
    with SourceRegistry(tmp_path) as registry:
        news_doc = registry.capture_document(
            "cnyes", b'{"news": "exact"}', source_ref="news-1",
            source_url="https://news.example/1", as_of="2026-06-02",
        )
        macro_doc = registry.capture_document(
            "market_macro", b'{"macro": "exact"}', source_ref="macro:2026-06-02",
            source_url="yfinance://macro", as_of="2026-06-02",
        )

    monkeypatch.setattr(
        pipeline,
        "now_tw",
        lambda: dt.datetime(2026, 6, 2, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_today_news",
        lambda **_kwargs: [NewsItem(
            news_id="news-1", title="台股新聞", summary="新聞摘要",
            source_document_id=news_doc.document_id,
        )],
    )
    monkeypatch.setattr(pipeline, "news_to_compact_text", lambda *_args, **_kwargs: "新聞摘要")
    monkeypatch.setattr(pipeline, "fetch_macro_snapshot", lambda **_kwargs: object())
    monkeypatch.setattr(
        pipeline,
        "macro_to_dict",
        lambda _snap: {
            "asof_date": "2026-06-02",
            "fetched_at": "2026-06-02T08:00:00+08:00",
            "indices": {"^TWII": {"pct_change": 1.0}},
            "source_document_id": macro_doc.document_id,
        },
    )
    monkeypatch.setattr(pipeline, "_consensus_tickers_today", lambda _root: [])
    monkeypatch.setattr(pipeline, "_watchlist_tickers", lambda _root: [])
    monkeypatch.setattr(pipeline, "load_supply_chain", lambda _root: {})

    class FakeGeminiClient:
        enabled = False

        def __init__(self, **_kwargs) -> None:
            pass

    monkeypatch.setattr(pipeline, "GeminiClient", FakeGeminiClient)

    report = pipeline.run_intraday(
        project_root=tmp_path,
        settings=SimpleNamespace(gemini_api_key="", gemini_model="model"),
        force_refresh_technicals=False,
    )

    assert len(report.evidence) == 2
    assert {item["provenance_status"] for item in report.evidence} == {"exact"}
    payload = json.loads((Path(report.output_dir) / "report.json").read_text(encoding="utf-8"))
    assert len(payload["evidence"]) == 2
