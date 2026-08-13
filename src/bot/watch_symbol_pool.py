"""合併四源關注清單為 BOT 監控池。

來源（隨交易日 T 追加）:
  1. intraday_prev   — T-1 的今日當沖戰情室 (asof=T-1)
  2. intraday_today  — T 的今日當沖戰情室 (asof=T)
  3. nextday_prev    — 昨日產出、目標為 T 的明日當沖關注 (target=T)
  4. live_open       — T 開盤後即時調查 (TWSE 報價 + 早盤戰情室追蹤)

手動 ``SYMBOLS`` 永遠保留（標籤 manual），並可與四源聯集。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bot.ownership import effective_trading_blacklist
from bot.trade_cost import max_affordable_qty
from bot.utils import get_logger, now_tw

if TYPE_CHECKING:
    from bot.config import Settings

_MARKET_OPEN = dt.time(9, 0)
_MARKET_REFRESH_END = dt.time(13, 30)

SOURCE_INTRADAY_PREV = "intraday_prev"
SOURCE_INTRADAY_TODAY = "intraday_today"
SOURCE_NEXTDAY_PREV = "nextday_prev"
SOURCE_LIVE_OPEN = "live_open"
SOURCE_MANUAL = "manual"


def prev_trading_day(asof: dt.date) -> dt.date:
    """回傳 asof 之前最近一個非週末交易日。"""
    cur = asof - dt.timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= dt.timedelta(days=1)
    return cur


def _clean_ticker(value: object) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def _ranking_tickers(
    report: dict | None,
    *,
    top_n: int,
    score_key: str = "day_trade_score",
) -> list[tuple[str, float]]:
    if not report or top_n <= 0:
        return []
    rows = report.get("rankings") or []
    scored: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = _clean_ticker(row.get("ticker"))
        if not ticker:
            continue
        score = float(row.get(score_key) or row.get("next_day_score") or 0.0)
        scored.append((ticker, score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:top_n]


def _price_hint_from_report(report: dict | None, ticker: str) -> float:
    if not report:
        return 0.0
    for row in report.get("rankings") or []:
        if not isinstance(row, dict):
            continue
        if _clean_ticker(row.get("ticker")) != ticker:
            continue
        for key in ("today_close", "close", "price"):
            val = row.get(key)
            if val is not None:
                try:
                    price = float(val)
                    if price > 0:
                        return price
                except (TypeError, ValueError):
                    continue
    return 0.0


def _affordable(settings: Settings, price: float) -> bool:
    if price <= 0:
        return True
    budget = float(settings.effective_fund_cap())
    if budget <= 0:
        return True
    lot_qty = max_affordable_qty(
        price,
        budget,
        "lot",
        max_qty=settings.max_lot_per_symbol,
        settings=settings,
    )
    if lot_qty >= 1:
        return True
    if getattr(settings, "use_odd_lot", False):
        share_qty = max_affordable_qty(
            price,
            budget,
            "share",
            max_qty=getattr(settings, "odd_lot_max_shares", 999),
            settings=settings,
        )
        return share_qty >= 1
    return False


@dataclass
class WatchPoolResult:
    symbols: list[str] = field(default_factory=list)
    sources_by_symbol: dict[str, list[str]] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    trading_day: str = ""
    skipped_blacklist: list[str] = field(default_factory=list)
    skipped_budget: list[str] = field(default_factory=list)


def _add_symbol(
    pool: dict[str, list[str]],
    ticker: str,
    source: str,
) -> None:
    if ticker not in pool:
        pool[ticker] = []
    if source not in pool[ticker]:
        pool[ticker].append(source)


def _live_open_tickers(
    root: Path,
    trading_day: dt.date,
    *,
    top_n: int,
    refresh_quotes: bool,
    logger: logging.Logger,
) -> list[tuple[str, float]]:
    from bot.intraday_live import build_live_tracking_rows, load_live_review
    from bot.intraday_pipeline import load_intraday_by_date

    report = load_intraday_by_date(root, trading_day)
    if not report:
        return []

    scored: list[tuple[str, float]] = []
    review = load_live_review(root, trading_day)
    if review:
        tracking = (review.get("tracking") or {})
        for row in tracking.get("rows") or []:
            if not isinstance(row, dict):
                continue
            ticker = _clean_ticker(row.get("ticker"))
            if not ticker:
                continue
            score = float(row.get("correctness_score") or row.get("initial_day_trade_score") or 0.0)
            scored.append((ticker, score))

    try:
        tracking = build_live_tracking_rows(
            report,
            root=root,
            max_tickers=max(top_n, 12),
            refresh_quotes=refresh_quotes,
            refresh_technicals=False,
            refresh_chips=False,
            refresh_news=False,
            include_news=False,
            logger=logger,
        )
    except Exception:
        logger.debug("live_open build_live_tracking_rows failed", exc_info=True)
        tracking = {"rows": []}

    seen = {t for t, _ in scored}
    for row in tracking.get("rows") or []:
        if not isinstance(row, dict):
            continue
        ticker = _clean_ticker(row.get("ticker"))
        if not ticker or ticker in seen:
            continue
        score = float(row.get("correctness_score") or row.get("initial_day_trade_score") or 0.0)
        scored.append((ticker, score))
        seen.add(ticker)

    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:top_n]


def resolve_watch_symbol_pool(
    settings: Settings,
    root: Path | None = None,
    *,
    asof: dt.datetime | None = None,
    include_live: bool | None = None,
    refresh_live_quotes: bool = False,
    logger: logging.Logger | None = None,
) -> WatchPoolResult:
    """解析四源關注清單並回傳合併結果（不修改 settings）。"""
    from bot.intraday_pipeline import load_intraday_by_date
    from bot.next_day_watch_pipeline import load_next_day_by_date

    log = logger or get_logger("watch-pool")
    root_path = root or Path.cwd()
    now = asof or now_tw()
    if isinstance(now, dt.datetime):
        trading_day = now.date()
        now_time = now.time()
    else:
        trading_day = now  # type: ignore[assignment]
        now_time = dt.time(12, 0)

    top_n = max(1, int(getattr(settings, "symbols_merge_top_n", 10)))
    max_total = max(1, int(getattr(settings, "symbols_merge_max_total", 24)))
    budget_filter = bool(getattr(settings, "symbols_merge_budget_filter", True))
    blacklist = effective_trading_blacklist(settings)

    pool: dict[str, list[str]] = {}
    price_hints: dict[str, float] = {}
    source_payloads: dict[str, list[tuple[str, float]]] = {}

    for raw in settings.symbols or []:
        ticker = _clean_ticker(raw)
        if ticker:
            _add_symbol(pool, ticker, SOURCE_MANUAL)

    prev_day = prev_trading_day(trading_day)
    intraday_prev = load_intraday_by_date(root_path, prev_day)
    intraday_today = load_intraday_by_date(root_path, trading_day)
    nextday_prev = load_next_day_by_date(root_path, trading_day, prefer_update=True)

    source_payloads[SOURCE_INTRADAY_PREV] = _ranking_tickers(
        intraday_prev, top_n=top_n, score_key="day_trade_score",
    )
    source_payloads[SOURCE_INTRADAY_TODAY] = _ranking_tickers(
        intraday_today, top_n=top_n, score_key="day_trade_score",
    )
    source_payloads[SOURCE_NEXTDAY_PREV] = _ranking_tickers(
        nextday_prev, top_n=top_n, score_key="next_day_score",
    )

    live_enabled = include_live
    if live_enabled is None:
        live_enabled = (
            trading_day.weekday() < 5
            and _MARKET_OPEN <= now_time <= _MARKET_REFRESH_END
        )
    if live_enabled:
        source_payloads[SOURCE_LIVE_OPEN] = _live_open_tickers(
            root_path,
            trading_day,
            top_n=top_n,
            refresh_quotes=refresh_live_quotes,
            logger=log,
        )
    else:
        source_payloads[SOURCE_LIVE_OPEN] = []

    for report in (intraday_prev, intraday_today, nextday_prev):
        if not report:
            continue
        for ticker, _ in _ranking_tickers(report, top_n=top_n, score_key="day_trade_score"):
            price_hints.setdefault(ticker, _price_hint_from_report(report, ticker))
        for ticker, _ in _ranking_tickers(report, top_n=top_n, score_key="next_day_score"):
            price_hints.setdefault(ticker, _price_hint_from_report(report, ticker))

    merge_order = (
        SOURCE_MANUAL,
        SOURCE_LIVE_OPEN,
        SOURCE_INTRADAY_TODAY,
        SOURCE_NEXTDAY_PREV,
        SOURCE_INTRADAY_PREV,
    )
    for source in merge_order:
        if source == SOURCE_MANUAL:
            continue
        for ticker, score in source_payloads.get(source, []):
            _add_symbol(pool, ticker, source)
            price_hints.setdefault(ticker, 0.0)
            if score and ticker in price_hints and price_hints[ticker] <= 0:
                price_hints[ticker] = 0.0

    manual_set = {
        t for t, tags in pool.items() if SOURCE_MANUAL in tags
    }
    skipped_blacklist: list[str] = []
    skipped_budget: list[str] = []
    ordered: list[str] = []

    def _try_add(ticker: str) -> None:
        if ticker in ordered:
            return
        if ticker in blacklist:
            skipped_blacklist.append(ticker)
            return
        if budget_filter and not _affordable(settings, price_hints.get(ticker, 0.0)):
            skipped_budget.append(ticker)
            return
        ordered.append(ticker)

    for ticker, tags in pool.items():
        if SOURCE_MANUAL in tags:
            _try_add(ticker)

    for source in merge_order:
        if source == SOURCE_MANUAL:
            continue
        for ticker, _ in source_payloads.get(source, []):
            _try_add(ticker)

    if len(ordered) > max_total:
        keep = [t for t in ordered if t in manual_set]
        for ticker in ordered:
            if ticker in keep:
                continue
            keep.append(ticker)
            if len(keep) >= max_total:
                break
        ordered = keep

    source_counts = {key: 0 for key in merge_order}
    for ticker in ordered:
        for tag in pool.get(ticker, []):
            if tag in source_counts:
                source_counts[tag] += 1

    return WatchPoolResult(
        symbols=ordered,
        sources_by_symbol={t: list(pool.get(t, [])) for t in ordered},
        source_counts=source_counts,
        trading_day=trading_day.isoformat(),
        skipped_blacklist=sorted(set(skipped_blacklist)),
        skipped_budget=sorted(set(skipped_budget)),
    )


def apply_watch_pool_to_settings(
    settings: Settings,
    result: WatchPoolResult,
) -> list[str]:
    """將合併結果寫入 settings.symbols，回傳新增代號。"""
    previous = list(settings.symbols or [])
    previous_set = set(previous)
    settings.symbols = list(result.symbols)
    return [s for s in result.symbols if s not in previous_set]


def merge_watch_symbols_into_settings(
    settings: Settings,
    root: Path | None = None,
    *,
    reason: str = "startup",
    include_live: bool | None = None,
    refresh_live_quotes: bool = False,
    logger: logging.Logger | None = None,
) -> WatchPoolResult:
    """解析四源並更新 settings.symbols；寫 log 供盤中對帳。"""
    log = logger or get_logger("watch-pool")
    result = resolve_watch_symbol_pool(
        settings,
        root,
        include_live=include_live,
        refresh_live_quotes=refresh_live_quotes,
        logger=log,
    )
    added = apply_watch_pool_to_settings(settings, result)
    log.info(
        "[監控池/%s] 交易日=%s 共 %d 檔 (新增 %d) 來源統計=%s",
        reason,
        result.trading_day,
        len(result.symbols),
        len(added),
        result.source_counts,
    )
    for ticker in result.symbols:
        tags = ",".join(result.sources_by_symbol.get(ticker, []))
        log.info("[監控池/%s] %s ← %s", reason, ticker, tags)
    if result.skipped_blacklist:
        log.info(
            "[監控池/%s] 黑名單略過: %s",
            reason, ",".join(result.skipped_blacklist),
        )
    if result.skipped_budget:
        log.info(
            "[監控池/%s] 預算不足略過: %s",
            reason, ",".join(result.skipped_budget),
        )
    return result


def in_watch_pool_refresh_window(asof: dt.datetime | None = None) -> bool:
    now = asof or now_tw()
    if now.weekday() >= 5:
        return False
    return _MARKET_OPEN <= now.time() <= _MARKET_REFRESH_END


__all__ = [
    "SOURCE_INTRADAY_PREV",
    "SOURCE_INTRADAY_TODAY",
    "SOURCE_LIVE_OPEN",
    "SOURCE_MANUAL",
    "SOURCE_NEXTDAY_PREV",
    "WatchPoolResult",
    "apply_watch_pool_to_settings",
    "in_watch_pool_refresh_window",
    "merge_watch_symbols_into_settings",
    "prev_trading_day",
    "resolve_watch_symbol_pool",
]
