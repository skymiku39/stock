"""Unified Taiwan market calendar events.

This module combines three catalyst sources into one event model:

* MOPS investor conferences from ``conference_calendar``.
* TWSE/TPEx ex-right and ex-dividend announcements.
* A small editable exhibition seed file under ``data/calendar``.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from bot.cloud_file_cache import mirror_file_to_cloud, read_json_cache, write_json_cache
from bot.utils import get_logger, now_tw


CALENDAR_DIR_REL = "data/calendar"
EXHIBITIONS_FILE = "exhibitions.json"
EXHIBITIONS_SCHEMA_VERSION = 2
TWSE_EX_DIVIDEND_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"
TPEX_EX_DIVIDEND_URL = "https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost"
EX_DIVIDEND_CACHE_TTL = 6 * 3600

CATEGORY_LABELS = {
    "conference": "法說會",
    "ex_dividend": "除息",
    "ex_right": "除權",
    "ex_right_dividend": "除權息",
    "exhibition": "國際展覽",
    "global_tech": "全球科技",
}


@dataclass(frozen=True)
class MarketCalendarEvent:
    date: dt.date
    title: str
    category: str
    end_date: Optional[dt.date] = None
    time: str = ""
    ticker: str = ""
    company: str = ""
    market: str = ""
    asset_type: str = ""
    note: str = ""
    source: str = ""
    source_quality: str = ""
    url: str = ""
    tickers: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    @property
    def effective_end_date(self) -> dt.date:
        return self.end_date or self.date

    def occurs_on(self, day: dt.date) -> bool:
        return self.date <= day <= self.effective_end_date

    def overlaps(self, start: dt.date, end: dt.date) -> bool:
        return self.date <= end and self.effective_end_date >= start

    def search_blob(self) -> str:
        parts = [
            self.title,
            self.category_label,
            self.ticker,
            self.company,
            self.market,
            self.note,
            " ".join(self.tickers),
        ]
        return " ".join(p for p in parts if p).lower()


def build_market_calendar(
    start: dt.date,
    end: dt.date,
    *,
    root: Optional[Path] = None,
    include_conferences: bool = True,
    include_dividends: bool = True,
    include_exhibitions: bool = True,
    include_global_tech: bool = True,
    dividend_security_scope: str = "stock",
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> List[MarketCalendarEvent]:
    log = logger or get_logger("market_calendar")
    events: List[MarketCalendarEvent] = []
    if include_conferences:
        events.extend(load_conference_events(start, end, root=root))
    if include_dividends:
        events.extend(fetch_ex_dividend_events(
            start,
            end,
            root=root,
            session=session,
            logger=log,
            security_scope=dividend_security_scope,
        ))
    if include_exhibitions:
        events.extend(load_exhibition_events(start, end, root=root))
    if include_global_tech:
        events.extend(load_global_tech_events(start, end, root=root))
    return sort_events(unique_events(events))


def load_conference_events(
    start: dt.date,
    end: dt.date,
    *,
    root: Optional[Path] = None,
) -> List[MarketCalendarEvent]:
    from bot.conference_calendar import load_calendar

    out: List[MarketCalendarEvent] = []
    for e in load_calendar(root=root):
        if not (start <= e.date <= end):
            continue
        title_parts = [e.ticker, e.company, "法說會"]
        out.append(MarketCalendarEvent(
            date=e.date,
            time=e.time or "",
            title=" ".join(p for p in title_parts if p),
            category="conference",
            ticker=e.ticker or "",
            company=e.company or "",
            note=e.note or "",
            source="MOPS 法說會行事曆",
            url=e.presentation_url or "",
            tickers=(e.ticker,) if e.ticker else (),
        ))
    return out


def fetch_ex_dividend_events(
    start: dt.date,
    end: dt.date,
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    use_cache: bool = True,
    cache_ttl: int = EX_DIVIDEND_CACHE_TTL,
    security_scope: str = "stock",
    logger: Optional[logging.Logger] = None,
) -> List[MarketCalendarEvent]:
    log = logger or get_logger("market_calendar")
    sess = session or requests.Session()
    twse_rows = _load_api_rows(
        TWSE_EX_DIVIDEND_URL,
        "ex_dividends_twse.json",
        root=root,
        session=sess,
        use_cache=use_cache,
        cache_ttl=cache_ttl,
        logger=log,
    )
    tpex_rows = _load_api_rows(
        TPEX_EX_DIVIDEND_URL,
        "ex_dividends_tpex.json",
        root=root,
        session=sess,
        use_cache=use_cache,
        cache_ttl=cache_ttl,
        logger=log,
    )
    events = parse_twse_dividend_events(twse_rows) + parse_tpex_dividend_events(tpex_rows)
    return [
        e for e in events
        if e.overlaps(start, end) and _include_dividend_security(e, security_scope)
    ]


def parse_twse_dividend_events(rows: Iterable[Dict[str, Any]]) -> List[MarketCalendarEvent]:
    out: List[MarketCalendarEvent] = []
    for row in rows:
        day = parse_roc_date(row.get("Date"))
        if not day:
            continue
        ticker = str(row.get("Code") or "").strip()
        name = str(row.get("Name") or "").strip()
        kind = _normalize_ex_kind(row.get("Exdividend"))
        note = _dividend_note(
            cash=row.get("CashDividend"),
            stock=row.get("StockDividendRatio"),
            subscription=row.get("SubscriptionRatio"),
            subscription_price=row.get("SubscriptionPricePerShare"),
        )
        out.append(MarketCalendarEvent(
            date=day,
            title=_event_title(ticker, name, kind),
            category=_category_for_ex_kind(kind),
            ticker=ticker,
            company=name,
            market="TWSE",
            asset_type=_security_type(ticker, name),
            note=note,
            source="TWSE 上市股票除權除息預告表",
            source_quality="official_api",
            url=TWSE_EX_DIVIDEND_URL,
            tickers=(ticker,) if ticker else (),
        ))
    return out


def parse_tpex_dividend_events(rows: Iterable[Dict[str, Any]]) -> List[MarketCalendarEvent]:
    out: List[MarketCalendarEvent] = []
    for row in rows:
        day = parse_roc_date(row.get("ExRrightsExDividendDate"))
        if not day:
            continue
        ticker = str(row.get("SecuritiesCompanyCode") or "").strip()
        name = str(row.get("CompanyName") or "").strip()
        kind = _normalize_ex_kind(row.get("ExRrightsExDividend"))
        note = _dividend_note(
            cash=row.get("CashDividend"),
            stock=row.get("StockDividendRatio"),
            subscription=row.get("SubscriptionRatioToNewSharesIssued"),
            subscription_price=row.get("SubscriptionPricePerShare"),
        )
        out.append(MarketCalendarEvent(
            date=day,
            title=_event_title(ticker, name, kind),
            category=_category_for_ex_kind(kind),
            ticker=ticker,
            company=name,
            market="TPEX",
            asset_type=_security_type(ticker, name),
            note=note,
            source="TPEx 上櫃股票除權除息預告表",
            source_quality="official_api",
            url=TPEX_EX_DIVIDEND_URL,
            tickers=(ticker,) if ticker else (),
        ))
    return out


def load_exhibition_events(
    start: dt.date,
    end: dt.date,
    *,
    root: Optional[Path] = None,
) -> List[MarketCalendarEvent]:
    rows = _load_exhibition_rows(root=root)
    out: List[MarketCalendarEvent] = []
    for row in rows:
        day = parse_iso_date(row.get("date"))
        if not day:
            continue
        end_day = parse_iso_date(row.get("end_date")) or day
        tickers = tuple(str(x).strip() for x in row.get("tickers", []) if str(x).strip())
        event = MarketCalendarEvent(
            date=day,
            end_date=end_day,
            title=str(row.get("title") or "").strip(),
            category="exhibition",
            time=str(row.get("time") or "").strip(),
            market=str(row.get("location") or "").strip(),
            asset_type="event",
            note=str(row.get("note") or "").strip(),
            source=str(row.get("source") or "手動展覽清單").strip(),
            source_quality=str(row.get("source_quality") or "").strip(),
            url=str(row.get("url") or "").strip(),
            tickers=tickers,
        )
        if event.overlaps(start, end):
            out.append(event)
    return out


def load_global_tech_events(
    start: dt.date,
    end: dt.date,
    *,
    root: Optional[Path] = None,
) -> List[MarketCalendarEvent]:
    from bot.global_event_calendar import load_global_events

    out: List[MarketCalendarEvent] = []
    for e in load_global_events(root=root):
        if not e.overlaps(start, end):
            continue
        out.append(MarketCalendarEvent(
            date=e.date,
            end_date=e.effective_end_date,
            title=e.title,
            category="global_tech",
            time=e.time or "",
            market=e.location or "",
            asset_type=e.event_type or "event",
            note=e.note or "",
            source=e.source or "全球科技事件",
            source_quality=e.source_quality or "",
            url=e.url or "",
            tickers=e.tickers,
        ))
    return out


def unique_events(events: Sequence[MarketCalendarEvent]) -> List[MarketCalendarEvent]:
    seen: set[Tuple[Any, ...]] = set()
    out: List[MarketCalendarEvent] = []
    for e in events:
        key = (
            e.date,
            e.effective_end_date,
            e.category,
            e.ticker,
            e.title,
            e.time,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def sort_events(events: Sequence[MarketCalendarEvent]) -> List[MarketCalendarEvent]:
    priority = {
        "conference": 0,
        "ex_right_dividend": 1,
        "ex_dividend": 2,
        "ex_right": 3,
        "exhibition": 4,
        "global_tech": 5,
    }
    return sorted(events, key=lambda e: (e.date, e.time or "99:99", priority.get(e.category, 9), e.ticker, e.title))


def event_to_row(event: MarketCalendarEvent) -> Dict[str, Any]:
    date_text = event.date.isoformat()
    if event.effective_end_date != event.date:
        date_text = f"{date_text} ~ {event.effective_end_date.isoformat()}"
    return {
        "日期": date_text,
        "時間": event.time,
        "類型": event.category_label,
        "代號": event.ticker or ", ".join(event.tickers),
        "名稱": event.company or event.title,
        "標的類型": event.asset_type,
        "市場/地點": event.market,
        "備註": event.note,
        "來源": event.source,
        "連結": event.url,
    }


def parse_roc_date(value: Any) -> Optional[dt.date]:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 7:
        year = int(digits[:3]) + 1911
        month = int(digits[3:5])
        day = int(digits[5:7])
    elif len(digits) == 8:
        year = int(digits[:4])
        month = int(digits[4:6])
        day = int(digits[6:8])
    else:
        return None
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def parse_iso_date(value: Any) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _calendar_dir(root: Optional[Path]) -> Path:
    return (root or Path.cwd()) / CALENDAR_DIR_REL


def _load_api_rows(
    url: str,
    cache_name: str,
    *,
    root: Optional[Path],
    session: requests.Session,
    use_cache: bool,
    cache_ttl: int,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    cache_path = _calendar_dir(root) / cache_name
    if use_cache:
        cached = read_json_cache(cache_path, root=root, ttl_seconds=cache_ttl)
        rows = _payload_rows(cached)
        if rows is not None:
            mirror_file_to_cloud(cache_path, root=root)
            return rows

    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        data = resp.json()
        rows = data if isinstance(data, list) else []
        write_json_cache(
            cache_path,
            {
                "source": url,
                "fetched_at": now_tw().isoformat(timespec="seconds"),
                "entries": rows,
            },
            root=root,
            indent=2,
        )
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.warning("除權息資料抓取失敗，改用舊快取: %s (%s)", url, exc)
        stale = read_json_cache(cache_path, root=root, ttl_seconds=None)
        rows = _payload_rows(stale) or []
        if rows:
            mirror_file_to_cloud(cache_path, root=root)
        return rows


def _payload_rows(payload: Any) -> Optional[List[Dict[str, Any]]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
        return [x for x in payload["entries"] if isinstance(x, dict)]
    return None


def _load_exhibition_rows(*, root: Optional[Path]) -> List[Dict[str, Any]]:
    path = _calendar_dir(root) / EXHIBITIONS_FILE
    payload = read_json_cache(path, root=root, ttl_seconds=None)
    rows = _payload_rows(payload)
    if rows is not None:
        version = _payload_schema_version(payload)
        if version < EXHIBITIONS_SCHEMA_VERSION and _looks_like_legacy_exhibitions(rows):
            payload = _curated_exhibition_payload(custom_rows=_custom_exhibition_rows(rows))
            write_json_cache(path, payload, root=root, indent=2)
            return payload["entries"]
        cleaned = _clean_exhibition_rows(rows)
        if cleaned != rows:
            payload = {
                "schema_version": max(version, EXHIBITIONS_SCHEMA_VERSION),
                "updated_at": now_tw().isoformat(timespec="seconds"),
                "note": "Editable curated exhibition list. Keep only verified main events by default.",
                "entries": cleaned,
            }
            write_json_cache(path, payload, root=root, indent=2)
            return cleaned
        mirror_file_to_cloud(path, root=root)
        return rows

    payload = _curated_exhibition_payload()
    write_json_cache(path, payload, root=root, indent=2)
    return payload["entries"]


def _payload_schema_version(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    try:
        return int(payload.get("schema_version") or 0)
    except Exception:
        return 0


def _curated_exhibition_payload(
    *,
    custom_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    entries = [asdict(x) for x in _default_exhibition_seeds()]
    entries.extend(custom_rows or [])
    return {
        "schema_version": EXHIBITIONS_SCHEMA_VERSION,
        "updated_at": now_tw().isoformat(timespec="seconds"),
        "note": (
            "Editable curated exhibition list. Defaults keep only official main events "
            "to avoid duplicate COMPUTEX side activities or loosely related shows."
        ),
        "entries": _clean_exhibition_rows(entries),
    }


def _clean_exhibition_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("enabled") is False:
            continue
        title = str(row.get("title") or "").strip()
        date = str(row.get("date") or "").strip()
        if not title or not date:
            continue
        end_date = str(row.get("end_date") or date).strip()
        canonical = str(row.get("canonical_key") or title).strip().lower()
        key = (canonical, date, end_date, title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


_LEGACY_EXHIBITION_TITLES = {
    "AI EXPO Taiwan 2026",
    "Secutech Taiwan 2026",
    "CYBERSEC 2026 臺灣資安大會",
    "NVIDIA GTC Taipei at COMPUTEX 2026",
    "COMPUTEX 2026 台北國際電腦展",
    "2026 AI TAIWAN 未來商務展",
    "Automation Taipei 2026 台北國際自動化工業大展",
    "SEMICON Taiwan 2026 國際半導體展",
}


def _looks_like_legacy_exhibitions(rows: Sequence[Dict[str, Any]]) -> bool:
    titles = {str(row.get("title") or "").strip() for row in rows}
    return len(titles & _LEGACY_EXHIBITION_TITLES) >= 4


def _custom_exhibition_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    curated_titles = {seed.title for seed in _default_exhibition_seeds()}
    custom: List[Dict[str, Any]] = []
    for row in rows:
        title = str(row.get("title") or "").strip()
        if title in _LEGACY_EXHIBITION_TITLES or title in curated_titles:
            continue
        custom.append(dict(row))
    return custom


@dataclass(frozen=True)
class _ExhibitionSeed:
    date: str
    end_date: str
    title: str
    location: str
    time: str
    note: str
    source: str
    url: str
    canonical_key: str
    source_quality: str = "official"
    verified_at: str = "2026-06-02"
    enabled: bool = True
    tickers: Tuple[str, ...] = ()


def _default_exhibition_seeds() -> List[_ExhibitionSeed]:
    return [
        _ExhibitionSeed(
            date="2026-05-05",
            end_date="2026-05-07",
            title="CYBERSEC 2026 臺灣資安大會",
            location="台北南港展覽館 2 館",
            time="",
            note="台灣大型資安會展，適合追蹤資安、零信任、雲端安全與網通題材。",
            source="CYBERSEC 官方頁",
            url="https://cybersec.ithome.com.tw/en/2026/about",
            canonical_key="cybersec-2026",
        ),
        _ExhibitionSeed(
            date="2026-06-02",
            end_date="2026-06-05",
            title="COMPUTEX 2026 台北國際電腦展",
            location="南港 1、2 館 / 世貿 1 館 / TICC",
            time="",
            note="官方主展：AI、運算、機器人、次世代科技供應鏈大展；NVIDIA GTC Taipei 等同期活動併入此事件，不另列重複主事件。",
            source="COMPUTEX 官方新聞",
            url="https://www.computextaipei.com.tw/en/news/8F914C77B6AF77A5/info.html?cid=news&cr=5&lt=data",
            canonical_key="computex-2026",
            tickers=("2357", "2353", "2376", "2377", "2317", "2382", "4938", "6669", "2454", "2308"),
        ),
        _ExhibitionSeed(
            date="2026-08-19",
            end_date="2026-08-22",
            title="Automation Taipei 2026 台北國際自動化工業大展",
            location="台北南港展覽館 1、2 館",
            time="10:00-17:00",
            note="官方主展：自動化、智慧製造、機器人與工業電腦題材。",
            source="Automation Taipei 官方頁",
            url="https://automationtaipei.chanchao.com.tw/en/VisitorInfo",
            canonical_key="automation-taipei-2026",
        ),
        _ExhibitionSeed(
            date="2026-09-02",
            end_date="2026-09-04",
            title="SEMICON Taiwan 2026 國際半導體展",
            location="台北南港展覽館 1、2 館",
            time="09/02-09/03 10:00-17:00; 09/04 10:00-16:00",
            note="官方主展：半導體設備、材料、封測、先進製程與供應鏈大展。",
            source="SEMICON Taiwan 官方頁",
            url="https://www.semicontaiwan.org/en/about/overview",
            canonical_key="semicon-taiwan-2026",
        ),
    ]


def _normalize_ex_kind(value: Any) -> str:
    text = str(value or "").strip()
    has_right = "權" in text
    has_dividend = "息" in text
    if has_right and has_dividend:
        return "除權息"
    if has_right:
        return "除權"
    if has_dividend:
        return "除息"
    return "除權息"


def _category_for_ex_kind(kind: str) -> str:
    if kind == "除權":
        return "ex_right"
    if kind == "除息":
        return "ex_dividend"
    return "ex_right_dividend"


def _include_dividend_security(event: MarketCalendarEvent, scope: str) -> bool:
    normalized = (scope or "stock").strip().lower()
    if normalized in {"all", "全部", "全部商品"}:
        return True
    if normalized in {"stock", "stocks", "equity", "個股"}:
        return event.asset_type == "stock"
    return True


def _security_type(ticker: str, name: str = "") -> str:
    code = (ticker or "").strip().upper()
    text = f"{code} {name or ''}".upper()
    if not code:
        return ""
    if code.startswith("010"):
        return "reit"
    if code.startswith("020"):
        return "etn"
    if "B" in code or "債" in text:
        return "bond_etf"
    if code.startswith(("006", "007", "008", "009")) or "ETF" in text or "指數" in text:
        return "etf"
    if re.fullmatch(r"\d{4}", code):
        return "stock"
    return "other"


def _event_title(ticker: str, name: str, kind: str) -> str:
    return " ".join(part for part in (ticker, name, kind) if part)


def _dividend_note(
    *,
    cash: Any,
    stock: Any,
    subscription: Any,
    subscription_price: Any,
) -> str:
    parts: List[str] = []
    cash_text = _clean_value(cash)
    stock_text = _clean_value(stock)
    sub_text = _clean_value(subscription)
    sub_price = _clean_value(subscription_price)
    if cash_text and cash_text != "0":
        parts.append(f"現金股利 {cash_text}")
    if stock_text and stock_text != "0":
        parts.append(f"無償配股率 {stock_text}")
    if sub_text and sub_text != "0":
        parts.append(f"現增配股率 {sub_text}")
    if sub_price and sub_price != "0":
        parts.append(f"認購價 {sub_price}")
    return "；".join(parts)


def _clean_value(value: Any) -> str:
    text = str(value or "").replace(",", "").strip()
    if not text or text in {"-", "--", "0.00000000", "0.000000", "0.00"}:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    if number == 0:
        return ""
    return f"{number:g}"


__all__ = [
    "CATEGORY_LABELS",
    "MarketCalendarEvent",
    "build_market_calendar",
    "event_to_row",
    "fetch_ex_dividend_events",
    "load_conference_events",
    "load_exhibition_events",
    "load_global_tech_events",
    "parse_iso_date",
    "parse_roc_date",
    "parse_tpex_dividend_events",
    "parse_twse_dividend_events",
    "sort_events",
    "unique_events",
]
