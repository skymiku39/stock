"""美股隔夜重點的資料產生端：美股行情 + 隔夜美國新聞候選清單，寫成 JSON。

用法：python -m bot.overnight_brief --root <路徑> --slot morning|preopen [--no-notify]

只做規則篩選，不呼叫模型；模型摘要留了 `summarize` 掛勾，沒接上時 summary.status 為 pending_model。
輸出：data/overnight_brief/<date>.json 與 latest.json（結構給顯示端讀，欄位名不可改）。

API 新聞：main 先跑 API_NEWS_COLLECTORS 的收集器（失敗只記警告），build_brief 再把
data/news/api_<來源>_<日期>.json 併入視窗候選；缺口偵測只看 RSS。
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from bot.rss_archive import archive_path
from bot.runtime_state import atomic_json, read_json
from bot.smile_sentiment import _parse_ts, _read_day
from bot.utils import configure_utf8_stdio, get_logger, now_tw
from bot.web_search import OFFICIAL_STANCE

SUMMARY_PENDING_NOTE = "模型摘要尚未接上（模型額度恢復後才會有）"
MAX_CANDIDATES = 40
SIMILARITY = 0.4
STALE_ARCHIVE_MINUTES = 90
BRIEF_INDICES = ("^GSPC", "^IXIC", "^DJI", "^SOX", "^VIX")

# 隔夜重點產生前要先跑的 API 新聞收集器：來源名 -> 模組（須有 collect(root, *, now) -> dict）。
# 其他來源接線時各加一行。
API_NEWS_COLLECTORS: dict = {
    "finnhub": "bot.free_sources.collect_finnhub",
}
API_NEWS_RE = re.compile(r"^api_([a-z0-9]+)_(\d{4}-\d{2}-\d{2})\.json$")
COLLECT_WARN_STATUSES = frozenset({"error", "partial", "quota_exceeded", "failed"})

STOPWORDS = frozenset({
    "that", "with", "from", "this", "says", "said", "after", "over", "will", "have",
    "amid", "into", "about", "stock", "stocks", "shares", "company", "their", "than",
    "more", "what", "were", "been", "could", "would", "your", "news", "report",
})

TOPIC_KEYWORDS = {
    "半導體": ["semiconductor", "chip", "chips", "chipmaker", "foundry", "TSMC", "Nvidia",
               "AMD", "Micron", "ASML", "Broadcom"],
    "AI": ["AI", "artificial intelligence", "data center", "datacenter"],
    "聯準會與利率": ["Fed", "Federal Reserve", "rate cut", "rate hike", "interest rate", "Powell",
                "Warsh", "Treasury yield", "inflation", "CPI", "jobs report", "payrolls"],
    "關稅與貿易": ["tariff", "tariffs", "trade war", "export control", "export controls",
               "sanction", "sanctions"],
    "台灣": ["Taiwan", "Taiwanese"],
    "大盤": ["S&P 500", "Nasdaq", "Dow", "Wall Street", "selloff", "sell-off", "rally"],
}
_TOPIC_RE = {
    topic: re.compile(
        "|".join(rf"(?<![A-Za-z0-9]){re.escape(k)}(?![A-Za-z0-9])" for k in words), re.IGNORECASE)
    for topic, words in TOPIC_KEYWORDS.items()
}


def previous_weekday(d: dt.date) -> dt.date:
    """嚴格早於 d 的最近一個平日。"""
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def window_start(now: dt.datetime) -> dt.datetime:
    prev = previous_weekday(now.date())
    return dt.datetime.combine(prev, dt.time(21, 0), tzinfo=now.tzinfo)


def _tokens(title: str) -> frozenset:
    words = re.findall(r"[a-z]+", str(title).lower())
    return frozenset(w for w in words if len(w) >= 4 and w not in STOPWORDS)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _read_archives(root: Path, start: dt.datetime, now: dt.datetime):
    """讀 start 那天到 now 那天的存檔，回傳 (全部項目, 最新 collected_at)。"""
    rows: list = []
    collected: list = []
    day = start.date()
    while day <= now.date():
        data = _read_day(archive_path(root, day.isoformat()))
        day += dt.timedelta(days=1)
        if data is None:
            continue
        if data.get("collected_at"):
            collected.append(str(data["collected_at"]))
        items = data.get("items")
        if isinstance(items, list):
            rows.extend(i for i in items if isinstance(i, dict))
    parsed = [(_parse_ts(c, now.tzinfo), c) for c in collected]
    parsed = [p for p in parsed if p[0] is not None]
    latest = max(parsed, key=lambda p: p[0]) if parsed else None
    return rows, latest


def read_api_news(root: Path, start: dt.datetime, now: dt.datetime):
    """讀 start 那天到 now 那天的 data/news/api_<來源>_<日期>.json。

    回傳 (全部項目, {來源: {"files": [檔名], "items": 筆數}})；損毀或非 list 的檔視為空。
    """
    news_dir = Path(root) / "data" / "news"
    days = set()
    day = start.date()
    while day <= now.date():
        days.add(day.isoformat())
        day += dt.timedelta(days=1)
    rows: list = []
    sources: dict = {}
    try:
        paths = sorted(news_dir.iterdir())
    except OSError:
        return rows, sources
    for path in paths:
        m = API_NEWS_RE.match(path.name)
        if not m or m.group(2) not in days:
            continue
        src = sources.setdefault(m.group(1), {"files": [], "items": 0})
        src["files"].append(path.name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, list):
            continue
        items = [i for i in data if isinstance(i, dict)]
        src["items"] += len(items)
        rows.extend(items)
    return rows, sources


def _source_key(item: dict) -> str:
    """算「幾個來源」用：API 新聞以發布者（source 欄）計，RSS 以 feed 計。

    Finnhub 的 feed 是查詢代號（finnhub:NVDA、finnhub:AMD），同一發布者在多檔查詢出現不能算多個來源。
    """
    publisher = str(item.get("source") or "").strip()
    return f"publisher:{publisher.lower()}" if publisher else str(item.get("feed") or "")


def _source_of(item: dict) -> str:
    return str(item.get("feed") or "").split(":", 1)[0]


def run_api_collectors(root: Path, now: dt.datetime, collectors: Optional[dict] = None) -> dict:
    """依序跑收集器；任何例外都吞掉並記成 {"status": "failed"}，回傳 {來源: 摘要}。"""
    from bot.free_sources.base import redact

    results: dict = {}
    for name, module in (API_NEWS_COLLECTORS if collectors is None else collectors).items():
        try:
            fn = module if callable(module) else importlib.import_module(module).collect
            summary = fn(Path(root), now=now)
            results[name] = summary if isinstance(summary, dict) else {"status": "failed",
                                                                       "error": "回傳不是 dict"}
        except Exception as exc:  # noqa: BLE001 - 收集失敗不能讓隔夜重點失敗
            results[name] = {"status": "failed", "error": redact(f"{type(exc).__name__}: {exc}")}
    return results


def _collect_warning(name: str, summary: dict) -> Optional[str]:
    status = str(summary.get("status") or "")
    if status not in COLLECT_WARN_STATUSES:
        return None
    detail = summary.get("error") or "、".join(sorted((summary.get("errors") or {}).keys()))
    label = {"partial": "部分失敗", "quota_exceeded": "用量已達上限"}.get(status, "失敗")
    return f"{name} 新聞收集{label}" + (f"：{detail}" if detail else "") + "，隔夜重點只用已有的資料"


def _item_ts(item: dict, tz) -> Optional[dt.datetime]:
    return _parse_ts(item.get("published_at") or item.get("first_seen_at"), tz)


def _window_us_items(rows, start: dt.datetime, now: dt.datetime) -> list:
    """美國、非官媒、時間落在視窗內、依 url 去重；回傳 [(ts, item)] 依時間由舊到新。"""
    seen: set = set()
    out: list = []
    for item in rows:
        if item.get("region") != "us" or item.get("stance") == OFFICIAL_STANCE:
            continue
        ts = _item_ts(item, now.tzinfo)
        if ts is None or ts < start or ts > now + dt.timedelta(hours=1):
            continue
        key = item.get("url") or item.get("title")
        if key in seen:
            continue
        seen.add(key)
        out.append((ts, item))
    out.sort(key=lambda p: p[0])
    return out


def build_candidates(items: list) -> list:
    """items 為 [(ts, item)]（由舊到新）；同一件事歸群後打分，回傳前 MAX_CANDIDATES 群。"""
    groups: list = []  # 每群 {"seed": tokens, "members": [(ts, item)]}
    for ts, item in items:
        toks = _tokens(item.get("title", ""))
        for g in groups:
            if _jaccard(toks, g["seed"]) >= SIMILARITY:
                g["members"].append((ts, item))
                break
        else:
            groups.append({"seed": toks, "members": [(ts, item)]})
    out: list = []
    for g in groups:
        members = g["members"]  # 已依時間由舊到新，第一則最早
        ts0, rep = members[0]
        feeds = sorted({str(i.get("feed", "")) for _, i in members if i.get("feed")})
        n_sources = len({k for _, i in members for k in [_source_key(i)] if k})
        tickers = sorted({t for _, i in members for t in (i.get("related_tickers") or [])})
        us_tickers = sorted({str(t) for _, i in members for t in (i.get("us_tickers") or [])})
        text = " ".join(str(i.get("title", "")) for _, i in members)
        topics = [name for name, rx in _TOPIC_RE.items() if rx.search(text)]
        score = 2 * n_sources + (2 if tickers else 0) + (1 if topics else 0)
        out.append((score, ts0, {
            "title": rep.get("title", ""), "url": rep.get("url", ""), "feed": rep.get("feed", ""),
            "published_at": rep.get("published_at") or rep.get("first_seen_at", ""),
            "desc": rep.get("desc", ""), "feeds": feeds, "source_count": n_sources,
            "related_tickers": tickers, "us_tickers": us_tickers, "topics": topics,
            "score": score,
        }))
    out.sort(key=lambda p: p[1], reverse=True)       # 同分：新的在前
    out.sort(key=lambda p: p[0], reverse=True)       # 穩定排序，分數高的在前
    return [c for _, _, c in out[:MAX_CANDIDATES]]


def detect_gaps(rows, start: dt.datetime, now: dt.datetime):
    """以 first_seen_at 數每個完整整點小時的新項目；連續 >= 2 小時為 0 記一個缺口。"""
    hour = start.replace(minute=0, second=0, microsecond=0)
    if hour < start:
        hour += dt.timedelta(hours=1)
    last_full = now.replace(minute=0, second=0, microsecond=0)  # 此刻所在小時尚未結束
    counts: dict = {}
    for item in rows:
        ts = _parse_ts(item.get("first_seen_at"), now.tzinfo)
        if ts is None:
            continue
        h = ts.replace(minute=0, second=0, microsecond=0)
        counts[h] = counts.get(h, 0) + 1
    gaps: list = []
    warnings: list = []
    run_start = None
    run_len = 0

    def close(run_end):
        if run_start is not None and run_len >= 2:
            gaps.append({"start": run_start.isoformat(), "end": run_end.isoformat()})
            warnings.append(
                f"{run_start:%m-%d %H}:00～{run_end:%H}:00 沒有收到任何新項目，"
                "電腦可能關機或睡眠，這段時間的新聞可能有缺")

    while hour + dt.timedelta(hours=1) <= last_full:
        if counts.get(hour, 0) == 0:
            if run_len == 0:
                run_start = hour
            run_len += 1
        else:
            close(hour)
            run_start, run_len = None, 0
        hour += dt.timedelta(hours=1)
    close(hour)
    return gaps, warnings


def _quote_row(q: Any, extra: tuple = ()) -> dict:
    row = {"symbol": q.symbol, "name": q.name, "price": q.price,
           "pct_change": q.pct_change, "source_date": q.source_date}
    for k in extra:
        row[k] = getattr(q, k, "")
    return row


def _market_section(macro_loader: Callable, now: dt.datetime, expected_date: str):
    warnings: list = []
    market = {"status": "ok", "error": "", "fetched_at": "", "usdtwd": None,
              "indices": [], "stocks": [], "adr_premiums": []}
    try:
        snap = macro_loader()
        market["fetched_at"] = snap.fetched_at
        market["usdtwd"] = snap.usdtwd
        market["indices"] = [_quote_row(snap.indices[s]) for s in BRIEF_INDICES if s in snap.indices]
        market["stocks"] = [_quote_row(q, ("parent_tw_ticker",)) for q in snap.stocks.values()]
        market["adr_premiums"] = [
            {"adr_symbol": a.adr_symbol, "tw_ticker": a.tw_ticker,
             "premium_pct": a.premium_pct, "temporal_status": a.temporal_status}
            for a in snap.adr_premiums]
        if not market["indices"]:
            raise RuntimeError("沒有拿到任何美股指數")
        gspc = snap.indices.get("^GSPC")
        if gspc is not None and gspc.source_date != expected_date:
            warnings.append(
                f"美股行情的日期是 {gspc.source_date}，不是預期的 {expected_date}，"
                "可能不是最新收盤（美股休市時屬正常）")
    except Exception as exc:  # noqa: BLE001 - 行情失敗不能讓整個工作失敗
        market["status"] = "unavailable"
        market["error"] = f"{type(exc).__name__}: {exc}"
        warnings.append(f"行情取得失敗：{market['error']}")
    return market, warnings


def _pct(row: dict) -> str:
    return f"{row['pct_change']:+.2f}%"


def notification_text(brief: dict) -> str:
    """第一行標題，其餘為內文。"""
    lines = [f"美股隔夜重點（{brief['date'][5:]}）"]
    market = brief["market"]
    rows = {r["symbol"]: r for r in market["indices"] + market["stocks"]}
    parts = [f"{label} {_pct(rows[sym])}"
             for sym, label in (("^GSPC", "S&P 500"), ("^IXIC", "NASDAQ"),
                                ("^SOX", "費半"), ("TSM", "台積電 ADR")) if sym in rows]
    lines.append("、".join(parts) if market["status"] == "ok" and parts else "行情暫時取不到")
    points = brief["summary"].get("points") or []
    if points:
        first = points[0]
        lines.append(str(first.get("text", first)) if isinstance(first, dict) else str(first))
    else:
        n = len(brief["news"]["candidates"])
        lines.append(f"模型摘要尚未可用；候選新聞 {n} 則，詳見系統匣或儀表板")
    if brief["warnings"]:
        lines.append(f"注意：{brief['warnings'][0]}")
    return "\n".join(lines)


def build_brief(root: Path, *, now: dt.datetime, slot: str, macro_loader=None, notifier=None,
                notify: bool = True, summarize=None, collect_results: Optional[dict] = None) -> dict:
    root = Path(root)
    start = window_start(now)
    expected_date = previous_weekday(now.date()).isoformat()
    warnings: list = []

    rows, latest = _read_archives(root, start, now)
    rss_in_window = _window_us_items(rows, start, now)
    api_rows, api_files = read_api_news(root, start, now)
    in_window = _window_us_items(rows + api_rows, start, now)  # RSS 在前，同 url 以 RSS 為準
    rss_ids = {id(i) for _, i in rss_in_window}
    api_in_window = [i for _, i in in_window if id(i) not in rss_ids]
    api_sources: dict = {}
    for name in sorted(set(api_files) | set(collect_results or {})):
        api_sources[name] = {
            "items_in_window": sum(1 for i in api_in_window if _source_of(i) == name),
            "files": api_files.get(name, {}).get("files", []),
            "collect": (collect_results or {}).get(name),
        }
    for name, summary in (collect_results or {}).items():
        msg = _collect_warning(name, summary) if isinstance(summary, dict) else None
        if msg:
            warnings.append(msg)
    gaps, gap_warnings = detect_gaps(rows, start, now)  # 只看 RSS
    warnings += gap_warnings
    collected_at = latest[1] if latest else ""
    if latest is None:
        warnings.append("找不到任何 RSS 存檔，新聞可能完全沒有收到")
    elif now - latest[0] > dt.timedelta(minutes=STALE_ARCHIVE_MINUTES):
        warnings.append(f"RSS 存檔最後更新於 {collected_at}，已超過 {STALE_ARCHIVE_MINUTES} 分鐘，新聞可能不是最新")
    if not in_window:
        warnings.append("視窗內沒有任何美國新聞項目")

    if macro_loader is None:
        from bot.market_macro import fetch_macro_snapshot

        def macro_loader():
            return fetch_macro_snapshot(root=root, force_refresh=True)
    market, market_warnings = _market_section(macro_loader, now, expected_date)
    warnings += market_warnings

    day = now.date().isoformat()
    out_dir = root / "data" / "overnight_brief"
    existing = read_json(out_dir / f"{day}.json", {})
    if not isinstance(existing, dict):
        existing = {}
    at = now.isoformat(timespec="seconds")
    brief = {
        "date": day,
        "generated_at": at,
        "slot": slot,
        "runs": list(existing.get("runs") or []) + [{"slot": slot, "at": at}],
        "window": {"start": start.isoformat(), "end": now.isoformat(timespec="seconds")},
        "market": market,
        "news": {
            "items_in_window": len(in_window),
            "rss_items_in_window": len(rss_in_window),
            "api_items_in_window": len(api_in_window),
            "api_sources": api_sources,
            "feeds_in_window": len({i.get("feed") for _, i in in_window if i.get("feed")}),
            "archive_collected_at": collected_at,
            "gaps": gaps,
            "candidates": build_candidates(in_window),
        },
        "summary": {"status": "pending_model", "points": [], "model": "", "generated_at": "",
                    "note": SUMMARY_PENDING_NOTE},
        "warnings": warnings,
        "notified_at": str(existing.get("notified_at") or ""),
    }
    if summarize is not None:
        try:
            brief["summary"] = summarize(brief)
        except Exception as exc:  # noqa: BLE001
            brief["summary"] = {"status": "failed", "points": [], "model": "", "generated_at": "",
                                "note": f"{type(exc).__name__}: {exc}"}

    if notify and slot == "morning" and not brief["notified_at"]:
        try:
            if notifier is None:
                from bot.config import Settings
                from bot.local_notify import LocalNotifier
                notifier = LocalNotifier(Settings().local_notify_enabled)
            notifier.send(notification_text(brief))
            brief["notified_at"] = at
        except Exception:  # noqa: BLE001 - 通知失敗不能讓工作失敗
            pass

    atomic_json(out_dir / f"{day}.json", brief)
    atomic_json(out_dir / "latest.json", brief)
    return brief


def main(argv=None) -> int:
    configure_utf8_stdio()
    p = argparse.ArgumentParser(description="產生美股隔夜重點 JSON")
    p.add_argument("--root", default=".")
    p.add_argument("--slot", choices=("morning", "preopen"), required=True)
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--no-collect", action="store_true", help="不先跑 API 新聞收集")
    args = p.parse_args(argv)
    root = Path(args.root)
    now = now_tw()
    collect_results = None
    if not args.no_collect:
        try:
            collect_results = run_api_collectors(root, now)
        except Exception:  # noqa: BLE001
            get_logger("overnight_brief", log_dir=root / "log").exception("API 新聞收集失敗")
    try:
        brief = build_brief(root, now=now, slot=args.slot, notify=not args.no_notify,
                            collect_results=collect_results)
    except Exception:  # noqa: BLE001
        get_logger("overnight_brief", log_dir=root / "log").exception("美股隔夜重點產生失敗")
        return 2
    print(json.dumps({
        "date": brief["date"], "slot": brief["slot"], "market": brief["market"]["status"],
        "items_in_window": brief["news"]["items_in_window"],
        "api_items_in_window": brief["news"]["api_items_in_window"],
        "api_sources": {k: {"items_in_window": v["items_in_window"],
                            "collect": (v["collect"] or {}).get("status", "")}
                        for k, v in brief["news"]["api_sources"].items()},
        "candidates": len(brief["news"]["candidates"]), "gaps": len(brief["news"]["gaps"]),
        "summary": brief["summary"]["status"], "warnings": brief["warnings"],
        "notified_at": brief["notified_at"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
