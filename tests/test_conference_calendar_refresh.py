"""Tests for calendar URL refresh gate and resolve_llm_bundle behavior."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.conference_calendar import (
    calendar_needs_url_refresh,
    ensure_calendar_fresh,
    presentation_url_fill_rate,
)


def _write_month(root: Path, year: int, month: int, entries: list[dict]) -> None:
    p = root / "data" / "calendar" / f"conferences_{year:04d}-{month:02d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_calendar_needs_url_refresh_when_old_cache_has_no_urls(tmp_path: Path):
    import datetime as dt
    today = dt.date.today()
    _write_month(tmp_path, today.year, today.month, [
        {
            "date": today.isoformat(),
            "time": "10:00",
            "ticker": "2330",
            "company": "台積電",
            "note": "",
            "presentation_url": "",
        },
    ])
    assert calendar_needs_url_refresh(tmp_path) is True
    filled, total, rate = presentation_url_fill_rate(tmp_path)
    assert filled == 0 and total == 1


def test_ensure_calendar_fresh_forces_refresh_on_low_url_rate(tmp_path: Path):
    import datetime as dt
    today = dt.date.today()
    _write_month(tmp_path, today.year, today.month, [
        {
            "date": today.isoformat(),
            "time": "10:00",
            "ticker": "2330",
            "company": "台積電",
            "note": "",
            "presentation_url": "",
        },
    ])
    state = tmp_path / "data" / "calendar" / "calendar_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"last_full_refresh_at": "2099-01-01T00:00:00"}),
        encoding="utf-8",
    )
    with patch("bot.conference_calendar.update_calendar") as mock_update:
        mock_update.return_value = {"2099-01": 1}
        refreshed = ensure_calendar_fresh(tmp_path)
    assert refreshed is True
    mock_update.assert_called_once()


def test_resolve_llm_bundle_skips_auto_analyze_when_pipeline_exists(tmp_path: Path):
    from bot.auto_llm import resolve_llm_bundle

    pipeline = {"ticker": "2330", "summary": "from pipeline", "source": "pipeline"}
    cache_dir = tmp_path / "data" / "auto_llm"
    cache_dir.mkdir(parents=True)
    (cache_dir / "2330.json").write_text(
        json.dumps({
            "ticker": "2330",
            "fetched_at": "2099-01-01T00:00:00",
            "source_sections": {"calendar_upcoming": "(無)", "mops_materials": []},
        }),
        encoding="utf-8",
    )
    live_sections = {"calendar_upcoming": "- live", "_source": "live", "mops_materials": []}
    with patch("bot.auto_llm.auto_analyze_ticker") as mock_analyze, patch(
        "bot.auto_llm.build_live_source_sections", return_value=live_sections,
    ):
        bundle = resolve_llm_bundle(
            "2330",
            tmp_path,
            pipeline_analysis=pipeline,
            auto_llm=True,
        )
    mock_analyze.assert_not_called()
    assert bundle["analysis"] == pipeline
    assert bundle["source_sections"]["_source"] == "live"


def test_source_sections_has_content_rejects_empty_shell():
    from bot.auto_llm import _source_sections_has_content

    assert _source_sections_has_content({
        "calendar_upcoming": "(無)",
        "mops_materials": [],
    }) is False
    assert _source_sections_has_content({
        "calendar_upcoming": "- 2026-06-10 2330",
    }) is True


def test_resolve_llm_bundle_uses_live_sections_without_cache(tmp_path: Path):
    from bot.auto_llm import resolve_llm_bundle

    pipeline = {"ticker": "2330", "summary": "from pipeline"}
    live_sections = {
        "calendar_upcoming": "- 2026-06-10 2330",
        "mops_materials": [],
        "_source": "live",
    }
    with patch("bot.auto_llm.auto_analyze_ticker") as mock_analyze, patch(
        "bot.auto_llm.build_live_source_sections", return_value=live_sections,
    ):
        bundle = resolve_llm_bundle(
            "2330",
            tmp_path,
            pipeline_analysis=pipeline,
            auto_llm=True,
        )
    mock_analyze.assert_not_called()
    assert bundle["source_sections"] == live_sections


def test_patch_material_detail_in_cache(tmp_path: Path):
    from bot.auto_llm import patch_material_detail_in_cache

    cache_dir = tmp_path / "data" / "auto_llm"
    cache_dir.mkdir(parents=True)
    url = "https://mops.twse.com.tw/mops/web/t05st10?seq=1"
    (cache_dir / "2330.json").write_text(
        json.dumps({
            "ticker": "2330",
            "fetched_at": "2099-01-01T00:00:00",
            "source_sections": {
                "mops_materials": [
                    {"date": "2026-06-01", "subject": "test", "detail_url": url},
                ],
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    ok = patch_material_detail_in_cache(
        "2330", tmp_path, detail_url=url, text="full body text",
    )
    assert ok is True
    data = json.loads((cache_dir / "2330.json").read_text(encoding="utf-8"))
    assert data["source_sections"]["mops_materials"][0]["detail"] == "full body text"
