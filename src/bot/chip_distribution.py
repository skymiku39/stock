"""chip_distribution -- 集保戶股權分散表 (大戶 vs 散戶) 自動拉取。

資料來源
========
集保結算所 (TDCC) 公開資料 — 每週公布的股權分散表：
* https://opendata.tdcc.com.tw/getOD.ashx?id=1-5

欄位定義 (節錄)：
    資料日期 / 證券代號 / 持股分級 / 人數 / 股數 / 占集保庫存數比例(%)

對應 Gemini 對話中的「大戶與羊群持股比例」維度：
* 大戶（鯨魚）：持股 ≥ 400 張 (持股分級 14 以上) 或 ≥ 1000 張 (分級 15+)
* 散戶（羊群）：持股 < 10 張 (分級 1-3)

設計
====
* 整張表會被快取在 `data/chip_distribution/<date>.json`
* 個股摘要會額外存 `data/chip_distribution/<ticker>/history.json`，
  方便畫趨勢圖（大戶比例 vs 散戶比例 時間序列）
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from bot.utils import get_logger, mk_folder, now_tw


URL_TDCC = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"


# 持股分級 (TDCC) — 分 15 級
# 1:1-999 股 / 2:1,000-5,000 / 3:5,001-10,000 / 4:10,001-15,000 ...
# 13:600,001-800,000 / 14:800,001-1,000,000 / 15:>1,000,000 (張數 ≥ 1000)
# 14 以上 ≈ 持股 800 張以上 (≈ 400 張需用 12 為界)
LEVEL_LARGE = {12, 13, 14, 15}        # 大戶（>= 400 張）
LEVEL_WHALE = {14, 15}                # 超大戶（>= 800 張）
LEVEL_RETAIL = {1, 2, 3}              # 散戶（< 10 張）


@dataclass
class DistributionLevel:
    """單一持股分級的人數/股數/占比。"""

    level: int
    label: str
    holders: int = 0
    shares: float = 0.0
    pct: float = 0.0


@dataclass
class DistributionWeekly:
    """個股單週集保股權分散摘要。"""

    ticker: str
    week_date: str               # ISO 日期
    total_holders: int = 0
    total_shares: float = 0.0
    levels: List[DistributionLevel] = field(default_factory=list)

    large_holder_pct: float = 0.0    # 大戶持股占比 %
    whale_holder_pct: float = 0.0    # 超大戶持股占比 %
    retail_holder_pct: float = 0.0   # 散戶持股占比 %

    large_holder_count: int = 0
    whale_holder_count: int = 0
    retail_holder_count: int = 0


@dataclass
class DistributionTrend:
    """個股集保歷史趨勢。"""

    ticker: str
    weeks: List[DistributionWeekly] = field(default_factory=list)

    def latest(self) -> Optional[DistributionWeekly]:
        if not self.weeks:
            return None
        return self.weeks[-1]

    def large_holder_change_pct(self, lookback_weeks: int = 4) -> Optional[float]:
        """近 N 週大戶比例變動 (百分點)。"""
        if len(self.weeks) < 2:
            return None
        recent = self.weeks[-1]
        anchor_idx = max(0, len(self.weeks) - 1 - lookback_weeks)
        anchor = self.weeks[anchor_idx]
        return round(recent.large_holder_pct - anchor.large_holder_pct, 2)

    def retail_holder_change_pct(self, lookback_weeks: int = 4) -> Optional[float]:
        if len(self.weeks) < 2:
            return None
        recent = self.weeks[-1]
        anchor_idx = max(0, len(self.weeks) - 1 - lookback_weeks)
        anchor = self.weeks[anchor_idx]
        return round(recent.retail_holder_pct - anchor.retail_holder_pct, 2)


# ----------------------------------------------------------------------
# HTTP & 快取
# ----------------------------------------------------------------------


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
    })
    return s


def _root_dir(root: Optional[Path] = None) -> Path:
    base = (root or Path.cwd()) / "data" / "chip_distribution"
    mk_folder(str(base))
    return base


def _ticker_dir(ticker: str, root: Optional[Path] = None) -> Path:
    p = _root_dir(root) / ticker
    mk_folder(str(p))
    return p


def _to_int(x: Any) -> int:
    if x is None:
        return 0
    try:
        return int(str(x).replace(",", "").strip() or 0)
    except Exception:
        return 0


def _to_float(x: Any) -> float:
    if x is None:
        return 0.0
    try:
        return float(str(x).replace(",", "").strip() or 0)
    except Exception:
        return 0.0


# ----------------------------------------------------------------------
# 抓取
# ----------------------------------------------------------------------


def fetch_distribution_all(
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    use_cache: bool = True,
    cache_ttl: int = 12 * 3600,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """抓 TDCC 最新一期股權分散表 (全市場)。"""
    log = logger or get_logger("chip-dist")
    sess = session or _session()
    today = now_tw().date()
    cache_path = _root_dir(root) / f"raw_{today.isoformat()}.json"
    if use_cache and cache_path.exists():
        try:
            age = time.time() - cache_path.stat().st_mtime
            if age <= cache_ttl:
                return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        resp = sess.get(URL_TDCC, timeout=60)
        if resp.status_code != 200:
            log.warning("TDCC HTTP %d", resp.status_code)
            return []
        data = resp.json()
    except Exception:
        log.exception("TDCC 抓取失敗")
        return []
    if not isinstance(data, list):
        return []
    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def parse_distribution_for_ticker(
    raw: List[Dict[str, Any]],
    ticker: str,
) -> Optional[DistributionWeekly]:
    """從整張 TDCC 表抓出單一 ticker 的分級。"""
    rows = [r for r in raw if str(r.get("證券代號") or r.get("stock_code") or "").strip() == ticker]
    if not rows:
        return None
    week = ""
    levels: List[DistributionLevel] = []
    total_holders = 0
    total_shares = 0.0
    for r in rows:
        week = str(r.get("資料日期") or r.get("data_date") or week)
        lvl = _to_int(r.get("持股分級") or r.get("level"))
        label = str(r.get("持股分級") and r.get("持股/單位數分級") or "")
        # 部分版本欄位名稱不同
        if not label:
            label = str(r.get("HoldingsLevel") or "")
        holders = _to_int(r.get("人數") or r.get("number_of_holders"))
        shares = _to_float(r.get("股數") or r.get("number_of_shares"))
        pct = _to_float(r.get("占集保庫存數比例(%)") or r.get("percent_of_holdings"))
        levels.append(DistributionLevel(
            level=lvl, label=label or str(lvl),
            holders=holders, shares=shares, pct=pct,
        ))
        total_holders += holders
        total_shares += shares

    # 將 week 字串標準化為 ISO 日期 (TDCC 通常給 YYYYMMDD 或 YYYY/MM/DD)
    week_iso = _normalize_date(week)
    levels.sort(key=lambda l: l.level)

    snap = DistributionWeekly(
        ticker=ticker,
        week_date=week_iso,
        total_holders=total_holders,
        total_shares=total_shares,
        levels=levels,
    )
    snap.large_holder_pct = round(sum(l.pct for l in levels if l.level in LEVEL_LARGE), 3)
    snap.whale_holder_pct = round(sum(l.pct for l in levels if l.level in LEVEL_WHALE), 3)
    snap.retail_holder_pct = round(sum(l.pct for l in levels if l.level in LEVEL_RETAIL), 3)
    snap.large_holder_count = sum(l.holders for l in levels if l.level in LEVEL_LARGE)
    snap.whale_holder_count = sum(l.holders for l in levels if l.level in LEVEL_WHALE)
    snap.retail_holder_count = sum(l.holders for l in levels if l.level in LEVEL_RETAIL)
    return snap


def _normalize_date(s: str) -> str:
    s = s.strip()
    if not s:
        return ""
    # YYYYMMDD
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # YYYY/MM/DD
    parts = re.split(r"[/-]", s)
    if len(parts) == 3:
        try:
            y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
            if y < 1911:
                y += 1911
            return dt.date(y, mo, d).isoformat()
        except Exception:
            pass
    return s


# ----------------------------------------------------------------------
# 個股趨勢
# ----------------------------------------------------------------------


def build_distribution_snapshot(
    ticker: str,
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    refresh: bool = True,
) -> Optional[DistributionWeekly]:
    """抓最新一期，並 append 到該 ticker 的趨勢歷史。"""
    log = logger or get_logger("chip-dist")
    if refresh:
        raw = fetch_distribution_all(
            root=root, session=session, logger=log,
        )
    else:
        latest_cache = sorted(_root_dir(root).glob("raw_*.json"), reverse=True)
        if not latest_cache:
            raw = []
        else:
            try:
                raw = json.loads(latest_cache[0].read_text(encoding="utf-8"))
            except Exception:
                raw = []
    snap = parse_distribution_for_ticker(raw, ticker) if raw else None
    if not snap:
        return None
    # append to history
    hist_path = _ticker_dir(ticker, root) / "history.json"
    history: List[Dict[str, Any]] = []
    if hist_path.exists():
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:
            history = []
    history = [h for h in history if h.get("week_date") != snap.week_date]
    history.append({
        "ticker": snap.ticker,
        "week_date": snap.week_date,
        "total_holders": snap.total_holders,
        "total_shares": snap.total_shares,
        "large_holder_pct": snap.large_holder_pct,
        "whale_holder_pct": snap.whale_holder_pct,
        "retail_holder_pct": snap.retail_holder_pct,
        "large_holder_count": snap.large_holder_count,
        "whale_holder_count": snap.whale_holder_count,
        "retail_holder_count": snap.retail_holder_count,
    })
    history.sort(key=lambda h: h.get("week_date", ""))
    hist_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    # 個股目前一週完整明細
    detail_path = _ticker_dir(ticker, root) / f"{snap.week_date or 'latest'}.json"
    detail_path.write_text(
        json.dumps(snapshot_to_dict(snap), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return snap


def load_distribution_trend(
    ticker: str,
    *,
    root: Optional[Path] = None,
) -> DistributionTrend:
    hist_path = _ticker_dir(ticker, root) / "history.json"
    if not hist_path.exists():
        return DistributionTrend(ticker=ticker)
    try:
        history = json.loads(hist_path.read_text(encoding="utf-8"))
    except Exception:
        return DistributionTrend(ticker=ticker)
    weeks: List[DistributionWeekly] = []
    for h in history:
        try:
            weeks.append(DistributionWeekly(
                ticker=ticker,
                week_date=h.get("week_date", ""),
                total_holders=h.get("total_holders", 0),
                total_shares=h.get("total_shares", 0.0),
                large_holder_pct=h.get("large_holder_pct", 0.0),
                whale_holder_pct=h.get("whale_holder_pct", 0.0),
                retail_holder_pct=h.get("retail_holder_pct", 0.0),
                large_holder_count=h.get("large_holder_count", 0),
                whale_holder_count=h.get("whale_holder_count", 0),
                retail_holder_count=h.get("retail_holder_count", 0),
            ))
        except Exception:
            continue
    weeks.sort(key=lambda w: w.week_date)
    return DistributionTrend(ticker=ticker, weeks=weeks)


def snapshot_to_dict(s: DistributionWeekly) -> Dict[str, Any]:
    return {
        "ticker": s.ticker,
        "week_date": s.week_date,
        "total_holders": s.total_holders,
        "total_shares": s.total_shares,
        "large_holder_pct": s.large_holder_pct,
        "whale_holder_pct": s.whale_holder_pct,
        "retail_holder_pct": s.retail_holder_pct,
        "large_holder_count": s.large_holder_count,
        "whale_holder_count": s.whale_holder_count,
        "retail_holder_count": s.retail_holder_count,
        "levels": [asdict(l) for l in s.levels],
    }


def interpret_distribution(
    trend: DistributionTrend,
) -> Tuple[str, str, float]:
    """把趨勢翻成「大戶吸籌 / 大戶倒貨 / 中性」 + 分數 0-100。"""
    latest = trend.latest()
    if not latest:
        return "no_data", "尚無 TDCC 資料", 50.0
    delta_large = trend.large_holder_change_pct(lookback_weeks=4) or 0.0
    delta_retail = trend.retail_holder_change_pct(lookback_weeks=4) or 0.0
    score = 50.0 + delta_large * 5.0 - delta_retail * 3.0
    score = max(0.0, min(100.0, score))
    if delta_large >= 0.5 and delta_retail <= -0.2:
        label = "accumulation"
        detail = f"大戶 +{delta_large:+.2f}pp、散戶 {delta_retail:+.2f}pp，呈現吸籌結構"
    elif delta_large <= -0.5 and delta_retail >= 0.2:
        label = "distribution"
        detail = f"大戶 {delta_large:+.2f}pp、散戶 +{delta_retail:+.2f}pp，疑似出貨"
        score = max(0.0, score - 10.0)
    else:
        label = "neutral"
        detail = f"大戶 {delta_large:+.2f}pp、散戶 {delta_retail:+.2f}pp，無明顯方向"
    return label, detail, round(score, 1)


__all__ = [
    "DistributionLevel",
    "DistributionTrend",
    "DistributionWeekly",
    "LEVEL_LARGE",
    "LEVEL_RETAIL",
    "LEVEL_WHALE",
    "build_distribution_snapshot",
    "fetch_distribution_all",
    "interpret_distribution",
    "load_distribution_trend",
    "parse_distribution_for_ticker",
    "snapshot_to_dict",
]
