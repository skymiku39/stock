"""Fill-driven smile trading. Broker orders are currently simulation-only."""
from __future__ import annotations

import datetime as dt
import json
import math
import shutil
import time
from dataclasses import asdict

from bot.models import MarketTick, qty_multiplier
from bot.smile_curve import SmileCurveEngine, SmileLot
from bot.smile_execution import round_tick
from bot.strategy import BaseStrategy
from bot.trade_cost import max_affordable_qty, net_pnl_twd
from bot.utils import now_tw_time


class SmileCurveStrategy(BaseStrategy):
    def __init__(self, broker, settings, *args, **kwargs):
        if settings.run_mode == "trade" and not settings.simulation:
            raise ValueError("Smile trading requires SIMULATION=true")
        auto_merge_was_on = bool(getattr(settings, "symbols_auto_merge", True))
        if auto_merge_was_on:
            settings = settings.model_copy(update={"symbols_auto_merge": False})
        super().__init__(broker, settings, *args, **kwargs)
        if auto_merge_was_on:
            self.logger.warning("微笑策略不支援觀察池自動併入代號，已強制關閉 symbols_auto_merge")
        self.engines: dict[str, SmileCurveEngine] = {}
        self._smile_intent: dict[str, tuple[int | None, str]] = {}
        self._smile_lock = self._submission_lock
        self._smile_state_path = self._state_root / "data" / "smile_state.json"
        for symbol in settings.symbols:
            self._engine(symbol)
        if self._smile_state_path.exists():
            try:
                data = json.loads(self._smile_state_path.read_text(encoding="utf-8"))
                loaded = {}
                for symbol, row in data.items():
                    loaded[symbol] = (row["reference"], set(row["tiers_done"]),
                                      set(row["bottom_dates"]),
                                      [SmileLot(**lot) for lot in row["lots"]])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
                path = self._smile_state_path.resolve()
                raise RuntimeError(
                    f"smile_state.json 損毀：{path}（{type(exc).__name__}: {exc}）。"
                    "可從同目錄 smile_state.json.bak 還原（最後一次成功寫入前的版本）；"
                    "若確定沒有微笑持倉與未結委託，可刪除此檔讓策略重建標準價") from exc
            for symbol, (ref, tiers, dates, lots) in loaded.items():
                e = self._engine(symbol)
                e.state.reference = ref
                e.state.tiers_done = tiers
                e.state.bottom_dca_done_dates = dates
                e.state.lots = lots

    def _engine(self, symbol):
        if symbol != "0050" and (not symbol.isdigit() or len(symbol) != 4 or symbol.startswith("0")):
            raise ValueError(f"不支援的微笑策略代號：{symbol}。支援範圍：普通股 4 碼數字（非 0 開頭）與 0050")
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
        if self._smile_state_path.exists():
            shutil.copy2(self._smile_state_path, self._smile_state_path.with_name("smile_state.json.bak"))
        temp.replace(self._smile_state_path)

    def _order_timeout_seconds(self):
        return self.settings.smile_order_timeout_seconds or None

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

    def _unrealized_loss_guard(self) -> bool:
        """全部微笑持倉浮虧達上限時回傳 True（不買），並拉一次 Kill Switch。"""
        pct = self.settings.smile_max_unrealized_loss_pct
        if pct <= 0:
            return False
        pnl = sum(
            net_pnl_twd(lot.entry_price, self._last_price.get(s, lot.entry_price),
                        lot.quantity, lot.unit, settings=self.settings,
                        sell_tax_rate=eng.sell_tax)
            for s, eng in self.engines.items() for lot in eng.state.lots
        )
        limit = pct / 100.0 * float(self.settings.effective_fund_cap())
        if pnl > -limit:
            return False
        if not self.risk.is_kill_switch_engaged():
            self.risk.engage_kill_switch(f"smile_unrealized_loss {pnl:.0f} <= -{limit:.0f}")
        return True

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
                if (pos and e.open_lots and e._position_profitable(price)):
                    self._place_stop_sell(symbol, pos.quantity, "smile",
                                          limit_price=round_tick(price, True, symbol))
                return
            if self._unrealized_loss_guard():
                return
            drop = e._drop_pct(price)
            day = tick.ts.date().isoformat()
            max_days = self.settings.smile_max_bottom_dca_days
            candidates = [(idx, tier.multiplier) for idx, tier in enumerate(e.tiers)
                          if idx not in e.state.tiers_done and drop + 1e-9 >= tier.drop_pct]
            if (not candidates and drop + 1e-9 >= e.tiers[-1].drop_pct
                    and day not in e.state.bottom_dca_done_dates
                    and (max_days == 0 or len(e.state.bottom_dca_done_dates) < max_days)):
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
