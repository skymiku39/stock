"""data_pipeline -- 把 ETF 抓取、籌碼面、法說會、LLM 分析、每日報告一條龍串起來。

每執行一次 `run_full_pipeline()` 會：
1. 自動抓所有有 holdings_url 的主動式 ETF 持股，存為當日 CSV
2. 計算共識持股 / 共識新建倉 / 共識加碼
3. 對被多檔 ETF 持有的「焦點個股」抓近 N 日籌碼面 (TWSE)
4. 若有指定法說會文字 (PDF/逐字稿)，呼叫 Gemini 解析
5. 結合 (3) + (4) 做 logic_check
6. 用 daily_brief prompt 產出當日 markdown 簡報
7. 全部結果寫到 data/pipeline_runs/<timestamp>/，並 append 一筆 manifest
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.active_etf import (
    ActiveEtf,
    HoldingsSnapshot,
    list_holdings_dates,
    load_active_etfs,
    load_holdings,
)
from bot.chips_fetcher import ChipSummary, build_chip_summary, summary_to_chips_context, summary_to_dict
from bot.etf_consensus import (
    ConsensusHolding,
    FollowSignal,
    build_consensus,
    consensus_additions,
    consensus_new_builds,
    diff_snapshots,
)
from bot.etf_holdings_fetcher import FetchResult, fetch_all_active_etfs
from bot.llm_analyzer import (
    GeminiClient,
    LogicCheckResult,
    PresentationAnalysis,
    analyze_presentation,
    extract_json,
    gemini_call,
    logic_check,
)
from bot.utils import get_logger, mk_folder, now_tw


# ----------------------------------------------------------------------
# 資料模型
# ----------------------------------------------------------------------


@dataclass
class PresentationInput:
    """要送進 LLM 分析的法說會原文。"""

    ticker: str
    text: str
    source: str = "manual"  # manual / pdf_path / url
    label: str = ""


@dataclass
class PipelineConfig:
    project_root: Path
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    focus_tickers: List[str] = field(default_factory=list)
    chip_lookback_days: int = 5
    fetch_etf_holdings: bool = True
    fetch_chips: bool = True
    fetch_macro: bool = True
    run_llm_analysis: bool = True
    generate_brief: bool = True
    generate_us_brief: bool = True
    min_consensus_for_focus: int = 2
    presentation_inputs: List[PresentationInput] = field(default_factory=list)


@dataclass
class PipelineRun:
    """單次 pipeline 執行的完整紀錄。"""

    run_id: str
    started_at: str
    ended_at: str = ""
    duration_sec: float = 0.0
    config: Dict[str, Any] = field(default_factory=dict)

    etf_fetch_summary: Dict[str, Any] = field(default_factory=dict)
    consensus_top: List[Dict[str, Any]] = field(default_factory=list)
    new_build_signals: List[Dict[str, Any]] = field(default_factory=list)
    add_signals: List[Dict[str, Any]] = field(default_factory=list)
    focus_tickers: List[str] = field(default_factory=list)
    chip_summaries: List[Dict[str, Any]] = field(default_factory=list)
    presentation_analyses: List[Dict[str, Any]] = field(default_factory=list)
    logic_checks: List[Dict[str, Any]] = field(default_factory=list)
    daily_brief_md: str = ""
    daily_brief_prompt_id: str = ""
    daily_brief_prompt_version: str = ""
    us_brief_md: str = ""
    us_brief_prompt_id: str = ""
    us_brief_prompt_version: str = ""
    macro_summary: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    output_dir: str = ""


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def run_full_pipeline(
    config: PipelineConfig,
    logger: Optional[logging.Logger] = None,
) -> PipelineRun:
    """一鍵跑完整個自動化研究流程。"""
    log = logger or get_logger("pipeline")
    started = now_tw()
    run_id = started.strftime("%Y%m%d_%H%M%S")
    output_dir = config.project_root / "data" / "pipeline_runs" / run_id
    mk_folder(str(output_dir))

    run = PipelineRun(
        run_id=run_id,
        started_at=started.isoformat(timespec="seconds"),
        config={
            "fetch_etf_holdings": config.fetch_etf_holdings,
            "fetch_chips": config.fetch_chips,
            "fetch_macro": config.fetch_macro,
            "run_llm_analysis": config.run_llm_analysis,
            "generate_brief": config.generate_brief,
            "generate_us_brief": config.generate_us_brief,
            "chip_lookback_days": config.chip_lookback_days,
            "gemini_model": config.gemini_model,
            "focus_tickers": config.focus_tickers,
            "presentation_count": len(config.presentation_inputs),
        },
        output_dir=str(output_dir),
    )

    client = GeminiClient(
        api_key=config.gemini_api_key,
        model=config.gemini_model,
        logger=log,
    )

    t0 = time.time()

    # ---- 1. ETF 持股自動抓取 ----
    today = started.date()
    if config.fetch_etf_holdings:
        log.info("[1/6] 自動抓取 ETF 持股 …")
        try:
            results = fetch_all_active_etfs(
                client=client,
                snapshot_date=today,
                root=config.project_root,
                logger=log,
            )
            run.etf_fetch_summary = {
                "total": len(results),
                "success": sum(1 for r in results if r.success),
                "failed": sum(1 for r in results if not r.success),
                "details": [
                    {
                        "symbol": r.etf.symbol,
                        "name": r.etf.name,
                        "success": r.success,
                        "holdings_count": r.holdings_count,
                        "error": r.error,
                        "saved_path": str(r.saved_path) if r.saved_path else "",
                    } for r in results
                ],
            }
        except Exception as e:
            log.exception("ETF 抓取階段例外")
            run.errors.append(f"etf_fetch: {e}")
    else:
        log.info("[1/6] (略) ETF 抓取已關閉")

    # ---- 2. 共識計算 ----
    log.info("[2/6] 計算 ETF 共識與變動 …")
    etfs = load_active_etfs(config.project_root)
    etf_meta = {e.symbol: e for e in etfs}

    latest: Dict[str, HoldingsSnapshot] = {}
    prev: Dict[str, HoldingsSnapshot] = {}
    for e in etfs:
        dates = list_holdings_dates(e.symbol, config.project_root)
        if not dates:
            continue
        l = load_holdings(e.symbol, dates[0], config.project_root)
        if l:
            latest[e.symbol] = l
        if len(dates) >= 2:
            p = load_holdings(e.symbol, dates[1], config.project_root)
            if p:
                prev[e.symbol] = p

    consensus: List[ConsensusHolding] = build_consensus(
        latest, etf_meta, min_etf_count=1,
    )
    run.consensus_top = [
        {
            "ticker": c.ticker, "name": c.name,
            "etf_count": c.etf_count,
            "total_weight": round(c.total_weight, 2),
            "held_by": [w.etf_symbol for w in c.held_by],
        } for c in consensus[:50]
    ]

    changes = []
    for sym, after in latest.items():
        before = prev.get(sym)
        if before:
            changes.extend(diff_snapshots(before, after))
    news = consensus_new_builds(changes, min_etfs=config.min_consensus_for_focus)
    adds = consensus_additions(changes, min_etfs=max(2, config.min_consensus_for_focus + 1))
    run.new_build_signals = [_signal_dict(s) for s in news]
    run.add_signals = [_signal_dict(s) for s in adds]

    # ---- 3. 焦點個股 ----
    focus = set(config.focus_tickers)
    for c in consensus:
        if c.etf_count >= config.min_consensus_for_focus:
            focus.add(c.ticker)
    for s in news + adds:
        focus.add(s.ticker)
    focus_list = sorted(focus)
    run.focus_tickers = focus_list
    log.info("[3/6] 焦點個股 %d 檔: %s", len(focus_list), focus_list[:10])

    # ---- 4. 籌碼面 ----
    chip_map: Dict[str, ChipSummary] = {}
    if config.fetch_chips and focus_list:
        log.info("[4/6] 自動抓籌碼面 (近 %d 日) …", config.chip_lookback_days)
        for t in focus_list:
            try:
                summary = build_chip_summary(
                    t, end_date=today,
                    days=config.chip_lookback_days,
                    root=config.project_root,
                    logger=log,
                )
                chip_map[t] = summary
                run.chip_summaries.append(summary_to_dict(summary))
            except Exception as e:
                log.exception("[%s] 籌碼面抓取失敗", t)
                run.errors.append(f"chips_{t}: {e}")
    else:
        log.info("[4/6] (略) 籌碼面抓取已關閉")

    # ---- 5. 法說會 LLM 分析 + 言行反查 ----
    analyses: List[PresentationAnalysis] = []
    logic_results: List[LogicCheckResult] = []
    if config.run_llm_analysis and config.presentation_inputs:
        log.info("[5/6] 用 Gemini 解析 %d 場法說會 …", len(config.presentation_inputs))
        for pi in config.presentation_inputs:
            try:
                a = analyze_presentation(
                    pi.text, ticker=pi.ticker, client=client, logger=log,
                )
                analyses.append(a)
                run.presentation_analyses.append(_analysis_dict(a, pi))
                chip_ctx = None
                if pi.ticker in chip_map:
                    chip_ctx = summary_to_chips_context(chip_map[pi.ticker])
                if chip_ctx is not None:
                    r = logic_check(a, chip_ctx, client=client, logger=log)
                    logic_results.append(r)
                    run.logic_checks.append(asdict(r))
            except Exception as e:
                log.exception("[%s] LLM 解析失敗", pi.ticker)
                run.errors.append(f"llm_{pi.ticker}: {e}")
    else:
        log.info("[5/6] (略) 法說會 LLM 分析未啟用或無輸入")

    # ---- 5b. 美股 / 跨市場資料 ----
    macro_snap_dict: Dict[str, Any] = {}
    if config.fetch_macro:
        log.info("[5b/6] 抓 macro (yfinance: US 指數 + 重點美股 + ADR 溢價) …")
        try:
            from bot.market_macro import (
                fetch_macro_snapshot,
                load_supply_chain,
                macro_to_dict,
            )

            macro_snap = fetch_macro_snapshot(
                root=config.project_root, force_refresh=True, logger=log,
            )
            macro_snap_dict = macro_to_dict(macro_snap)
            run.macro_summary = {
                "asof_date": macro_snap.asof_date,
                "usdtwd": macro_snap.usdtwd,
                "indices_count": len(macro_snap.indices),
                "stocks_count": len(macro_snap.stocks),
                "adr_premiums_count": len(macro_snap.adr_premiums),
                "top_movers": _top_movers(macro_snap_dict),
            }
        except Exception as e:
            log.exception("macro 抓取失敗")
            run.errors.append(f"macro: {e}")
    else:
        log.info("[5b/6] (略) macro 抓取已關閉")

    # ---- 5c. 美股盤後 → 台股早盤簡報 (LLM) ----
    if config.generate_us_brief and client.enabled and macro_snap_dict:
        log.info("[5c/6] 產出美股 → 台股早盤簡報 …")
        try:
            from bot.market_macro import load_supply_chain

            sc = load_supply_chain(config.project_root)
            raw, info = gemini_call(
                "us_market_brief",
                client=client,
                metadata={"task": "us_market_brief", "run_id": run_id},
                asof_date=today.isoformat(),
                indices_json=json.dumps(
                    macro_snap_dict.get("indices", {}),
                    ensure_ascii=False, indent=2,
                ),
                stocks_json=json.dumps(
                    macro_snap_dict.get("stocks", {}),
                    ensure_ascii=False, indent=2,
                ),
                adr_premiums_json=json.dumps(
                    macro_snap_dict.get("adr_premiums", []),
                    ensure_ascii=False, indent=2,
                ),
                supply_chain_json=json.dumps(
                    sc.get("us_stocks", {}),
                    ensure_ascii=False, indent=2,
                ),
            )
            run.us_brief_md = raw or "(LLM 未產出內容)"
            run.us_brief_prompt_id = info.get("prompt_id", "")
            run.us_brief_prompt_version = info.get("prompt_version", "")
        except Exception as e:
            log.exception("us_market_brief 失敗")
            run.errors.append(f"us_brief: {e}")

    # ---- 6. 每日簡報 ----
    if config.generate_brief and client.enabled:
        log.info("[6/6] 產出每日簡報 …")
        try:
            raw, info = gemini_call(
                "daily_brief",
                client=client,
                metadata={"task": "daily_brief", "run_id": run_id},
                date=today.isoformat(),
                consensus_signals_json=json.dumps({
                    "consensus_top": run.consensus_top[:20],
                    "new_build_signals": run.new_build_signals[:20],
                    "add_signals": run.add_signals[:20],
                }, ensure_ascii=False, indent=2),
                presentation_analyses_json=json.dumps(
                    run.presentation_analyses, ensure_ascii=False, indent=2,
                ),
                chips_summary_json=json.dumps(
                    [{k: v for k, v in d.items() if k != "rows"} for d in run.chip_summaries],
                    ensure_ascii=False, indent=2,
                ),
            )
            run.daily_brief_md = raw or "(LLM 未產出內容)"
            run.daily_brief_prompt_id = info.get("prompt_id", "")
            run.daily_brief_prompt_version = info.get("prompt_version", "")
        except Exception as e:
            log.exception("daily_brief 失敗")
            run.errors.append(f"daily_brief: {e}")
    else:
        log.info("[6/6] (略) 每日簡報未啟用或 LLM 未啟用")

    # ---- 收尾 ----
    ended = now_tw()
    run.ended_at = ended.isoformat(timespec="seconds")
    run.duration_sec = round(time.time() - t0, 2)
    _persist_run(run, output_dir, log)
    _append_manifest(run, config.project_root, log)
    log.info(
        "Pipeline 完成 (%s, %.1fs, 焦點 %d, 錯誤 %d)",
        run_id, run.duration_sec, len(run.focus_tickers), len(run.errors),
    )
    return run


# ----------------------------------------------------------------------
# 持久化
# ----------------------------------------------------------------------


def _persist_run(run: PipelineRun, out_dir: Path, log: logging.Logger) -> None:
    try:
        (out_dir / "run.json").write_text(
            json.dumps(asdict(run), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if run.daily_brief_md:
            (out_dir / "daily_brief.md").write_text(run.daily_brief_md, encoding="utf-8")
        if run.us_brief_md:
            (out_dir / "us_market_brief.md").write_text(run.us_brief_md, encoding="utf-8")
    except Exception:
        log.exception("Pipeline run 寫檔失敗")


def _append_manifest(run: PipelineRun, root: Path, log: logging.Logger) -> None:
    manifest = root / "data" / "pipeline_runs" / "manifest.jsonl"
    mk_folder(str(manifest.parent))
    try:
        with manifest.open("a", encoding="utf-8") as f:
            json.dump({
                "run_id": run.run_id,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                "duration_sec": run.duration_sec,
                "focus_tickers_count": len(run.focus_tickers),
                "errors": len(run.errors),
                "etf_success": run.etf_fetch_summary.get("success", 0),
                "etf_failed": run.etf_fetch_summary.get("failed", 0),
                "output_dir": run.output_dir,
            }, f, ensure_ascii=False)
            f.write("\n")
    except Exception:
        log.exception("manifest 寫入失敗")


def list_pipeline_runs(root: Path) -> List[Dict[str, Any]]:
    manifest = root / "data" / "pipeline_runs" / "manifest.jsonl"
    if not manifest.exists():
        return []
    out: List[Dict[str, Any]] = []
    with manifest.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    out.sort(key=lambda x: x.get("started_at", ""), reverse=True)
    return out


def load_pipeline_run(root: Path, run_id: str) -> Optional[PipelineRun]:
    p = root / "data" / "pipeline_runs" / run_id / "run.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # 缺欄位也能載入
        for k in PipelineRun.__dataclass_fields__:
            data.setdefault(k, [] if isinstance(
                PipelineRun.__dataclass_fields__[k].default_factory(),  # type: ignore[misc]
                (list, dict),
            ) else "")
        return PipelineRun(**{k: data.get(k) for k in PipelineRun.__dataclass_fields__})
    except Exception:
        return None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _top_movers(macro_dict: Dict[str, Any]) -> List[Dict[str, Any]]:
    """從 macro_snapshot 取美股漲跌前 5 名，給 manifest 用。"""
    stocks = macro_dict.get("stocks") or {}
    items = [
        {"symbol": sym, "name": info.get("name", sym), "pct_change": float(info.get("pct_change", 0) or 0)}
        for sym, info in stocks.items()
    ]
    items.sort(key=lambda x: abs(x["pct_change"]), reverse=True)
    return items[:8]


def _signal_dict(s: FollowSignal) -> Dict[str, Any]:
    return {
        "ticker": s.ticker,
        "name": s.name,
        "signal_type": s.signal_type,
        "etf_count": s.etf_count,
        "total_weight_delta": round(s.total_weight_delta, 2),
        "note": s.note,
        "related_etfs": s.related_etfs,
    }


def _analysis_dict(a: PresentationAnalysis, src: PresentationInput) -> Dict[str, Any]:
    return {
        "ticker": a.ticker,
        "source": src.source,
        "label": src.label,
        "sentiment": a.sentiment,
        "sentiment_score": a.sentiment_score,
        "confidence": a.confidence,
        "summary": a.summary,
        "growth_drivers": a.growth_drivers,
        "risks": a.risks,
        "capex_signal": a.capex_signal,
        "margin_outlook": a.margin_outlook,
        "key_metrics": a.key_metrics,
        "prompt_id": a.prompt_id,
        "prompt_version": a.prompt_version,
    }


__all__ = [
    "PipelineConfig",
    "PipelineRun",
    "PresentationInput",
    "list_pipeline_runs",
    "load_pipeline_run",
    "run_full_pipeline",
]
