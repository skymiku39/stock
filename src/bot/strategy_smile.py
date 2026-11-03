"""Fill-driven smile trading. Broker orders are currently simulation-only."""
from __future__ import annotations

import datetime as dt
import json
import math
import time
from dataclasses import asdict

from bot.models import MarketTick, qty_multiplier
from bot.smile_curve import SmileCurveEngine, SmileLot
from bot.smile_execution import round_tick
from bot.strategy import BaseStrategy
from bot.trade_cost import max_affordable_qty
from bot.utils import now_tw_time


class SmileCurveStrategy(BaseStrategy):
    def __init__(self, broker, settings, *args, **kwargs):
        if settings.run_mode == "trade" and not settings.simulation:
            raise ValueError("Smile trading requires SIMULATION=true")
        super().__init__(broker, settings, *args, **kwargs)
        self.engines: dict[str, SmileCurveEngine] = {}
        self._smile_intent: dict[str, tuple[int | None, str]] = {}
        self._smile_lock = self._submission_lock
        self._smile_state_path = self._state_root / "data" / "smile_state.json"
        for symbol in settings.symbols:
            self._engine(symbol)
        if self._smile_state_path.exists():
            for symbol, row in json.loads(self._smile_state_path.read_text(encoding="utf-8")).items():
                e = self._engine(symbol)
                e.state.reference = row["reference"]
                e.state.tiers_done = set(row["tiers_done"])
                e.state.bottom_dca_done_dates = set(row["bottom_dates"])
                e.state.lots = [SmileLot(**lot) for lot in row["lots"]]

    def _engine(self, symbol):
        if symbol != "0050" and (not symbol.isdigit() or len(symbol) != 4 or symbol.startswith("0")):
            raise ValueError(f"Unsupported smile instrument: {symbol}")
        if symbol not in self.engines:
            self.engines[symbol] = SmileCurveEngine(symbol, self.settings)
        return self.engines[symbol]

    def _save_smile_state(self):
        self._smile_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {s: dict(reference=e.state.reference, tiers_done=sorted(e.state.tiers_done),
                           bottom_dates=sorted(e.state.bottom_dca_done_dates),
                           lots=[asdict(lot) for lot in e.state.lots]) for s, e in self.engines.items()}
        temp = self._smile_state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temp.replace(self._smile_state_path)

    def _sell_tax_rate(self, symbol):
        return self._engine(symbol).sell_tax

    def _handle_deal(self, msg):
        key = self._match_order(msg, msg.get("code", ""), str(msg.get("action", "")))
        if key not in self._order_meta:
            self.logger.warning("忽略非本次微笑策略委託的成交回報")
            return
        super()._handle_deal(msg)

    def _run_startup_safety_and_restore(self):
        super()._run_startup_safety_and_restore()
        for symbol in set(self.positions) | set(self.engines):
            lots = self._engine(symbol).open_lots
            pos = self.positions.get(symbol)
            lot_shares = sum(l.quantity * qty_multiplier(l.unit) for l in lots)
            pos_shares = pos.quantity * qty_multiplier(pos.unit) if pos else 0
            if lot_shares != pos_shares:
                raise RuntimeError(f"{symbol}: smile batches and recorded positions do not reconcile")
        if self.broker and any(t.status.status.value not in ("Filled", "Cancelled", "Failed")
                               for t in self.broker.list_trades()
                               if t.contract.code in self.settings.symbols):
            raise RuntimeError("Unresolved simulation orders must be reconciled before restart")

    def _on_fill_applied(self, symbol, action, price, quantity, unit):
        with self._smile_lock:
            e = self._engine(symbol)
            if action == "Buy":
                tier, day = self._smile_intent.get(symbol, (None, ""))
                e.state.lots.append(SmileLot(price, quantity, unit, day))
                if tier is not None:
                    e.state.tiers_done.add(tier)
                elif day:
                    e.state.bottom_dca_done_dates.add(day)
            else:
                remaining = quantity
                for lot in list(e.state.lots):
                    sold = min(remaining, lot.quantity)
                    lot.quantity -= sold
                    remaining -= sold
                    if not lot.quantity:
                        e.state.lots.remove(lot)
                if symbol not in self.positions:
                    e._reset_cycle(price, "")
            self._save_smile_state()
            # Persist each fill, including partial fills, for restart reconciliation.
            if self._is_trade_mode:
                self.recorder.export_csv()

    def _quantity(self, symbol, price, multiplier):
        unit = "share" if self.settings.use_odd_lot else "lot"
        want = self.settings.smile_base_lot * multiplier
        held = self.positions.get(symbol)
        if held is not None and held.unit != unit:
            return 0, unit
        held_shares = held.quantity * qty_multiplier(held.unit) if held else 0
        cap = max(0, self.settings.max_lot_per_symbol * 1000 - held_shares)
        want = min(want, cap // qty_multiplier(unit))
        if unit == "share":
            want = min(want, self.settings.odd_lot_max_shares)
        return max_affordable_qty(price, self.risk.effective_fund_remaining(), unit,
                                  max_qty=want, settings=self.settings), unit

    def on_tick(self, tick: MarketTick):
        if self._is_trade_mode and (not self.settings.simulation or
                                   getattr(self.broker.api, "simulation", False) is not True):
            raise RuntimeError("Smile broker must be connected to simulation")
        if (tick.symbol not in self.settings.symbols or tick.quality_status != "ok"
                or not math.isfinite(tick.price) or tick.price <= 0
                or not dt.time(9) <= now_tw_time() < dt.time(13, 30)):
            return
        with self._smile_lock:
            symbol, price = tick.symbol, tick.price
            e = self._engine(symbol)
            self._last_price[symbol] = price
            if e.state.reference <= 0:
                e._init_reference(price, tick.ts.date().isoformat())
                self._save_smile_state()
                return
            if self._has_pending(symbol):
                return
            pos = self.positions.get(symbol)
            if price >= e.state.reference:
                if (pos and e.open_lots and all(e._is_profitable_sell(lot, price) for lot in e.open_lots)):
                    self._place_stop_sell(symbol, pos.quantity, "smile",
                                          limit_price=round_tick(price, True, symbol))
                return
            drop = e._drop_pct(price)
            day = tick.ts.date().isoformat()
            candidates = [(idx, tier.multiplier) for idx, tier in enumerate(e.tiers)
                          if idx not in e.state.tiers_done and drop + 1e-9 >= tier.drop_pct]
            if not candidates and drop + 1e-9 >= e.tiers[-1].drop_pct and day not in e.state.bottom_dca_done_dates:
                candidates = [(None, 1)]
            if candidates:
                tier, multiplier = candidates[0]
                price = round_tick(price, False, symbol)
                qty, unit = self._quantity(symbol, price, multiplier)
                if qty:
                    self._smile_intent[symbol] = (tier, day)
                    if not self._place_buy(symbol, price, qty, "smile", unit=unit):
                        self._smile_intent.pop(symbol, None)

    def _position_closure(self):
        # Smile is a swing strategy; daily closure must never sell losing batches.
        while self._running:
            if now_tw_time() >= dt.time(13, 30):
                self.stop()
                return
            time.sleep(1)
