"""global_event_calendar -- 全球科技事件快取與查詢 API。

對標 ``conference_calendar``：自動抓取、本地快取、提供 upcoming/recent/for_ticker 查詢。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.global_events_fetcher import fetch_all_global_events
from bot.utils import get_logger, mk_folder, now_tw

CALENDAR_DIR_REL = "data/calendar"
GLOBAL_EVENTS_FILE = "global_tech_events.json"
STATE_FILE = "calendar_state.json"
DEFAULT_AGE_HOURS = 12


@dataclass(frozen=True)
class GlobalTechEvent:
    date: dt.date
    title: str
    end_date: dt.date | None = None
    event_type: str = ""
    organizer: str = ""
    location: str = ""
    time: str = ""
    note: str = ""
    source: str = ""
    source_quality: str = ""
    url: str = ""
    canonical_key: str = ""
    tickers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def effective_end_date(self) -> dt.date:
        return self.end_date or self.date

    def occurs_on(self, day: dt.date) -> bool:
        return self.date <= day <= self.effective_end_date

    def overlaps(self, start: dt.date, end: dt.date) -> bool:
        return self.date <= end and self.effective_end_date >= start


def _calendar_dir(root: Path | None) -> Path:
    return (root or Path.cwd()) / CALENDAR_DIR_REL


def _events_path(root: Path | None) -> Path:
    return _calendar_dir(root) / GLOBAL_EVENTS_FILE


def _state_path(root: Path | None) -> Path:
    return _calendar_dir(root) / STATE_FILE


def _parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _to_dict(e: GlobalTechEvent) -> dict[str, Any]:
    return {
        "date": e.date.isoformat(),
        "end_date": e.effective_end_date.isoformat(),
        "title": e.title,
        "event_type": e.event_type,
        "organizer": e.organizer,
        "location": e.location,
        "time": e.time,
        "note": e.note,
        "source": e.source,
        "source_quality": e.source_quality,
        "url": e.url,
        "canonical_key": e.canonical_key,
        "enabled": True,
        "tickers": list(e.tickers),
    }


def _from_dict(d: dict[str, Any]) -> GlobalTechEvent | None:
    day = _parse_date(d.get("date"))
    if not day:
        return None
    end_day = _parse_date(d.get("end_date")) or day
    tickers = tuple(str(x).strip() for x in (d.get("tickers") or []) if str(x).strip())
    return GlobalTechEvent(
        date=day,
        end_date=end_day,
        title=str(d.get("title") or "").strip(),
        event_type=str(d.get("event_type") or "").strip(),
        organizer=str(d.get("organizer") or "").strip(),
        location=str(d.get("location") or "").strip(),
        time=str(d.get("time") or "").strip(),
        note=str(d.get("note") or "").strip(),
        source=str(d.get("source") or "").strip(),
        source_quality=str(d.get("source_quality") or "").strip(),
        url=str(d.get("url") or "").strip(),
        canonical_key=str(d.get("canonical_key") or "").strip(),
        tickers=tickers,
    )


def _read_state(root: Path | None) -> dict[str, Any]:
    p = _state_path(root)
    restore_file_from_cloud(p, root=root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict[str, Any], root: Path | None) -> None:
    p = _state_path(root)
    mk_folder(str(p.parent))
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    mirror_file_to_cloud(p, root=root)


def _save_events(rows: list[dict[str, Any]], root: Path | None) -> Path:
    p = _events_path(root)
    mk_folder(str(p.parent))
    payload = {
        "schema_version": 1,
        "fetched_at": now_tw().isoformat(timespec="seconds"),
        "entries": rows,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    mirror_file_to_cloud(p, root=root)
    return p


def _load_rows(root: Path | None) -> list[dict[str, Any]]:
    p = _events_path(root)
    restore_file_from_cloud(p, root=root)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    return [x for x in entries if isinstance(x, dict)]


def update_global_events(
    root: Path | None = None,
    *,
    force: bool = False,
    include_network: bool = True,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """重新抓取全球科技事件並寫入快取。"""
    log = logger or get_logger("global-events")
    try:
        rows = fetch_all_global_events(
            root=root, include_network=include_network, logger=log,
        )
    except Exception:
        log.exception("全球科技事件抓取失敗，沿用舊快取")
        rows = _load_rows(root)
        if not rows:
            rows = fetch_all_global_events(
                root=root, include_network=False, logger=log,
            )
    if rows:
        _save_events(rows, root)
    else:
        existing = _load_rows(root)
        if existing:
            rows = existing
        else:
            rows = fetch_all_global_events(
                root=root, include_network=False, logger=log,
            )
            _save_events(rows, root)

    state = _read_state(root)
    state["global_events"] = {
        "last_refresh_at": now_tw().isoformat(timespec="seconds"),
        "count": len(rows),
        "forced": force,
    }
    _write_state(state, root)
    log.info("全球科技事件已更新: %d 筆", len(rows))
    return {"count": len(rows), "entries": rows}


def _max_age_hours(override: int | None = None) -> int:
    if override is not None:
        return override
    raw = os.environ.get("GLOBAL_EVENTS_MAX_AGE_HOURS", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_AGE_HOURS


def ensure_global_events_fresh(
    root: Path | None = None,
    *,
    max_age_hours: int | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """過期才重抓。回傳 True=有重抓。"""
    log = logger or get_logger("global-events")
    age_hours = _max_age_hours(max_age_hours)
    state = _read_state(root)
    ge = state.get("global_events") or {}
    ts = ge.get("last_refresh_at")
    if ts:
        try:
            last = dt.datetime.fromisoformat(str(ts).replace("Z", ""))
            elapsed_h = (now_tw().replace(tzinfo=None) - last).total_seconds() / 3600
            if elapsed_h < age_hours:
                log.debug("全球事件快取仍新鮮 (%.1fh)，略過抓取", elapsed_h)
                return False
        except Exception:
            pass
    update_global_events(root=root, logger=log)
    return True


def load_global_events(root: Path | None = None) -> list[GlobalTechEvent]:
    out: list[GlobalTechEvent] = []
    for row in _load_rows(root):
        if row.get("enabled") is False:
            continue
        e = _from_dict(row)
        if e:
            out.append(e)
    out.sort(key=lambda e: (e.date, e.time, e.title))
    return out


def upcoming_global_events(
    days: int = 14,
    root: Path | None = None,
) -> list[GlobalTechEvent]:
    """未來 N 天內開始或進行中的事件 (含今日)。"""
    today = now_tw().date()
    end = today + dt.timedelta(days=days)
    return [
        e for e in load_global_events(root)
        if e.effective_end_date >= today and e.date <= end
    ]


def recent_global_events(
    days: int = 2,
    root: Path | None = None,
) -> list[GlobalTechEvent]:
    """過去 N 天內結束或仍在進行的事件 (含今日進行中)。"""
    today = now_tw().date()
    start = today - dt.timedelta(days=days)
    return [
        e for e in load_global_events(root)
        if e.effective_end_date >= start and e.date <= today + dt.timedelta(days=days)
    ]


def active_global_events_on(
    day: dt.date,
    root: Path | None = None,
) -> list[GlobalTechEvent]:
    return [e for e in load_global_events(root) if e.occurs_on(day)]


def global_events_for_ticker(
    ticker: str,
    *,
    lookahead: int = 60,
    lookback: int = 2,
    root: Path | None = None,
) -> dict[str, list[GlobalTechEvent]]:
    """回傳個股相關的全球科技事件 (upcoming / recent)。"""
    today = now_tw().date()
    upcoming: list[GlobalTechEvent] = []
    recent: list[GlobalTechEvent] = []
    for e in load_global_events(root):
        if ticker not in e.tickers:
            continue
        if e.date >= today and (e.date - today).days <= lookahead:
            upcoming.append(e)
        elif e.effective_end_date < today and (today - e.effective_end_date).days <= lookback or e.occurs_on(today):
            recent.append(e)
    upcoming.sort(key=lambda x: x.date)
    recent.sort(key=lambda x: x.date, reverse=True)
    return {"upcoming": upcoming, "recent": recent}


def upcoming_tickers_from_global_events(
    days: int = 14,
    root: Path | None = None,
) -> list[str]:
    out: list[str] = []
    for e in upcoming_global_events(days=days, root=root):
        for t in e.tickers:
            if t.isdigit() and t not in out:
                out.append(t)
    recent = recent_global_events(days=1, root=root)
    for e in recent:
        for t in e.tickers:
            if t.isdigit() and t not in out:
                out.append(t)
    return out


def last_global_refresh_at(root: Path | None = None) -> str:
    state = _read_state(root)
    ge = state.get("global_events") or {}
    return str(ge.get("last_refresh_at") or "")


def format_global_event_text(events: Sequence[GlobalTechEvent]) -> str:
    if not events:
        return "(無)"
    parts: list[str] = []
    for e in events:
        end = e.effective_end_date
        date_text = e.date.isoformat()
        if end != e.date:
            date_text = f"{date_text} ~ {end.isoformat()}"
        tickers = ", ".join(e.tickers) if e.tickers else ""
        line = f"- {date_text} {e.time} [{e.event_type or 'event'}] {e.title}"
        if tickers:
            line += f" (台股: {tickers})"
        if e.note:
            line += f" — {e.note}"
        parts.append(line)
    return "\n".join(parts)


__all__ = [
    "GlobalTechEvent",
    "active_global_events_on",
    "ensure_global_events_fresh",
    "format_global_event_text",
    "global_events_for_ticker",
    "last_global_refresh_at",
    "load_global_events",
    "recent_global_events",
    "upcoming_global_events",
    "upcoming_tickers_from_global_events",
    "update_global_events",
]
