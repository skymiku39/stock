from __future__ import annotations

import json
from pathlib import Path

from bot.pipeline_shared import (
    consensus_tickers_today,
    macro_summary_text,
    parse_json_blob,
)


def test_macro_summary_text_indices() -> None:
    macro = {
        "indices": {"^TWII": {"pct_change": 1.25}},
        "adr_premiums": [],
    }
    text = macro_summary_text(macro)
    assert "加權" in text
    assert "+1.25%" in text


def test_parse_json_blob_strips_fence() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert parse_json_blob(raw) == {"a": 1}


def test_consensus_tickers_today_reads_latest_run(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "data" / "pipeline_runs" / "2026-06-02"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"consensus_top": [{"ticker": "2330"}, {"ticker": "2317"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "bot.pipeline_shared.restore_tree_from_cloud",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "bot.pipeline_shared.restore_file_from_cloud",
        lambda *_a, **_k: None,
    )
    tickers = consensus_tickers_today(tmp_path)
    assert tickers == ["2330", "2317"]
