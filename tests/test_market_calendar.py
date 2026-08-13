from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from bot.market_calendar import (
    CATEGORY_LABELS,
    fetch_ex_dividend_events,
    load_exhibition_events,
    load_global_tech_events,
    parse_roc_date,
    parse_tpex_dividend_events,
    parse_twse_dividend_events,
)


def test_parse_roc_date_supports_roc_and_iso_digits() -> None:
    assert parse_roc_date("1150618") == dt.date(2026, 6, 18)
    assert parse_roc_date("115/06/18") == dt.date(2026, 6, 18)
    assert parse_roc_date("20260618") == dt.date(2026, 6, 18)
    assert parse_roc_date("bad") is None


def test_parse_twse_dividend_event() -> None:
    events = parse_twse_dividend_events([
        {
            "Date": "1150618",
            "Code": "2330",
            "Name": "台積電",
            "Exdividend": "息",
            "CashDividend": "16.000000",
            "StockDividendRatio": "",
            "SubscriptionRatio": "",
            "SubscriptionPricePerShare": "",
        }
    ])
    assert len(events) == 1
    event = events[0]
    assert event.date == dt.date(2026, 6, 18)
    assert event.category == "ex_dividend"
    assert event.market == "TWSE"
    assert event.ticker == "2330"
    assert event.asset_type == "stock"
    assert "現金股利 16" in event.note


def test_parse_tpex_dividend_event() -> None:
    events = parse_tpex_dividend_events([
        {
            "ExRrightsExDividendDate": "1150528",
            "SecuritiesCompanyCode": "5529",
            "CompanyName": "鉅陞",
            "ExRrightsExDividend": "除權",
            "StockDividendRatio": "0.03860925",
            "SubscriptionRatioToNewSharesIssued": "0.00000000",
            "SubscriptionPricePerShare": "0.00",
            "CashDividend": "0.00000000",
        }
    ])
    assert len(events) == 1
    event = events[0]
    assert event.date == dt.date(2026, 5, 28)
    assert event.category == "ex_right"
    assert event.market == "TPEX"
    assert event.asset_type == "stock"
    assert "無償配股率 0.0386092" in event.note


def test_fetch_ex_dividend_events_uses_both_sources(tmp_path: Path) -> None:
    class _Resp:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows
            self.encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, Any]]:
            return self.rows

    class _Session:
        def get(self, url: str, timeout: int = 20) -> _Resp:
            if "twse" in url:
                return _Resp([
                    {
                        "Date": "1150616",
                        "Code": "00900",
                        "Name": "富邦特選高股息30",
                        "Exdividend": "息",
                        "CashDividend": "0.1",
                    },
                    {
                        "Date": "1150618",
                        "Code": "2330",
                        "Name": "台積電",
                        "Exdividend": "息",
                        "CashDividend": "16",
                    }
                ])
            return _Resp([
                {
                    "ExRrightsExDividendDate": "1150619",
                    "SecuritiesCompanyCode": "6488",
                    "CompanyName": "環球晶",
                    "ExRrightsExDividend": "除息",
                    "CashDividend": "5.7",
                }
            ])

    events = fetch_ex_dividend_events(
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        root=tmp_path,
        session=_Session(),  # type: ignore[arg-type]
        use_cache=False,
    )
    assert {e.ticker for e in events} == {"2330", "6488"}

    all_events = fetch_ex_dividend_events(
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        root=tmp_path,
        session=_Session(),  # type: ignore[arg-type]
        use_cache=False,
        security_scope="all",
    )
    assert {e.ticker for e in all_events} == {"00900", "2330", "6488"}
    assert next(e for e in all_events if e.ticker == "00900").asset_type == "etf"
    assert (tmp_path / "data" / "calendar" / "ex_dividends_twse.json").exists()
    assert (tmp_path / "data" / "calendar" / "ex_dividends_tpex.json").exists()


def test_load_exhibition_events_creates_editable_seed_file(tmp_path: Path) -> None:
    events = load_exhibition_events(
        dt.date(2026, 5, 1),
        dt.date(2026, 6, 30),
        root=tmp_path,
    )
    titles = {e.title for e in events}
    assert "CYBERSEC 2026 臺灣資安大會" in titles
    assert "COMPUTEX 2026 台北國際電腦展" in titles
    assert "NVIDIA GTC Taipei at COMPUTEX 2026" not in titles
    assert "2026 AI TAIWAN 未來商務展" not in titles

    path = tmp_path / "data" / "calendar" / "exhibitions.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert isinstance(payload["entries"], list)
    assert payload["entries"]


def test_legacy_exhibition_seed_file_is_migrated_to_curated_main_events(tmp_path: Path) -> None:
    path = tmp_path / "data" / "calendar" / "exhibitions.json"
    path.parent.mkdir(parents=True)
    legacy_titles = [
        "AI EXPO Taiwan 2026",
        "Secutech Taiwan 2026",
        "CYBERSEC 2026 臺灣資安大會",
        "NVIDIA GTC Taipei at COMPUTEX 2026",
        "COMPUTEX 2026 台北國際電腦展",
        "2026 AI TAIWAN 未來商務展",
    ]
    path.write_text(
        json.dumps({
            "entries": [
                {
                    "date": "2026-06-02",
                    "end_date": "2026-06-05",
                    "title": title,
                    "location": "台北",
                }
                for title in legacy_titles
            ] + [
                {
                    "date": "2026-10-01",
                    "end_date": "2026-10-02",
                    "title": "Custom Investor Tech Day",
                    "location": "台北",
                }
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    events = load_exhibition_events(
        dt.date(2026, 1, 1),
        dt.date(2026, 12, 31),
        root=tmp_path,
    )
    titles = {e.title for e in events}
    assert "COMPUTEX 2026 台北國際電腦展" in titles
    assert "CYBERSEC 2026 臺灣資安大會" in titles
    assert "NVIDIA GTC Taipei at COMPUTEX 2026" not in titles
    assert "AI EXPO Taiwan 2026" not in titles
    assert "Custom Investor Tech Day" in titles

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    saved_titles = {row["title"] for row in payload["entries"]}
    assert "NVIDIA GTC Taipei at COMPUTEX 2026" not in saved_titles


def test_existing_exhibition_file_is_mirrored_to_cloud(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    cloud = tmp_path / "drive-cache"
    monkeypatch.setenv("GOOGLE_CACHE_DIR", str(cloud))

    path = project / "data" / "calendar" / "exhibitions.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "entries": [
                {
                    "date": "2026-06-02",
                    "end_date": "2026-06-05",
                    "title": "COMPUTEX 2026 台北國際電腦展",
                    "location": "台北南港展覽館",
                    "note": "manual edit",
                }
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    events = load_exhibition_events(
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        root=project,
    )

    assert len(events) == 1
    mirrored = cloud / "data" / "calendar" / "exhibitions.json"
    assert mirrored.exists()
    payload = json.loads(mirrored.read_text(encoding="utf-8"))
    assert payload["entries"][0]["note"] == "manual edit"


def test_load_global_tech_events_from_cache(tmp_path: Path) -> None:
    path = tmp_path / "data" / "calendar" / "global_tech_events.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "entries": [{
                "date": "2026-06-08",
                "end_date": "2026-06-12",
                "title": "WWDC 2026 Keynote",
                "event_type": "keynote",
                "organizer": "AAPL",
                "canonical_key": "wwdc-2026-keynote",
                "enabled": True,
                "tickers": ["2317", "2330"],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    events = load_global_tech_events(
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        root=tmp_path,
    )
    assert len(events) == 1
    assert events[0].category == "global_tech"
    assert events[0].title == "WWDC 2026 Keynote"
    assert "2317" in events[0].tickers
    assert CATEGORY_LABELS["global_tech"] == "全球科技"
