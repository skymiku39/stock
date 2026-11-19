"""實際持股的微笑盯盤：只提醒，不建立也不送出任何委託。

微笑策略（`strategy_smile.py`）只認模擬帳戶的部位。這個模組把券商的實際庫存（唯讀查詢）
當成一批微笑持倉，輪詢交易所公開報價，用同一個 `SmileCurveEngine` 的規則判斷，
條件第一次成立時寫紀錄並發 Telegram（有設定才發）。買賣由使用者自己決定與操作。

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


@dataclass
class Holding:
    symbol: str
    lots: int  # 張
    avg_price: float


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


@dataclass
class WatchTarget:
    symbol: str
    reference: float
    stop_loss_pct: Optional[float] = None  # None＝用 SMILE_SYMBOL_STOP_LOSS_PCT
    holding: Optional[Holding] = None  # 手動給的張數與均價；None＝讀券商庫存


def load_targets(path: Path) -> list:
    """讀盯盤名單：[{"symbol": "1514", "reference": 105, "stop_loss_pct": 25, "holding": "4@85.65"}]。

    後兩欄可省略。檔案不存在回傳空名單；內容不合法丟 ValueError。
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
            targets.append(WatchTarget(
                symbol, reference, None if stop is None else float(stop),
                parse_holding(symbol, row["holding"]) if row.get("holding") else None))
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"{path} 格式錯誤：{type(exc).__name__}: {exc}") from exc
    if len({t.symbol for t in targets}) != len(targets):
        raise ValueError(f"{path} 有重複的代號")
    return targets


def fetch_real_holdings(settings, symbols) -> dict:
    """唯讀查詢券商實際庫存（只登入與列出持股）。回傳 {代號: Holding}；查詢失敗丟 RuntimeError。"""
    from bot.portfolio import fetch_shioaji_positions
    snapshot = fetch_shioaji_positions(settings.model_copy(update={"simulation": False}))
    if snapshot.error:
        raise RuntimeError(f"讀取券商庫存失敗：{snapshot.error}")
    out = {}
    for pos in snapshot.positions:
        if pos.symbol in symbols and pos.qty > 0:
            if pos.qty != int(pos.qty):
                raise RuntimeError(f"{pos.symbol} 庫存 {pos.qty} 張不是整張，微笑盯盤只支援整張")
            out[pos.symbol] = Holding(pos.symbol, int(pos.qty), float(pos.avg_price))
    return out


def _usable_price(quote: Optional[dict], today: str) -> Optional[float]:
    if (not quote or quote.get("price") is None or quote.get("quote_date") != today
            or quote.get("price_basis") not in USABLE_PRICE_BASIS):
        return None
    return float(quote["price"])


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
            found.update(fetch_real_holdings(base_settings, from_broker))
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
    watcher = SmileWatcher(Path(args.out_dir), notifier.send if notifier.enabled else None, logger)
    source = TwsePublicMarketSource([t.symbol for t in targets], logger=logger)
    for t in targets:
        h = holdings.get(t.symbol)
        logger.info("盯 %s：%s、標準價 %.2f", t.symbol,
                    f"{h.lots} 張、均價 {h.avg_price:.2f}" if h else "目前沒有庫存", t.reference)
    logger.info("Telegram %s", "已啟用" if notifier.enabled else "未設定（只寫紀錄）")

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
            quotes = source.get_quotes() if engines else {}
            log_now = args.once or time.monotonic() - last_logged >= 300
            for symbol, engine in engines.items():
                quote = quotes.get(symbol)
                price = _usable_price(quote, now.date().isoformat()) if not args.once else (
                    float(quote["price"]) if quote and quote.get("price") is not None else None)
                if price is None:
                    logger.info("%s 沒有可用的當日報價，略過這一輪", symbol)
                    continue
                status = evaluate(engine, price, now.isoformat(timespec="seconds"))
                watcher.handle(status)
                if log_now:
                    logger.info("%s 現價 %.2f｜淨損益 %s 元（%+.1f%%）｜兩平 %.2f｜停損 %s｜訊號 %s",
                                symbol, price, f"{status.pnl_twd:,.0f}", status.pnl_pct,
                                status.breakeven_price, status.stop_price,
                                status.signals or "無")
            if log_now:
                last_logged = time.monotonic()
        if args.once:
            return 0
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
