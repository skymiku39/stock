from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import bot.intraday_pipeline as pipeline


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
