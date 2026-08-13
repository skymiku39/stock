"""Intraday live tracking helpers.

This module keeps the dashboard live-tracking page local-first: automatic
refreshes update market-derived rows, while LLM review remains an explicit
manual action in the UI.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.news_fetcher import NewsItem, fetch_today_news
from bot.utils import get_logger, mk_folder, now_tw


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _clean_ticker(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def extract_llm_mentions(
    report: dict[str, Any],
    *,
    max_tickers: int = 20,
) -> list[dict[str, Any]]:
    """Extract tickers the intraday LLM report mentioned.

    Rankings are kept first because they are the action list. Theme-only
    candidates follow so the page still catches names that the LLM discussed
    but the scoring layer ranked lower.
    """
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def add(ticker: str, payload: dict[str, Any]) -> None:
        ticker = _clean_ticker(ticker)
        if not ticker:
            return
        if ticker not in seen:
            seen[ticker] = {"ticker": ticker, "sources": []}
            order.append(ticker)
        row = seen[ticker]
        for key, value in payload.items():
            if key == "sources":
                for source in value if isinstance(value, list) else [value]:
                    if source and source not in row["sources"]:
                        row["sources"].append(source)
            elif value not in (None, "", []):
                row.setdefault(key, value)

    for idx, row in enumerate(report.get("rankings") or [], start=1):
        if not isinstance(row, dict):
            continue
        add(str(row.get("ticker") or ""), {
            "name": row.get("name", ""),
            "theme": row.get("theme", ""),
            "theme_heat": row.get("theme_heat", 0),
            "role": row.get("role", ""),
            "rank": idx,
            "initial_day_trade_score": row.get("day_trade_score", 0),
            "initial_technical_score": row.get("technical_score", 50),
            "initial_pct_change": row.get("today_pct_change", 0),
            "initial_volume_ratio": row.get("volume_ratio", 0),
            "initial_action": row.get("action", ""),
            "sources": ["ranking"],
        })

    for theme in report.get("themes") or []:
        if not isinstance(theme, dict):
            continue
        theme_name = str(theme.get("theme") or "")
        heat = theme.get("heat", 0)
        for candidate in theme.get("candidate_tickers") or []:
            if isinstance(candidate, dict):
                ticker = str(candidate.get("ticker") or "")
                payload = {
                    "name": candidate.get("name", ""),
                    "theme": theme_name,
                    "theme_heat": heat,
                    "role": candidate.get("role", ""),
                    "sources": ["theme"],
                }
            else:
                ticker = str(candidate or "")
                payload = {"theme": theme_name, "theme_heat": heat, "sources": ["theme"]}
            add(ticker, payload)

    return [seen[t] for t in order[:max_tickers]]


def related_news_for_ticker(items: Iterable[NewsItem], ticker: str, *, limit: int = 3) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items:
        haystack = f"{item.title} {item.summary}"
        if ticker not in (item.related_tickers or []) and ticker not in haystack:
            continue
        out.append({
            "title": item.title,
            "published_at": item.published_at,
            "url": item.url,
        })
        if len(out) >= limit:
            break
    return out


def assess_tracking_status(row: dict[str, Any]) -> dict[str, Any]:
    """Rule-based check of whether the original intraday thesis is holding up."""
    initial_score = _as_float(row.get("initial_technical_score"), 50.0)
    current_score = _as_float(row.get("current_technical_score"), 0.0)
    initial_pct = _as_float(row.get("initial_pct_change"), 0.0)
    current_pct = _as_float(row.get("current_pct_change"), 0.0)
    score_delta = current_score - initial_score if current_score else 0.0

    if row.get("quote_date") and row.get("quote_date") != now_tw().date().isoformat():
        return {
            "status": "非今日資料",
            "correctness": "無法檢討",
            "correctness_score": 45,
            "reason": f"最新報價日期為 {row.get('quote_date')}，不是今天",
        }
    if not row.get("has_current_data"):
        return {
            "status": "資料不足",
            "correctness": "無法檢討",
            "correctness_score": 50,
            "reason": "目前沒有可用的刷新資料",
        }
    if row.get("current_pct_change") is None:
        return {
            "status": "缺漲跌基準",
            "correctness": "無法檢討",
            "correctness_score": 50,
            "reason": "目前有盤中價格，但缺少可計算今日漲跌幅的基準",
        }
    if current_score >= 62 and current_pct >= max(-0.3, initial_pct - 1.0):
        return {
            "status": "偏多延續",
            "correctness": "暫時驗證",
            "correctness_score": 78,
            "reason": "技術分維持偏多，漲跌幅未明顯背離早盤假設",
        }
    if current_score <= 42 or current_pct <= -2.0 or score_delta <= -15:
        return {
            "status": "轉弱警戒",
            "correctness": "需要修正",
            "correctness_score": 28,
            "reason": "即時技術分或漲跌幅明顯轉弱，原先題材承接可能失效",
        }
    if current_pct < initial_pct - 1.5:
        return {
            "status": "不如預期",
            "correctness": "部分失準",
            "correctness_score": 42,
            "reason": "漲跌幅相對早盤基準走弱，追價假設需降溫",
        }
    return {
        "status": "觀察中",
        "correctness": "尚待驗證",
        "correctness_score": 58,
        "reason": "尚未出現明確延續或失效訊號",
    }


def build_live_tracking_rows(
    report: dict[str, Any],
    *,
    root: Path | None = None,
    max_tickers: int = 12,
    refresh_quotes: bool = True,
    refresh_technicals: bool = False,
    refresh_chips: bool = False,
    refresh_news: bool = False,
    include_news: bool = True,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Build the auto-refresh table payload for today's LLM-mentioned stocks."""
    root_path = root or Path.cwd()
    log = logger or get_logger("intraday-live")
    mentions = extract_llm_mentions(report, max_tickers=max_tickers)
    tickers = [m["ticker"] for m in mentions]
    quote_map: dict[str, dict[str, Any]] = {}
    if refresh_quotes and tickers:
        try:
            from bot.market_source import TwsePublicMarketSource
            quote_map = TwsePublicMarketSource(tickers, logger=log).get_quotes()
        except Exception:
            log.debug("TWSE MIS quote refresh failed", exc_info=True)

    news_items: list[NewsItem] = []
    if include_news:
        try:
            news_items = fetch_today_news(
                limit=120,
                use_cache=not refresh_news,
                force_refresh=refresh_news,
                root=root_path,
                logger=log,
            )
        except Exception:
            log.debug("fetch news for intraday live failed", exc_info=True)
            news_items = []

    rows: list[dict[str, Any]] = []
    for mention in mentions:
        ticker = mention["ticker"]
        live: dict[str, Any] = dict(mention)
        live.update({
            "has_current_data": False,
            "current_last_date": "",
            "current_close": None,
            "current_pct_change": None,
            "current_volume_ratio": None,
            "current_technical_score": None,
            "score_delta": None,
            "quote_price": None,
            "quote_pct_change": None,
            "quote_volume": None,
            "quote_date": "",
            "quote_time": "",
            "quote_source": "",
            "quote_price_basis": "",
            "quote_best_bid": None,
            "quote_best_ask": None,
            "chip_text": "",
            "foreign_net": None,
            "investment_trust_net": None,
            "news_count": 0,
            "news_titles": [],
            "refreshed_at": now_tw().isoformat(timespec="seconds"),
        })

        quote = quote_map.get(ticker) or {}
        if quote:
            live.update({
                "has_current_data": quote.get("price") is not None,
                "current_last_date": quote.get("quote_date", ""),
                "current_close": quote.get("price"),
                "current_pct_change": quote.get("pct_chg"),
                "quote_price": quote.get("price"),
                "quote_pct_change": quote.get("pct_chg"),
                "quote_volume": quote.get("volume"),
                "quote_date": quote.get("quote_date", ""),
                "quote_time": quote.get("quote_time", ""),
                "quote_source": quote.get("source", ""),
                "quote_price_basis": quote.get("price_basis", ""),
                "quote_best_bid": quote.get("best_bid"),
                "quote_best_ask": quote.get("best_ask"),
                "quote_fetched_at": quote.get("fetched_at", ""),
            })

        if refresh_technicals:
            try:
                from bot.technicals import build_technical_snapshot
                snap, _df = build_technical_snapshot(
                    ticker,
                    months=4,
                    root=root_path,
                    refresh=refresh_technicals,
                    logger=log,
                )
                if getattr(snap, "has_data", False):
                    vol_ratio = 0.0
                    volume_for_ratio = live.get("quote_volume") or snap.volume_last
                    if snap.vol_ma20 and snap.vol_ma20 > 0 and volume_for_ratio:
                        vol_ratio = round(float(volume_for_ratio) / float(snap.vol_ma20), 2)
                    live.update({
                        "has_current_data": bool(live.get("has_current_data") or True),
                        "current_last_date": live.get("current_last_date") or snap.last_date,
                        "current_close": live.get("current_close") or snap.last_close,
                        "current_pct_change": (
                            live.get("current_pct_change")
                            if live.get("current_pct_change") is not None
                            else snap.pct_change_1d
                        ),
                        "current_volume_ratio": vol_ratio,
                        "current_technical_score": snap.technical_score,
                        "score_delta": round(
                            snap.technical_score - _as_float(live.get("initial_technical_score"), 50.0),
                            1,
                        ),
                        "candle_pattern": snap.candle_pattern,
                        "candle_bias": snap.candle_bias,
                        "signals": snap.signals[:5],
                        "technical_source": "refreshed" if refresh_technicals else "cache",
                    })
            except Exception:
                log.debug("[%s] technical refresh failed", ticker, exc_info=True)
        elif quote:
            live["current_technical_score"] = live.get("initial_technical_score")
            live["score_delta"] = 0.0
            live["technical_source"] = "morning_report"
        else:
            live["technical_source"] = "no_quote"

        if refresh_chips:
            try:
                from bot.chips_fetcher import build_chip_summary, summary_to_dict
                chip = summary_to_dict(
                    build_chip_summary(ticker, days=3, root=root_path, logger=log)
                )
                foreign = _as_float(chip.get("foreign_net"))
                trust = _as_float(chip.get("investment_trust_net"))
                live.update({
                    "foreign_net": foreign,
                    "investment_trust_net": trust,
                    "chip_text": f"外資 {foreign:+.0f} 張 / 投信 {trust:+.0f} 張",
                    "chip_summary": chip,
                })
            except Exception:
                log.debug("[%s] chip refresh failed", ticker, exc_info=True)

        related_news = related_news_for_ticker(news_items, ticker)
        live["news_count"] = len(related_news)
        live["news_titles"] = [n["title"] for n in related_news]
        live.update(assess_tracking_status(live))
        rows.append(live)

    return {
        "asof": now_tw().isoformat(timespec="seconds"),
        "report_asof": report.get("asof", ""),
        "rows": rows,
        "news_total": len(news_items),
        "max_tickers": max_tickers,
    }


def live_review_path(root: Path | None, report_date: dt.date | str) -> Path:
    date_iso = report_date.isoformat() if hasattr(report_date, "isoformat") else str(report_date)
    return (root or Path.cwd()) / "data" / "intraday" / date_iso / "live_review.md"


def live_review_json_path(root: Path | None, report_date: dt.date | str) -> Path:
    date_iso = report_date.isoformat() if hasattr(report_date, "isoformat") else str(report_date)
    return (root or Path.cwd()) / "data" / "intraday" / date_iso / "live_review.json"


def save_live_review(
    *,
    root: Path | None,
    report_date: dt.date | str,
    markdown: str,
    payload: dict[str, Any],
) -> None:
    md_path = live_review_path(root, report_date)
    json_path = live_review_json_path(root, report_date)
    mk_folder(str(md_path.parent))
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    mirror_file_to_cloud(md_path, root=root)
    mirror_file_to_cloud(json_path, root=root)


def load_live_review(root: Path | None, report_date: dt.date | str) -> dict[str, Any] | None:
    md_path = live_review_path(root, report_date)
    json_path = live_review_json_path(root, report_date)
    restore_file_from_cloud(md_path, root=root)
    restore_file_from_cloud(json_path, root=root)
    if not md_path.exists():
        return None
    out: dict[str, Any] = {"markdown": md_path.read_text(encoding="utf-8"), "path": str(md_path)}
    if json_path.exists():
        try:
            out.update(json.loads(json_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


__all__ = [
    "assess_tracking_status",
    "build_live_tracking_rows",
    "extract_llm_mentions",
    "live_review_json_path",
    "live_review_path",
    "load_live_review",
    "save_live_review",
]
