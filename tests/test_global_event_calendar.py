from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch

from bot.global_event_calendar import (
    global_events_for_ticker,
    load_global_events,
    recent_global_events,
    upcoming_global_events,
    update_global_events,
)


def _write_global_events(tmp_path: Path, entries: list) -> None:
    path = tmp_path / "data" / "calendar" / "global_tech_events.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_load_and_query_global_events(tmp_path: Path) -> None:
    _write_global_events(tmp_path, [
        {
            "date": "2026-06-08",
            "end_date": "2026-06-12",
            "title": "WWDC 2026 Keynote",
            "event_type": "keynote",
            "organizer": "AAPL",
            "canonical_key": "wwdc-2026-keynote",
            "enabled": True,
            "tickers": ["2317", "2330"],
        },
        {
            "date": "2026-07-01",
            "end_date": "2026-07-01",
            "title": "Future Event",
            "canonical_key": "future",
            "enabled": True,
            "tickers": ["2454"],
        },
    ])
    events = load_global_events(tmp_path)
    assert len(events) == 2
    assert events[0].title == "WWDC 2026 Keynote"

    today = dt.date(2026, 6, 9)
    with patch("bot.global_event_calendar.now_tw") as mock_now:
        mock_now.return_value = dt.datetime(2026, 6, 9, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        recent = recent_global_events(days=2, root=tmp_path)
        assert any(e.canonical_key == "wwdc-2026-keynote" for e in recent)
        upcoming = upcoming_global_events(days=30, root=tmp_path)
        assert any(e.canonical_key == "future" for e in upcoming)

    with patch("bot.global_event_calendar.now_tw") as mock_now:
        mock_now.return_value = dt.datetime(2026, 6, 9, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        result = global_events_for_ticker("2317", lookahead=60, lookback=2, root=tmp_path)
    assert any(e.canonical_key == "wwdc-2026-keynote" for e in result["recent"])


def test_update_global_events_writes_cache(tmp_path: Path) -> None:
    with patch("bot.global_event_calendar.fetch_all_global_events") as mock_fetch:
        mock_fetch.return_value = [{
            "date": "2026-06-08",
            "end_date": "2026-06-12",
            "title": "WWDC 2026 Keynote",
            "canonical_key": "wwdc-2026-keynote",
            "enabled": True,
            "tickers": ["2317"],
        }]
        summary = update_global_events(root=tmp_path, include_network=False)
    assert summary["count"] == 1
    assert (tmp_path / "data" / "calendar" / "global_tech_events.json").exists()
