"""auto_llm -- 全自動為個股拉公開素材 + 上網搜尋 + 餵 LLM 做研究。

設計目標
========
過去 LLM 法說分析必須使用者**手動貼逐字稿**才會跑。本模組改為：
**只要設了 ``GEMINI_API_KEY``，就「無人值守」自動跑**。

工作流程
========
1. **自動素材彙整**
   * MOPS 最近一年「重大訊息」標題 (公開、免費)
   * 鉅亨網今日台股新聞中與此 ticker 相關的條目
   * **法說會行事曆**：自動拉本月 ±N 月，列「過去 90 天 + 未來 30 天」
   * **網頁搜尋** (DuckDuckGo HTML + Google News RSS) — 多 query 聚合
   * **法說簡報原文**：讀 ``data/mops_downloads`` / ``mops_cache`` 的 TXT/HTML/PDF；
     不足時可自動下載行事曆上的近期簡報（上限 N 份）並抽字
   * **量化證據**：本地基本面（月營收/PER/季報）、近 N 日籌碼、主動 ETF 共識
2. **LLM 結構化分析** — 使用 ``research_ticker`` prompt（含 ``presentation_block``、``quant_block``）
3. **回退機制** — 若無 research_ticker 結果且 ``AUTO_LLM_ALLOW_LEGACY=1``，退回 ``analyze_presentation``
4. **可選 logic_check** — 若呼叫者帶入 ChipsContext，會接著跑言行反查
5. 結果寫到 ``data/auto_llm/<ticker>.json``，並會被 ``ticker_view.build_snapshot``
   讀回掛在 ``snap.llm_analysis``
6. 每日只重跑一次 (依 fetched_at + max_age 判斷)，避免浪費 API quota

注意事項
========
* 沒設 ``GEMINI_API_KEY`` 會 graceful-skip
* 任一步素材失敗 (網路 / 端點異動) 都不會 raise，只是該段缺失
* 所有 LLM 呼叫都會被 llm_log 寫進 JSONL & stock_db.llm_analysis_history
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot import watchlist as wl
from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.config import Settings
from bot.conference_calendar import update_calendar, upcoming_tickers
from bot.env_io import load_env
from bot.evidence import make_evidence
from bot.llm_analyzer import (
    ChipsContext,
    DEFAULT_MODEL,
    GeminiClient,
    LogicCheckResult,
    PresentationAnalysis,
    analyze_presentation,
    extract_json,
    gemini_call,
    logic_check,
    validate_presentation_payload,
)
from bot.mops_scraper import MaterialInfo, fetch_material_info
from bot.news_fetcher import NewsItem, fetch_today_news
from bot.utils import get_logger, mk_folder, now_tw


AUTO_LLM_DIR_REL = "data/auto_llm"
DEFAULT_AGE_HOURS = 12          # 同一個 ticker 12 小時內不重跑
MIN_INPUT_CHARS = 80            # 素材少於此字數視為「沒料」，不浪費 API
PRESENTATION_MAX_CHARS = 12000  # 餵給 research_ticker 的簡報原文上限
MAX_AUTO_PRESENTATION_DOWNLOADS = 2  # 自動下載近期簡報份數上限
MIN_PRESENTATION_CHARS = 200    # 少於此字數視為抽字失敗／無實質內容
QUANT_BLOCK_MAX_CHARS = 4000
DEFAULT_CHIP_DAYS_FOR_QUANT = 5


# ----------------------------------------------------------------------
# 素材收集
# ----------------------------------------------------------------------


def _gather_material_info(
    ticker: str,
    *,
    logger: logging.Logger,
    limit: int = 12,
) -> List[MaterialInfo]:
    """抓最近一年的 MOPS 重大訊息 (最近 N 筆)。"""
    try:
        items = fetch_material_info(ticker, logger=logger)
    except Exception:
        logger.exception("[%s] fetch_material_info 失敗", ticker)
        return []
    items.sort(key=lambda m: m.date, reverse=True)
    return items[:limit]


def _gather_news(
    ticker: str,
    name_hint: str,
    *,
    root: Optional[Path],
    logger: logging.Logger,
    limit: int = 15,
) -> List[NewsItem]:
    """從鉅亨今日台股新聞挑出與此 ticker 相關的新聞。"""
    try:
        news = fetch_today_news(limit=200, root=root, logger=logger)
    except Exception:
        logger.exception("[%s] fetch_today_news 失敗", ticker)
        return []
    out: List[NewsItem] = []
    for n in news:
        hit = ticker in (n.related_tickers or [])
        if not hit and ticker in (n.title or "") + " " + (n.summary or ""):
            hit = True
        if not hit and name_hint and name_hint in (n.title or "") + " " + (n.summary or ""):
            hit = True
        if hit:
            out.append(n)
        if len(out) >= limit:
            break
    return out


def _gather_pipeline_text(
    ticker: str,
    *,
    root: Path,
    max_chars: int = PRESENTATION_MAX_CHARS,
) -> str:
    """讀本地法說會 PDF/TXT/HTML，抽出純文字供 LLM 使用。"""
    from bot.mops_scraper import extract_pdf_text

    raw_dirs = [
        root / "data" / "mops_downloads",
        root / "data" / "mops_cache",
    ]
    files: List[Path] = []
    for d in raw_dirs:
        if not d.exists():
            continue
        for f in d.rglob(f"*{ticker}*"):
            if f.is_file() and f.suffix.lower() in (".txt", ".html", ".pdf"):
                files.append(f)
    # 較新檔案優先
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

    chunks: List[str] = []
    for f in files:
        text = ""
        try:
            if f.suffix.lower() in (".txt", ".html"):
                text = f.read_text(encoding="utf-8", errors="ignore")
            elif f.suffix.lower() == ".pdf":
                pt = extract_pdf_text(f)
                text = (pt.text if pt else "") or ""
        except Exception:
            continue
        text = (text or "").strip()
        if len(text) >= MIN_PRESENTATION_CHARS:
            chunks.append(f"[{f.name}]\n{text}")
        if sum(len(c) for c in chunks) >= max_chars:
            break
    if not chunks:
        return ""
    joined = "\n\n--- 來源切換 ---\n\n".join(chunks)
    return joined[:max_chars]


def _download_recent_presentations(
    ticker: str,
    conference_entries: List[Any],
    *,
    root: Path,
    logger: logging.Logger,
    max_downloads: int = MAX_AUTO_PRESENTATION_DOWNLOADS,
    max_chars: int = PRESENTATION_MAX_CHARS,
) -> str:
    """對行事曆上具有效簡報連結的近期法說，自動下載並抽字。

    只處理過去場次（newest first），上限 ``max_downloads``，避免拖垮 MOPS。
    """
    from bot.mops_scraper import (
        download_presentation,
        is_meaningful_presentation_url,
    )

    today = now_tw().date()
    candidates: List[Any] = []
    for e in conference_entries:
        url = getattr(e, "presentation_url", "") or ""
        if not is_meaningful_presentation_url(url):
            continue
        edate = getattr(e, "date", None)
        if edate is None:
            continue
        try:
            if hasattr(edate, "isoformat"):
                d = edate
            else:
                d = dt.date.fromisoformat(str(edate)[:10])
        except Exception:
            continue
        if d > today:
            continue
        candidates.append(e)
    candidates.sort(
        key=lambda e: e.date if hasattr(e.date, "isoformat") else str(e.date),
        reverse=True,
    )

    dest_dir = root / "data" / "mops_downloads"
    mk_folder(str(dest_dir))
    chunks: List[str] = []
    downloaded = 0
    for e in candidates:
        if downloaded >= max_downloads:
            break
        url = e.presentation_url
        try:
            date_str = e.date.isoformat() if hasattr(e.date, "isoformat") else str(e.date)[:10]
        except Exception:
            date_str = "unknown"
        dest = dest_dir / f"{ticker}_{date_str}_auto.pdf"
        try:
            pt = download_presentation(url, dest, logger=logger)
        except Exception:
            logger.exception("[%s] 自動下載法說簡報失敗: %s", ticker, url)
            continue
        if pt is None or not (pt.text or "").strip():
            continue
        text = pt.text.strip()
        if len(text) < MIN_PRESENTATION_CHARS:
            continue
        chunks.append(f"[{date_str} 簡報]\n{text}")
        downloaded += 1
        if sum(len(c) for c in chunks) >= max_chars:
            break
    if not chunks:
        return ""
    return "\n\n--- 來源切換 ---\n\n".join(chunks)[:max_chars]


def _resolve_presentation_block(
    ticker: str,
    *,
    root: Path,
    calendar_data: Dict[str, List[Any]],
    logger: logging.Logger,
    enable_download: bool = True,
    max_chars: int = PRESENTATION_MAX_CHARS,
) -> str:
    """合併本地法說原文；不足時可選自動下載近期簡報。"""
    local = _gather_pipeline_text(ticker, root=root, max_chars=max_chars)
    if len(local) >= MIN_PRESENTATION_CHARS:
        return local
    if not enable_download:
        return local
    past = list(calendar_data.get("past") or [])
    downloaded = _download_recent_presentations(
        ticker, past, root=root, logger=logger, max_chars=max_chars,
    )
    if not downloaded:
        return local
    if local:
        return (local + "\n\n--- 來源切換 ---\n\n" + downloaded)[:max_chars]
    return downloaded


def _format_fundamentals_quant(fund: Any) -> List[str]:
    """把 FundamentalSnapshot 壓成短行列表。"""
    lines: List[str] = []
    if fund is None or not getattr(fund, "has_data", False):
        return lines
    lines.append("【基本面】")
    val = getattr(fund, "valuation", None)
    if val is not None:
        lines.append(
            f"- 估值 ({getattr(val, 'date', '') or '最新'}): "
            f"PER={getattr(val, 'pe_ratio', 0):.1f} "
            f"PBR={getattr(val, 'pb_ratio', 0):.2f} "
            f"殖利率={getattr(val, 'dividend_yield', 0):.2f}%"
        )
    latest_rev = None
    try:
        latest_rev = fund.latest_revenue()
    except Exception:
        latest_rev = None
    if latest_rev is not None:
        streak = 0
        try:
            streak = int(fund.revenue_yoy_streak())
        except Exception:
            streak = 0
        lines.append(
            f"- 月營收 {latest_rev.year}/{latest_rev.month:02d}: "
            f"YoY={latest_rev.yoy:+.1f}% MoM={latest_rev.mom:+.1f}% "
            f"累計YoY={latest_rev.cum_yoy:+.1f}% "
            f"(連續 YoY>0：{streak} 月)"
        )
    qs = list(getattr(fund, "quarterlies", None) or [])
    if qs:
        latest_q = sorted(qs, key=lambda q: (q.year, q.quarter))[-1]
        lines.append(
            f"- 季報 {latest_q.year}Q{latest_q.quarter}: "
            f"EPS={latest_q.eps:.2f} "
            f"毛利率={latest_q.gross_margin:.1f}% "
            f"營益率={latest_q.operating_margin:.1f}% "
            f"淨利率={latest_q.net_margin:.1f}%"
        )
    divs = list(getattr(fund, "dividends", None) or [])
    if divs:
        latest_d = sorted(divs, key=lambda d: d.year)[-1]
        lines.append(
            f"- 股利 {latest_d.year}: 現金={latest_d.cash_dividend:.2f} "
            f"股票={latest_d.stock_dividend:.2f}"
        )
    return lines


def _format_chips_quant(
    chips: Optional[ChipsContext] = None,
    *,
    summary: Optional[Any] = None,
) -> List[str]:
    """籌碼摘要短行。優先用 ChipsContext，否則 ChipSummary。"""
    foreign = trust = dealer = margin = borrow = block = None
    days = None
    if chips is not None:
        foreign = chips.foreign_net
        trust = chips.investment_trust_net
        dealer = chips.dealer_net
        margin = chips.margin_buy_change_pct
        borrow = chips.short_borrow_change_pct
        block = chips.block_trade_net
        notes = getattr(chips, "notes", "") or ""
        if "最近" in notes:
            days = notes
    elif summary is not None:
        foreign = summary.foreign_net
        trust = summary.investment_trust_net
        dealer = summary.dealer_net
        margin = summary.margin_buy_change_pct
        borrow = summary.short_borrow_change_pct
        block = summary.block_trade_net
        days = f"近 {summary.days} 日"
    else:
        return []
    label = days or "近幾日"
    return [
        "【籌碼面】",
        f"- {label}: 外資淨買超={foreign:,.0f} "
        f"投信={trust:,.0f} 自營商={dealer:,.0f}",
        f"- 融資餘額變動={margin:+.1f}% 借券賣出變動={borrow:+.1f}% "
        f"鉅額淨額={block:,.0f}",
    ]


def _format_etf_consensus_quant(ticker: str, *, root: Path) -> List[str]:
    """只讀本地 ETF 持股快照，組共識／新建倉／加碼短行。"""
    try:
        from bot.active_etf import list_holdings_dates, load_active_etfs, load_holdings
        from bot.etf_consensus import (
            build_consensus,
            consensus_additions,
            consensus_new_builds,
            diff_snapshots,
        )
    except Exception:
        return []

    try:
        etfs = load_active_etfs(root)
    except Exception:
        return []
    if not etfs:
        return []

    etf_meta = {e.symbol: e for e in etfs}
    latest_holds: Dict[str, Any] = {}
    prev_holds: Dict[str, Any] = {}
    for e in etfs:
        try:
            dates = list_holdings_dates(e.symbol, root)
        except Exception:
            continue
        if not dates:
            continue
        try:
            latest = load_holdings(e.symbol, dates[0], root)
        except Exception:
            latest = None
        if latest:
            latest_holds[e.symbol] = latest
        if len(dates) >= 2:
            try:
                prev = load_holdings(e.symbol, dates[1], root)
            except Exception:
                prev = None
            if prev:
                prev_holds[e.symbol] = prev

    if not latest_holds:
        return []

    lines: List[str] = ["【主動 ETF 共識】"]
    consensus = build_consensus(latest_holds, etf_meta, min_etf_count=1)
    match = next((c for c in consensus if c.ticker == ticker), None)
    if match:
        etf_names = ", ".join(
            (etf_meta[w.etf_symbol].name if w.etf_symbol in etf_meta else w.etf_symbol)
            for w in match.held_by[:6]
        )
        lines.append(
            f"- 被 {match.etf_count} 檔持有，合計權重≈{match.total_weight:.2f}% "
            f"({etf_names}{'…' if match.etf_count > 6 else ''})"
        )
    else:
        lines.append("- 目前未出現在已抓取的主動 ETF 持股清單")

    changes = []
    for sym, after in latest_holds.items():
        before = prev_holds.get(sym)
        if before:
            changes.extend(diff_snapshots(before, after))
    if changes:
        news = [s for s in consensus_new_builds(changes, min_etfs=1) if s.ticker == ticker]
        adds = [s for s in consensus_additions(changes, min_etfs=1) if s.ticker == ticker]
        for s in news:
            lines.append(
                f"- 訊號：新建倉共識 ({s.etf_count} 檔) "
                f"權重Δ={s.total_weight_delta:+.2f}% — {s.note}"
            )
        for s in adds:
            lines.append(
                f"- 訊號：加碼共識 ({s.etf_count} 檔) "
                f"權重Δ={s.total_weight_delta:+.2f}% — {s.note}"
            )
    return lines


def _build_quant_block(
    ticker: str,
    *,
    root: Path,
    logger: logging.Logger,
    chips: Optional[ChipsContext] = None,
    chip_days: int = DEFAULT_CHIP_DAYS_FOR_QUANT,
    refresh_fundamentals_if_empty: bool = True,
    max_chars: int = QUANT_BLOCK_MAX_CHARS,
) -> str:
    """組裝餵給 research_ticker 的量化證據純文字。失敗不 raise。"""
    parts: List[str] = []

    # 基本面：先讀 cache，空則可選補抓一次
    try:
        from bot.fundamentals_fetcher import build_fundamental_snapshot

        fund = build_fundamental_snapshot(
            ticker, root=root, refresh=False, logger=logger,
        )
        if refresh_fundamentals_if_empty and (
            fund is None or not fund.has_data
        ):
            fund = build_fundamental_snapshot(
                ticker, root=root, refresh=True, logger=logger,
            )
        parts.extend(_format_fundamentals_quant(fund))
    except Exception:
        logger.exception("[%s] quant 基本面組裝失敗", ticker)

    # 籌碼：呼叫端已帶 ChipsContext 則直接用；否則抓近 N 日
    try:
        if chips is not None:
            parts.extend(_format_chips_quant(chips=chips))
        else:
            from bot.chips_fetcher import build_chip_summary

            summary = build_chip_summary(
                ticker, days=chip_days, root=root, logger=logger,
            )
            parts.extend(_format_chips_quant(summary=summary))
    except Exception:
        logger.exception("[%s] quant 籌碼組裝失敗", ticker)

    # ETF 共識：純本地
    try:
        parts.extend(_format_etf_consensus_quant(ticker, root=root))
    except Exception:
        logger.exception("[%s] quant ETF 共識組裝失敗", ticker)

    text = "\n".join(parts).strip()
    if not text:
        return "(無量化快取；請先跑 stock-watch-fetch / stock-auto-research)"
    return text[:max_chars]


def _gather_calendar(
    ticker: str,
    *,
    root: Path,
    logger: logging.Logger,
) -> Dict[str, List[Any]]:
    """讀取/補抓法說會與全球科技事件，取出此 ticker 的相關項目。"""
    try:
        from bot.conference_calendar import (
            conferences_for_ticker,
            ensure_calendar_fresh,
            upcoming_conferences,
        )
        from bot.global_event_calendar import (
            ensure_global_events_fresh,
            format_global_event_text,
            global_events_for_ticker,
        )
        ensure_calendar_fresh(root=root, logger=logger)
        ensure_global_events_fresh(root=root, logger=logger)
        today = now_tw().date()
        all_for_ticker = conferences_for_ticker(ticker, root=root)
        upcoming = [
            e for e in all_for_ticker
            if e.date >= today and (e.date - today).days <= 60
        ]
        past = [
            e for e in all_for_ticker
            if e.date < today and (today - e.date).days <= 365
        ]
        ge = global_events_for_ticker(ticker, lookahead=60, lookback=2, root=root)
        global_upcoming = ge.get("upcoming") or []
        global_recent = ge.get("recent") or []
        upcoming_all = upcoming_conferences(days=14, root=root)
        return {
            "upcoming": upcoming,
            "past": past,
            "global_upcoming": global_upcoming,
            "global_recent": global_recent,
            "global_upcoming_text": format_global_event_text(global_upcoming),
            "global_recent_text": format_global_event_text(global_recent),
            "near_market_size": len(upcoming_all) + len(global_upcoming),
        }
    except Exception:
        logger.exception("[%s] 行事曆整合失敗", ticker)
        return {
            "upcoming": [], "past": [],
            "global_upcoming": [], "global_recent": [],
            "global_upcoming_text": "(無)", "global_recent_text": "(無)",
            "near_market_size": 0,
        }


def _gather_web(
    ticker: str,
    name_hint: str,
    *,
    root: Path,
    logger: logging.Logger,
) -> Optional["object"]:
    """跑網頁搜尋並抓部分原文 (回 WebMaterial)。"""
    try:
        from bot.web_search import research_ticker as web_research

        return web_research(
            ticker, name=name_hint, root=root, logger=logger,
        )
    except Exception:
        logger.exception("[%s] 網頁搜尋失敗", ticker)
        return None


# ----------------------------------------------------------------------
# 組合輸入文本
# ----------------------------------------------------------------------


def _format_calendar_text(items: List[Any]) -> str:
    if not items:
        return "(無)"
    parts: List[str] = []
    for e in items:
        try:
            date_str = e.date.isoformat() if hasattr(e.date, "isoformat") else str(e.date)
        except Exception:
            date_str = str(e.date)
        line = f"- {date_str} {e.time or ''} {e.ticker} {e.company}".rstrip()
        if getattr(e, "note", ""):
            line += f" — {e.note}"
        url = getattr(e, "presentation_url", "") or ""
        if url:
            line += f" | 簡報: {url}"
        parts.append(line)
    return "\n".join(parts)


def _format_materials(materials: List[MaterialInfo]) -> str:
    if not materials:
        return "(無)"
    parts: List[str] = []
    for m in materials:
        try:
            date_str = m.date.isoformat() if hasattr(m.date, "isoformat") else str(m.date)
        except Exception:
            date_str = str(m.date)
        parts.append(f"- {date_str} {m.subject}")
    return "\n".join(parts)


def _format_news(news: List[NewsItem]) -> str:
    if not news:
        return "(無)"
    parts: List[str] = []
    for n in news:
        line = f"- [{n.category or 'news'}] {n.title}"
        if n.summary:
            line += f" — {n.summary[:200]}"
        parts.append(line)
    return "\n".join(parts)


def _truncate_text(text: str, max_chars: int) -> str:
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars] + "\n...(已截斷)"


def _calendar_lines(items: List[Any], *, include_url: bool = True) -> List[str]:
    lines: List[str] = []
    for e in items:
        try:
            date_str = e.date.isoformat() if hasattr(e.date, "isoformat") else str(e.date)
        except Exception:
            date_str = str(e.date)
        line = f"- {date_str} {getattr(e, 'time', '') or ''} {getattr(e, 'ticker', '')} {getattr(e, 'company', '')}".rstrip()
        note = getattr(e, "note", "") or ""
        if note:
            line += f" — {note}"
        if include_url:
            url = getattr(e, "presentation_url", "") or ""
            if url:
                line += f" | 簡報: {url}"
        lines.append(line)
    return lines


def _build_source_sections(
    ticker: str,
    *,
    materials: List[MaterialInfo],
    news: List[NewsItem],
    calendar_data: Dict[str, List[Any]],
    web_material: Optional[Any],
    pipeline_text: str,
    composed_preview: str = "",
    quant_block: str = "",
    logger: logging.Logger,
    detail_limit: int = 5,
    detail_max_chars: int = 3000,
) -> Dict[str, Any]:
    """組合結構化原始素材，供 dashboard 顯示與快取。"""
    from bot.mops_scraper import fetch_material_detail

    upcoming = calendar_data.get("upcoming") or []
    past = calendar_data.get("past") or []

    mops_items: List[Dict[str, Any]] = []
    evidence: List[Dict[str, Any]] = []
    for m in materials[:12]:
        date_str = m.date.isoformat() if hasattr(m.date, "isoformat") else str(m.date)
        mops_items.append({
            "date": date_str,
            "time": m.time,
            "subject": m.subject,
            "detail_url": m.detail_url or "",
            "detail": "",
        })
        evidence.append(make_evidence(
            "mops_material",
            source="mops",
            source_ref=m.detail_url or f"{ticker}:{date_str}:{m.subject}",
            title=m.subject,
            url=m.detail_url or "",
            observed_at=date_str,
            excerpt=m.subject,
        ))

    fetched = 0
    for item in mops_items:
        if fetched >= detail_limit:
            break
        url = str(item.get("detail_url") or "")
        if not url:
            continue
        try:
            detail = fetch_material_detail(url, logger=logger, max_chars=detail_max_chars)
            if detail:
                item["detail"] = detail
                fetched += 1
        except Exception:
            logger.exception("[%s] fetch_material_detail 失敗", ticker)

    news_items = [
        {
            "title": n.title,
            "summary": _truncate_text(n.summary or "", 500),
            "category": n.category or "",
        }
        for n in news[:15]
    ]
    for n in news[:15]:
        evidence.append(make_evidence(
            "news",
            source="cnyes",
            source_ref=str(getattr(n, "url", "") or getattr(n, "link", "") or n.title),
            title=n.title,
            url=str(getattr(n, "url", "") or getattr(n, "link", "") or ""),
            observed_at=str(getattr(n, "published_at", "") or getattr(n, "date", "") or ""),
            excerpt=n.summary or n.title,
        ))

    web_items: List[Dict[str, str]] = []
    if web_material is not None:
        for p in (web_material.pages or [])[:10]:
            web_items.append({
                "url": p.url,
                "title": p.title or "",
                "text": _truncate_text(p.text or "", detail_max_chars),
            })
            evidence.append(make_evidence(
                "web",
                source="web_search",
                source_ref=p.url,
                title=p.title or "",
                url=p.url,
                excerpt=p.text or "",
            ))

    if pipeline_text:
        evidence.append(make_evidence(
            "presentation_text",
            source="local_presentation",
            source_ref=f"{ticker}:presentation",
            title=f"{ticker} presentation text",
            excerpt=pipeline_text,
        ))
    if quant_block and quant_block.strip() not in {"", "(無)"}:
        evidence.append(make_evidence(
            "quant",
            source="local_quant",
            source_ref=f"{ticker}:quant",
            title=f"{ticker} quant block",
            excerpt=quant_block,
        ))

    preview = composed_preview.strip()
    if not preview:
        parts: List[str] = []
        up_lines = _calendar_lines(upcoming)
        past_lines = _calendar_lines(past[:12])
        global_up_text = calendar_data.get("global_upcoming_text") or ""
        global_recent_text = calendar_data.get("global_recent_text") or ""
        if up_lines:
            parts.append("【即將舉行的法說會】\n" + "\n".join(up_lines))
        if global_up_text and global_up_text != "(無)":
            parts.append("【即將發生的全球科技事件】\n" + global_up_text)
        if global_recent_text and global_recent_text != "(無)":
            parts.append("【近期全球科技事件】\n" + global_recent_text)
        if past_lines:
            parts.append("【過去法說會】\n" + "\n".join(past_lines))
        if materials:
            parts.append("【MOPS 重大訊息】\n" + _format_materials(materials))
        if quant_block and quant_block.strip() not in {"", "(無)"}:
            parts.append("【量化證據】\n" + quant_block)
        if news:
            parts.append("【鉅亨網新聞】\n" + _format_news(news))
        if web_material is not None:
            block = web_material.compact_text()
            if block:
                parts.append("【網路搜尋整理】\n" + block)
        if pipeline_text:
            parts.append("【既有法說 / 公開資料文本】\n" + pipeline_text)
        preview = "\n\n".join(parts)

    global_up_text = calendar_data.get("global_upcoming_text") or "(無)"
    global_recent_text = calendar_data.get("global_recent_text") or "(無)"
    return {
        "calendar_upcoming": "\n".join(_calendar_lines(upcoming)) or "(無)",
        "calendar_past": "\n".join(_calendar_lines(past[:12])) or "(無)",
        "global_events_upcoming": global_up_text,
        "global_events_recent": global_recent_text,
        "mops_materials": mops_items,
        "news": news_items,
        "web_search": web_items,
        "pipeline_text": _truncate_text(pipeline_text, detail_max_chars) if pipeline_text else "",
        "quant_block": _truncate_text(quant_block, detail_max_chars) if quant_block else "",
        "composed_preview": _truncate_text(preview, 8000),
        "evidence": evidence,
    }


def _compose_legacy_text(
    ticker: str,
    name: str,
    materials: List[MaterialInfo],
    news: List[NewsItem],
    pipeline_text: str,
    calendar_data: Dict[str, List[Any]],
    web_block: str,
) -> str:
    """退回到 analyze_presentation 用的長文 (沒有 research_ticker prompt 時)。"""
    parts: List[str] = []
    parts.append(
        f"以下為 {ticker} {name} 近期公開資訊綜整 "
        "(自動彙整：MOPS 重大訊息 + 鉅亨新聞 + MOPS 法說會行事曆 + 網頁搜尋 + 既有法說文件)。"
    )
    upcoming = calendar_data.get("upcoming") or []
    past = calendar_data.get("past") or []
    global_up_text = calendar_data.get("global_upcoming_text") or "(無)"
    global_recent_text = calendar_data.get("global_recent_text") or "(無)"
    if upcoming:
        parts.append("\n【即將舉行的法說會 (未來 60 天)】")
        parts.append(_format_calendar_text(upcoming))
    if global_up_text and global_up_text != "(無)":
        parts.append("\n【即將發生的全球科技事件】")
        parts.append(global_up_text)
    if global_recent_text and global_recent_text != "(無)":
        parts.append("\n【近期全球科技事件 (含昨日發表)】")
        parts.append(global_recent_text)
    if past:
        parts.append("\n【過去 365 天舉行過的法說會】")
        parts.append(_format_calendar_text(past[:8]))
    if materials:
        parts.append("\n【MOPS 重大訊息】")
        parts.append(_format_materials(materials))
    if news:
        parts.append("\n【鉅亨網新聞 (今日)】")
        parts.append(_format_news(news))
    if web_block:
        parts.append("\n【網路搜尋整理】")
        parts.append(web_block)
    if pipeline_text:
        parts.append("\n【既有法說 / 公開資料文本】")
        parts.append(pipeline_text)
    return "\n".join(parts).strip()


# ----------------------------------------------------------------------
# 快取
# ----------------------------------------------------------------------


def _cache_path(ticker: str, root: Path) -> Path:
    return root / AUTO_LLM_DIR_REL / f"{ticker}.json"


def load_cached_auto_analysis(
    ticker: str,
    root: Path,
    *,
    max_age_hours: int = DEFAULT_AGE_HOURS,
) -> Optional[Dict[str, Any]]:
    """讀取仍有效的快取 (None 表示過期或不存在)。"""
    p = _cache_path(ticker, root)
    restore_file_from_cloud(p, root=root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    ts = data.get("fetched_at", "")
    if not ts:
        return data
    try:
        fetched = dt.datetime.fromisoformat(ts.replace("Z", ""))
        if (now_tw().replace(tzinfo=None) - fetched).total_seconds() > max_age_hours * 3600:
            return None
    except Exception:
        pass
    return data


def _save_cache(ticker: str, root: Path, payload: Dict[str, Any]) -> Path:
    p = _cache_path(ticker, root)
    mk_folder(str(p.parent))
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    mirror_file_to_cloud(p, root=root)
    return p


# ----------------------------------------------------------------------
# LLM 呼叫 — research_ticker 優先，退回 analyze_presentation
# ----------------------------------------------------------------------


def _try_research_ticker(
    ticker: str,
    *,
    name: str,
    upcoming: List[Any],
    past: List[Any],
    materials: List[MaterialInfo],
    news: List[NewsItem],
    web_block: str,
    client: GeminiClient,
    logger: logging.Logger,
    global_upcoming_text: str = "(無)",
    global_recent_text: str = "(無)",
    presentation_block: str = "",
    quant_block: str = "",
    memory_context: str = "",
) -> Optional[Dict[str, Any]]:
    """嘗試呼叫 ``research_ticker`` prompt；若 registry 沒有此 prompt 回 None。"""
    try:
        from bot.prompt_registry import get_registry
        reg = get_registry()
        if reg.get("research_ticker") is None:
            return None
    except Exception:
        return None

    past_events: List[str] = []
    if past:
        past_events.append("【法說會 (過去 365 天)】")
        past_events.append(_format_calendar_text(past[:8]))
    if materials:
        past_events.append("【MOPS 重大訊息 (近一年)】")
        past_events.append(_format_materials(materials))
    if memory_context:
        past_events.append("【歷史研究記憶（僅供參考，需以本次來源重新驗證）】")
        past_events.append(memory_context)
    past_block = "\n".join(past_events).strip() or "(無)"

    upcoming_parts: List[str] = []
    if upcoming:
        upcoming_parts.append("【法說會】\n" + _format_calendar_text(upcoming))
    if global_upcoming_text and global_upcoming_text != "(無)":
        upcoming_parts.append("【全球科技事件 (未來)】\n" + global_upcoming_text)
    if global_recent_text and global_recent_text != "(無)":
        upcoming_parts.append("【全球科技事件 (近期/進行中)】\n" + global_recent_text)
    upcoming_block = "\n\n".join(upcoming_parts).strip() or "(無)"
    news_block = _format_news(news) if news else "(無)"
    presentation = (presentation_block or "").strip() or "(無)"
    quant = (quant_block or "").strip() or "(無)"

    raw, info = gemini_call(
        "research_ticker",
        client=client,
        metadata={"ticker": ticker, "task": "research_ticker"},
        ticker=ticker,
        name=name or "Unknown",
        today=now_tw().date().isoformat(),
        upcoming_events=upcoming_block,
        past_events=past_block,
        presentation_block=presentation,
        quant_block=quant,
        news_block=news_block,
        web_block=web_block or "(無)",
    )
    if raw is None:
        return None
    data = extract_json(raw) or {}
    if not isinstance(data, dict):
        return None
    return {
        "raw": raw,
        "info": info,
        "data": data,
    }


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def _load_research_memory_context(
    ticker: str,
    *,
    root: Path,
    max_chars: int = 6000,
    logger: Optional[logging.Logger] = None,
) -> str:
    """取回同一 ticker 的可靠短／長記憶，並保留分層標籤供 LLM 分辨。"""
    try:
        from bot.memory_store import MemoryStore
        from bot.stock_db import StockDB

        context = MemoryStore(StockDB.open(root=root)).build_context(
            ticker,
            scope_type="ticker",
            scope_id=ticker,
            max_chars=max_chars,
            per_layer_limit=8,
        )
        sections: List[str] = []
        for label, rows in (("短期", context.short), ("長期", context.long)):
            if not rows:
                continue
            sections.append(f"【{label}記憶】")
            for row in rows:
                text = row.content or row.summary
                sections.append(f"[{row.memory_id}] {row.summary}\n{text[:1800]}")
        return "\n\n".join(sections)[:max_chars]
    except Exception:
        if logger is not None:
            logger.debug("[%s] 讀取研究記憶失敗", ticker, exc_info=True)
        return ""


def auto_analyze_ticker(
    ticker: str,
    *,
    root: Path,
    name_hint: str = "",
    client: Optional[GeminiClient] = None,
    model: str = DEFAULT_MODEL,
    force_refresh: bool = False,
    max_age_hours: int = DEFAULT_AGE_HOURS,
    enable_web_search: bool = True,
    enable_calendar: bool = True,
    enable_presentation_download: bool = True,
    enable_quant: bool = True,
    chips: Optional[ChipsContext] = None,
    chip_days: int = DEFAULT_CHIP_DAYS_FOR_QUANT,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    """全自動為個股做 LLM 法說/情緒分析 + 反查。

    Returns:
        ``dict`` 與 ``snap.llm_analysis`` 結構同：
          ticker / summary / sentiment / sentiment_score / confidence /
          key_metrics / growth_drivers / risks / capex_signal / margin_outlook /
          catalyst_outlook / news_sentiment / evidence_sources /
          prompt_id / prompt_version / model / source / fetched_at /
          source_materials / logic_check (可選)

        ``GEMINI_API_KEY`` 未設定或素材太少時回 None。
    """
    log = logger or get_logger("auto-llm")

    if not force_refresh:
        cached = load_cached_auto_analysis(ticker, root, max_age_hours=max_age_hours)
        if cached:
            return cached

    if client is None:
        api_key = load_env().get("GEMINI_API_KEY", "")
        if not api_key:
            log.info("[%s] 未設 GEMINI_API_KEY，自動 LLM 跳過", ticker)
            return None
        client = GeminiClient(api_key=api_key, model=model, logger=log)
    if not client.enabled:
        log.info("[%s] GeminiClient 未啟用，自動 LLM 跳過", ticker)
        return None

    materials = _gather_material_info(ticker, logger=log)
    news = _gather_news(ticker, name_hint=name_hint, root=root, logger=log)
    calendar_data: Dict[str, List[Any]] = (
        _gather_calendar(ticker, root=root, logger=log)
        if enable_calendar else {
            "upcoming": [], "past": [],
            "global_upcoming_text": "(無)", "global_recent_text": "(無)",
        }
    )
    pipeline_text = _resolve_presentation_block(
        ticker,
        root=root,
        calendar_data=calendar_data,
        logger=log,
        enable_download=enable_presentation_download,
    )
    web_material = (
        _gather_web(ticker, name_hint, root=root, logger=log)
        if enable_web_search else None
    )
    web_block = web_material.compact_text() if web_material is not None else ""
    memory_context = _load_research_memory_context(
        ticker, root=root, logger=log,
    )
    quant_block = ""
    if enable_quant:
        quant_block = _build_quant_block(
            ticker,
            root=root,
            logger=log,
            chips=chips,
            chip_days=chip_days,
        )

    res = _try_research_ticker(
        ticker,
        name=name_hint,
        upcoming=calendar_data.get("upcoming") or [],
        past=calendar_data.get("past") or [],
        materials=materials, news=news, web_block=web_block,
        client=client, logger=log,
        global_upcoming_text=str(calendar_data.get("global_upcoming_text") or "(無)"),
        global_recent_text=str(calendar_data.get("global_recent_text") or "(無)"),
        presentation_block=pipeline_text,
        quant_block=quant_block,
        memory_context=memory_context,
    )

    analysis: Optional[PresentationAnalysis] = None
    payload: Dict[str, Any]
    if res is not None:
        data = res["data"]
        info = res["info"]
        normalized, validation_errors = validate_presentation_payload(data)
        raw_evidence_sources = data.get("evidence_sources", []) if isinstance(data, dict) else []
        if not isinstance(raw_evidence_sources, list):
            raw_evidence_sources = []
        payload = {
            "ticker": ticker,
            "summary": normalized["summary"],
            "sentiment": normalized["sentiment"],
            "sentiment_score": normalized["sentiment_score"],
            "confidence": normalized["confidence"],
            "key_metrics": normalized["key_metrics"],
            "growth_drivers": normalized["growth_drivers"],
            "risks": normalized["risks"],
            "capex_signal": normalized["capex_signal"],
            "margin_outlook": normalized["margin_outlook"],
            "catalyst_outlook": str(data.get("catalyst_outlook", "")),
            "news_sentiment": str(data.get("news_sentiment", "")),
            "evidence_sources": [str(x)[:500] for x in raw_evidence_sources[:30]],
            "validation_errors": validation_errors,
            "prompt_id": info.get("prompt_id", "research_ticker"),
            "prompt_version": info.get("prompt_version", ""),
            "model": client.model,
            "source": "auto_research_v2",
        }
        analysis = PresentationAnalysis(
            ticker=ticker,
            summary=payload["summary"],
            sentiment=payload["sentiment"],
            sentiment_score=payload["sentiment_score"],
            confidence=payload["confidence"],
            key_metrics=payload["key_metrics"],
            growth_drivers=payload["growth_drivers"],
            risks=payload["risks"],
            capex_signal=payload["capex_signal"],
            margin_outlook=payload["margin_outlook"],
            model=client.model,
            prompt_id=payload["prompt_id"],
            prompt_version=payload["prompt_version"],
            raw_response=res["raw"],
            validation_errors=validation_errors,
        )
    else:
        allow_legacy = str(
            load_env().get("AUTO_LLM_ALLOW_LEGACY", "0") or "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not allow_legacy:
            log.info("[%s] 無 research_ticker 結果且 AUTO_LLM_ALLOW_LEGACY=0，跳過", ticker)
            return None
        composed = _compose_legacy_text(
            ticker, name_hint, materials, news, pipeline_text,
            calendar_data, web_block,
        )
        if len(composed) < MIN_INPUT_CHARS:
            log.info("[%s] 自動素材不足 (%d 字)，跳過 LLM", ticker, len(composed))
            return None
        analysis = analyze_presentation(
            composed, ticker=ticker, client=client, logger=log,
        )
        if not analysis.enabled:
            return None
        payload = {
            "ticker": analysis.ticker,
            "summary": analysis.summary,
            "sentiment": analysis.sentiment,
            "sentiment_score": analysis.sentiment_score,
            "confidence": analysis.confidence,
            "key_metrics": analysis.key_metrics,
            "growth_drivers": analysis.growth_drivers,
            "risks": analysis.risks,
            "capex_signal": analysis.capex_signal,
            "margin_outlook": analysis.margin_outlook,
            "prompt_id": analysis.prompt_id,
            "prompt_version": analysis.prompt_version,
            "model": analysis.model,
            "source": "auto_llm_legacy",
        }

    composed_for_preview = ""
    if res is None:
        composed_for_preview = _compose_legacy_text(
            ticker, name_hint, materials, news, pipeline_text,
            calendar_data, web_block,
        )
    payload["source_materials"] = {
        "mops_count": len(materials),
        "news_count": len(news),
        "pipeline_text_chars": len(pipeline_text),
        "quant_block_chars": len(quant_block),
        "upcoming_conferences": len(calendar_data.get("upcoming") or []),
        "past_conferences": len(calendar_data.get("past") or []),
        "web_search_results": len(web_material.search_results) if web_material else 0,
        "web_pages_fetched": len(web_material.pages) if web_material else 0,
    }
    payload["source_sections"] = _build_source_sections(
        ticker,
        materials=materials,
        news=news,
        calendar_data=calendar_data,
        web_material=web_material,
        pipeline_text=pipeline_text,
        composed_preview=composed_for_preview,
        quant_block=quant_block,
        logger=log,
    )
    payload["evidence"] = payload["source_sections"].get("evidence", [])
    payload["fetched_at"] = now_tw().isoformat(timespec="seconds")

    if chips is not None and analysis is not None:
        try:
            lc = logic_check(analysis, chips, client=client, logger=log)
            payload["logic_check"] = {
                "verdict": lc.verdict,
                "reasoning": lc.reasoning,
                "confidence": lc.confidence,
                "suggestion": lc.suggestion,
                "prompt_id": lc.prompt_id,
                "prompt_version": lc.prompt_version,
            }
        except Exception:
            log.exception("[%s] auto logic_check 失敗", ticker)

    try:
        _save_cache(ticker, root, payload)
    except Exception:
        log.exception("[%s] auto LLM 快取寫入失敗", ticker)

    # 只有新版 research_ticker 且 schema validation 無錯誤時才升級成長期記憶。
    # legacy/fallback 仍保留 JSONL 與 auto_llm cache，但不進正式長期知識層。
    if res is not None and not payload.get("validation_errors"):
        try:
            from bot.memory_store import MemoryStore
            from bot.stock_db import StockDB

            memory_db = StockDB.open(root=root)
            fetched_at = str(payload.get("fetched_at") or now_tw().isoformat(timespec="seconds"))
            MemoryStore(memory_db).put_long(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                memory_id=f"auto-research:{ticker}:{fetched_at}",
                memory_type="research_summary",
                scope_type="ticker",
                scope_id=ticker,
                subject=ticker,
                summary=str(payload.get("summary") or ""),
                keywords=[ticker, str(payload.get("sentiment") or ""), "research_ticker"],
                importance=0.8,
                confidence=float(payload.get("confidence") or 0.0),
                quality_status="ok",
                source="auto_llm",
                source_ref=f"{ticker}:{fetched_at}",
                source_record_id=f"auto-research:{ticker}:{fetched_at}",
                evidence=payload.get("evidence") or [],
                metadata={
                    "prompt_id": payload.get("prompt_id", ""),
                    "prompt_version": payload.get("prompt_version", ""),
                    "model": payload.get("model", ""),
                },
                observed_at=fetched_at,
            )
        except Exception:
            log.exception("[%s] 長期研究記憶寫入失敗", ticker)
    return payload


def _source_sections_has_content(sections: Optional[Dict[str, Any]]) -> bool:
    """快取可能含空結構（truthy dict 但無實質內容）。"""
    if not sections:
        return False
    for key in ("calendar_upcoming", "calendar_past", "llm_input_preview"):
        val = str(sections.get(key) or "").strip()
        if val and val not in {"(無)", "(即時素材；執行自動研究可取得完整新聞與網頁原文)"}:
            return True
    for key in ("mops_materials", "news", "web_search"):
        if sections.get(key):
            return True
    return False


def build_live_source_sections(
    ticker: str,
    root: Path,
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    """不呼叫 LLM，從行事曆快取與 MOPS 重訊組合即時原始素材。"""
    log = logger or get_logger("auto-llm")
    try:
        from bot.conference_calendar import conferences_for_ticker
        from bot.mops_scraper import fetch_material_info
        from bot.utils import now_tw

        today = now_tw().date()
        all_conf = conferences_for_ticker(ticker, root=root)
        upcoming = [e for e in all_conf if e.date >= today]
        past = [e for e in all_conf if e.date < today]
        past.sort(key=lambda e: e.date, reverse=True)
        materials = fetch_material_info(ticker, logger=log)
        if not upcoming and not past and not materials:
            return None
        mops_items = [
            {
                "date": m.date.isoformat() if hasattr(m.date, "isoformat") else str(m.date),
                "time": m.time,
                "subject": m.subject,
                "detail_url": m.detail_url or "",
                "detail": "",
            }
            for m in materials[:12]
        ]
        return {
            "calendar_upcoming": "\n".join(_calendar_lines(upcoming)) or "(無)",
            "calendar_past": "\n".join(_calendar_lines(past[:12])) or "(無)",
            "mops_materials": mops_items,
            "news": [],
            "web_search": [],
            "llm_input_preview": "(即時素材；執行自動研究可取得完整新聞與網頁原文)",
            "_source": "live",
        }
    except Exception:
        log.exception("[%s] build_live_source_sections 失敗", ticker)
        return None


def patch_material_detail_in_cache(
    ticker: str,
    root: Path,
    *,
    detail_url: str,
    text: str,
) -> bool:
    """將 MOPS 重訊內文寫回 auto_llm 快取 ``source_sections``。"""
    if not detail_url or not text:
        return False
    cached = load_cached_auto_analysis(ticker, root, max_age_hours=999999)
    if not cached:
        return False
    sections = dict(cached.get("source_sections") or {})
    items = list(sections.get("mops_materials") or [])
    updated = False
    for item in items:
        if str(item.get("detail_url") or "") == detail_url:
            item["detail"] = text
            updated = True
            break
    if not updated:
        return False
    sections["mops_materials"] = items
    cached["source_sections"] = sections
    _save_cache(ticker, root, cached)
    return True


def resolve_llm_bundle(
    ticker: str,
    root: Path,
    *,
    pipeline_analysis: Optional[Dict[str, Any]] = None,
    auto_llm: bool = False,
    name_hint: str = "",
    logger: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """合併 pipeline 摘要與 auto_llm 原始素材。

    - ``analysis``：優先 ``pipeline_analysis``，否則 auto_llm 快取 / 自動分析
    - ``source_sections``：來自 auto_llm 快取；僅在 **無** pipeline 摘要時才觸發自動分析
    """
    log = logger or get_logger("auto-llm")
    analysis: Optional[Dict[str, Any]] = pipeline_analysis
    source_sections: Dict[str, Any] = {}

    cached_any = load_cached_auto_analysis(ticker, root, max_age_hours=999999)
    if cached_any:
        source_sections = dict(cached_any.get("source_sections") or {})

    if analysis is None:
        if cached_any:
            analysis = cached_any
        elif auto_llm:
            fresh = auto_analyze_ticker(
                ticker, root=root, name_hint=name_hint, logger=log,
            )
            if fresh:
                analysis = fresh
                source_sections = dict(fresh.get("source_sections") or {})
    elif not _source_sections_has_content(source_sections) and cached_any:
        source_sections = dict(cached_any.get("source_sections") or {})
    if not _source_sections_has_content(source_sections):
        live = build_live_source_sections(ticker, root, logger=log)
        if live:
            source_sections = live

    return {
        "analysis": analysis,
        "source_sections": source_sections if _source_sections_has_content(source_sections) else None,
    }


def auto_research_ticker(
    ticker: str,
    *,
    root: Path,
    name_hint: str = "",
    days: int = 5,
    force_refresh: bool = True,
    refresh_calendar: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    """完整版自動研究：行事曆刷新 → 抓籌碼 → 自動分析 + 反查。

    比 ``auto_analyze_ticker`` 更主動：會強制刷新 + 抓籌碼面 + 強制跑反查。
    被 ``stock-auto-research --llm-only`` 與 dashboard「自動研究」按鈕使用。
    """
    log = logger or get_logger("auto-llm")
    if refresh_calendar:
        try:
            from bot.conference_calendar import update_calendar
            from bot.global_event_calendar import update_global_events
            update_calendar(root=root, logger=log)
            update_global_events(root=root, logger=log)
        except Exception:
            log.exception("行事曆更新失敗 (忽略)")

    chips_ctx: Optional[ChipsContext] = None
    try:
        from bot.chips_fetcher import build_chip_summary, summary_to_chips_context
        summary = build_chip_summary(ticker, days=days, root=root, logger=log)
        chips_ctx = summary_to_chips_context(summary)
    except Exception:
        log.exception("[%s] 自動研究：籌碼面抓取失敗 (忽略)", ticker)

    return auto_analyze_ticker(
        ticker,
        root=root,
        name_hint=name_hint,
        force_refresh=force_refresh,
        chips=chips_ctx,
        chip_days=days,
        logger=log,
    )


def _parse_llm_tickers(args_tickers: List[str]) -> List[str]:
    out: List[str] = []
    for raw in args_tickers:
        for t in str(raw).split(","):
            t = t.strip()
            if t and t not in out:
                out.append(t)
    return out


def _load_watchlist_tickers(root: Path) -> List[str]:
    try:
        items = wl.load(root).items
    except Exception:
        return []
    return [i.ticker for i in items if i.ticker]


def _name_hint_for(ticker: str, root: Path) -> str:
    try:
        for it in wl.load(root).items:
            if it.ticker == ticker:
                return it.name or ""
    except Exception:
        pass
    try:
        from bot.stock_db import get_db
        info = get_db().get_stock_info(ticker)
        if info and info.name:
            return info.name
    except Exception:
        pass
    return ""


def _append_research_log(root: Path, entry: Dict[str, object]) -> None:
    log_path = root / "data" / "auto_llm" / "research_log.jsonl"
    mk_folder(str(log_path.parent))
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        mirror_file_to_cloud(log_path, root=root)
    except Exception:
        pass


def run_llm_research_batch(
    *,
    tickers: List[str],
    root: Path,
    upcoming: bool = False,
    upcoming_days: int = 14,
    no_calendar: bool = False,
    refresh: bool = False,
    chip_days: int = 5,
    logger: Optional[logging.Logger] = None,
) -> int:
    """對多檔個股跑 auto_research_ticker（供 stock-auto-research --llm-only 使用）。"""
    log = logger or get_logger("llm-research")
    settings = Settings()

    if not settings.gemini_api_key:
        log.error(
            "未設定 GEMINI_API_KEY；無法跑自動研究。請到 .env 或 dashboard「組態設定」填入。"
        )
        return 2

    if not no_calendar:
        log.info("=== 更新 MOPS 法說會行事曆 ===")
        try:
            summary = update_calendar(root=root, logger=log)
            log.info("行事曆更新完成: %s", summary)
        except Exception:
            log.exception("行事曆更新失敗 (繼續)")

    work = list(tickers)
    if not work:
        work = _load_watchlist_tickers(root)
        if work:
            log.info("未指定 tickers，使用 watchlist 共 %d 檔", len(work))
    if upcoming:
        extra = upcoming_tickers(days=upcoming_days, root=root)
        try:
            from bot.global_event_calendar import upcoming_tickers_from_global_events
            extra.extend(upcoming_tickers_from_global_events(days=upcoming_days, root=root))
        except Exception:
            log.debug("upcoming_tickers_from_global_events 失敗", exc_info=True)
        new_add = [t for t in extra if t not in work]
        work.extend(new_add)
        log.info(
            "加入未來 %d 天有法說會或全球科技事件的 %d 檔: %s",
            upcoming_days, len(new_add), new_add[:10],
        )

    if not work:
        log.warning("沒有任何 ticker 可以分析。請傳參數或建立 watchlist。")
        return 1

    log.info("=== 開始自動研究 %d 檔 ===", len(work))
    okay = 0
    failed = 0
    for i, ticker in enumerate(work, 1):
        name = _name_hint_for(ticker, root)
        log.info("[%d/%d] 研究 %s %s ...", i, len(work), ticker, name)
        try:
            result = auto_research_ticker(
                ticker,
                root=root,
                name_hint=name,
                days=chip_days,
                force_refresh=refresh,
                refresh_calendar=False,
                logger=log,
            )
        except Exception:
            log.exception("[%s] 自動研究例外", ticker)
            failed += 1
            _append_research_log(root, {
                "ts": now_tw().isoformat(timespec="seconds"),
                "ticker": ticker, "status": "exception",
            })
            continue
        if result is None:
            failed += 1
            _append_research_log(root, {
                "ts": now_tw().isoformat(timespec="seconds"),
                "ticker": ticker, "status": "no_result",
            })
            continue
        okay += 1
        _append_research_log(root, {
            "ts": now_tw().isoformat(timespec="seconds"),
            "ticker": ticker,
            "status": "ok",
            "sentiment": result.get("sentiment"),
            "sentiment_score": result.get("sentiment_score"),
            "confidence": result.get("confidence"),
            "catalyst_outlook": result.get("catalyst_outlook", "")[:80],
            "logic_verdict": (result.get("logic_check") or {}).get("verdict"),
        })

    log.info("=== 完成：成功 %d 檔 / 失敗 %d 檔 ===", okay, failed)
    return 0 if okay > 0 else 2


__all__ = [
    "auto_analyze_ticker",
    "auto_research_ticker",
    "load_cached_auto_analysis",
    "run_llm_research_batch",
]
