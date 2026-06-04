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
   * 若有先前 pipeline 跑過的法說 PDF 文字，也一併納入
2. **LLM 結構化分析** — 使用新版 ``research_ticker`` prompt
3. **回退機制** — 若無 research_ticker prompt，會退回 ``analyze_presentation``
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

import argparse
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
)
from bot.mops_scraper import MaterialInfo, fetch_material_info
from bot.news_fetcher import NewsItem, fetch_today_news
from bot.utils import get_logger, mk_folder, now_tw


AUTO_LLM_DIR_REL = "data/auto_llm"
DEFAULT_AGE_HOURS = 12          # 同一個 ticker 12 小時內不重跑
MIN_INPUT_CHARS = 80            # 素材少於此字數視為「沒料」，不浪費 API


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
    max_chars: int = 12000,
) -> str:
    """如果之前 pipeline 跑過法說會 PDF/TXT，把純文字撈回來。"""
    raw_dirs = [
        root / "data" / "mops_downloads",
        root / "data" / "mops_cache",
    ]
    chunks: List[str] = []
    for d in raw_dirs:
        if not d.exists():
            continue
        for f in d.rglob(f"*{ticker}*"):
            if not f.is_file():
                continue
            try:
                if f.suffix.lower() in (".txt", ".html"):
                    text = f.read_text(encoding="utf-8", errors="ignore")
                else:
                    continue
            except Exception:
                continue
            if text:
                chunks.append(text.strip())
    if not chunks:
        return ""
    joined = "\n\n--- 來源切換 ---\n\n".join(chunks)
    return joined[:max_chars]


def _gather_calendar(
    ticker: str,
    *,
    root: Path,
    logger: logging.Logger,
) -> Dict[str, List[Any]]:
    """讀取/補抓法說會行事曆並取出此 ticker 的相關項目。"""
    try:
        from bot.conference_calendar import (
            conferences_for_ticker,
            ensure_calendar_fresh,
            upcoming_conferences,
        )
        ensure_calendar_fresh(root=root, logger=logger)
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
        upcoming_all = upcoming_conferences(days=14, root=root)
        return {
            "upcoming": upcoming,
            "past": past,
            "near_market_size": len(upcoming_all),
        }
    except Exception:
        logger.exception("[%s] 行事曆整合失敗", ticker)
        return {"upcoming": [], "past": [], "near_market_size": 0}


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
    if upcoming:
        parts.append("\n【即將舉行的法說會 (未來 60 天)】")
        parts.append(_format_calendar_text(upcoming))
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
    past_block = "\n".join(past_events).strip() or "(無)"

    upcoming_block = _format_calendar_text(upcoming) if upcoming else "(無)"
    news_block = _format_news(news) if news else "(無)"

    raw, info = gemini_call(
        "research_ticker",
        client=client,
        metadata={"ticker": ticker, "task": "research_ticker"},
        ticker=ticker,
        name=name or "Unknown",
        today=now_tw().date().isoformat(),
        upcoming_events=upcoming_block,
        past_events=past_block,
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
    chips: Optional[ChipsContext] = None,
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
    pipeline_text = _gather_pipeline_text(ticker, root=root)
    calendar_data: Dict[str, List[Any]] = (
        _gather_calendar(ticker, root=root, logger=log)
        if enable_calendar else {"upcoming": [], "past": []}
    )
    web_material = (
        _gather_web(ticker, name_hint, root=root, logger=log)
        if enable_web_search else None
    )
    web_block = web_material.compact_text() if web_material is not None else ""

    res = _try_research_ticker(
        ticker,
        name=name_hint,
        upcoming=calendar_data.get("upcoming") or [],
        past=calendar_data.get("past") or [],
        materials=materials, news=news, web_block=web_block,
        client=client, logger=log,
    )

    analysis: Optional[PresentationAnalysis] = None
    payload: Dict[str, Any]
    if res is not None:
        data = res["data"]
        info = res["info"]
        payload = {
            "ticker": ticker,
            "summary": str(data.get("summary", "")),
            "sentiment": str(data.get("sentiment", "neutral")),
            "sentiment_score": float(data.get("sentiment_score", 0.0)),
            "confidence": float(data.get("confidence", 0.5)),
            "key_metrics": dict(data.get("key_metrics", {}) or {}),
            "growth_drivers": list(data.get("growth_drivers", []) or []),
            "risks": list(data.get("risks", []) or []),
            "capex_signal": str(data.get("capex_signal", "")),
            "margin_outlook": str(data.get("margin_outlook", "")),
            "catalyst_outlook": str(data.get("catalyst_outlook", "")),
            "news_sentiment": str(data.get("news_sentiment", "")),
            "evidence_sources": list(data.get("evidence_sources", []) or []),
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

    payload["source_materials"] = {
        "mops_count": len(materials),
        "news_count": len(news),
        "pipeline_text_chars": len(pipeline_text),
        "upcoming_conferences": len(calendar_data.get("upcoming") or []),
        "past_conferences": len(calendar_data.get("past") or []),
        "web_search_results": len(web_material.search_results) if web_material else 0,
        "web_pages_fetched": len(web_material.pages) if web_material else 0,
    }
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
    return payload


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
    被 ``stock-llm-research`` CLI 與 dashboard「自動研究」按鈕使用。
    """
    log = logger or get_logger("auto-llm")
    if refresh_calendar:
        try:
            from bot.conference_calendar import update_calendar
            update_calendar(root=root, logger=log)
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
    """對多檔個股跑 auto_research_ticker（原 stock-llm-research 核心邏輯）。"""
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
        new_add = [t for t in extra if t not in work]
        work.extend(new_add)
        log.info(
            "加入未來 %d 天有法說會的 %d 檔: %s",
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


def llm_research_main(argv: Optional[List[str]] = None) -> int:
    """CLI 入口（``stock-llm-research``，已 deprecated，請改用 ``stock-auto-research --llm-only``）。"""
    parser = argparse.ArgumentParser(
        prog="stock-llm-research",
        description=(
            "全自動 LLM 個股研究 (deprecated：請改用 stock-auto-research --llm-only)"
        ),
    )
    parser.add_argument(
        "tickers", nargs="*",
        help="個股代號 (可逗號分隔；無參數則跑 watchlist)",
    )
    parser.add_argument("--upcoming", action="store_true", help="納入未來有法說會的個股")
    parser.add_argument("--upcoming-days", type=int, default=14)
    parser.add_argument("--no-calendar", action="store_true")
    parser.add_argument("--no-web", action="store_true", help="保留相容，目前由 auto_research_ticker 控制")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--days", type=int, default=5, help="籌碼面回顧天數")
    args = parser.parse_args(argv)

    if args.no_web:
        get_logger("llm-research").warning("--no-web 已廢棄，行為與完整研究相同")

    return run_llm_research_batch(
        tickers=_parse_llm_tickers(args.tickers),
        root=Path.cwd(),
        upcoming=args.upcoming,
        upcoming_days=args.upcoming_days,
        no_calendar=args.no_calendar,
        refresh=args.refresh,
        chip_days=args.days,
    )


__all__ = [
    "auto_analyze_ticker",
    "auto_research_ticker",
    "load_cached_auto_analysis",
    "llm_research_main",
    "run_llm_research_batch",
]
