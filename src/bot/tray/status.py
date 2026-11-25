"""系統匣顯示用的狀態快照：只讀現有的狀態檔，不連網、不登入券商、不開資料庫。

資料來源都是背景工作已經在寫的檔案：
- data/scheduler_status.json       排程器心跳與各工作狀態
- data/freshness_status.json       資料新鮮度檢查（每輪 daily_inputs 重寫）
- data/smile_watch/status-*.json   實際持股的盯盤狀態
- data/smile_watch/holdings.json   券商回報的全部庫存（盯盤每次讀庫存時重寫）
- data/notifications/history.jsonl 本機通知紀錄
- data/etf_focus/latest.json       ETF 關注名單
- data/overnight_brief/latest.json 美股隔夜重點（只讀，不影響健康等級）
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
    cost_note: str = ""  # 成本不是券商帳面均價時的說明
    price_source: str = "primary"  # 盯盤報價來源；舊狀態檔沒有這欄視為主來源
    price_note: str = ""  # 備援報價的說明，例如「鎖漲停、最後成交價 X、時間 T」

    @property
    def source_tag(self) -> str:
        """選單列用的短標示；主來源回傳空字串。"""
        if self.price_source == "primary":
            return ""
        tag = "富果備援" if self.price_source == "fugle" else f"{self.price_source}備援"
        for lock in ("鎖漲停", "鎖跌停"):
            if self.price_note.startswith(lock):
                tag += f"・{lock}"
        return tag

    @property
    def source_note(self) -> str:
        """主視窗用的完整來源說明；主來源回傳空字串。"""
        tag = self.source_tag
        if not tag:
            return ""
        return f"報價來源：{tag.split('・')[0]}" + (f"｜{self.price_note}" if self.price_note else "")

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
        if self.source_tag:
            text += f"　{self.source_tag}"
        if self.stale:
            text += f"　（{self.ts[:10]} 的報價）"
        return text


@dataclass
class OtherHolding:
    """不在盯盤名單的持股：只有券商回報的數字（那次查詢當下的價格與損益）。"""
    symbol: str
    shares: float
    avg_price: float
    last_price: float
    pnl_twd: float

    @property
    def quantity_text(self) -> str:
        if self.shares >= 1000 and self.shares % 1000 == 0:
            return f"{self.shares / 1000:.0f} 張"
        return f"{self.shares:,.0f} 股"

    @property
    def pnl_pct(self) -> Optional[float]:
        cost = self.shares * self.avg_price
        return self.pnl_twd / cost * 100.0 if cost > 0 else None

    def line(self) -> str:
        pct = self.pnl_pct
        return (f"{self.symbol}　{self.quantity_text}　現價 {self.last_price:.2f}　"
                f"損益 {self.pnl_twd:+,.0f}" + ("" if pct is None else f"（{pct:+.1f}%）"))


@dataclass
class FocusSummary:
    as_of: str = ""
    focus_count: int = 0
    caution_count: int = 0
    top_adds: List[str] = field(default_factory=list)
    top_reduces: List[str] = field(default_factory=list)


@dataclass
class OvernightBrief:
    date: str = ""
    generated_at: str = ""
    stale: bool = False  # date 不是今天
    market_lines: List[str] = field(default_factory=list)
    summary_status: str = ""
    points: List[str] = field(default_factory=list)
    candidate_count: int = 0
    warnings: List[str] = field(default_factory=list)


@dataclass
class TraySnapshot:
    level: str
    headline: str
    checks: List[Check] = field(default_factory=list)
    holdings: List[HoldingStatus] = field(default_factory=list)
    other_holdings: List[OtherHolding] = field(default_factory=list)
    holdings_asof: str = ""  # 券商庫存的查詢時間；空字串＝還沒有全部持股的資料
    notifications: List[dict] = field(default_factory=list)
    focus: FocusSummary = field(default_factory=FocusSummary)
    overnight: OvernightBrief = field(default_factory=OvernightBrief)
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


def read_cost_note(root: Path, symbol: str, lots: int, avg_price: float) -> str:
    """盯盤改用「賣出先扣最近買進」算成本時的說明；沒有改用或資料對不上就回傳空字串。

    張數與均價都要和盯盤狀態一致才顯示：執行中的盯盤還是舊設定時，狀態檔仍是券商帳面均價。
    """
    data = _read_json(root / "data" / "smile_watch" / f"lots-{symbol}.json")
    try:
        if int(data["lots"]) != lots or abs(float(data["avg_price"]) - avg_price) > 0.005:
            return ""
        text = f"成本以「賣出先扣最近買進」計，券商帳面均價 {float(data['broker_avg_price']):.2f}"
        if data.get("sales"):
            text += (f"；{str(data['since'])[5:]} 起已實現 {float(data['realized_pnl_twd']):+,.0f} 元"
                     f"（券商帳面 {float(data['broker_realized_pnl_twd']):+,.0f}）")
        return text
    except (KeyError, TypeError, ValueError):
        return ""


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
                stale=str(data.get("ts", ""))[:10] != now.date().isoformat(),
                cost_note=read_cost_note(root, str(data["symbol"]), int(data["lots"]),
                                         float(data["avg_price"])),
                price_source=str(data.get("price_source") or "primary"),
                price_note=str(data.get("price_note") or "")))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def read_all_holdings(root: Path) -> tuple:
    """券商回報的全部庫存。回傳（查詢時間, [OtherHolding]）；檔案不存在或壞掉回傳 ("", [])。"""
    data = _read_json(root / "data" / "smile_watch" / "holdings.json")
    if not isinstance(data, dict) or not isinstance(data.get("holdings"), list):
        return "", []
    out = []
    for row in data["holdings"]:
        try:
            item = OtherHolding(symbol=str(row["symbol"]), shares=float(row["shares"]),
                                avg_price=float(row["avg_price"]),
                                last_price=float(row["last_price"]), pnl_twd=float(row["pnl_twd"]))
        except (KeyError, TypeError, ValueError):
            continue
        if item.shares > 0:
            out.append(item)
    return str(data.get("asof", "")), out


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


_INDEX_SHORT = {"^GSPC": "S&P 500", "^IXIC": "NASDAQ", "^DJI": "道瓊", "^SOX": "費半", "^VIX": "VIX"}
_BRIEF_STOCKS = ("TSM", "NVDA", "AMD", "MU")


def _pct_text(row: dict, label: str) -> str:
    return f"{label} {float(row['pct_change']):+.2f}%"


def _market_lines(market) -> List[str]:
    if not isinstance(market, dict) or market.get("status") != "ok":
        return ["行情暫時取不到"]
    indices, stocks = [], []
    for row in market.get("indices") or []:
        try:
            label = _INDEX_SHORT.get(row["symbol"]) or str(row.get("name") or row["symbol"])
            indices.append(_pct_text(row, label))
        except (KeyError, TypeError, ValueError):
            continue
    picked = {}
    for row in market.get("stocks") or []:
        try:
            if row["symbol"] in _BRIEF_STOCKS:
                picked[row["symbol"]] = _pct_text(row, "台積電 ADR" if row["symbol"] == "TSM"
                                                  else row["symbol"])
        except (KeyError, TypeError, ValueError):
            continue
    stocks = [picked[s] for s in _BRIEF_STOCKS if s in picked]
    return [("　".join(part)) for part in (indices, stocks) if part]


def _night_line(night) -> List[str]:
    """台指期夜盤一行；沒有資料時不顯示（舊版 latest.json 沒有這個欄位）。"""
    if not isinstance(night, dict) or night.get("status") != "ok":
        return []
    front = night.get("front") if isinstance(night.get("front"), dict) else {}
    try:
        text = f"台指期夜盤 {float(front['last']):,.0f}（{float(front['pct_change']):+.2f}%）"
    except (KeyError, TypeError, ValueError):
        return []
    if not front.get("closed"):
        text += " 未收盤"
    return [text]


def read_overnight(root: Path, now: dt.datetime) -> OvernightBrief:
    data = _read_json(Path(root) / "data" / "overnight_brief" / "latest.json")
    if not isinstance(data, dict):
        return OvernightBrief()
    try:
        summary = data["summary"] if isinstance(data.get("summary"), dict) else {}
        status = str(summary.get("status", ""))
        points = []
        if status == "ok":
            for p in summary.get("points") or []:
                if isinstance(p, dict) and p.get("text"):
                    points.append(str(p["text"]))
        news = data.get("news") if isinstance(data.get("news"), dict) else {}
        warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
        date = str(data.get("date", ""))
        return OvernightBrief(
            date=date, generated_at=str(data.get("generated_at", "")),
            stale=date != now.date().isoformat(),
            market_lines=(_market_lines(data.get("market"))
                          + _night_line(data.get("night_futures"))),
            summary_status=status, points=points[:3],
            candidate_count=len(news.get("candidates") or []),
            warnings=[str(w) for w in warnings])
    except (TypeError, ValueError):
        return OvernightBrief()


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
    holdings = read_holdings(root, now)
    watched = {h.symbol for h in holdings}
    holdings_asof, everything = read_all_holdings(root)
    return TraySnapshot(level=level, headline=headline, checks=checks,
                        holdings=holdings, holdings_asof=holdings_asof,
                        other_holdings=[h for h in everything if h.symbol not in watched],
                        notifications=read_notifications(root), focus=read_focus(root),
                        overnight=read_overnight(root, now),
                        scheduler_state=scheduler_state, heartbeat_age_seconds=heartbeat_age,
                        running_jobs=running, read_at=now.isoformat(timespec="seconds"))
