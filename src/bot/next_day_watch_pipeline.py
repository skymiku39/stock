"""next_day_watch_pipeline -- 「明日當沖預備清單」主流程。

定位
====
與 ``intraday_pipeline`` (今日盤前) 互補，本管線聚焦「明日當沖預備」：
盤後 14:00-18:00 跑 **draft**、隔日凌晨 02:00-06:00 跑 **update**。

三大候選來源
============
1. **題材延續 (carry_themes)**  -- 沿用今日 / 今晚熱門題材，挑明日續熱的補漲、二線標的
2. **強勢承接 (strong_carry)**  -- 今日收盤強勢 (漲幅 > 0、量比放大) 且法人買超
3. **明日事件 (event_focus)**   -- 法說 / 財報 / 權息 / 政策事件對應受惠股 (來源：conference_calendar + 新聞)

主流程
======
1. 計算 target_date = 下一個交易日
2. 抓今日新聞 + macro snapshot (依 mode 選擇是否強制刷新)
3. 取明日法說 catalysts (conference_calendar)
4. 掃描候選池 (watchlist + ETF 共識 + 明日 conference) → 算今日強勢度
5. LLM ``next_day_radar`` → 萃取題材 + 事件 + 強勢承接候選
6. 合併三大來源 → 算 next_day_score
7. 排序 → LLM ``next_day_brief`` 寫明日預備清單簡報
8. 輸出到 ``data/next_day_watch/<target_date>/report.json``
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bot.config import Settings
from bot.intraday_pipeline import _consensus_tickers_today, _macro_summary_text, _parse_json
from bot.llm_analyzer import GeminiClient, gemini_call
from bot.market_macro import fetch_macro_snapshot, load_supply_chain, macro_to_dict
from bot.news_fetcher import fetch_today_news, news_to_compact_text
from bot.utils import get_logger, mk_folder, now_tw


REPORT_TYPE = "next_day_watch"


# ----------------------------------------------------------------------
# 資料模型
# ----------------------------------------------------------------------


@dataclass
class NextDayCandidate:
    """明日預備清單候選股。"""

    ticker: str
    name: str = ""

    # 來源
    sources: List[str] = field(default_factory=list)  # 'carry_theme','strong_carry','event','watchlist','etf'
    theme: str = ""
    theme_heat: int = 0
    entry_logic: str = ""           # 補漲 / 強勢承接 / 拉回承接 / 事件驅動

    # 今日盤後數據
    today_close: Optional[float] = None
    today_pct_change: float = 0.0
    volume_ratio: float = 0.0       # 今日量 / 20日均量
    strength_score: float = 0.0     # 0-100，由 pct/量比/法人共同計算

    # 籌碼
    foreign_net: float = 0.0
    investment_trust_net: float = 0.0
    chip_summary_text: str = ""

    # 事件
    event: str = ""                 # 法說 / 財報 / 權息 / 政策
    event_time: str = ""            # 明日上午 / 明日下午 / 盤前 / 盤後 / 未指定
    event_side: str = ""            # 受惠 / 受壓 / 中性

    # 美股連動 (供參考)
    adr_premium_pct: Optional[float] = None

    # 綜合分
    next_day_score: float = 0.0
    watch_level: str = ""           # open_strong / pullback_buy / observe
    note: str = ""                  # LLM/規則給的一句話原因


@dataclass
class NextDayReport:
    asof: str                       # 本次跑的時間日期 (台股交易日)
    target_date: str = ""           # 明日目標交易日
    mode: str = "draft"             # draft | update
    market_tone: str = "neutral"
    overall_brief: str = ""
    carry_themes: List[Dict[str, Any]] = field(default_factory=list)
    event_focus: List[Dict[str, Any]] = field(default_factory=list)
    strong_carry_llm: List[Dict[str, Any]] = field(default_factory=list)
    rankings: List[NextDayCandidate] = field(default_factory=list)
    macro_summary: Dict[str, Any] = field(default_factory=dict)
    catalyst_count: int = 0
    brief_md: str = ""
    brief_prompt_id: str = ""
    brief_prompt_version: str = ""
    duration_sec: float = 0.0
    errors: List[str] = field(default_factory=list)
    output_dir: str = ""


# ----------------------------------------------------------------------
# 交易日 / 工具
# ----------------------------------------------------------------------


def next_trading_day(asof: dt.date) -> dt.date:
    """回傳 ``asof`` 之後的下一個非週末日期。

    不檢國定假日 (TWSE 缺檔資料時 pipeline 自動容錯)。
    """
    cur = asof + dt.timedelta(days=1)
    while cur.weekday() >= 5:  # 5=Sat, 6=Sun
        cur += dt.timedelta(days=1)
    return cur


def _to_int(x: Any) -> Optional[int]:
    try:
        return int(float(x))
    except Exception:
        return None


# ----------------------------------------------------------------------
# 1) 候選池來源
# ----------------------------------------------------------------------


def _watchlist_tickers(root: Path) -> List[Tuple[str, str]]:
    try:
        from bot import watchlist as wl_mod
        wl = wl_mod.load(root)
        return [(i.ticker, i.name) for i in wl.items if i.ticker.isdigit()]
    except Exception:
        return []


def _tomorrow_event_tickers(
    target_date: dt.date,
    root: Path,
    log: logging.Logger,
) -> List[Dict[str, Any]]:
    """從 conference_calendar 取明日法說 / 財報事件。"""
    try:
        from bot.conference_calendar import load_calendar
        items = load_calendar(root)
        out: List[Dict[str, Any]] = []
        for e in items:
            if hasattr(e.date, "isoformat"):
                d = e.date
            else:
                try:
                    d = dt.date.fromisoformat(str(e.date))
                except Exception:
                    continue
            if d != target_date:
                continue
            if not (e.ticker and e.ticker.isdigit()):
                continue
            out.append({
                "ticker": e.ticker,
                "name": e.company or "",
                "event": "法說會",
                "event_time": e.time or "未指定",
            })
        log.info("明日 (%s) 法說事件 %d 檔", target_date.isoformat(), len(out))
        return out
    except Exception:
        log.debug("load conference_calendar 失敗", exc_info=True)
        return []


# ----------------------------------------------------------------------
# 2) 今日強勢承接掃描
# ----------------------------------------------------------------------


def _today_strength_for_ticker(
    ticker: str,
    *,
    today: dt.date,
    project_root: Path,
    refresh: bool,
    log: logging.Logger,
) -> Optional[Dict[str, Any]]:
    """取單檔今日 K 線 / 量比 / RSI 摘要。

    回傳 ``None`` 表示資料不可用或非今日。
    """
    try:
        from bot.technicals import build_technical_snapshot
        snap, _df = build_technical_snapshot(
            ticker, months=4, root=project_root, refresh=refresh, logger=log,
        )
    except Exception:
        log.debug("[%s] technical snapshot 失敗", ticker, exc_info=True)
        return None

    if snap.rows < 5 or not snap.last_close:
        return None

    last_date = snap.last_date or ""
    try:
        last_d = dt.date.fromisoformat(last_date)
    except Exception:
        return None

    # 只接受最近 3 個交易日內的資料 (週末 / 假日仍可用)
    if (today - last_d).days > 4:
        return None

    vol_ratio = 0.0
    if snap.vol_ma20 and snap.vol_ma20 > 0:
        vol_ratio = round((snap.volume_last or 0) / snap.vol_ma20, 2)

    return {
        "ticker": ticker,
        "last_date": last_date,
        "close": snap.last_close,
        "pct_1d": snap.pct_change_1d,
        "vol_ratio": vol_ratio,
        "rsi14": snap.rsi14 or 0.0,
        "ma20": snap.ma20,
    }


def _chip_for_ticker(
    ticker: str,
    *,
    today: dt.date,
    project_root: Path,
    log: logging.Logger,
) -> Optional[Dict[str, Any]]:
    """取近 3 日法人累計買賣超。"""
    try:
        from bot.chips_fetcher import build_chip_summary, summary_to_dict
        summary = build_chip_summary(
            ticker,
            end_date=today,
            days=3,
            root=project_root,
            logger=log,
        )
        if not summary or not summary.rows:
            return None
        return summary_to_dict(summary)
    except Exception:
        log.debug("[%s] chip summary 失敗", ticker, exc_info=True)
        return None


def _compute_strength_score(
    *,
    pct_1d: float,
    vol_ratio: float,
    foreign_net: float,
    trust_net: float,
) -> float:
    """0-100 強勢承接綜合分。

    規則 (簡化版)：
    * 漲幅 0%~+6%: 0 -> 40 分；> 6% 過熱衰減
    * 量比 1.0~3.0: 0 -> 30 分；> 3.0 鎖死 30
    * 法人合計買超: > +1000 張 +20、> +500 +10、< -500 -10
    * 上限 100、下限 0
    """
    s = 0.0
    if pct_1d >= 0:
        s += min(40.0, pct_1d * 7.5)
        if pct_1d > 7:
            s -= (pct_1d - 7) * 4
    else:
        s += max(-10.0, pct_1d * 2.5)

    if vol_ratio > 0:
        s += min(30.0, (vol_ratio - 0.8) * 18)

    fnet = foreign_net + trust_net
    if fnet >= 1000:
        s += 20
    elif fnet >= 500:
        s += 10
    elif fnet <= -500:
        s -= 10

    return max(0.0, min(100.0, round(s, 1)))


def _scan_today_strength(
    tickers: Iterable[str],
    *,
    today: dt.date,
    project_root: Path,
    refresh: bool,
    log: logging.Logger,
    max_tickers: int = 60,
) -> Dict[str, Dict[str, Any]]:
    """掃描候選池中每檔今日表現，回傳 ticker -> strength data。"""
    out: Dict[str, Dict[str, Any]] = {}
    pool = list(dict.fromkeys(tickers))[:max_tickers]
    log.info("掃描 %d 檔今日強勢度 (refresh=%s) ...", len(pool), refresh)
    for t in pool:
        tech = _today_strength_for_ticker(
            t, today=today, project_root=project_root, refresh=refresh, log=log,
        )
        if tech is None:
            continue
        chip = _chip_for_ticker(
            t, today=today, project_root=project_root, log=log,
        )
        foreign = chip.get("foreign_net", 0.0) if chip else 0.0
        trust = chip.get("investment_trust_net", 0.0) if chip else 0.0
        strength = _compute_strength_score(
            pct_1d=tech["pct_1d"],
            vol_ratio=tech["vol_ratio"],
            foreign_net=foreign,
            trust_net=trust,
        )
        out[t] = {
            **tech,
            "foreign_net": foreign,
            "investment_trust_net": trust,
            "strength_score": strength,
        }
    log.info("成功掃描 %d 檔", len(out))
    return out


def _strong_carry_text(
    strength_map: Dict[str, Dict[str, Any]],
    name_map: Dict[str, str],
    top_n: int = 15,
) -> str:
    """挑強勢度前 N 名，輸出為 LLM 可讀文字。"""
    rows = sorted(
        [(t, d) for t, d in strength_map.items() if d.get("pct_1d", 0) > 0],
        key=lambda x: -x[1].get("strength_score", 0),
    )[:top_n]
    if not rows:
        return "(今日無顯著強勢承接候選)"
    lines: List[str] = []
    for i, (t, d) in enumerate(rows, 1):
        nm = name_map.get(t, "")
        lines.append(
            f"{i:02d}. {t} {nm} ｜ 漲{d['pct_1d']:+.2f}% ｜ 量比 {d['vol_ratio']:.2f} ｜ "
            f"外資{d['foreign_net']:+.0f}/投信{d['investment_trust_net']:+.0f} ｜ 強勢分 {d['strength_score']:.0f}"
        )
    return "\n".join(lines)


def _radar_candidate_tickers(report: NextDayReport) -> List[Tuple[str, str]]:
    """Collect tickers discovered by LLM radar output."""
    out: List[Tuple[str, str]] = []
    seen: set[str] = set()

    def add(raw_ticker: Any, raw_name: Any = "") -> None:
        ticker = str(raw_ticker or "").strip()
        if not ticker or not ticker.isdigit() or ticker in seen:
            return
        seen.add(ticker)
        out.append((ticker, str(raw_name or "").strip()))

    for theme in report.carry_themes:
        for candidate in theme.get("candidate_tickers", []) or []:
            add(candidate.get("ticker"), candidate.get("name"))

    for candidate in report.strong_carry_llm:
        add(candidate.get("ticker"), candidate.get("name"))

    for event in report.event_focus:
        for candidate in event.get("tickers", []) or []:
            add(candidate.get("ticker"), candidate.get("name"))

    return out


def _catalyst_text(events: List[Dict[str, Any]]) -> str:
    if not events:
        return "(無已知法說/權息)"
    lines: List[str] = []
    for i, e in enumerate(events, 1):
        lines.append(
            f"{i:02d}. {e.get('ticker','')} {e.get('name','')} "
            f"｜ {e.get('event','法說')} ｜ {e.get('event_time','未指定')}"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 3) 主流程
# ----------------------------------------------------------------------


def run_next_day_watch(
    *,
    mode: str = "draft",
    project_root: Optional[Path] = None,
    settings: Optional[Settings] = None,
    news_limit: int = 120,
    candidate_limit: int = 25,
    scan_limit: int = 60,
    force_refresh_news: bool = False,
    force_refresh_macro: bool = False,
    force_refresh_technicals: Optional[bool] = None,
    target_date: Optional[dt.date] = None,
    logger: Optional[logging.Logger] = None,
) -> NextDayReport:
    """跑明日當沖預備清單管線。

    Args:
        mode: ``draft`` (盤後初版) 或 ``update`` (凌晨更新版)。
        force_refresh_technicals: ``None`` 表示依 mode 預設 (update=True、draft=True 第一次)；
            可手動覆寫。
    """
    if mode not in ("draft", "update"):
        raise ValueError("mode 必須是 'draft' 或 'update'")

    log = logger or get_logger("next-day")
    root = project_root or Path.cwd()
    settings = settings or Settings()
    today = now_tw().date()
    target = target_date or next_trading_day(today)

    if force_refresh_technicals is None:
        # update 必須刷今夜美股、draft 也建議刷一次今日 K 線
        force_refresh_technicals = True

    log.info(
        "=== next-day-watch (%s) === today=%s, target=%s",
        mode, today.isoformat(), target.isoformat(),
    )
    report = NextDayReport(
        asof=today.isoformat(),
        target_date=target.isoformat(),
        mode=mode,
    )
    t0 = time.time()

    # ---- 1. 新聞 ----
    log.info("[1/7] 抓今日新聞 (force_refresh=%s) ...", force_refresh_news)
    try:
        news_items = fetch_today_news(
            limit=news_limit, force_refresh=force_refresh_news,
            root=root, logger=log,
        )
        news_text = news_to_compact_text(news_items, max_chars=15000)
        log.info("新聞 %d 條", len(news_items))
    except Exception as e:
        log.exception("news 抓取失敗")
        report.errors.append(f"news: {e}")
        news_items = []
        news_text = ""

    # ---- 2. Macro ----
    log.info("[2/7] 抓 macro (force=%s) ...", force_refresh_macro)
    try:
        macro_snap = fetch_macro_snapshot(
            root=root, force_refresh=force_refresh_macro, logger=log,
        )
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

    # ---- 3. 明日事件 ----
    log.info("[3/7] 取明日 catalysts ...")
    try:
        from bot.conference_calendar import ensure_calendar_fresh
        ensure_calendar_fresh(root, logger=log)
    except Exception:
        log.debug("ensure_calendar_fresh 失敗", exc_info=True)
    events = _tomorrow_event_tickers(target, root, log)
    report.catalyst_count = len(events)
    catalyst_text = _catalyst_text(events)

    # ---- 4. 候選池 + 強勢度掃描 ----
    log.info("[4/7] 組候選池 + 掃描今日強勢度 ...")
    wl = _watchlist_tickers(root)
    name_map: Dict[str, str] = {t: n for t, n in wl}
    consensus = _consensus_tickers_today(root)
    event_tickers = [e["ticker"] for e in events]
    for e in events:
        name_map.setdefault(e["ticker"], e.get("name", ""))

    initial_pool: List[str] = []
    for src_list in (event_tickers, [t for t, _n in wl], consensus):
        for t in src_list:
            if t and t.isdigit() and t not in initial_pool:
                initial_pool.append(t)

    strength_map = _scan_today_strength(
        initial_pool,
        today=today,
        project_root=root,
        refresh=force_refresh_technicals,
        log=log,
        max_tickers=scan_limit,
    )
    today_strong_text = _strong_carry_text(strength_map, name_map)

    # ---- 5. LLM next_day_radar ----
    client = GeminiClient(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        logger=log,
    )
    radar_obj: Dict[str, Any] = {}
    if client.enabled:
        log.info("[5/7] LLM next_day_radar (%s) ...", mode)
        try:
            raw, _info = gemini_call(
                "next_day_radar",
                client=client,
                metadata={"task": "next_day_radar", "mode": mode, "target": target.isoformat()},
                target_date=target.isoformat(),
                mode=mode,
                news_text=news_text or "(無新聞)",
                today_strong_text=today_strong_text,
                macro_summary=macro_text,
                catalyst_text=catalyst_text,
            )
            if raw:
                obj = _parse_json(raw)
                if obj:
                    radar_obj = obj
                    report.market_tone = str(obj.get("market_tone") or "neutral")
                    report.overall_brief = str(obj.get("overall_brief") or "")
                    report.carry_themes = obj.get("carry_themes") or []
                    report.event_focus = obj.get("event_focus") or []
                    report.strong_carry_llm = obj.get("strong_carry") or []
                    log.info(
                        "next_day_radar: %d 題材 / %d 事件 / %d 強勢承接 (tone=%s)",
                        len(report.carry_themes), len(report.event_focus),
                        len(report.strong_carry_llm), report.market_tone,
                    )
        except Exception as e:
            log.exception("next_day_radar 失敗")
            report.errors.append(f"next_day_radar: {e}")
    else:
        log.info("[5/7] (略) LLM 未啟用")

    # LLM can discover topical/event candidates that were not in the initial
    # watchlist/ETF/event scan. Fill their technical/chip data before scoring.
    radar_tickers = _radar_candidate_tickers(report)
    for ticker, name in radar_tickers:
        if name:
            name_map.setdefault(ticker, name)
    missing_radar_tickers = [
        ticker for ticker, _name in radar_tickers if ticker not in strength_map
    ]
    if missing_radar_tickers:
        log.info(
            "[5.5/7] 補掃 LLM 題材/事件候選 %d 檔今日強勢度 ...",
            len(missing_radar_tickers),
        )
        strength_map.update(_scan_today_strength(
            missing_radar_tickers,
            today=today,
            project_root=root,
            refresh=force_refresh_technicals,
            log=log,
            max_tickers=scan_limit,
        ))

    # ---- 6. 合併候選股 + 算 next_day_score ----
    log.info("[6/7] 合併三大來源 + 排序 ...")
    pool: Dict[str, NextDayCandidate] = {}

    def _ensure(t: str, nm: str = "") -> NextDayCandidate:
        if t not in pool:
            pool[t] = NextDayCandidate(ticker=t, name=nm or name_map.get(t, ""))
        elif nm and not pool[t].name:
            pool[t].name = nm
        return pool[t]

    # (a) carry_themes 候選
    for th in report.carry_themes:
        heat = int(th.get("heat", 0) or 0)
        theme = str(th.get("theme", ""))
        for ct in th.get("candidate_tickers", []) or []:
            t = str(ct.get("ticker", "")).strip()
            if not t or not t.isdigit():
                continue
            row = _ensure(t, str(ct.get("name", "")))
            if heat > row.theme_heat:
                row.theme = theme
                row.theme_heat = heat
                row.entry_logic = row.entry_logic or str(ct.get("entry_logic", ""))
            if "carry_theme" not in row.sources:
                row.sources.append("carry_theme")

    # (b) strong_carry LLM 標的
    for sc in report.strong_carry_llm:
        t = str(sc.get("ticker", "")).strip()
        if not t or not t.isdigit():
            continue
        row = _ensure(t, str(sc.get("name", "")))
        if "strong_carry" not in row.sources:
            row.sources.append("strong_carry")
        row.entry_logic = row.entry_logic or "強勢承接"
        row.watch_level = row.watch_level or str(sc.get("watch_level", ""))
        row.note = row.note or str(sc.get("reason", ""))

    # (c) event_focus 標的
    for ev in report.event_focus:
        event = str(ev.get("event", ""))
        event_time = str(ev.get("event_time", ""))
        for ct in ev.get("tickers", []) or []:
            t = str(ct.get("ticker", "")).strip()
            if not t or not t.isdigit():
                continue
            row = _ensure(t, str(ct.get("name", "")))
            row.event = row.event or event
            row.event_time = row.event_time or event_time
            row.event_side = row.event_side or str(ct.get("side", ""))
            if "event" not in row.sources:
                row.sources.append("event")
            row.entry_logic = row.entry_logic or "事件驅動"

    # (d) 規則層補：今日強勢且漲超過 +3% / 量比 > 1.5 也納入排序候選
    for t, d in strength_map.items():
        if d.get("pct_1d", 0) < 1.0 and d.get("strength_score", 0) < 35:
            continue
        row = _ensure(t, name_map.get(t, ""))
        if "strong_carry" not in row.sources and d.get("strength_score", 0) >= 50:
            row.sources.append("strong_carry")
            row.entry_logic = row.entry_logic or "強勢承接"

    # (e) conference 事件兜底 (即便 LLM 沒抓到)
    for ev in events:
        t = ev["ticker"]
        row = _ensure(t, ev.get("name", ""))
        if "event" not in row.sources:
            row.sources.append("event")
        row.event = row.event or ev.get("event", "法說")
        row.event_time = row.event_time or ev.get("event_time", "未指定")

    # (f) watchlist / etf tag (僅標 source，不主動入榜)
    for t, _nm in wl:
        if t in pool:
            if "watchlist" not in pool[t].sources:
                pool[t].sources.append("watchlist")
    for t in consensus:
        if t in pool:
            if "etf" not in pool[t].sources:
                pool[t].sources.append("etf")

    # 套上強勢度 / 籌碼資料
    for t, row in pool.items():
        s = strength_map.get(t)
        if s:
            row.today_close = s.get("close")
            row.today_pct_change = s.get("pct_1d", 0.0)
            row.volume_ratio = s.get("vol_ratio", 0.0)
            row.strength_score = s.get("strength_score", 0.0)
            row.foreign_net = s.get("foreign_net", 0.0)
            row.investment_trust_net = s.get("investment_trust_net", 0.0)
            row.chip_summary_text = (
                f"外資{row.foreign_net:+.0f}/投信{row.investment_trust_net:+.0f}"
            )

        # ADR 溢價
        for p in macro.get("adr_premiums") or []:
            if p.get("tw_ticker") == t:
                row.adr_premium_pct = float(p.get("premium_pct", 0) or 0)
                break

    # 算 next_day_score
    for row in pool.values():
        row.next_day_score = round(_next_day_composite(row), 1)

    rankings = sorted(
        pool.values(),
        key=lambda r: (-r.next_day_score, -r.strength_score, -r.theme_heat),
    )[:candidate_limit]
    report.rankings = rankings

    # ---- 7. LLM next_day_brief ----
    if client.enabled and (report.carry_themes or rankings):
        log.info("[7/7] LLM next_day_brief ...")
        try:
            themes_payload = {
                "carry_themes": report.carry_themes,
                "event_focus": report.event_focus,
                "strong_carry": report.strong_carry_llm,
            }
            top_for_brief = [asdict(r) for r in rankings[:15]]
            raw, info = gemini_call(
                "next_day_brief",
                client=client,
                metadata={"task": "next_day_brief", "mode": mode, "target": target.isoformat()},
                target_date=target.isoformat(),
                mode=mode,
                themes_json=json.dumps(themes_payload, ensure_ascii=False, indent=2),
                ranked_candidates_json=json.dumps(top_for_brief, ensure_ascii=False, indent=2),
                macro_summary=macro_text,
                catalyst_text=catalyst_text,
            )
            if raw:
                report.brief_md = raw
                report.brief_prompt_id = info.get("prompt_id", "")
                report.brief_prompt_version = info.get("prompt_version", "")
        except Exception as e:
            log.exception("next_day_brief 失敗")
            report.errors.append(f"next_day_brief: {e}")
    else:
        log.info("[7/7] (略) LLM 未啟用或無題材")

    report.duration_sec = round(time.time() - t0, 2)

    out_dir = root / "data" / "next_day_watch" / target.isoformat()
    report.output_dir = str(out_dir)

    # ---- 持久化 ----
    mk_folder(str(out_dir))
    report_json = _report_to_json(report)
    try:
        # 同一目標日 draft / update 各存一份
        fname = "report.json" if mode == "draft" else "report_update.json"
        (out_dir / fname).write_text(
            json.dumps(report_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if report.brief_md:
            brief_name = "next_day_brief.md" if mode == "draft" else "next_day_brief_update.md"
            (out_dir / brief_name).write_text(report.brief_md, encoding="utf-8")
    except Exception:
        log.exception("next-day 持久化失敗")
    _persist_report_json_to_db(report_json, root=root, log=log)

    log.info(
        "next-day-watch 完成 (%.1fs, 題材 %d / 候選 %d / 事件 %d / 錯誤 %d)",
        report.duration_sec, len(report.carry_themes),
        len(rankings), len(events), len(report.errors),
    )
    return report


# ----------------------------------------------------------------------
# 排序綜合分
# ----------------------------------------------------------------------


def _next_day_composite(row: NextDayCandidate) -> float:
    """next_day_score = 50% 強勢承接 + 30% 題材熱度 + 20% 事件加分。

    再依 source 多樣性 (多個來源同時命中) 加 5-10 分。
    """
    s = row.strength_score * 0.5
    s += row.theme_heat * 6  # heat 1~5 → 6~30
    if row.event:
        bonus = 14
        if row.event_side == "受惠":
            bonus = 18
        elif row.event_side == "受壓":
            bonus = -8
        s += bonus

    bonus_src = 0
    if {"strong_carry", "carry_theme"}.issubset(set(row.sources)):
        bonus_src += 6
    if "event" in row.sources and len(row.sources) > 1:
        bonus_src += 4
    s += bonus_src

    # 過熱衰減：今日漲幅 > 7% → 抑制 (避開追高)
    if row.today_pct_change > 7:
        s -= (row.today_pct_change - 7) * 3
    return max(0.0, min(100.0, s))


# ----------------------------------------------------------------------
# 序列化 / 讀取
# ----------------------------------------------------------------------


def _report_to_json(r: NextDayReport) -> Dict[str, Any]:
    return {
        "asof": r.asof,
        "target_date": r.target_date,
        "mode": r.mode,
        "market_tone": r.market_tone,
        "overall_brief": r.overall_brief,
        "carry_themes": r.carry_themes,
        "event_focus": r.event_focus,
        "strong_carry_llm": r.strong_carry_llm,
        "rankings": [asdict(c) for c in r.rankings],
        "macro_summary": r.macro_summary,
        "catalyst_count": r.catalyst_count,
        "brief_md": r.brief_md,
        "brief_prompt_id": r.brief_prompt_id,
        "brief_prompt_version": r.brief_prompt_version,
        "duration_sec": r.duration_sec,
        "errors": r.errors,
        "output_dir": r.output_dir,
    }


def load_latest_next_day(
    root: Optional[Path] = None,
    *,
    prefer_update: bool = True,
) -> Optional[Dict[str, Any]]:
    """讀最近一次 next_day_watch 報告。

    優先讀 ``report_update.json`` (凌晨更新版)，沒有再退 ``report.json``。
    """
    root_path = root or Path.cwd()
    try:
        from bot.stock_db import StockDB
        db = StockDB.open(root=root_path)
        rows = db.list_llm_daily_reports(report_type=REPORT_TYPE, limit=200)
        seen_dates: List[str] = []
        for row in rows:
            if row.report_date not in seen_dates:
                seen_dates.append(row.report_date)
        for date_iso in seen_dates:
            data = load_next_day_by_date(
                root_path,
                date_iso,
                prefer_update=prefer_update,
            )
            if data:
                return data
    except Exception:
        get_logger("next-day").debug("load latest next-day from DB failed", exc_info=True)

    base = root_path / "data" / "next_day_watch"
    if not base.exists():
        return None
    days = sorted([d for d in base.iterdir() if d.is_dir()], key=lambda p: p.name, reverse=True)
    for d in days:
        candidates = ["report_update.json", "report.json"] if prefer_update else ["report.json", "report_update.json"]
        for fname in candidates:
            p = d / fname
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    _persist_report_json_to_db(data, root=root_path, log=get_logger("next-day"))
                    return data
                except Exception:
                    continue
    return None


def load_next_day_by_date(
    root: Optional[Path] = None,
    target_date: Optional[dt.date | str] = None,
    *,
    prefer_update: bool = True,
    mode: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """依 target_date 讀取明日當沖報告，不會觸發 LLM 生成。"""
    root_path = root or Path.cwd()
    if target_date is None:
        date_iso = next_trading_day(now_tw().date()).isoformat()
    elif hasattr(target_date, "isoformat"):
        date_iso = target_date.isoformat()  # type: ignore[union-attr]
    else:
        date_iso = str(target_date)

    modes = [mode] if mode else (
        ["update", "draft"] if prefer_update else ["draft", "update"]
    )

    try:
        from bot.stock_db import StockDB
        db = StockDB.open(root=root_path)
        for m in modes:
            data = _daily_report_row_to_payload(
                db.get_llm_daily_report(REPORT_TYPE, date_iso, mode=m or "")
            )
            if data:
                return data
    except Exception:
        get_logger("next-day").debug("load next-day from DB failed", exc_info=True)

    file_names = {
        "draft": "report.json",
        "update": "report_update.json",
    }
    for m in modes:
        fname = file_names.get(m or "")
        if not fname:
            continue
        p = root_path / "data" / "next_day_watch" / date_iso / fname
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            _persist_report_json_to_db(data, root=root_path, log=get_logger("next-day"))
            return data
        except Exception:
            get_logger("next-day").debug("load next-day file failed: %s", p, exc_info=True)
    return None


def load_next_day_by_asof(
    root: Optional[Path] = None,
    asof_date: Optional[dt.date | str] = None,
    *,
    prefer_update: bool = True,
    mode: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """依產生日(asof)讀取明日當沖報告，不會觸發 LLM 生成。"""
    root_path = root or Path.cwd()
    if asof_date is None:
        date_iso = now_tw().date().isoformat()
    elif hasattr(asof_date, "isoformat"):
        date_iso = asof_date.isoformat()  # type: ignore[union-attr]
    else:
        date_iso = str(asof_date)

    try:
        from bot.stock_db import StockDB
        db = StockDB.open(root=root_path)
        rows = db.list_llm_daily_reports(report_type=REPORT_TYPE, limit=500)
        seen_dates: List[str] = []
        for row in rows:
            if row.asof != date_iso:
                continue
            if mode is not None and row.mode != mode:
                continue
            if row.report_date not in seen_dates:
                seen_dates.append(row.report_date)
        for target_iso in seen_dates:
            data = load_next_day_by_date(
                root_path,
                target_iso,
                prefer_update=prefer_update,
                mode=mode,
            )
            if data and str(data.get("asof") or "") == date_iso:
                return data
    except Exception:
        get_logger("next-day").debug("load next-day by asof from DB failed", exc_info=True)

    base = root_path / "data" / "next_day_watch"
    if not base.exists():
        return None
    days = sorted([d for d in base.iterdir() if d.is_dir()], key=lambda p: p.name, reverse=True)
    for d in days:
        data = load_next_day_by_date(
            root_path,
            d.name,
            prefer_update=prefer_update,
            mode=mode,
        )
        if data and str(data.get("asof") or "") == date_iso:
            return data
    return None


def _daily_report_row_to_payload(row: Any) -> Optional[Dict[str, Any]]:
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
    payload.setdefault("_db_mode", row.mode)
    return payload


def _persist_report_json_to_db(
    data: Dict[str, Any],
    *,
    root: Path,
    log: logging.Logger,
) -> None:
    report_date = str(data.get("target_date") or "").strip()
    if not report_date:
        return
    mode = str(data.get("mode") or "draft")
    try:
        from bot.stock_db import LlmDailyReportRow, StockDB
        db = StockDB.open(root=root)
        db.upsert_llm_daily_report(
            LlmDailyReportRow(
                report_type=REPORT_TYPE,
                report_date=report_date,
                mode=mode,
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
        log.exception("next-day DB 持久化失敗")


__all__ = [
    "NextDayCandidate",
    "NextDayReport",
    "load_next_day_by_asof",
    "load_next_day_by_date",
    "load_latest_next_day",
    "next_trading_day",
    "run_next_day_watch",
]
