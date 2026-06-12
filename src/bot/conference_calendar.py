"""conference_calendar -- 全自動法說會行事曆抓取與快取。

設計目標
========
過去 dashboard 「LLM 法說分析 → 行事曆」需要使用者手動選月份、按按鈕才會抓 MOPS。
本模組改為「**啟動 dashboard / 跑 pipeline 時自動更新**」：

1. 每天自動跑 ``update_calendar()`` 抓上月+本月+下月共三個月的 MOPS 法說會
2. 結果存到 ``data/calendar/conferences_<YYYY-MM>.json``
3. 同時維護一份合併索引 ``data/calendar/calendar_state.json``
4. 提供查詢介面：
   * ``load_calendar()`` — 所有快取項合併排序
   * ``upcoming_conferences(days=14)`` — 未來 N 天
   * ``recent_conferences(days=14)`` — 過去 N 天
   * ``conferences_for_ticker(ticker)`` — 個股的全部紀錄
   * ``ensure_calendar_fresh(max_age_hours=24)`` — 過期才重抓 (lightweight gate)

使用情境
========
* dashboard 進入「LLM 法說分析」頁時自動呼叫 ``ensure_calendar_fresh()``
* ``stock-auto-research`` / ``stock-auto-research --llm-only`` CLI 開頭呼叫
* ``data_pipeline.run_full_pipeline`` step 0 呼叫
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.mops_scraper import ConferenceEntry, fetch_conference_schedule
from bot.utils import get_logger, mk_folder, now_tw


CALENDAR_DIR_REL = "data/calendar"
STATE_FILE = "calendar_state.json"
DEFAULT_AGE_HOURS = 24
WINDOW_MONTHS = (-1, 0, 1, 2)        # 上月 / 本月 / 下月 / 兩個月後


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------


def _calendar_dir(root: Optional[Path]) -> Path:
    return (root or Path.cwd()) / CALENDAR_DIR_REL


def _month_file(year: int, month: int, root: Optional[Path]) -> Path:
    return _calendar_dir(root) / f"conferences_{year:04d}-{month:02d}.json"


def _shift_month(today: dt.date, delta: int) -> Tuple[int, int]:
    m = today.month + delta
    y = today.year + (m - 1) // 12
    m = ((m - 1) % 12) + 1
    return y, m


def _to_dict(e: ConferenceEntry) -> Dict[str, Any]:
    return {
        "date": e.date.isoformat() if hasattr(e.date, "isoformat") else str(e.date),
        "time": e.time,
        "ticker": e.ticker,
        "company": e.company,
        "note": e.note,
        "presentation_url": e.presentation_url,
    }


def _from_dict(d: Dict[str, Any]) -> Optional[ConferenceEntry]:
    try:
        date_str = str(d.get("date", ""))
        if not date_str:
            return None
        return ConferenceEntry(
            date=dt.date.fromisoformat(date_str[:10]),
            time=str(d.get("time", "")),
            ticker=str(d.get("ticker", "")),
            company=str(d.get("company", "")),
            note=str(d.get("note", "")),
            presentation_url=str(d.get("presentation_url", "")),
        )
    except Exception:
        return None


# ----------------------------------------------------------------------
# 抓取 + 寫入
# ----------------------------------------------------------------------


def _fetch_month(
    year: int,
    month: int,
    *,
    logger: logging.Logger,
) -> List[ConferenceEntry]:
    """抓單一西元年/月 (內部會轉成民國年)。"""
    year_roc = year - 1911
    try:
        entries = fetch_conference_schedule(year_roc, month, logger=logger)
    except Exception:
        logger.exception("MOPS 月份抓取失敗 %d/%d", year, month)
        return []
    return entries


def _save_month(
    year: int, month: int, entries: List[ConferenceEntry],
    root: Optional[Path],
) -> Path:
    p = _month_file(year, month, root)
    mk_folder(str(p.parent))
    payload = {
        "year": year,
        "month": month,
        "fetched_at": now_tw().isoformat(timespec="seconds"),
        "entries": [_to_dict(e) for e in entries],
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    mirror_file_to_cloud(p, root=root)
    return p


def _load_month(
    year: int, month: int, root: Optional[Path],
) -> List[ConferenceEntry]:
    p = _month_file(year, month, root)
    restore_file_from_cloud(p, root=root)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: List[ConferenceEntry] = []
    for d in raw.get("entries", []) or []:
        e = _from_dict(d)
        if e:
            out.append(e)
    return out


def _state_path(root: Optional[Path]) -> Path:
    return _calendar_dir(root) / STATE_FILE


def _read_state(root: Optional[Path]) -> Dict[str, Any]:
    p = _state_path(root)
    restore_file_from_cloud(p, root=root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: Dict[str, Any], root: Optional[Path]) -> None:
    p = _state_path(root)
    mk_folder(str(p.parent))
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    mirror_file_to_cloud(p, root=root)


# ----------------------------------------------------------------------
# 主要 API
# ----------------------------------------------------------------------


def update_calendar(
    root: Optional[Path] = None,
    *,
    months: Tuple[int, ...] = WINDOW_MONTHS,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, int]:
    """重新抓取本月前後 N 個月的法說會行事曆，覆蓋本地快取。

    Returns:
        ``{"YYYY-MM": 抓到的筆數, ...}``
    """
    log = logger or get_logger("calendar")
    today = now_tw().date()
    summary: Dict[str, int] = {}
    for delta in months:
        y, m = _shift_month(today, delta)
        entries = _fetch_month(y, m, logger=log)
        if entries:
            _save_month(y, m, entries, root)
        else:
            existing = _load_month(y, m, root)
            if not existing:
                _save_month(y, m, [], root)
        summary[f"{y:04d}-{m:02d}"] = len(entries)

    state = _read_state(root)
    state["last_full_refresh_at"] = now_tw().isoformat(timespec="seconds")
    state["last_summary"] = summary
    _write_state(state, root)
    all_entries = load_calendar(root)
    filled = sum(1 for e in all_entries if getattr(e, "presentation_url", ""))
    total = len(all_entries)
    rate = (filled / total) if total else 0.0
    log.info(
        "法說會行事曆已自動更新: %s | 簡報連結填充 %d/%d (%.0f%%)",
        summary, filled, total, rate * 100,
    )
    return summary


def calendar_needs_url_refresh(
    root: Optional[Path] = None,
    *,
    min_rate: float = 0.05,
) -> bool:
    """快取有資料但簡報連結填充率過低（常見於舊版抓取）。"""
    filled, total, rate = presentation_url_fill_rate(root)
    return total > 0 and rate < min_rate


def ensure_calendar_fresh(
    root: Optional[Path] = None,
    *,
    max_age_hours: int = DEFAULT_AGE_HOURS,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """過期或簡報連結缺失時重抓。回傳 True=有重抓 / False=直接走快取。"""
    log = logger or get_logger("calendar")
    needs_url = calendar_needs_url_refresh(root)
    if needs_url:
        log.info(
            "行事曆簡報連結填充率過低，強制重抓 MOPS（舊快取可能無 presentation_url）",
        )
        update_calendar(root=root, logger=log)
        return True
    state = _read_state(root)
    ts = state.get("last_full_refresh_at")
    if ts:
        try:
            last = dt.datetime.fromisoformat(ts.replace("Z", ""))
            elapsed_h = (now_tw().replace(tzinfo=None) - last).total_seconds() / 3600
            if elapsed_h < max_age_hours:
                log.debug("行事曆快取仍新鮮 (%.1fh)，略過抓取", elapsed_h)
                return False
        except Exception:
            pass
    update_calendar(root=root, logger=log)
    return True


def load_calendar(
    root: Optional[Path] = None,
    *,
    months: Tuple[int, ...] = WINDOW_MONTHS,
) -> List[ConferenceEntry]:
    """讀取上下文窗口內的所有法說會 (依日期排序)。"""
    today = now_tw().date()
    out: List[ConferenceEntry] = []
    for delta in months:
        y, m = _shift_month(today, delta)
        out.extend(_load_month(y, m, root))
    seen: set = set()
    unique: List[ConferenceEntry] = []
    for e in out:
        key = (e.date, e.ticker, e.time)
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    unique.sort(key=lambda e: (e.date, e.time))
    return unique


def upcoming_conferences(
    days: int = 14,
    root: Optional[Path] = None,
) -> List[ConferenceEntry]:
    """未來 ``days`` 天內 (含今日) 的法說會。"""
    today = now_tw().date()
    end = today + dt.timedelta(days=days)
    return [
        e for e in load_calendar(root) if today <= e.date <= end
    ]


def recent_conferences(
    days: int = 14,
    root: Optional[Path] = None,
) -> List[ConferenceEntry]:
    """過去 ``days`` 天內 (不含今日) 的法說會。"""
    today = now_tw().date()
    start = today - dt.timedelta(days=days)
    return [
        e for e in load_calendar(root) if start <= e.date < today
    ]


def conferences_for_ticker(
    ticker: str,
    root: Optional[Path] = None,
) -> List[ConferenceEntry]:
    """此 ticker 在快取窗口內的所有法說會 (含過往)，依日期由新到舊。"""
    items = [e for e in load_calendar(root) if e.ticker == ticker]
    items.sort(key=lambda e: e.date, reverse=True)
    return items


def last_refresh_at(root: Optional[Path] = None) -> str:
    return str(_read_state(root).get("last_full_refresh_at", ""))


def presentation_url_fill_rate(root: Optional[Path] = None) -> Tuple[int, int, float]:
    """回傳 (有簡報連結筆數, 總筆數, 填充率 0~1)。"""
    items = load_calendar(root)
    total = len(items)
    if total == 0:
        return 0, 0, 0.0
    from bot.mops_scraper import is_meaningful_presentation_url

    filled = sum(
        1 for e in items
        if is_meaningful_presentation_url(getattr(e, "presentation_url", ""))
    )
    return filled, total, filled / total


def upcoming_tickers(
    days: int = 14,
    root: Optional[Path] = None,
) -> List[str]:
    """未來 N 天有法說會的個股代號 (去重，依日期排序)。"""
    out: List[str] = []
    for e in upcoming_conferences(days=days, root=root):
        if e.ticker and e.ticker not in out:
            out.append(e.ticker)
    return out


__all__ = [
    "ConferenceEntry",
    "calendar_needs_url_refresh",
    "conferences_for_ticker",
    "ensure_calendar_fresh",
    "last_refresh_at",
    "load_calendar",
    "presentation_url_fill_rate",
    "recent_conferences",
    "update_calendar",
    "upcoming_conferences",
    "upcoming_tickers",
]
