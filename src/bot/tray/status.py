"""系統匣顯示用的狀態快照：只讀現有的狀態檔，不連網、不登入券商、不開資料庫。

資料來源都是背景工作已經在寫的檔案：
- data/scheduler_status.json       排程器心跳與各工作狀態
- data/freshness_status.json       資料新鮮度檢查（每輪 daily_inputs 重寫）
- data/smile_watch/status-*.json   實際持股的盯盤狀態
- data/notifications/history.jsonl 本機通知紀錄
- data/etf_focus/latest.json       ETF 關注名單
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
LEVEL_LABEL = {OK: "正常", WARN: "有警告", FAIL: "有問題", UNKNOWN: "讀不到狀態"}
HEARTBEAT_STALE_SECONDS = 180      # 排程器超過這麼久沒有心跳就視為沒有回應
FRESHNESS_STALE_SECONDS = 30 * 60  # 新鮮度檢查超過這麼久沒重寫就提醒
_RANK = {OK: 0, UNKNOWN: 1, WARN: 2, FAIL: 3}


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class HoldingStatus:
    symbol: str
    price: float
    lots: int
    avg_price: float
    pnl_twd: float
    pnl_pct: float
    breakeven_price: float
    stop_price: Optional[float]
    signals: list
    ts: str
    stale: bool  # 不是今天的報價

    @property
    def stop_gap_pct(self) -> Optional[float]:
        """現價距離停損價還有幾 %（正值＝還沒到）。"""
        if not self.stop_price or self.price <= 0:
            return None
        return (self.price - self.stop_price) / self.price * 100.0

    def line(self) -> str:
        text = (f"{self.symbol}　{self.lots} 張　現價 {self.price:.2f}　"
                f"損益 {self.pnl_twd:+,.0f}（{self.pnl_pct:+.1f}%）")
        gap = self.stop_gap_pct
        if gap is not None:
            text += f"　距停損 {gap:.1f}%"
        if self.stale:
            text += f"　（{self.ts[:10]} 的報價）"
        return text


@dataclass
class FocusSummary:
    as_of: str = ""
    focus_count: int = 0
    caution_count: int = 0
    top_adds: List[str] = field(default_factory=list)
    top_reduces: List[str] = field(default_factory=list)


@dataclass
class TraySnapshot:
    level: str
    headline: str
    checks: List[Check] = field(default_factory=list)
    holdings: List[HoldingStatus] = field(default_factory=list)
    notifications: List[dict] = field(default_factory=list)
    focus: FocusSummary = field(default_factory=FocusSummary)
    scheduler_state: str = ""
    heartbeat_age_seconds: Optional[float] = None
    running_jobs: List[str] = field(default_factory=list)
    read_at: str = ""

    def problems(self) -> List[Check]:
        return [c for c in self.checks if c.status in (WARN, FAIL)]

    def tooltip(self) -> str:
        """系統匣提示文字（Windows 上限 127 字）。"""
        lines = [f"Stock Lab：{LEVEL_LABEL[self.level]}", self.headline]
        lines += [f"{h.symbol} {h.price:.2f}（{h.pnl_pct:+.1f}%）" for h in self.holdings[:2]]
        return "\n".join(line for line in lines if line)[:127]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _age_seconds(text, now: dt.datetime) -> Optional[float]:
    try:
        then = dt.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=now.tzinfo)
    return (now - then).total_seconds()


def _worst(levels) -> str:
    return max(levels, key=lambda level: _RANK.get(level, 1), default=UNKNOWN)


def read_holdings(root: Path, now: dt.datetime) -> List[HoldingStatus]:
    out = []
    for path in sorted((root / "data" / "smile_watch").glob("status-*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        try:
            out.append(HoldingStatus(
                symbol=str(data["symbol"]), price=float(data["price"]), lots=int(data["lots"]),
                avg_price=float(data["avg_price"]), pnl_twd=float(data["pnl_twd"]),
                pnl_pct=float(data["pnl_pct"]), breakeven_price=float(data["breakeven_price"]),
                stop_price=None if data.get("stop_price") is None else float(data["stop_price"]),
                signals=list(data.get("signals") or []), ts=str(data.get("ts", "")),
                stale=str(data.get("ts", ""))[:10] != now.date().isoformat()))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def read_notifications(root: Path, limit: int = 5) -> List[dict]:
    try:
        lines = (root / "data" / "notifications" / "history.jsonl").read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("title"):
            out.append(row)
            if len(out) >= limit:
                break
    return out


def read_focus(root: Path, top: int = 5) -> FocusSummary:
    data = _read_json(root / "data" / "etf_focus" / "latest.json")
    if not isinstance(data, dict):
        return FocusSummary()
    stocks = [s for s in data.get("stocks", []) if isinstance(s, dict)]

    def label(stock) -> str:
        return f"{stock.get('ticker', '')} {stock.get('name', '')}（{stock.get('active_net', 0):+d}）"

    adds = sorted((s for s in stocks if s.get("active_net", 0) > 0),
                  key=lambda s: -s["active_net"])[:top]
    reduces = sorted((s for s in stocks if s.get("active_net", 0) < 0),
                     key=lambda s: s["active_net"])[:top]
    return FocusSummary(as_of=str(data.get("as_of") or ""),
                        focus_count=len(data.get("focus", [])),
                        caution_count=len(data.get("caution", [])),
                        top_adds=[label(s) for s in adds],
                        top_reduces=[label(s) for s in reduces])


def read_snapshot(root: Path, now: dt.datetime) -> TraySnapshot:
    root = Path(root)
    scheduler = _read_json(root / "data" / "scheduler_status.json")
    freshness = _read_json(root / "data" / "freshness_status.json")
    levels, headline = [], ""
    scheduler_state, heartbeat_age, running = "", None, []

    if isinstance(scheduler, dict):
        scheduler_state = str(scheduler.get("state", ""))
        heartbeat_age = _age_seconds(scheduler.get("heartbeat_at"), now)
        jobs = scheduler.get("jobs") if isinstance(scheduler.get("jobs"), dict) else {}
        running = sorted(name for name, job in jobs.items()
                         if isinstance(job, dict) and job.get("state") == "running")
        if heartbeat_age is None or heartbeat_age > HEARTBEAT_STALE_SECONDS:
            levels.append(FAIL)
            minutes = "" if heartbeat_age is None else f"（{heartbeat_age / 60:.0f} 分鐘沒有心跳）"
            headline = f"排程器沒有回應{minutes}"
        else:
            levels.append(OK)
    else:
        levels.append(UNKNOWN)
        headline = "讀不到排程器狀態"

    checks: List[Check] = []
    if isinstance(freshness, dict):
        for row in freshness.get("checks", []):
            if isinstance(row, dict) and row.get("name"):
                status = str(row.get("status", UNKNOWN))
                checks.append(Check(str(row["name"]),
                                    status if status in (OK, WARN, FAIL) else OK,
                                    str(row.get("detail", ""))))
        levels.extend(c.status for c in checks)
        age = _age_seconds(freshness.get("checked_at"), now)
        if age is None or age > FRESHNESS_STALE_SECONDS:
            levels.append(WARN)
            checks.insert(0, Check("狀態檢查", WARN,
                                   "資料新鮮度檢查超過 30 分鐘沒有更新，下面的內容可能是舊的"))
    else:
        levels.append(UNKNOWN)
        headline = headline or "還沒有資料新鮮度檢查的結果"

    level = _worst(levels)
    if not headline:
        problems = [c for c in checks if c.status in (WARN, FAIL)]
        fails = [c for c in problems if c.status == FAIL]
        if problems:
            names = "、".join(c.name for c in (fails or problems)[:3])
            headline = f"{len(problems)} 項要注意：{names}"
        else:
            headline = "資料與排程都正常"
    return TraySnapshot(level=level, headline=headline, checks=checks,
                        holdings=read_holdings(root, now),
                        notifications=read_notifications(root), focus=read_focus(root),
                        scheduler_state=scheduler_state, heartbeat_age_seconds=heartbeat_age,
                        running_jobs=running, read_at=now.isoformat(timespec="seconds"))
