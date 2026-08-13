"""intraday_pipeline -- 「先定主題 → 找股 → 排序」的當沖戰情室主流程。

流程
====
1. 拉今日台股新聞 (news_fetcher)
2. 拉美股盤後 (market_macro)
3. 呼叫 LLM `theme_radar` → 萃取 5-8 個今日熱門題材 + 對應台股代號
4. 合併候選股 (題材股 + supply_chain + ETF 共識 + watchlist)
5. 對每檔算 day_trade 分數 (scoring.compute_scorecard)
6. 排序前 20 名 → 呼叫 LLM `intraday_brief` 寫戰情簡報
7. 全部結果存 `data/intraday/YYYY-MM-DD/`

輸出物件 (`IntradayReport`) 提供 dashboard / CLI 共用。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bot.cloud_file_cache import (
    mirror_file_to_cloud,
    restore_file_from_cloud,
    restore_tree_from_cloud,
)
from bot.config import Settings
from bot.events.pipeline_helpers import publish_pipeline_completed
from bot.llm_analyzer import GeminiClient, gemini_call
from bot.market_macro import (
    fetch_macro_snapshot,
    load_supply_chain,
    macro_to_dict,
    related_us_stocks_for_tw,
)
from bot.news_fetcher import fetch_today_news, news_to_compact_text
from bot.pipeline_shared import (
    consensus_tickers_today as _consensus_tickers_today,
)
from bot.pipeline_shared import (
    intraday_report_to_dict as _report_to_json,
)
from bot.pipeline_shared import (
    macro_summary_text as _macro_summary_text,
)
from bot.pipeline_shared import (
    parse_json_blob as _parse_json,
)
from bot.scoring import compute_scorecard
from bot.utils import get_logger, mk_folder, now_tw

REPORT_TYPE = "intraday"


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------


@dataclass
class CandidateRow:
    ticker: str
    name: str = ""
    theme: str = ""
    theme_heat: int = 0
    role: str = ""
    today_close: float | None = None
    today_pct_change: float = 0.0
    volume: float = 0.0
    volume_ratio: float = 0.0
    technical_score: float = 50.0
    day_trade_score: float = 0.0
    action: str = "HOLD"
    us_market_score: float = 50.0
    adr_premium_pct: float | None = None
    chip_summary_text: str = ""
    risk_text: str = ""
    sources: list[str] = field(default_factory=list)  # ['theme','supply_chain','etf','watchlist']


@dataclass
class IntradayReport:
    asof: str
    market_tone: str = "neutral"
    overall_brief: str = ""
    themes: list[dict[str, Any]] = field(default_factory=list)
    rankings: list[CandidateRow] = field(default_factory=list)
    macro_summary: dict[str, Any] = field(default_factory=dict)
    brief_md: str = ""
    brief_prompt_id: str = ""
    brief_prompt_version: str = ""
    duration_sec: float = 0.0
    errors: list[str] = field(default_factory=list)
    output_dir: str = ""


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def _build_candidate_pool(
    themes: list[dict[str, Any]],
    supply_chain: dict[str, Any],
    consensus_tickers: list[str],
    watchlist_tickers: list[str],
    macro: dict[str, Any],
) -> dict[str, CandidateRow]:
    """合併 4 個來源產出唯一候選股清單。"""
    pool: dict[str, CandidateRow] = {}

    # 1) 題材股
    for th in themes:
        theme_name = th.get("theme", "")
        heat = int(th.get("heat", 0) or 0)
        for ct in th.get("candidate_tickers", []) or []:
            t = str(ct.get("ticker", "")).strip()
            if not t or not t.isdigit():
                continue
            if t not in pool:
                pool[t] = CandidateRow(ticker=t)
            row = pool[t]
            row.name = row.name or str(ct.get("name", ""))
            if heat > row.theme_heat:
                row.theme = theme_name
                row.theme_heat = heat
                row.role = str(ct.get("role", ""))
            if "theme" not in row.sources:
                row.sources.append("theme")

    # 2) 供應鏈龍頭 (美股當夜漲幅前段)
    stocks = macro.get("stocks") or {}
    hot_us = sorted(
        [(s, info.get("pct_change", 0)) for s, info in stocks.items()],
        key=lambda x: -x[1],
    )[:5]
    for us_sym, _pct in hot_us:
        for entry in (supply_chain.get("us_stocks", {}).get(us_sym, {}).get("tw_supply_chain") or []):
            t = str(entry.get("tw_ticker", "")).strip()
            if not t or not t.isdigit():
                continue
            if t not in pool:
                pool[t] = CandidateRow(ticker=t, name=str(entry.get("name", "")))
            if "supply_chain" not in pool[t].sources:
                pool[t].sources.append("supply_chain")

    # 3) ETF 共識
    for t in consensus_tickers:
        if not t or not str(t).isdigit():
            continue
        if t not in pool:
            pool[t] = CandidateRow(ticker=t)
        if "etf" not in pool[t].sources:
            pool[t].sources.append("etf")

    # 4) Watchlist
    for t in watchlist_tickers:
        if not t or not str(t).isdigit():
            continue
        if t not in pool:
            pool[t] = CandidateRow(ticker=t)
        if "watchlist" not in pool[t].sources:
            pool[t].sources.append("watchlist")

    return pool


def _technical_for_ticker(
    ticker: str,
    *,
    today: dt.date,
    project_root: Path,
    refresh: bool,
    log: logging.Logger,
) -> dict[str, Any] | None:
    try:
        from bot.technicals import build_technical_snapshot, snapshot_to_dict
        snap, _df = build_technical_snapshot(
            ticker,
            months=4,
            root=project_root,
            refresh=refresh,
            logger=log,
        )
    except Exception:
        log.debug("[%s] technical snapshot 失敗", ticker, exc_info=True)
        return None

    has_data = bool(getattr(snap, "has_data", False) or getattr(snap, "rows", 0))
    if not has_data:
        return None

    last_date = str(getattr(snap, "last_date", "") or "")
    try:
        last_d = dt.date.fromisoformat(last_date)
    except Exception:
        return None

    # 盤前通常只能拿到上一交易日 K 線；假日後最多容忍 4 天。
    if (today - last_d).days > 4:
        log.debug("[%s] technical snapshot 過舊: %s", ticker, last_date)
        return None

    try:
        return snapshot_to_dict(snap)
    except Exception:
        return {
            "ticker": ticker,
            "last_date": last_date,
            "last_close": getattr(snap, "last_close", 0.0),
            "pct_change_1d": getattr(snap, "pct_change_1d", 0.0),
            "volume_last": getattr(snap, "volume_last", 0.0),
            "vol_ma20": getattr(snap, "vol_ma20", None),
            "technical_score": getattr(snap, "technical_score", 50.0),
            "rows": getattr(snap, "rows", 0),
        }


def _score_candidate(
    row: CandidateRow,
    *,
    macro: dict[str, Any],
    supply_chain: dict[str, Any],
    project_root: Path,
    today: dt.date | None = None,
    chip_lookback: int = 5,
    refresh_technicals: bool = True,
    log: logging.Logger,
) -> CandidateRow:
    """跑 day_trade 評分，只填當沖戰情室需要的欄位。"""
    from bot.chips_fetcher import build_chip_summary, summary_to_dict

    asof = today or now_tw().date()

    # 籌碼摘要
    chip_dict: dict[str, Any] | None = None
    try:
        summary = build_chip_summary(
            row.ticker,
            end_date=asof,
            days=chip_lookback,
            root=project_root,
            logger=log,
        )
        if summary:
            chip_dict = summary_to_dict(summary)
            f_net = chip_dict.get("foreign_net", 0)
            t_net = chip_dict.get("investment_trust_net", 0)
            row.chip_summary_text = f"外資{f_net:+.0f}張、投信{t_net:+.0f}"
    except Exception:
        log.debug("[%s] chip summary 失敗", row.ticker)

    # 技術面 / 今日量價
    technical_dict = _technical_for_ticker(
        row.ticker,
        today=asof,
        project_root=project_root,
        refresh=refresh_technicals,
        log=log,
    )
    price = 0.0
    pct_change = 0.0
    volume = 0.0
    if technical_dict:
        price = float(technical_dict.get("last_close", 0.0) or 0.0)
        pct_change = float(technical_dict.get("pct_change_1d", 0.0) or 0.0)
        volume = float(technical_dict.get("volume_last", 0.0) or 0.0)
        vol_ma20 = float(technical_dict.get("vol_ma20", 0.0) or 0.0)
        row.today_close = price or None
        row.today_pct_change = pct_change
        row.volume = volume
        row.volume_ratio = round(volume / vol_ma20, 2) if vol_ma20 > 0 else 0.0
        row.technical_score = float(technical_dict.get("technical_score", 50.0) or 50.0)

    # 美股關聯
    related = related_us_stocks_for_tw(row.ticker, supply_chain, project_root)

    # ADR 溢價 (若有)
    adr_prem = None
    for p in macro.get("adr_premiums") or []:
        if p.get("tw_ticker") == row.ticker:
            adr_prem = p
            row.adr_premium_pct = float(p.get("premium_pct", 0) or 0)
            break

    # day_trade 評分：技術面權重最高，搭配美股連動與籌碼。
    card = compute_scorecard(
        ticker=row.ticker, name=row.name,
        price=price, pct_change=pct_change, volume=volume,
        chip_summary=chip_dict,
        technical_snapshot=technical_dict,
        macro_snapshot=macro,
        related_us_stocks=related,
        adr_premium=adr_prem,
    )
    tf = card.timeframes.get("day_trade")
    if tf:
        row.day_trade_score = round(tf.total, 1)
        row.action = tf.action
        us_factor = next((f for f in tf.factors if f.key == "us_market"), None)
        if us_factor:
            row.us_market_score = round(us_factor.score, 1)

    return row


def run_intraday(
    *,
    project_root: Path | None = None,
    settings: Settings | None = None,
    news_limit: int = 120,
    candidate_limit: int = 25,
    force_refresh_news: bool = False,
    force_refresh_technicals: bool = True,
    logger: logging.Logger | None = None,
    publisher=None,
) -> IntradayReport:
    log = logger or get_logger("intraday")
    root = project_root or Path.cwd()
    settings = settings or Settings()
    today = now_tw().date()
    report = IntradayReport(asof=today.isoformat())

    t0 = time.time()

    # ---- 1. 抓新聞 ----
    log.info("[1/6] 抓今日台股新聞 (cnyes)...")
    try:
        items = fetch_today_news(
            limit=news_limit, force_refresh=force_refresh_news,
            root=root, logger=log,
        )
        news_text = news_to_compact_text(items, max_chars=15000)
        log.info("新聞 %d 條", len(items))
    except Exception as e:
        log.exception("news 抓取失敗")
        report.errors.append(f"news: {e}")
        items = []
        news_text = ""

    # ---- 2. 抓 macro ----
    log.info("[2/6] 抓 macro (yfinance)...")
    try:
        macro_snap = fetch_macro_snapshot(root=root, logger=log)
        macro = macro_to_dict(macro_snap)
        report.macro_summary = {
            "asof_date": macro.get("asof_date", ""),
            "indices_count": len(macro.get("indices") or {}),
            "adr_premiums_count": len(macro.get("adr_premiums") or []),
        }
    except Exception as e:
        log.exception("macro 抓取失敗")
        report.errors.append(f"macro: {e}")
        macro = {}

    macro_text = _macro_summary_text(macro)
    log.info("macro: %s", macro_text)

    # ---- 3. LLM 主題雷達 ----
    client = GeminiClient(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        logger=log,
    )
    themes: list[dict[str, Any]] = []
    if client.enabled and news_text:
        log.info("[3/6] LLM 萃取今日熱門題材...")
        try:
            raw, info = gemini_call(
                "theme_radar",
                client=client,
                metadata={"task": "theme_radar", "asof": today.isoformat()},
                asof_date=today.isoformat(),
                news_text=news_text,
                macro_summary=macro_text,
            )
            if raw:
                obj = _parse_json(raw)
                if obj:
                    report.market_tone = str(obj.get("market_tone") or "neutral")
                    report.overall_brief = str(obj.get("overall_brief") or "")
                    themes = obj.get("themes") or []
                    report.themes = themes
                    log.info("LLM 萃取 %d 個題材 (tone=%s)", len(themes), report.market_tone)
        except Exception as e:
            log.exception("theme_radar 失敗")
            report.errors.append(f"theme_radar: {e}")
    else:
        log.info("[3/6] (略) LLM 未啟用或無新聞")

    # ---- 4. 合併候選股 ----
    log.info("[4/6] 合併候選股 ...")
    supply_chain = load_supply_chain(root)
    consensus_tickers = _consensus_tickers_today(root)
    watchlist_tickers = _watchlist_tickers(root)
    pool = _build_candidate_pool(
        themes, supply_chain, consensus_tickers, watchlist_tickers, macro,
    )
    log.info(
        "候選池 %d (題材%d / 供應鏈%d / ETF%d / WL%d)",
        len(pool),
        sum(1 for r in pool.values() if "theme" in r.sources),
        sum(1 for r in pool.values() if "supply_chain" in r.sources),
        sum(1 for r in pool.values() if "etf" in r.sources),
        sum(1 for r in pool.values() if "watchlist" in r.sources),
    )

    # ---- 5. 評分排序 ----
    log.info("[5/6] 對 %d 檔候選股計算當沖分 ...", len(pool))
    for row in pool.values():
        _score_candidate(
            row,
            macro=macro, supply_chain=supply_chain,
            project_root=root,
            today=today,
            refresh_technicals=force_refresh_technicals,
            log=log,
        )
    rankings = sorted(
        pool.values(),
        key=lambda r: (-r.day_trade_score, -r.theme_heat),
    )[:candidate_limit]
    report.rankings = rankings

    # ---- 6. LLM 戰情簡報 ----
    if client.enabled and themes:
        log.info("[6/6] LLM 撰寫戰情簡報 ...")
        try:
            top_for_brief = [asdict(r) for r in rankings[:15]]
            raw, info = gemini_call(
                "intraday_brief",
                client=client,
                metadata={"task": "intraday_brief", "asof": today.isoformat()},
                asof_date=today.isoformat(),
                themes_json=json.dumps(themes, ensure_ascii=False, indent=2),
                ranked_candidates_json=json.dumps(top_for_brief, ensure_ascii=False, indent=2),
                macro_summary=macro_text,
            )
            if raw:
                report.brief_md = raw
                report.brief_prompt_id = info.get("prompt_id", "")
                report.brief_prompt_version = info.get("prompt_version", "")
        except Exception as e:
            log.exception("intraday_brief 失敗")
            report.errors.append(f"brief: {e}")
    else:
        log.info("[6/6] (略) 略過戰情簡報")

    report.duration_sec = round(time.time() - t0, 2)

    out_dir = root / "data" / "intraday" / today.isoformat()
    report.output_dir = str(out_dir)

    # ---- 持久化 ----
    mk_folder(str(out_dir))
    report_json = _report_to_json(report)
    try:
        report_path = out_dir / "report.json"
        report_path.write_text(
            json.dumps(report_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        mirror_file_to_cloud(report_path, root=root)
        if report.brief_md:
            brief_path = out_dir / "intraday_brief.md"
            brief_path.write_text(report.brief_md, encoding="utf-8")
            mirror_file_to_cloud(brief_path, root=root)
    except Exception:
        log.exception("intraday 持久化失敗")
    _persist_report_json_to_db(report_json, root=root, log=log)

    log.info(
        "Intraday 完成 (%.1fs, %d 題材, %d 候選, 錯誤 %d)",
        report.duration_sec, len(themes), len(rankings), len(report.errors),
    )
    publish_pipeline_completed(
        publisher,
        pipeline="intraday",
        run_id=today.isoformat(),
        output_dir=report.output_dir,
        success=len(report.errors) == 0,
        error_count=len(report.errors),
        duration_sec=report.duration_sec,
        extra={"themes": len(themes), "candidates": len(rankings)},
    )
    return report


# ----------------------------------------------------------------------
# 輔助
# ----------------------------------------------------------------------


def _watchlist_tickers(root: Path) -> list[str]:
    try:
        from bot import watchlist as wl_mod
        wl = wl_mod.load(root)
        return [i.ticker for i in wl.items if i.ticker.isdigit()]
    except Exception:
        return []


def load_latest_intraday(root: Path | None = None) -> dict[str, Any] | None:
    root_path = root or Path.cwd()
    try:
        from bot.stock_db import StockDB
        db = StockDB.open(root=root_path)
        data = _daily_report_row_to_payload(
            db.get_latest_llm_daily_report(REPORT_TYPE, mode="")
        )
        if data:
            return data
    except Exception:
        get_logger("intraday").debug("load latest intraday from DB failed", exc_info=True)

    base = root_path / "data" / "intraday"
    restore_tree_from_cloud(base, root=root)
    if not base.exists():
        return None
    days = sorted([d for d in base.iterdir() if d.is_dir()], key=lambda p: p.name, reverse=True)
    for d in days:
        p = d / "report.json"
        restore_file_from_cloud(p, root=root)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                _persist_report_json_to_db(data, root=root_path, log=get_logger("intraday"))
                return data
            except Exception:
                continue
    return None


def load_intraday_by_date(
    root: Path | None = None,
    report_date: dt.date | str | None = None,
) -> dict[str, Any] | None:
    """依 asof 日期讀取當沖報告，不會觸發 LLM 生成。"""
    root_path = root or Path.cwd()
    if report_date is None:
        date_iso = now_tw().date().isoformat()
    elif hasattr(report_date, "isoformat"):
        date_iso = report_date.isoformat()  # type: ignore[union-attr]
    else:
        date_iso = str(report_date)

    try:
        from bot.stock_db import StockDB
        db = StockDB.open(root=root_path)
        data = _daily_report_row_to_payload(
            db.get_llm_daily_report(REPORT_TYPE, date_iso, mode="")
        )
        if data:
            return data
    except Exception:
        get_logger("intraday").debug("load intraday from DB failed", exc_info=True)

    p = root_path / "data" / "intraday" / date_iso / "report.json"
    restore_file_from_cloud(p, root=root_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        _persist_report_json_to_db(data, root=root_path, log=get_logger("intraday"))
        return data
    except Exception:
        get_logger("intraday").debug("load intraday file failed: %s", p, exc_info=True)
        return None


def _daily_report_row_to_payload(row: Any) -> dict[str, Any] | None:
    if row is None or not row.payload_json:
        return None
    try:
        payload = json.loads(row.payload_json)
    except Exception:
        return None
    if row.brief_md and not payload.get("brief_md"):
        payload["brief_md"] = row.brief_md
    payload.setdefault("_db_generated_at", row.generated_at)
    payload.setdefault("_db_updated_at", row.updated_at)
    payload.setdefault("_db_report_date", row.report_date)
    return payload


def _persist_report_json_to_db(
    data: dict[str, Any],
    *,
    root: Path,
    log: logging.Logger,
) -> None:
    report_date = str(data.get("asof") or "").strip()
    if not report_date:
        return
    try:
        from bot.stock_db import LlmDailyReportRow, StockDB
        db = StockDB.open(root=root)
        db.upsert_llm_daily_report(
            LlmDailyReportRow(
                report_type=REPORT_TYPE,
                report_date=report_date,
                mode="",
                asof=str(data.get("asof") or ""),
                generated_at=now_tw().isoformat(timespec="seconds"),
                market_tone=str(data.get("market_tone") or ""),
                prompt_id=str(data.get("brief_prompt_id") or ""),
                prompt_version=str(data.get("brief_prompt_version") or ""),
                brief_md=str(data.get("brief_md") or ""),
                payload_json=json.dumps(data, ensure_ascii=False),
            )
        )
    except Exception:
        log.exception("intraday DB 持久化失敗")


__all__ = [
    "CandidateRow",
    "IntradayReport",
    "load_intraday_by_date",
    "load_latest_intraday",
    "run_intraday",
]
