from __future__ import annotations

import datetime as dt
from pathlib import Path

from bot.global_events_fetcher import (
    MEGA_TECH_ORGANIZERS,
    canonical_from_title,
    merge_global_event_rows,
    mega_tech_organizers_in_supply_chain,
    tickers_for_organizer,
    _default_global_tech_seeds,
)


def test_tickers_for_organizer_from_supply_chain(tmp_path: Path) -> None:
    sc_path = tmp_path / "data" / "supply_chain.json"
    sc_path.parent.mkdir(parents=True)
    sc_path.write_text(
        """{
  "us_stocks": {
    "AAPL": {
      "tw_supply_chain": [
        {"tw_ticker": "2317", "weight": 1.0},
        {"tw_ticker": "3035", "weight": 0.3}
      ]
    }
  }
}""",
        encoding="utf-8",
    )
    tickers = tickers_for_organizer("AAPL", root=tmp_path, min_weight=0.5)
    assert tickers == ["2317"]


def test_merge_global_event_rows_prefers_official_source() -> None:
    official = {
        "date": "2026-06-08",
        "end_date": "2026-06-12",
        "title": "WWDC 2026 Keynote",
        "canonical_key": "wwdc-2026-keynote",
        "source_quality": "official",
        "note": "官方",
        "enabled": True,
        "tickers": ["2317"],
    }
    news = {
        "date": "2026-06-08",
        "end_date": "2026-06-08",
        "title": "Apple unveils Siri AI at WWDC",
        "canonical_key": "wwdc-2026-keynote",
        "source_quality": "news",
        "note": "新聞補充",
        "enabled": True,
        "tickers": ["2317"],
    }
    merged = merge_global_event_rows([official], [news])
    assert len(merged) == 1
    assert merged[0]["source_quality"] == "official"
    assert merged[0]["note"] == "官方"
    assert "新聞補充" not in merged[0]["note"]


def test_canonical_from_title_wwdc() -> None:
    day = dt.date(2026, 6, 8)
    assert canonical_from_title("WWDC 2026 live blog", day) == "wwdc-2026-keynote"


def test_mega_tech_seeds_cover_all_organizers(tmp_path: Path) -> None:
    sc_path = tmp_path / "data" / "supply_chain.json"
    sc_path.parent.mkdir(parents=True)
    orgs = {f"ORG{i}": {} for i in range(3)}
    for code in MEGA_TECH_ORGANIZERS:
        orgs[code] = {"tw_supply_chain": [{"tw_ticker": "2330", "weight": 1.0}]}
    sc_path.write_text(
        '{"us_stocks": ' + __import__("json").dumps(orgs) + "}",
        encoding="utf-8",
    )
    seeds = _default_global_tech_seeds()
    seed_orgs = {s.organizer for s in seeds if s.organizer}
    assert MEGA_TECH_ORGANIZERS <= seed_orgs
    assert "SAMSUNG" in mega_tech_organizers_in_supply_chain(root=tmp_path)


def test_canonical_from_title_microsoft_google_samsung() -> None:
    assert canonical_from_title("Microsoft Build 2026", dt.date(2026, 5, 19)) == "microsoft-build-2026"
    assert canonical_from_title("Google I/O 2026", dt.date(2026, 5, 20)) == "google-io-2026"
    assert canonical_from_title("Samsung Galaxy Unpacked", dt.date(2026, 7, 9)) == "samsung-unpacked-summer-2026"


def test_merge_does_not_pollute_official_seed_with_news() -> None:
    official = {
        "date": "2026-06-08",
        "end_date": "2026-06-12",
        "title": "WWDC 2026 Keynote",
        "canonical_key": "wwdc-2026-keynote",
        "source_quality": "official",
        "note": "Apple Keynote 官方說明。",
        "enabled": True,
    }
    news = {
        "date": "2026-06-08",
        "end_date": "2026-06-08",
        "title": "WWDC 2026 懶人包",
        "canonical_key": "wwdc-2026-keynote",
        "source_quality": "news",
        "note": "很長的新聞標題 " * 20,
        "enabled": True,
    }
    merged = merge_global_event_rows([official], [news])
    assert len(merged) == 1
    assert merged[0]["note"] == "Apple Keynote 官方說明。"
    assert len(merged[0]["note"]) < 100
