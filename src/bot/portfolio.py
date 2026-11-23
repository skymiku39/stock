"""Portfolio helpers built from local trade CSV files.

The dashboard records fills in ``data/trades_*.csv``.  This module turns those
fills into open positions with FIFO cost basis so pages can share one answer.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bot.ownership import BOT_OWNER_TAG, infer_owner_tag, is_bot_owner


LOT_SIZE = 1000.0


@dataclass(frozen=True)
class PortfolioTrade:
    symbol: str
    side: str
    qty: float
    price: float
    ts: str = ""
    note: str = ""
    owner_tag: str = ""
    source_file: str = ""
    source_row: int = 0

    @property
    def amount(self) -> float:
        return self.qty * self.price * LOT_SIZE

    @property
    def is_bot_owned(self) -> bool:
        return is_bot_owner(self.owner_tag)


@dataclass
class PositionLot:
    qty: float
    price: float
    owner_tag: str = ""


@dataclass
class PortfolioPosition:
    symbol: str
    qty: float = 0.0
    lots: List[PositionLot] = field(default_factory=list)
    trade_count: int = 0
    last_trade_ts: str = ""
    owner_tag: str = ""

    @property
    def cost_basis(self) -> float:
        return sum(lot.qty * lot.price * LOT_SIZE for lot in self.lots)

    @property
    def avg_cost(self) -> float:
        if self.qty <= 0:
            return 0.0
        return self.cost_basis / self.qty / LOT_SIZE


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float
    avg_price: float = 0.0
    last_price: float = 0.0
    pnl: float = 0.0
    direction: str = ""
    yd_qty: float = 0.0
    cond: str = ""
    broker: str = "shioaji"
    account: str = ""
    raw_id: str = ""

    @property
    def market_value(self) -> float:
        if self.last_price <= 0:
            return 0.0
        return self.qty * self.last_price * LOT_SIZE

    @property
    def cost_basis(self) -> float:
        if self.avg_price <= 0:
            return 0.0
        return self.qty * self.avg_price * LOT_SIZE


@dataclass(frozen=True)
class BrokerPositionsSnapshot:
    broker: str
    account: str
    asof: str
    simulation: bool
    positions: List[BrokerPosition] = field(default_factory=list)
    error: str = ""
    # 以「股」為單位的同一份庫存（含零股）；qty 是股數。查不到時是空的，不影響 positions。
    share_positions: List[BrokerPosition] = field(default_factory=list)
    # 有要求時才查：{代號: {"remaining": [Lot], "sales": [Sale]}}（見 bot/lot_view.py）
    lot_history: Dict[str, Dict[str, list]] = field(default_factory=dict)
    lot_history_error: str = ""


@dataclass(frozen=True)
class BotOwnership:
    label: str
    bot_qty: float
    manual_qty: float
    detail: str


def load_portfolio(
    root: Path,
    *,
    owner_filter: Optional[str] = None,
) -> Tuple[List[PortfolioTrade], Dict[str, PortfolioPosition]]:
    """Load local trade files and return all trades plus current open positions."""
    trades = load_trades(root)
    return trades, build_positions(trades, owner_filter=owner_filter)


def load_bot_portfolio(root: Path) -> Tuple[List[PortfolioTrade], Dict[str, PortfolioPosition]]:
    """Load local trade files and return only AI-owned open positions."""
    return load_portfolio(root, owner_filter=BOT_OWNER_TAG)


def fetch_broker_positions(settings: Any, *, timeout: int = 8000) -> BrokerPositionsSnapshot:
    """Read current stock positions from Shioaji (production broker)."""
    return fetch_shioaji_positions(settings, timeout=timeout)


def _shioaji_lot_history(api: Any, account: Any, positions: Iterable[Any],
                         lots_since: Dict[str, str], timeout: int) -> Dict[str, Dict[str, list]]:
    """唯讀查詢逐筆庫存與指定日期以後的已實現損益明細，整理成 lot_view 的輸入。

    數量單位是張；逐筆庫存的 price 與已實現明細的 cost 都是該筆含手續費的總成本（元）。
    賣出淨額 = 券商回報的損益 + 被扣掉的成本。
    """
    from bot.lot_view import Lot, Sale

    history: Dict[str, Dict[str, list]] = {s: {"remaining": [], "sales": []} for s in lots_since}
    for pos in positions:
        code = str(_get_attr(pos, "code", default="") or "").strip()
        if code in history:
            for d in api.list_position_detail(account, _get_attr(pos, "id"), timeout=timeout):
                history[code]["remaining"].append(Lot(
                    str(_get_attr(d, "date", default="")), _to_float(_get_attr(d, "quantity", default=0)),
                    _to_float(_get_attr(d, "price", default=0)), str(_get_attr(d, "dseq", default=""))))
    today = _now_iso()[:10]
    for row in api.list_profit_loss(account, min(lots_since.values()), today, timeout=timeout):
        code = str(_get_attr(row, "code", default="") or "").strip()
        date = str(_get_attr(row, "date", default=""))
        if code not in history or date < lots_since[code]:
            continue
        matched = [Lot(str(_get_attr(d, "date", default="")),
                       _to_float(_get_attr(d, "quantity", default=0)),
                       _to_float(_get_attr(d, "cost", default=0)), str(_get_attr(d, "dseq", default="")))
                   for d in api.list_profit_loss_detail(account, _get_attr(row, "id"), timeout=timeout)]
        history[code]["sales"].append(Sale(
            date, _to_float(_get_attr(row, "quantity", default=0)),
            _to_float(_get_attr(row, "pnl", default=0)) + sum(lot.cost for lot in matched),
            tuple(matched), str(_get_attr(row, "dseq", default=""))))
    return history


def fetch_shioaji_positions(settings: Any, *, timeout: int = 8000,
                            lots_since: Optional[Dict[str, str]] = None) -> BrokerPositionsSnapshot:
    if not getattr(settings, "api_key", "") or not getattr(settings, "secret_key", ""):
        return BrokerPositionsSnapshot(
            broker="shioaji",
            account="",
            asof=_now_iso(),
            simulation=bool(getattr(settings, "simulation", False)),
            error="缺少 API_KEY / SECRET_KEY，無法讀取券商庫存",
        )

    try:
        import shioaji as sj
        from shioaji.constant import Unit
    except Exception as exc:
        return BrokerPositionsSnapshot(
            broker="shioaji",
            account="",
            asof=_now_iso(),
            simulation=bool(getattr(settings, "simulation", False)),
            error=f"Shioaji 套件不可用: {exc}",
        )

    api = sj.Shioaji(simulation=bool(getattr(settings, "simulation", True)))
    account: Optional[Any] = None
    account_label = ""
    try:
        api.login(
            api_key=getattr(settings, "api_key", ""),
            secret_key=getattr(settings, "secret_key", ""),
            contracts_timeout=10_000,
        )
        account = getattr(api, "stock_account", None)
        if account is None:
            return BrokerPositionsSnapshot(
                broker="shioaji",
                account="",
                asof=_now_iso(),
                simulation=bool(getattr(settings, "simulation", False)),
                error="登入成功但沒有 stock_account",
            )
        account_label = _account_label(account)
        positions = api.list_positions(account, unit=Unit.Common, timeout=timeout)
        rows = [
            broker_position_from_shioaji(p, account=account_label)
            for p in positions
        ]
        try:  # 整張的查詢不含零股，另外以股為單位再查一次；失敗不影響整張的結果
            share_rows = [
                broker_position_from_shioaji(p, account=account_label)
                for p in api.list_positions(account, unit=Unit.Share, timeout=timeout)
            ]
        except Exception:
            share_rows = []
        lot_history: Dict[str, Dict[str, list]] = {}
        lot_history_error = ""
        if lots_since:
            try:
                lot_history = _shioaji_lot_history(api, account, positions, lots_since, timeout)
            except Exception as exc:  # 查不到逐筆明細時仍回傳庫存，呼叫端改用券商帳面均價
                lot_history_error = f"{type(exc).__name__}: {exc}"
        return BrokerPositionsSnapshot(
            broker="shioaji",
            account=account_label,
            asof=_now_iso(),
            simulation=bool(getattr(settings, "simulation", False)),
            positions=[p for p in rows if p.symbol and p.qty != 0],
            share_positions=[p for p in share_rows if p.symbol and p.qty != 0],
            lot_history=lot_history,
            lot_history_error=lot_history_error,
        )
    except Exception as exc:
        return BrokerPositionsSnapshot(
            broker="shioaji",
            account=account_label,
            asof=_now_iso(),
            simulation=bool(getattr(settings, "simulation", False)),
            error=str(exc),
        )
    finally:
        try:
            api.logout()
        except Exception:
            pass


def broker_position_from_shioaji(pos: Any, *, account: str = "") -> BrokerPosition:
    return BrokerPosition(
        symbol=str(_get_attr(pos, "code", default="") or "").strip(),
        qty=_to_float(_get_attr(pos, "quantity", default=0)),
        avg_price=_to_float(_get_attr(pos, "price", default=0)),
        last_price=_to_float(_get_attr(pos, "last_price", default=0)),
        pnl=_to_float(_get_attr(pos, "pnl", default=0)),
        direction=_enum_text(_get_attr(pos, "direction", default="")),
        yd_qty=_to_float(_get_attr(pos, "yd_quantity", default=0)),
        cond=_enum_text(_get_attr(pos, "cond", default="")),
        broker="shioaji",
        account=account,
        raw_id=str(_get_attr(pos, "id", default="") or ""),
    )


def classify_bot_ownership(
    broker_qty: float,
    bot_position: Optional[PortfolioPosition],
) -> BotOwnership:
    local_qty = max(float(bot_position.qty if bot_position else 0.0), 0.0)
    bot_qty = min(max(float(broker_qty), 0.0), local_qty)
    manual_qty = max(float(broker_qty) - bot_qty, 0.0)
    if broker_qty <= 0:
        return BotOwnership("無券商庫存", 0.0, 0.0, "券商回報庫存為 0")
    if bot_qty <= 1e-9:
        return BotOwnership("非本工具", 0.0, float(broker_qty), "本工具成交紀錄沒有尚未出清部位")
    if manual_qty <= 1e-9:
        return BotOwnership("本工具", float(broker_qty), 0.0, "券商庫存可由本工具成交紀錄覆蓋")
    return BotOwnership(
        "部分本工具",
        bot_qty,
        manual_qty,
        f"本工具約 {bot_qty:g} 張，其餘約 {manual_qty:g} 張可能為手動/外部交易",
    )


def load_trades(root: Path) -> List[PortfolioTrade]:
    data_dir = root / "data"
    if not data_dir.exists():
        return []

    out: List[PortfolioTrade] = []
    for path in sorted(data_dir.glob("trades_*.csv")):
        out.extend(_read_trade_file(path))
    return out


def build_positions(
    trades: Iterable[PortfolioTrade],
    *,
    owner_filter: Optional[str] = None,
) -> Dict[str, PortfolioPosition]:
    positions: Dict[str, PortfolioPosition] = {}
    normalized_owner = (owner_filter or "").strip().upper()

    for trade in trades:
        if trade.qty <= 0 or trade.price <= 0:
            continue
        if normalized_owner and trade.owner_tag.strip().upper() != normalized_owner:
            continue
        pos = positions.setdefault(trade.symbol, PortfolioPosition(symbol=trade.symbol))
        pos.trade_count += 1
        if trade.ts:
            pos.last_trade_ts = trade.ts
        if trade.owner_tag and not pos.owner_tag:
            pos.owner_tag = trade.owner_tag

        if trade.side == "buy":
            pos.lots.append(PositionLot(
                qty=trade.qty,
                price=trade.price,
                owner_tag=trade.owner_tag,
            ))
        elif trade.side == "sell":
            _consume_fifo(pos.lots, trade.qty)

        pos.qty = round(sum(lot.qty for lot in pos.lots), 6)

    return {
        symbol: pos
        for symbol, pos in positions.items()
        if pos.qty > 0
    }


def _read_trade_file(path: Path) -> List[PortfolioTrade]:
    rows: List[PortfolioTrade] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fp:
            reader = csv.DictReader(fp)
            for idx, row in enumerate(reader, 1):
                trade = _parse_trade_row(row, path.name, idx)
                if trade is not None:
                    rows.append(trade)
    except Exception:
        return []
    return rows


def _parse_trade_row(
    row: Dict[str, str],
    source_file: str,
    source_row: int,
) -> PortfolioTrade | None:
    symbol = _first(row, "ticker", "symbol", "code", "stock_id", "證券代號")
    side = _normalize_side(_first(row, "side", "action", "買賣", "交易別"))
    qty = _to_float(_first(row, "qty", "quantity", "lots", "張數", "數量"))
    if _first(row, "unit").strip().lower() == "share":
        qty /= LOT_SIZE
    price = _to_float(_first(row, "price", "成交價", "價格"))
    ts = _first(row, "ts", "datetime", "time", "date", "成交時間", "時間")
    note = _first(row, "note", "reason", "custom_field", "custom", "備註")
    explicit_owner = _first(row, "owner_tag", "owner", "label", "tag", "來源標籤")
    owner_tag = infer_owner_tag(custom_field=note, explicit_owner=explicit_owner)

    if not symbol or side not in {"buy", "sell"} or qty <= 0 or price <= 0:
        return None

    return PortfolioTrade(
        symbol=symbol.strip(),
        side=side,
        qty=qty,
        price=price,
        ts=ts,
        note=note,
        owner_tag=owner_tag,
        source_file=source_file,
        source_row=source_row,
    )


def _consume_fifo(lots: List[PositionLot], sell_qty: float) -> None:
    remaining = sell_qty
    while lots and remaining > 0:
        lot = lots[0]
        if lot.qty <= remaining + 1e-9:
            remaining -= lot.qty
            lots.pop(0)
        else:
            lot.qty = round(lot.qty - remaining, 6)
            remaining = 0


def _normalize_side(value: str) -> str:
    raw = value.strip()
    lowered = raw.lower()
    if lowered.startswith("buy") or lowered == "b" or "買" in raw:
        return "buy"
    if lowered.startswith("sell") or lowered == "s" or "賣" in raw:
        return "sell"
    return lowered


def _first(row: Dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _to_float(value: str) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _get_attr(obj: Any, name: str, *, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum_text(value: Any) -> str:
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw)


def _account_label(account: Any) -> str:
    broker_id = str(getattr(account, "broker_id", "") or "")
    account_id = str(getattr(account, "account_id", "") or "")
    if not broker_id and not account_id:
        return ""
    masked = account_id if len(account_id) <= 4 else f"***{account_id[-4:]}"
    return "-".join(x for x in (broker_id, masked) if x)


def _now_iso() -> str:
    tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.now(tz).isoformat(timespec="seconds")


__all__ = [
    "LOT_SIZE",
    "BotOwnership",
    "BrokerPosition",
    "BrokerPositionsSnapshot",
    "PortfolioPosition",
    "PortfolioTrade",
    "PositionLot",
    "broker_position_from_shioaji",
    "build_positions",
    "classify_bot_ownership",
    "fetch_broker_positions",
    "fetch_shioaji_positions",
    "load_bot_portfolio",
    "load_portfolio",
    "load_trades",
]
