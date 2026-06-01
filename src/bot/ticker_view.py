"""ticker_view -- 把分散在各模組的資料整合成「單一個股快照」。

來源整理
========
* 最新 chips 摘要 (近 5 日)         -- chips_fetcher
* 主動 ETF 共識 (即時計算)           -- etf_consensus + active_etf
* LLM 法說分析 / 言行反查 / 來源檔  -- 掃 data/pipeline_runs/*/run.json
* 法說會 PDF / TXT / ETF 持股 CSV   -- 從 data/etf_holdings_raw 與 data/etf_holdings
* 交易紀錄                          -- data/trades_*.csv (簡易掃描)

設計
====
* 完全 read-only：不會去打 Shioaji / TWSE 即時報價
* chip_summary 採用「最近一筆 cached 籌碼」，要主動抓需呼叫 `refresh_chips=True`
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.active_etf import (
    HoldingsSnapshot,
    list_holdings_dates,
    load_active_etfs,
    load_holdings,
)
from bot.chip_distribution import (
    DistributionTrend,
    DistributionWeekly,
    build_distribution_snapshot,
    interpret_distribution,
    load_distribution_trend,
)
from bot.chips_fetcher import (
    ChipSummary,
    build_chip_summary,
    fetch_daily_chips,
    summary_to_dict,
)
from bot.etf_consensus import (
    ConsensusHolding,
    FollowSignal,
    build_consensus,
    consensus_additions,
    consensus_new_builds,
    diff_snapshots,
)
from bot.fundamentals_fetcher import (
    FundamentalSnapshot,
    build_fundamental_snapshot,
    snapshot_to_dict as fundamental_to_dict,
)
from bot.quarterly import summarize_quarterly
from bot.technicals import (
    TechnicalSnapshot,
    build_technical_snapshot,
    snapshot_to_dict as technical_to_dict,
)
from bot.utils import get_logger, now_tw


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------


@dataclass
class HistoryItem:
    run_id: str
    started_at: str
    llm_sentiment: Optional[str] = None
    llm_score: Optional[float] = None
    logic_verdict: Optional[str] = None
    foreign_net: Optional[float] = None
    note: str = ""


@dataclass
class TradeRecord:
    """來自 data/trades_*.csv 的單筆交易；不全部讀入只取與此 ticker 相關。"""

    ts: str
    side: str
    qty: float
    price: float
    note: str = ""


@dataclass
class TickerSnapshot:
    """個股快照 — 整合所有資料源，唯一的事實來源 (給 UI 與 scoring 用)。"""

    ticker: str
    name: str = ""
    fetched_at: str = ""

    # 報價 (從 cached chips 或外部塞入)
    price: float = 0.0
    pct_change: float = 0.0
    volume: float = 0.0

    # 籌碼
    chip_summary: Optional[Dict[str, Any]] = None       # summary_to_dict 輸出
    chip_summary_obj: Optional[ChipSummary] = None      # 原始物件 (圖表用)

    # ETF
    consensus: Optional[Dict[str, Any]] = None
    new_build_signal: Optional[Dict[str, Any]] = None
    add_signal: Optional[Dict[str, Any]] = None
    held_by_etfs: List[Dict[str, Any]] = field(default_factory=list)

    # LLM
    llm_analysis: Optional[Dict[str, Any]] = None
    logic_check: Optional[Dict[str, Any]] = None

    # 原始資料 / 譜系
    pipeline_run_id: str = ""
    pipeline_run_dir: str = ""
    source_files: List[Dict[str, str]] = field(default_factory=list)

    # 歷史
    history: List[HistoryItem] = field(default_factory=list)

    # 交易/持倉
    trades: List[TradeRecord] = field(default_factory=list)
    position_qty: float = 0.0
    position_avg_cost: float = 0.0
    unrealized_pl: Optional[float] = None

    # 基本面 / 技術面 / 集保 / 季報 (3D 視角)
    fundamentals: Optional[FundamentalSnapshot] = None
    technicals: Optional[TechnicalSnapshot] = None
    distribution: Optional[DistributionWeekly] = None
    distribution_trend: Optional[DistributionTrend] = None
    distribution_label: str = ""
    distribution_detail: str = ""
    distribution_score: float = 50.0
    quarterly_view: Dict[str, Any] = field(default_factory=dict)

    # 美股 / 跨市場連動
    macro_snapshot: Optional[Dict[str, Any]] = None  # market_macro.macro_to_dict()
    us_related: List[Dict[str, Any]] = field(default_factory=list)  # 對應美股客戶/夥伴
    adr_premium: Optional[Dict[str, Any]] = None     # 若本身有 ADR (例如 2330)


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def build_snapshot(
    ticker: str,
    project_root: Path,
    *,
    refresh_chips: bool = False,
    chip_days: int = 5,
    name_hint: str = "",
    refresh_fundamentals: bool = False,
    refresh_technicals: bool = False,
    refresh_distribution: bool = False,
    refresh_macro: bool = False,
    technical_months: int = 6,
) -> TickerSnapshot:
    """整合所有資料源，組出單一個股快照。"""
    log = get_logger("ticker-view")
    snap = TickerSnapshot(
        ticker=ticker,
        name=name_hint,
        fetched_at=now_tw().isoformat(timespec="seconds"),
    )

    # ---- 1. ETF 共識 ----
    etfs = load_active_etfs(project_root)
    etf_meta = {e.symbol: e for e in etfs}
    latest_holds: Dict[str, HoldingsSnapshot] = {}
    prev_holds: Dict[str, HoldingsSnapshot] = {}
    for e in etfs:
        dates = list_holdings_dates(e.symbol, project_root)
        if not dates:
            continue
        l = load_holdings(e.symbol, dates[0], project_root)
        if l:
            latest_holds[e.symbol] = l
        if len(dates) >= 2:
            p = load_holdings(e.symbol, dates[1], project_root)
            if p:
                prev_holds[e.symbol] = p

    consensus_list: List[ConsensusHolding] = build_consensus(
        latest_holds, etf_meta, min_etf_count=1,
    )
    match = next((c for c in consensus_list if c.ticker == ticker), None)
    if match:
        snap.consensus = {
            "ticker": match.ticker,
            "name": match.name,
            "etf_count": match.etf_count,
            "total_weight": round(match.total_weight, 2),
        }
        snap.held_by_etfs = [
            {
                "etf_symbol": w.etf_symbol,
                "etf_name": etf_meta[w.etf_symbol].name if w.etf_symbol in etf_meta else "",
                "weight_pct": w.weight_pct,
            } for w in match.held_by
        ]
        if not snap.name and match.name:
            snap.name = match.name

    # 計算當下 new_build / add 訊號
    changes = []
    for sym, after in latest_holds.items():
        before = prev_holds.get(sym)
        if before:
            changes.extend(diff_snapshots(before, after))
    news = consensus_new_builds(changes, min_etfs=1)
    adds = consensus_additions(changes, min_etfs=2)
    nb = next((s for s in news if s.ticker == ticker), None)
    ad = next((s for s in adds if s.ticker == ticker), None)
    if nb:
        snap.new_build_signal = _signal_to_dict(nb)
    if ad:
        snap.add_signal = _signal_to_dict(ad)

    # ---- 2. Pipeline 歷史 ----
    pipeline_dir = project_root / "data" / "pipeline_runs"
    runs = _list_runs(pipeline_dir)
    history: List[HistoryItem] = []
    for run_path in runs:
        run_data = _load_run(run_path)
        if not run_data:
            continue
        match_analysis = next(
            (a for a in run_data.get("presentation_analyses", [])
             if a.get("ticker") == ticker), None,
        )
        match_logic = next(
            (l for l in run_data.get("logic_checks", [])
             if l.get("ticker") == ticker), None,
        )
        match_chip = next(
            (c for c in run_data.get("chip_summaries", [])
             if c.get("ticker") == ticker), None,
        )

        if not (match_analysis or match_logic or match_chip):
            continue

        # 最新一筆 (清單已依時間反序) 才填入 current
        if snap.pipeline_run_id == "":
            snap.pipeline_run_id = run_data.get("run_id", "")
            snap.pipeline_run_dir = str(run_path.parent)
            if match_analysis:
                snap.llm_analysis = match_analysis
                if not snap.name:
                    snap.name = match_analysis.get("name", "") or snap.name
            if match_logic:
                snap.logic_check = match_logic
            if match_chip:
                snap.chip_summary = match_chip

        history.append(HistoryItem(
            run_id=run_data.get("run_id", ""),
            started_at=run_data.get("started_at", ""),
            llm_sentiment=match_analysis.get("sentiment") if match_analysis else None,
            llm_score=match_analysis.get("sentiment_score") if match_analysis else None,
            logic_verdict=match_logic.get("verdict") if match_logic else None,
            foreign_net=match_chip.get("foreign_net") if match_chip else None,
            note=(match_analysis.get("label") if match_analysis else "")
                 or (str(match_chip.get("days")) + " 日籌碼" if match_chip else ""),
        ))

    snap.history = history

    # ---- 3. 籌碼 (refresh 或回填 cached) ----
    if refresh_chips:
        try:
            summary = build_chip_summary(
                ticker, days=chip_days, root=project_root,
            )
            snap.chip_summary = summary_to_dict(summary)
            snap.chip_summary_obj = summary
        except Exception:
            log.exception("[%s] 籌碼面 refresh 失敗", ticker)
    elif snap.chip_summary is None:
        # 退而求其次：直接讀今日 cache (若有)
        try:
            daily = fetch_daily_chips(now_tw().date(), root=project_root, use_cache=True)
            row = daily.get(ticker)
            if row:
                snap.chip_summary = {
                    "ticker": ticker,
                    "days": 1,
                    "foreign_net": row.foreign_net,
                    "investment_trust_net": row.investment_trust_net,
                    "dealer_net": row.dealer_net,
                    "margin_buy_change_pct": 0.0,
                    "short_borrow_change_pct": 0.0,
                    "block_trade_net": 0.0,
                    "rows": [dataclasses.asdict(row)],
                }
        except Exception:
            pass
        # 仍然沒有 → 直接跑近 N 日 summary（TWSE 公開資料，免費）
        if snap.chip_summary is None:
            try:
                log.info("[%s] 籌碼面快取為空，自動抓近 %d 日", ticker, chip_days)
                summary = build_chip_summary(
                    ticker, days=chip_days, root=project_root,
                )
                snap.chip_summary = summary_to_dict(summary)
                snap.chip_summary_obj = summary
            except Exception:
                log.exception("[%s] 自動抓籌碼面失敗", ticker)

    # ---- 4. 來源檔案清單 ----
    sources: List[Dict[str, str]] = []
    # 4-a ETF 原始抓取頁
    etf_raw_dir = project_root / "data" / "etf_holdings_raw"
    if etf_raw_dir.exists():
        for f in etf_raw_dir.rglob("*.*"):
            try:
                if f.is_file() and f.stat().st_size > 0:
                    text = ""
                    if f.suffix.lower() in (".txt", ".html"):
                        try:
                            text = f.read_text(encoding="utf-8")
                        except Exception:
                            text = ""
                    if ticker in text:
                        sources.append({
                            "type": "etf_holdings_raw",
                            "path": str(f.relative_to(project_root)),
                            "etf": f.parent.name,
                        })
            except Exception:
                continue
    # 4-b ETF 持股 CSV (本檔被列入)
    for e in etfs:
        for d in list_holdings_dates(e.symbol, project_root)[:2]:
            snap_h = load_holdings(e.symbol, d, project_root)
            if not snap_h:
                continue
            if any(h.ticker == ticker for h in snap_h.holdings):
                sources.append({
                    "type": "etf_holdings_csv",
                    "path": f"data/etf_holdings/{e.symbol}/{d.isoformat()}.csv",
                    "etf": e.symbol,
                })
    # 4-c MOPS / 法說 PDF
    for sub in ("data/mops_downloads", "data/mops_cache"):
        d = project_root / sub
        if d.exists():
            for f in d.rglob(f"*{ticker}*"):
                if f.is_file():
                    sources.append({
                        "type": "mops_file",
                        "path": str(f.relative_to(project_root)),
                        "etf": "",
                    })
    snap.source_files = sources

    # ---- 5. 交易紀錄 / 持倉 ----
    trades, qty, avg = _scan_trades_for(ticker, project_root)
    snap.trades = trades
    snap.position_qty = qty
    snap.position_avg_cost = avg
    if qty > 0 and snap.price > 0:
        snap.unrealized_pl = round((snap.price - avg) * qty * 1000.0, 0)  # 假設 1 張 = 1000 股

    # ---- 6. 基本面 (月營收、估值、股利、季報) ----
    # 先試讀快取；快取為空（首次查或之前抓失敗）自動再抓一次，避免使用者誤以為「無資料」。
    try:
        snap.fundamentals = build_fundamental_snapshot(
            ticker,
            name_hint=snap.name,
            root=project_root,
            refresh=refresh_fundamentals,
        )
        if (
            not refresh_fundamentals
            and (snap.fundamentals is None or not snap.fundamentals.has_data)
        ):
            log.info("[%s] 基本面快取為空，自動抓取一次", ticker)
            snap.fundamentals = build_fundamental_snapshot(
                ticker,
                name_hint=snap.name,
                root=project_root,
                refresh=True,
            )
        if snap.fundamentals and snap.fundamentals.name and not snap.name:
            snap.name = snap.fundamentals.name
    except Exception:
        log.exception("[%s] 基本面 snapshot 失敗", ticker)

    # ---- 7. 技術面 (日K + 指標) ----
    try:
        tech_snap, _ = build_technical_snapshot(
            ticker,
            months=technical_months,
            root=project_root,
            refresh=refresh_technicals,
        )
        if not refresh_technicals and not tech_snap.has_data:
            log.info("[%s] 技術面快取為空，自動抓取一次", ticker)
            tech_snap, _ = build_technical_snapshot(
                ticker,
                months=technical_months,
                root=project_root,
                refresh=True,
            )
        snap.technicals = tech_snap
        if tech_snap.has_data:
            if snap.price <= 0:
                snap.price = tech_snap.last_close
            if snap.pct_change == 0.0:
                snap.pct_change = tech_snap.pct_change_1d
    except Exception:
        log.exception("[%s] 技術面 snapshot 失敗", ticker)

    # ---- 8. 集保大戶/散戶 ----
    # TDCC 每週公布、資料免費；無快取時自動抓一次，避免使用者誤以為「無資料」。
    try:
        trend = load_distribution_trend(ticker, root=project_root)
        cur: Optional[DistributionWeekly] = None
        need_refresh = refresh_distribution or not trend.weeks
        if need_refresh:
            try:
                cur = build_distribution_snapshot(ticker, root=project_root)
                if cur is not None:
                    trend = load_distribution_trend(ticker, root=project_root)
            except Exception:
                log.exception("[%s] 自動抓 TDCC 失敗", ticker)
        if cur is None and trend.weeks:
            cur = trend.weeks[-1]
        snap.distribution = cur
        snap.distribution_trend = trend
        label, detail, score = interpret_distribution(trend)
        snap.distribution_label = label
        snap.distribution_detail = detail
        snap.distribution_score = score
    except Exception:
        log.exception("[%s] 集保戶分散 snapshot 失敗", ticker)

    # ---- 9. 季報視角 ----
    try:
        if snap.fundamentals:
            snap.quarterly_view = summarize_quarterly(
                snap.fundamentals.quarterlies,
                snap.fundamentals.revenues,
                today=now_tw().date(),
            )
    except Exception:
        log.exception("[%s] 季報 view 整合失敗", ticker)

    # ---- 9.5 自動 LLM 法說分析（無逐字稿時用 MOPS + 鉅亨新聞自動跑）----
    # ⚠ 隱式 LLM 呼叫點：dashboard 的「個股深入分析」「個股總覽 → 計算評分」
    #    都會走到這裡，呼叫 Gemini 並消耗 token (12 小時內快取)。
    # 條件：尚未有 pipeline 法說分析、且設了 GEMINI_API_KEY。
    # 未設 API Key 時 auto_analyze_ticker 會回 None，graceful-skip，不會收費。
    if snap.llm_analysis is None:
        try:
            from bot.auto_llm import auto_analyze_ticker
            auto = auto_analyze_ticker(
                ticker, root=project_root, name_hint=snap.name, logger=log,
            )
            if auto:
                snap.llm_analysis = auto
        except Exception:
            log.exception("[%s] 自動 LLM 分析失敗", ticker)

    # ---- 9.6 把不會變動的基本資料持久化到 stock_db (供 Google Sheet 同步) ----
    try:
        if snap.name or (snap.fundamentals and snap.fundamentals.name):
            from bot.stock_db import StockInfo, get_db
            db = get_db()
            existing = db.get_stock_info(ticker)
            name_to_save = snap.name or (snap.fundamentals.name if snap.fundamentals else "")
            if existing is None or existing.name != name_to_save:
                info = existing or StockInfo(symbol=ticker)
                if name_to_save:
                    info.name = name_to_save
                db.upsert_stock_info(info)
    except Exception:
        log.debug("[%s] stock_info 持久化失敗", ticker, exc_info=True)

    # ---- 10. 美股 / 跨市場連動 ----
    try:
        from bot.market_macro import (
            fetch_macro_snapshot,
            load_supply_chain,
            macro_to_dict,
            related_us_stocks_for_tw,
        )

        macro_snap = fetch_macro_snapshot(
            root=project_root,
            force_refresh=refresh_macro,
            use_cache=True,
            logger=log,
        )
        snap.macro_snapshot = macro_to_dict(macro_snap)

        sc = load_supply_chain(project_root)
        snap.us_related = related_us_stocks_for_tw(ticker, sc, project_root)

        # ADR 溢價（若此股自身有 ADR）
        for p in macro_snap.adr_premiums:
            if p.tw_ticker == ticker:
                from dataclasses import asdict as _asdict
                snap.adr_premium = _asdict(p)
                break
    except Exception:
        log.exception("[%s] 美股/跨市場資料整合失敗", ticker)

    return snap


# ----------------------------------------------------------------------
# Helper
# ----------------------------------------------------------------------


def _signal_to_dict(s: FollowSignal) -> Dict[str, Any]:
    return {
        "ticker": s.ticker, "name": s.name,
        "signal_type": s.signal_type, "etf_count": s.etf_count,
        "total_weight_delta": round(s.total_weight_delta, 2),
        "note": s.note, "related_etfs": list(s.related_etfs),
    }


def _list_runs(pipeline_dir: Path) -> List[Path]:
    if not pipeline_dir.exists():
        return []
    out: List[Path] = []
    for d in pipeline_dir.iterdir():
        if d.is_dir() and (d / "run.json").exists():
            out.append(d / "run.json")
    out.sort(key=lambda p: p.parent.name, reverse=True)
    return out


def _load_run(p: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _scan_trades_for(
    ticker: str, root: Path,
) -> tuple[List[TradeRecord], float, float]:
    """掃 data/trades_*.csv 找此 ticker 的所有交易。簡易 FIFO 估算持倉。"""
    trades: List[TradeRecord] = []
    qty = 0.0
    cost = 0.0
    sum_cost = 0.0
    trades_dir = root / "data"
    if not trades_dir.exists():
        return [], 0.0, 0.0
    for f in sorted(trades_dir.glob("trades_*.csv")):
        try:
            with f.open("r", encoding="utf-8") as fp:
                reader = csv.DictReader(fp)
                for row in reader:
                    t = (row.get("ticker") or row.get("code") or "").strip()
                    if t != ticker:
                        continue
                    side = (row.get("side") or row.get("action") or "").lower()
                    try:
                        q = float(row.get("qty") or row.get("quantity") or 0)
                        px = float(row.get("price") or 0)
                    except ValueError:
                        continue
                    ts = row.get("ts") or row.get("time") or ""
                    trades.append(TradeRecord(
                        ts=ts, side=side, qty=q, price=px,
                        note=row.get("note", ""),
                    ))
                    if side.startswith("b"):
                        qty += q
                        sum_cost += q * px
                    elif side.startswith("s"):
                        if qty > 0:
                            qty -= q
                            if qty <= 0:
                                qty = 0
                                sum_cost = 0
        except Exception:
            continue
    if qty > 0:
        cost = sum_cost / qty
    return trades, qty, round(cost, 2)


def snapshot_to_dict(s: TickerSnapshot) -> Dict[str, Any]:
    distribution_out: Optional[Dict[str, Any]] = None
    if s.distribution is not None:
        try:
            distribution_out = dataclasses.asdict(s.distribution)
        except TypeError:
            distribution_out = None
    return {
        "ticker": s.ticker, "name": s.name, "fetched_at": s.fetched_at,
        "price": s.price, "pct_change": s.pct_change, "volume": s.volume,
        "chip_summary": s.chip_summary, "consensus": s.consensus,
        "new_build_signal": s.new_build_signal, "add_signal": s.add_signal,
        "held_by_etfs": s.held_by_etfs,
        "llm_analysis": s.llm_analysis, "logic_check": s.logic_check,
        "pipeline_run_id": s.pipeline_run_id, "pipeline_run_dir": s.pipeline_run_dir,
        "source_files": s.source_files,
        "history": [dataclasses.asdict(h) for h in s.history],
        "trades": [dataclasses.asdict(t) for t in s.trades],
        "position_qty": s.position_qty,
        "position_avg_cost": s.position_avg_cost,
        "unrealized_pl": s.unrealized_pl,
        "fundamentals": (
            fundamental_to_dict(s.fundamentals) if s.fundamentals else None
        ),
        "technicals": (
            technical_to_dict(s.technicals) if s.technicals else None
        ),
        "distribution": distribution_out,
        "distribution_label": s.distribution_label,
        "distribution_detail": s.distribution_detail,
        "distribution_score": s.distribution_score,
        "macro_snapshot": s.macro_snapshot,
        "us_related": s.us_related,
        "adr_premium": s.adr_premium,
        "quarterly_view": s.quarterly_view,
    }


__all__ = [
    "HistoryItem",
    "TickerSnapshot",
    "TradeRecord",
    "build_snapshot",
    "snapshot_to_dict",
]
