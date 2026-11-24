"""實際持股的微笑盯盤：只提醒，不建立也不送出任何委託。

微笑策略（`strategy_smile.py`）只認模擬帳戶的部位。這個模組把券商的實際庫存（唯讀查詢）
當成一批微笑持倉，輪詢交易所公開報價，用同一個 `SmileCurveEngine` 的規則判斷，
條件第一次成立時寫紀錄、跳本機通知，並發 Telegram（有設定才發）。買賣由使用者自己決定與操作。
另外，持股當天第一筆成交相對昨收跳空達門檻（預設 ±3%）時提醒一次（名單外的持股也一起查）。

    python -m bot.smile_watch --symbol 1514 --reference 105
    python -m bot.smile_watch --config config/smile_watch.json   # 排程器用

限制：
- 實際庫存只有均價，沒有逐批成交紀錄，整個部位視為一批；已經買過哪幾階無從得知，
  所以加碼提醒只看「跌幅已達階梯、且張數低於 MAX_LOT_PER_SYMBOL」。
- 報價是公開資訊觀測站的延遲報價；這一輪沒有成交價時用委買委賣中間價，只有單邊掛單時略過。
- 沒有「時事＋即時股價」的情緒賣出判斷。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from bot.smile_curve import SmileCurveEngine, SmileLot
from bot.trade_cost import buy_cash_required
from bot.utils import configure_utf8_stdio, get_logger, now_tw

SIGNAL_LABELS = {
    "take_profit": "出場條件成立",
    "stop_loss": "單檔停損觸發",
    "stop_near": "接近單檔停損",
    "breakeven": "部位轉為淨利",
    "add_buy": "跌幅已達加碼階梯",
}
USABLE_PRICE_BASIS = ("last_trade", "previous_trade", "bid_ask_mid")
PRIMARY_SOURCE = "primary"
FUGLE_SOURCE = "fugle"
OPEN_GAP_SIGNAL = "open_gap"
DEFAULT_OPEN_GAP_PCT = 3.0  # config/smile_watch.json 的 open_gap_alert_pct；0＝停用


@dataclass
class Holding:
    symbol: str
    lots: int  # 張
    avg_price: float
    broker_avg_price: Optional[float] = None  # 有值＝avg_price 已改用「賣出先扣最近買進」重算


@dataclass
class WatchStatus:
    symbol: str
    ts: str
    price: float
    lots: int
    avg_price: float
    reference: float
    cost_twd: float
    pnl_twd: float
    pnl_pct: float
    drop_pct: float  # 自標準價回落 %
    breakeven_price: float
    stop_price: Optional[float]  # None＝未啟用單檔停損
    tiers_crossed: int
    add_room_lots: int
    signals: list = field(default_factory=list)
    price_source: str = PRIMARY_SOURCE  # 報價來源：primary（主來源）或 fugle（備援）
    price_note: str = ""  # 備援報價的說明，例如「鎖漲停、最後成交價 X、時間 T」


def parse_holding(symbol: str, text: str) -> Holding:
    """'4@85.65' → 4 張、均價 85.65。"""
    try:
        lots, price = text.split("@")
        holding = Holding(symbol, int(lots), float(price))
    except ValueError as exc:
        raise ValueError(f"--holding 格式為 張數@均價，收到 {text!r}") from exc
    if holding.lots <= 0 or holding.avg_price <= 0:
        raise ValueError(f"--holding 的張數與均價須 > 0：{text!r}")
    return holding


def build_engine(holding: Holding, reference: float, settings) -> SmileCurveEngine:
    if reference <= 0:
        raise ValueError("標準價須 > 0")
    engine = SmileCurveEngine(holding.symbol, settings)
    engine.state.reference = float(reference)
    engine.state.lots = [SmileLot(holding.avg_price, holding.lots, "lot", "")]
    return engine


def _solve_price(engine: SmileCurveEngine, target_pnl: float) -> float:
    """未實現淨損益等於 target_pnl 的股價（損益對股價單調遞增，二分法）。"""
    low, high = 0.0, max(engine.state.reference, engine.state.lots[0].entry_price) * 4
    for _ in range(60):
        mid = (low + high) / 2
        if engine.unrealized_pnl(mid) < target_pnl:
            low = mid
        else:
            high = mid
    return high


def evaluate(engine: SmileCurveEngine, price: float, ts: str,
             near_stop_pct: float = 3.0) -> WatchStatus:
    """純函式：以目前股價套用微笑規則，回傳狀態與成立中的訊號。"""
    settings = engine.settings
    lots = engine.state.lots
    held = sum(l.quantity for l in lots)
    cost = sum(buy_cash_required(l.entry_price, l.quantity, l.unit, settings=settings)
               for l in lots)
    pnl = engine.unrealized_pnl(price)
    drop = engine._drop_pct(price)
    stop_pct = float(getattr(settings, "smile_symbol_stop_loss_pct", 0.0))
    stop_price = _solve_price(engine, -stop_pct / 100.0 * cost) if stop_pct > 0 else None
    crossed = sum(1 for tier in engine.tiers if drop + 1e-9 >= tier.drop_pct)
    room = max(0, int(settings.max_lot_per_symbol) - held)

    signals = []
    if engine.take_profit_hit(price):
        signals.append("take_profit")
    elif pnl > 0:
        signals.append("breakeven")
    if engine.stop_loss_breached(price):
        signals.append("stop_loss")
    elif stop_price is not None and price <= stop_price * (1 + near_stop_pct / 100.0):
        signals.append("stop_near")
    if crossed and room > 0 and "stop_loss" not in signals:
        signals.append("add_buy")
    return WatchStatus(
        symbol=engine.symbol, ts=ts, price=price, lots=held, avg_price=lots[0].entry_price,
        reference=engine.state.reference, cost_twd=round(cost, 2), pnl_twd=round(pnl, 2),
        pnl_pct=round(100.0 * pnl / cost, 2) if cost else 0.0, drop_pct=round(drop, 2),
        breakeven_price=round(_solve_price(engine, 0.0), 2),
        stop_price=None if stop_price is None else round(stop_price, 2),
        tiers_crossed=crossed, add_room_lots=room, signals=signals)


def format_alert(status: WatchStatus, signal: str) -> str:
    lines = [f"微笑盯盤 {status.symbol}：{SIGNAL_LABELS[signal]}",
             f"現價 {status.price:.2f}｜{status.lots} 張、均價 {status.avg_price:.2f}",
             f"未實現淨損益 {status.pnl_twd:,.0f} 元（{status.pnl_pct:+.1f}%）",
             f"標準價 {status.reference:.2f}（回落 {status.drop_pct:.1f}%）｜"
             f"損益兩平 {status.breakeven_price:.2f}"]
    if status.stop_price is not None:
        lines.append(f"單檔停損價約 {status.stop_price:.2f}")
    if signal == "add_buy":
        lines.append(f"已跌破 {status.tiers_crossed} 階，距單檔上限還有 {status.add_room_lots} 張")
    if status.price_source != PRIMARY_SOURCE:
        lines.append(f"報價來源：{status.price_source}（備援）｜{status.price_note}")
    lines.append("只是規則提醒，程式不會下單。")
    return "\n".join(lines)


class SmileWatcher:
    """同一個訊號每個交易日只提醒一次；狀態存檔，重啟後不重複提醒。"""

    def __init__(self, out_dir: Path, notify: Optional[Callable[[str], None]] = None,
                 logger=None):
        self.out_dir = Path(out_dir)
        self.notify = notify
        self.logger = logger or get_logger("smile-watch")
        self._state_path = self.out_dir / "state.json"
        self._alerted: dict = {}
        if self._state_path.exists():
            try:
                self._alerted = json.loads(self._state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                self.logger.warning("讀取 %s 失敗，視為沒有提醒過：%s", self._state_path, exc)

    def _write(self, path: Path, payload) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False),
                        encoding="utf-8")
        temp.replace(path)

    def handle(self, status: WatchStatus) -> list:
        """寫最新狀態；回傳這次新發出的訊號。"""
        day = status.ts[:10]
        seen = self._alerted.get(status.symbol, {})
        if seen.get("day") != day:
            seen = {"day": day, "signals": []}
        fresh = [s for s in status.signals if s not in seen["signals"]]
        self._write(self.out_dir / f"status-{status.symbol}.json", asdict(status))
        for signal in fresh:
            text = format_alert(status, signal)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            with (self.out_dir / "alerts.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"signal": signal, **asdict(status)},
                                    ensure_ascii=False, allow_nan=False) + "\n")
            self.logger.warning(text.replace("\n", "｜"))
            if self.notify:
                self.notify(text)
        if fresh:
            seen["signals"] = seen["signals"] + fresh
            self._alerted[status.symbol] = seen
            self._write(self._state_path, self._alerted)
        return fresh

    def handle_open_gap(self, symbol: str, quote: Optional[dict], day: str, threshold_pct: float,
                        held: str = "") -> bool:
        """持股開盤跳空：當天第一筆成交相對昨收的漲跌幅 |x| ≥ 門檻就提醒一次。回傳這次是否發出提醒。

        第一筆成交用 MIS 的開盤價 o；沒有 o 時用當天第一次拿到的成交價 z（不用委買委賣中間價）。
        一旦拿到第一筆成交就記為「今天已判斷」，之後的成交價不再拿來判斷（不論有沒有提醒）。
        """
        if threshold_pct <= 0:
            return False
        seen = self._alerted.get(symbol, {})
        if seen.get("day") != day:
            seen = {"day": day, "signals": []}
        if OPEN_GAP_SIGNAL in seen["signals"] or seen.get("open_gap_checked"):
            return False
        got = first_trade_price(quote, day)
        prev_close = float((quote or {}).get("prev_close") or 0.0)
        if got is None or prev_close <= 0:
            return False
        price, basis = got
        gap_pct = round(100.0 * (price - prev_close) / prev_close, 2)
        fired = abs(gap_pct) + 1e-9 >= threshold_pct
        if fired:
            text = format_open_gap_alert(symbol, price, prev_close, gap_pct, basis, held)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            with (self.out_dir / "alerts.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"signal": OPEN_GAP_SIGNAL, "symbol": symbol, "day": day,
                                     "first_trade": price, "basis": basis,
                                     "prev_close": prev_close, "gap_pct": gap_pct},
                                    ensure_ascii=False, allow_nan=False) + "\n")
            self.logger.warning(text.replace("\n", "｜"))
            if self.notify:
                self.notify(text)
            seen["signals"] = seen["signals"] + [OPEN_GAP_SIGNAL]
        seen["open_gap_checked"] = True
        self._alerted[symbol] = seen
        self._write(self._state_path, self._alerted)
        return fired


def first_trade_price(quote: Optional[dict], today: str) -> Optional[tuple]:
    """回傳 (當天第一筆成交價, 依據)；依據是 open（開盤價 o）或 last_trade（第一次拿到的成交價 z）。

    只認當日報價；只有委買委賣（中間價、單邊）時回 None，不拿來判斷跳空。
    """
    if not quote or quote.get("quote_date") != today:
        return None
    if quote.get("open"):
        return float(quote["open"]), "open"
    if quote.get("last_trade"):
        return float(quote["last_trade"]), "last_trade"
    return None


def format_open_gap_alert(symbol: str, price: float, prev_close: float, gap_pct: float,
                          basis: str, held: str = "") -> str:
    lines = [f"持股開盤跳空 {symbol}：{'開高' if gap_pct > 0 else '開低'} {gap_pct:+.1f}%",
             f"第一筆成交 {price:.2f}｜昨收 {prev_close:.2f}"
             f"（{'開盤價' if basis == 'open' else '開盤價未提供，用當天第一次拿到的成交價'}）"]
    if held:
        lines.append(f"持有 {held}")
    lines.append("只是提醒，程式不會下單。")
    return "\n".join(lines)


def load_open_gap_pct(path: Path) -> float:
    """讀 config/smile_watch.json 的 open_gap_alert_pct；檔案或欄位不存在用預設 3，0＝停用。"""
    path = Path(path)
    if not path.exists():
        return DEFAULT_OPEN_GAP_PCT
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get(
            "open_gap_alert_pct", DEFAULT_OPEN_GAP_PCT)
        pct = float(DEFAULT_OPEN_GAP_PCT if value is None else value)
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{path} 的 open_gap_alert_pct 格式錯誤：{exc}") from exc
    if pct < 0:
        raise ValueError(f"{path} 的 open_gap_alert_pct 不可為負：{pct}")
    return pct


def read_other_holdings(root: Path) -> dict:
    """讀 data/smile_watch/holdings.json（券商全部庫存快照，唯讀）：{代號: 股數}。讀不到回空。"""
    path = Path(root) / "data" / "smile_watch" / "holdings.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("holdings", [])
        return {str(r["symbol"]): float(r.get("shares") or 0) for r in rows
                if float(r.get("shares") or 0) > 0}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
        return {}


def make_notify_all(senders: list, logger=None) -> Callable[[str], None]:
    """逐一送出；任何一個通道（例如 Telegram）出錯都不影響其他通道（例如本機通知）。"""
    def notify_all(text: str) -> None:
        for send in senders:
            try:
                send(text)
            except Exception:  # noqa: BLE001 - 通知失敗不能讓盯盤停下
                if logger:
                    logger.warning("通知通道 %s 發送失敗", getattr(send, "__qualname__", send))
    return notify_all


@dataclass
class WatchTarget:
    symbol: str
    reference: float
    stop_loss_pct: Optional[float] = None  # None＝用 SMILE_SYMBOL_STOP_LOSS_PCT
    holding: Optional[Holding] = None  # 手動給的張數與均價；None＝讀券商庫存
    latest_first_since: Optional[str] = None  # 這天起的賣出改扣最近買進的；None＝照券商先進先出


def load_targets(path: Path) -> list:
    """讀盯盤名單：[{"symbol": "1514", "reference": 105, "stop_loss_pct": 25, "holding": "4@85.65",
    "sell_latest_first_since": "2026-11-23"}]。

    後三欄可省略。sell_latest_first_since：這天起的賣出改認定為賣掉最近買進的那筆（券商是扣最舊的），
    均價、損益兩平與停損價都照這個認定算；只對讀券商庫存的標的有效。檔案不存在回傳空名單；內容不合法丟 ValueError。
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("targets", [])
        targets = []
        for row in rows:
            symbol = str(row["symbol"]).strip()
            reference = float(row["reference"])
            stop = row.get("stop_loss_pct")
            if not symbol or reference <= 0 or (stop is not None and float(stop) < 0):
                raise ValueError(f"symbol、reference 或 stop_loss_pct 不合法：{row!r}")
            since = row.get("sell_latest_first_since")
            if since is not None:
                since = dt.date.fromisoformat(str(since)).isoformat()
            targets.append(WatchTarget(
                symbol, reference, None if stop is None else float(stop),
                parse_holding(symbol, row["holding"]) if row.get("holding") else None, since))
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError(f"{path} 格式錯誤：{type(exc).__name__}: {exc}") from exc
    if len({t.symbol for t in targets}) != len(targets):
        raise ValueError(f"{path} 有重複的代號")
    return targets


def write_holdings_snapshot(root: Path, snapshot) -> Path:
    """把券商回報的全部庫存（含名單外的與零股）寫到 data/smile_watch/holdings.json，給系統匣顯示。

    優先用以股為單位的查詢結果（含零股）；沒有時用整張的結果換算成股數。
    """
    from bot.runtime_state import atomic_json
    share_rows = [p for p in (getattr(snapshot, "share_positions", None) or []) if p.qty > 0]
    rows = [(p, float(p.qty)) for p in share_rows] or [
        (p, float(p.qty) * 1000.0) for p in snapshot.positions if p.qty > 0]
    holdings = [{"symbol": str(p.symbol), "shares": shares,
                 "avg_price": float(getattr(p, "avg_price", 0.0) or 0.0),
                 "last_price": float(getattr(p, "last_price", 0.0) or 0.0),
                 "pnl_twd": float(getattr(p, "pnl", 0.0) or 0.0)} for p, shares in rows]
    path = Path(root) / "data" / "smile_watch" / "holdings.json"
    atomic_json(path, {"asof": getattr(snapshot, "asof", "") or now_tw().isoformat(timespec="seconds"),
                       "includes_odd_lots": bool(share_rows),
                       "holdings": sorted(holdings, key=lambda h: h["symbol"])})
    return path


def latest_first_holding(root: Path, pos, history: Optional[dict], since: str) -> Optional[Holding]:
    """用「賣出先扣最近買進」重算均價，並把明細寫到 data/smile_watch/lots-<代號>.json 給系統匣。

    對不起來（張數和券商庫存不符、明細缺漏）時回傳 None，呼叫端改用券商帳面均價。
    """
    from bot.lot_view import rematch_latest_first
    from bot.runtime_state import atomic_json
    logger = get_logger("smile-watch")
    path = Path(root) / "data" / "smile_watch" / f"lots-{pos.symbol}.json"
    try:
        if not history:
            raise ValueError("沒有逐筆明細")
        view = rematch_latest_first(history["remaining"], history["sales"], since)
        if abs(view.qty - float(pos.qty)) > 1e-6 or view.cost <= 0:
            raise ValueError(f"重算後 {view.qty:g} 張，券商庫存 {pos.qty:g} 張")
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("%s 無法改用「賣出先扣最近買進」，沿用券商帳面均價：%s", pos.symbol, exc)
        path.unlink(missing_ok=True)
        return None
    avg = round(view.cost / (view.qty * 1000.0), 2)
    atomic_json(path, {
        "symbol": pos.symbol, "asof": now_tw().isoformat(timespec="seconds"), "since": since,
        "lots": int(view.qty), "avg_price": avg, "broker_avg_price": float(pos.avg_price),
        "realized_pnl_twd": round(view.realized_pnl), "broker_realized_pnl_twd": round(view.broker_realized_pnl),
        "remaining": [{"date": l.date, "lots": l.qty, "cost_twd": round(l.cost)} for l in view.lots],
        "sales": [{"date": s.date, "lots": s.qty, "proceeds_twd": round(s.proceeds),
                   "cost_twd": round(s.cost), "pnl_twd": round(s.pnl),
                   "broker_pnl_twd": round(s.broker_pnl)} for s in view.sales]})
    return Holding(pos.symbol, int(pos.qty), avg, float(pos.avg_price))


def fetch_real_holdings(settings, symbols, latest_first_since: Optional[dict] = None) -> dict:
    """唯讀查詢券商實際庫存（只登入與列出持股）。回傳 {代號: Holding}；查詢失敗丟 RuntimeError。

    latest_first_since：{代號: 日期}，這些標的的均價改用「賣出先扣最近買進」重算。
    """
    from bot.portfolio import fetch_shioaji_positions
    real = settings.model_copy(update={"simulation": False})
    rules = {s: d for s, d in (latest_first_since or {}).items() if s in symbols and d}
    snapshot = (fetch_shioaji_positions(real, lots_since=rules) if rules
                else fetch_shioaji_positions(real))
    if snapshot.error:
        raise RuntimeError(f"讀取券商庫存失敗：{snapshot.error}")
    try:
        # 查到的持股全部記下來（含名單外的），每日收集會把它們併入名單。
        from bot.held_symbols import record_held
        fresh = record_held(Path.cwd(), [p.symbol for p in snapshot.positions if p.qty > 0])
        if fresh:
            get_logger("smile-watch").info("新記下持股 %s，之後每日收集會包含", "、".join(fresh))
    except Exception:  # noqa: BLE001 - 記錄失敗不影響盯盤
        get_logger("smile-watch").exception("記錄持股失敗")
    try:
        write_holdings_snapshot(Path.cwd(), snapshot)
    except Exception:  # noqa: BLE001 - 同上
        get_logger("smile-watch").exception("寫入全部持股快照失敗")
    out = {}
    for pos in snapshot.positions:
        if pos.symbol in symbols and pos.qty > 0:
            if pos.qty != int(pos.qty):
                raise RuntimeError(f"{pos.symbol} 庫存 {pos.qty} 張不是整張，微笑盯盤只支援整張")
            holding = None
            if pos.symbol in rules:
                if getattr(snapshot, "lot_history_error", ""):
                    get_logger("smile-watch").warning("查詢逐筆庫存失敗：%s", snapshot.lot_history_error)
                holding = latest_first_holding(
                    Path.cwd(), pos, (getattr(snapshot, "lot_history", None) or {}).get(pos.symbol),
                    rules[pos.symbol])
            out[pos.symbol] = holding or Holding(pos.symbol, int(pos.qty), float(pos.avg_price))
    for symbol in symbols:  # 不再用這個算法（或已經沒有庫存）的標的，清掉舊的明細檔
        if symbol not in rules or symbol not in out:
            (Path.cwd() / "data" / "smile_watch" / f"lots-{symbol}.json").unlink(missing_ok=True)
    return out


def _usable_price(quote: Optional[dict], today: str) -> Optional[float]:
    if (not quote or quote.get("price") is None or quote.get("quote_date") != today
            or quote.get("price_basis") not in USABLE_PRICE_BASIS):
        return None
    return float(quote["price"])


def build_fugle_client(settings):
    """有富果金鑰才建立 client；沒有（或空白）回 None，之後完全不查。"""
    key = (getattr(settings, "fugle_api_key", "") or "").strip()
    if not key:
        return None
    from bot.free_sources.fugle import FugleClient
    return FugleClient(key)


def resolve_price(symbol: str, quote: Optional[dict], today: dt.date, once: bool = False,
                  fugle=None, logger=None) -> Optional[tuple]:
    """回傳 (價格, 來源, 說明)；主來源優先，只有它拿不到當日報價才查富果備援。

    富果出錯（額度、網路、金鑰）一律只記警告並回 None，不影響盯盤迴圈。
    """
    if not once:
        price = _usable_price(quote, today.isoformat())
    else:
        price = float(quote["price"]) if quote and quote.get("price") is not None else None
    if price is not None:
        return price, PRIMARY_SOURCE, ""
    if fugle is None:
        return None
    from bot.free_sources.base import SourceError
    from bot.free_sources.fugle import describe_quote
    from bot.free_sources.quota import QuotaExceeded
    try:
        got = describe_quote(fugle.intraday_quote(symbol), today)
    except (SourceError, QuotaExceeded) as exc:
        if logger:
            logger.warning("%s 富果備援查詢失敗：%s", symbol, exc)
        return None
    if got is None:
        return None
    return got["price"], FUGLE_SOURCE, got["note"]


def main(argv=None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="實際持股的微笑盯盤（只提醒、不下單）")
    parser.add_argument("--config", help="盯盤名單 JSON（排程器用 config/smile_watch.json）")
    parser.add_argument("--symbol")
    parser.add_argument("--reference", type=float, help="微笑標準價")
    parser.add_argument("--holding", help="張數@均價；不給就唯讀查詢券商實際庫存")
    parser.add_argument("--stop-loss-pct", type=float,
                        help="單檔停損 %%；不給就用 SMILE_SYMBOL_STOP_LOSS_PCT")
    parser.add_argument("--interval", type=float, default=20.0, help="報價輪詢秒數")
    parser.add_argument("--refresh-minutes", type=float, default=30.0,
                        help="每隔幾分鐘重讀一次券商庫存；0 不重讀")
    parser.add_argument("--max-minutes", type=float, default=0.0,
                        help="最多執行幾分鐘就正常結束（排程器會再啟動）；0＝到收盤")
    parser.add_argument("--once", action="store_true", help="只判斷一次就結束（盤外也可用）")
    parser.add_argument("--out-dir", default="data/smile_watch")
    args = parser.parse_args(argv)

    from bot.config import Settings
    from bot.local_notify import LocalNotifier
    from bot.market_source import TwsePublicMarketSource
    from bot.notifier import TelegramNotifier

    logger = get_logger("smile-watch")
    if args.config:
        targets = load_targets(Path(args.config))
        if not targets:
            logger.info("%s 沒有盯盤標的，結束", args.config)
            return 0
    elif args.symbol and args.reference:
        targets = [WatchTarget(args.symbol, args.reference, args.stop_loss_pct,
                               parse_holding(args.symbol, args.holding) if args.holding else None)]
    else:
        parser.error("要給 --config，或 --symbol 與 --reference")
    base_settings = Settings()
    target_settings = {
        t.symbol: base_settings if t.stop_loss_pct is None else base_settings.model_copy(
            update={"smile_symbol_stop_loss_pct": t.stop_loss_pct}) for t in targets}
    from_broker = [t.symbol for t in targets if t.holding is None]

    def load_holdings() -> dict:
        found = {t.symbol: t.holding for t in targets if t.holding is not None}
        if from_broker:
            found.update(fetch_real_holdings(
                base_settings, from_broker,
                {t.symbol: t.latest_first_since for t in targets if t.latest_first_since}))
        return found

    def build_engines(holdings: dict) -> dict:
        return {t.symbol: build_engine(holdings[t.symbol], t.reference, target_settings[t.symbol])
                for t in targets if t.symbol in holdings}

    try:
        holdings = load_holdings()
    except RuntimeError as exc:
        if args.once:
            raise
        # 排程執行時不因一次查詢失敗就結束（結束會被排程器立刻重啟、重複登入券商）。
        logger.warning("%s；先不盯，%.0f 分鐘後重試", exc, args.refresh_minutes)
        holdings = {t.symbol: t.holding for t in targets if t.holding is not None}
    if args.once and not holdings:
        logger.error("券商庫存沒有 %s，沒有東西可盯", "、".join(from_broker))
        return 2
    engines = build_engines(holdings)
    notifier = TelegramNotifier(base_settings.telegram_bot_token, base_settings.telegram_chat_id,
                                logger)
    local = LocalNotifier(base_settings.local_notify_enabled, logger=logger)
    senders = [n.send for n in (notifier, local) if n.enabled]

    notify_all = make_notify_all(senders, logger)
    gap_pct = load_open_gap_pct(Path(args.config)) if args.config else DEFAULT_OPEN_GAP_PCT
    target_symbols = [t.symbol for t in targets]

    def other_holdings() -> dict:
        # 名單外的持股也一起查開盤跳空（併進同一次 MIS 請求）；門檻 0 時不查。
        if gap_pct <= 0:
            return {}
        return {s: n for s, n in read_other_holdings(Path.cwd()).items()
                if s not in target_symbols}

    extra_held = other_holdings()
    watcher = SmileWatcher(Path(args.out_dir), notify_all if senders else None, logger)
    source = TwsePublicMarketSource(target_symbols + sorted(extra_held), logger=logger)
    logger.info("持股開盤跳空提醒 %s", f"門檻 ±{gap_pct:g}%（含名單外持股 {len(extra_held)} 檔）"
                if gap_pct > 0 else "已停用")
    for t in targets:
        h = holdings.get(t.symbol)
        logger.info("盯 %s：%s、標準價 %.2f", t.symbol,
                    f"{h.lots} 張、均價 {h.avg_price:.2f}" + (
                        "" if h.broker_avg_price is None else
                        f"（賣出先扣最近買進；券商帳面 {h.broker_avg_price:.2f}）")
                    if h else "目前沒有庫存", t.reference)
    logger.info("Telegram %s", "已啟用" if notifier.enabled else "未設定")
    logger.info("本機通知 %s", f"已啟用（{local.mode}）" if local.enabled else "已停用")

    fugle = build_fugle_client(base_settings)
    logger.info("富果報價備援 %s", "已啟用" if fugle else "未設定金鑰，不啟用")
    last_source: dict = {}
    started = refreshed = time.monotonic()
    last_logged = float("-inf")
    while True:
        now = now_tw()
        if not args.once and now.time() >= dt.time(13, 30):
            logger.info("收盤，結束盯盤")
            return 0
        if (not args.once and args.max_minutes > 0
                and time.monotonic() - started >= args.max_minutes * 60):
            logger.info("已執行 %.0f 分鐘，結束這一輪", args.max_minutes)
            return 0
        if args.once or dt.time(9) <= now.time():
            if (from_broker and args.refresh_minutes > 0
                    and time.monotonic() - refreshed >= args.refresh_minutes * 60):
                refreshed = time.monotonic()
                try:
                    latest = load_holdings()
                except RuntimeError as exc:
                    logger.warning("%s；沿用上一次的庫存", exc)
                else:
                    if latest != holdings:
                        logger.warning("庫存變動：%s → %s",
                                       {s: (h.lots, h.avg_price) for s, h in holdings.items()},
                                       {s: (h.lots, h.avg_price) for s, h in latest.items()})
                        holdings, engines = latest, build_engines(latest)
                    extra_held = other_holdings()
                    source.symbols = target_symbols + sorted(extra_held)
            quotes = source.get_quotes() if engines or extra_held else {}
            log_now = args.once or time.monotonic() - last_logged >= 300
            if gap_pct > 0:
                gap_held = {s: f"{h.lots} 張、均價 {h.avg_price:.2f}" for s, h in holdings.items()
                            if s in engines}
                gap_held.update({s: f"{n:,.0f} 股（名單外）" for s, n in extra_held.items()})
                for symbol, held in gap_held.items():
                    watcher.handle_open_gap(symbol, quotes.get(symbol), now.date().isoformat(),
                                            gap_pct, held)
            for symbol, engine in engines.items():
                quote = quotes.get(symbol)
                resolved = resolve_price(symbol, quote, now.date(), args.once, fugle, logger)
                if resolved is None:
                    logger.info("%s 沒有可用的當日報價，略過這一輪", symbol)
                    continue
                price, price_source, price_note = resolved
                status = evaluate(engine, price, now.isoformat(timespec="seconds"))
                status.price_source, status.price_note = price_source, price_note
                watcher.handle(status)
                if price_source != last_source.get(symbol):
                    logger.info("%s 報價來源改為 %s %s", symbol, price_source, price_note)
                last_source[symbol] = price_source
                if log_now:
                    logger.info("%s 現價 %.2f｜淨損益 %s 元（%+.1f%%）｜兩平 %.2f｜停損 %s｜訊號 %s"
                                "｜來源 %s%s",
                                symbol, price, f"{status.pnl_twd:,.0f}", status.pnl_pct,
                                status.breakeven_price, status.stop_price,
                                status.signals or "無", price_source,
                                f"（{price_note}）" if price_note else "")
            if log_now:
                last_logged = time.monotonic()
        if args.once:
            return 0
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
