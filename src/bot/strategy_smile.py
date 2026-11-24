"""Fill-driven smile trading. Broker orders are currently simulation-only."""
from __future__ import annotations

import datetime as dt
import json
import math
import shutil
import time
from dataclasses import asdict

from bot.models import MarketTick, qty_multiplier
from bot.smile_curve import SmileAccount, SmileCurveEngine, SmileLot, smile_fund_cap
from bot.smile_execution import round_tick
from bot.smile_sentiment import (evaluate_mood, load_recent_items, market_news_mood,
                                 symbol_bad_news)
from bot.strategy import BaseStrategy
from bot.trade_cost import max_affordable_qty, net_pnl_twd
from bot.utils import now_tw, now_tw_time


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
        self._smile_account = SmileAccount()  # 累計已實現損益，所有代號共用，存進 smile_state.json
        self._smile_intent: dict[str, tuple[int | None, str]] = {}
        self._smile_stop_selling: set[str] = set()  # 代號的在途賣單屬於單檔停損
        # 代號的在途賣單屬於持有到期：[送單當天日期, 已成交部分的累計淨損益]
        self._smile_expiry_selling: dict[str, list] = {}
        self._reference_saved_at = float("-inf")
        self._news_cache = None  # (monotonic 時間, 項目, 市場新聞情緒, collected_at)
        self._news_block_logged: set[tuple[str, str]] = set()  # 每檔每天只記一次壞消息擋買
        self._smile_lock = self._submission_lock
        self._smile_state_path = self._state_root / "data" / "smile_state.json"
        for symbol in settings.symbols:
            self._engine(symbol)
        if self._smile_state_path.exists():
            try:
                data = json.loads(self._smile_state_path.read_text(encoding="utf-8"))
                loaded = {}
                self._smile_account.realized_pnl_twd = float(
                    data.pop("_account", {}).get("realized_pnl_twd", 0.0))
                for symbol, row in data.items():
                    loaded[symbol] = (row["reference"], set(row["tiers_done"]),
                                      set(row["bottom_dates"]),
                                      [SmileLot(**lot) for lot in row["lots"]],
                                      bool(row.get("disabled", False)),
                                      str(row.get("cooldown_until", "") or ""))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
                path = self._smile_state_path.resolve()
                raise RuntimeError(
                    f"smile_state.json 損毀：{path}（{type(exc).__name__}: {exc}）。"
                    "可從同目錄 smile_state.json.bak 還原（最後一次成功寫入前的版本）；"
                    "若確定沒有微笑持倉與未結委託，可刪除此檔讓策略重建標準價") from exc
            for symbol, (ref, tiers, dates, lots, disabled, cooldown) in loaded.items():
                e = self._engine(symbol)
                e.state.disabled = disabled
                e.state.cooldown_until = cooldown
                if disabled:
                    self.logger.warning("%s 已因單檔停損停用，不會再買進；重新啟用見 docs/operations.md 4.6", symbol)
                e.state.reference = ref
                e.state.tiers_done = tiers
                e.state.bottom_dca_done_dates = dates
                e.state.lots = lots

    def _engine(self, symbol):
        if symbol != "0050" and (not symbol.isdigit() or len(symbol) != 4 or symbol.startswith("0")):
            raise ValueError(f"不支援的微笑策略代號：{symbol}。支援範圍：普通股 4 碼數字（非 0 開頭）與 0050")
        if symbol not in self.engines:
            self.engines[symbol] = SmileCurveEngine(symbol, self.settings)
            self.engines[symbol].account = self._smile_account
        return self.engines[symbol]

    def _save_smile_state(self):
        self._smile_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {s: dict(reference=e.state.reference, tiers_done=sorted(e.state.tiers_done),
                           bottom_dates=sorted(e.state.bottom_dca_done_dates),
                           disabled=e.state.disabled, cooldown_until=e.state.cooldown_until,
                           lots=[asdict(lot) for lot in e.state.lots]) for s, e in self.engines.items()}
        payload["_account"] = dict(realized_pnl_twd=self._smile_account.realized_pnl_twd)
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
                expiry = self._smile_expiry_selling.get(symbol)
                for lot in list(e.state.lots):
                    sold = min(remaining, lot.quantity)
                    if sold:
                        pnl = net_pnl_twd(lot.entry_price, price, sold, lot.unit,
                                          settings=self.settings, sell_tax_rate=e.sell_tax)
                        self._smile_account.realized_pnl_twd += pnl
                        if expiry is not None:
                            expiry[1] += pnl
                    lot.quantity -= sold
                    remaining -= sold
                    if not lot.quantity:
                        e.state.lots.remove(lot)
                if symbol not in self.positions:
                    if symbol in self._smile_stop_selling:
                        self._smile_stop_selling.discard(symbol)
                        e.state.disabled = True
                        self.logger.warning(
                            "%s 單檔停損已出清並停用，不會再買進；重新啟用見 docs/operations.md 4.6",
                            symbol)
                    elif symbol in self._smile_expiry_selling:
                        day, pnl = self._smile_expiry_selling.pop(symbol)
                        e.finish_expiry(price, day, pnl)
                        self.logger.warning(
                            "%s 持有到期已出清（淨損益 %.0f 元）%s", symbol, pnl,
                            f"，{e.state.cooldown_until} 之前不再買進" if e.in_cooldown(day) else "")
                    else:
                        e._reset_cycle(price, "")
            self._save_smile_state()
            # Persist each fill, including partial fills, for restart reconciliation.
            if self._is_trade_mode:
                self.recorder.export_csv()

    def _quantity(self, symbol, price, multiplier):
        unit = "share" if self.settings.use_odd_lot else "lot"
        want = self._engine(symbol).tier_quantity(price, multiplier, unit)
        held = self.positions.get(symbol)
        if held is not None and held.unit != unit:
            return 0, unit
        held_shares = held.quantity * qty_multiplier(held.unit) if held else 0
        cap = max(0, self.settings.max_lot_per_symbol * 1000 - held_shares)
        want = min(want, cap // qty_multiplier(unit))
        if unit == "share":
            want = min(want, self.settings.odd_lot_max_shares)
        budget = self.risk.effective_fund_remaining()
        if self.settings.smile_initial_fund > 0:
            # 複利：再受 min(MAX_FUND, 權益) 限制；RiskGuard 的 MAX_FUND 硬上限照舊。
            held_cost = sum(eng._lot_cost(eng.state.lots) for eng in self.engines.values())
            budget = min(budget, max(0.0, smile_fund_cap(self.settings, self._smile_account)
                                     - held_cost))
        room = self._engine(symbol).symbol_fund_room()
        if room is not None:
            budget = min(budget, room)
        return max_affordable_qty(price, budget, unit, max_qty=want, settings=self.settings), unit

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
        limit = pct / 100.0 * smile_fund_cap(self.settings, self._smile_account)
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
            day = tick.ts.date().isoformat()
            holding = bool(pos and e.open_lots)
            # 出場檢查順序：停利（或回到標準價且有淨利）→ 單檔停損 → 持有到期 → 買進。
            if holding and e.take_profit_hit(price):
                self._smile_stop_selling.discard(symbol)
                self._smile_expiry_selling.pop(symbol, None)
                self._place_stop_sell(symbol, pos.quantity, "smile",
                                      limit_price=round_tick(price, True, symbol))
                return
            if holding and self.settings.smile_mood_sell_enabled:
                if self._try_mood_sell(symbol, pos, e, price):
                    return
            if price >= e.state.reference:
                if holding and e.hold_expired(day):
                    self._place_expiry_sell(symbol, pos, price, day)
                elif not pos and e.trail_reference(price):
                    # 新高可能每個 tick 都出現；最多每分鐘落盤一次，成交時另會儲存。
                    if time.monotonic() - self._reference_saved_at >= 60:
                        self._reference_saved_at = time.monotonic()
                        self._save_smile_state()
                return
            if pos and e.open_lots and e.stop_loss_breached(price):
                # 單檔停損：整個部位以現價限價賣出，出清後停用該檔。
                # 成交回報可能在送單呼叫內就回來，所以先標記再送單。
                self._smile_stop_selling.add(symbol)
                if not self._place_stop_sell(symbol, pos.quantity, "smile_stop",
                                             limit_price=round_tick(price, False, symbol)):
                    self._smile_stop_selling.discard(symbol)
                return
            if holding and e.hold_expired(day):
                self._place_expiry_sell(symbol, pos, price, day)
                return
            if e.state.disabled or e.in_cooldown(day):
                return
            if self._unrealized_loss_guard():
                return
            drop = e._drop_pct(price)
            candidates = [(idx, tier.multiplier) for idx, tier in enumerate(e.tiers)
                          if idx not in e.state.tiers_done and drop + 1e-9 >= tier.drop_pct]
            if (not candidates and drop + 1e-9 >= e.tiers[-1].drop_pct
                    and day not in e.state.bottom_dca_done_dates and e.bottom_dca_allowed()):
                candidates = [(None, 1)]
            if candidates and self.settings.smile_news_block_enabled:
                bad = symbol_bad_news(self._news_snapshot()[0], symbol)
                if bad:
                    self._log_news_block(symbol, day, price, bad)
                    return
            if candidates:
                tier, multiplier = candidates[0]
                price = round_tick(price, False, symbol)
                qty, unit = self._quantity(symbol, price, multiplier)
                if qty:
                    self._smile_intent[symbol] = (tier, day)
                    if not self._place_buy(symbol, price, qty, "smile", unit=unit):
                        self._smile_intent.pop(symbol, None)

    # --- 時事＋即時股價（只在盤中生效；歷史新聞不存在，日 K 引擎與回測沒有這套規則）---

    def _news_snapshot(self):
        """回傳 (視窗內項目, 市場新聞情緒 dict, 新聞存檔距今分鐘數)。最多每 60 秒重讀一次檔案。"""
        now_m = time.monotonic()
        cache = self._news_cache
        if cache is None or now_m - cache[0] >= 60:
            now = now_tw()
            try:
                items, collected = load_recent_items(
                    self._project_root, now, self.settings.smile_news_window_hours)
            except Exception as exc:  # 新聞只是輔助判斷，讀取失敗不可影響交易迴圈
                self.logger.warning("讀取新聞存檔失敗：%s: %s", type(exc).__name__, exc)
                items, collected = [], None
            cache = self._news_cache = (now_m, items, market_news_mood(items), collected)
        _, items, mood, collected = cache
        age = None
        if collected:
            try:
                ts = dt.datetime.fromisoformat(collected)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=now_tw().tzinfo)
                age = max(0.0, (now_tw() - ts).total_seconds() / 60)
            except ValueError:
                age = None
        return items, mood, age

    def _market_change_pct(self):
        """有 0050 用 0050 的即時漲跌幅；否則取監控代號漲跌幅的中位數；都沒有回 None。"""
        def change(sym):
            prev, last = self._prev_close.get(sym, 0.0), self._last_price.get(sym, 0.0)
            if prev and prev > 0 and last and last > 0:
                return (last / prev - 1) * 100
            return None
        if "0050" in self.settings.symbols:
            value = change("0050")
            if value is not None:
                return value
        values = sorted(v for v in (change(s) for s in self.settings.symbols) if v is not None)
        if not values:
            return None
        mid = len(values) // 2
        return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2

    def _current_mood(self):
        _, news, age = self._news_snapshot() if (
            self.settings.smile_mood_require != "price") else (None, None, None)
        return evaluate_mood(news=news, news_age_minutes=age,
                             market_change_pct=self._market_change_pct(), settings=self.settings)

    def _try_mood_sell(self, symbol, pos, e, price) -> bool:
        """持倉有淨利且市場悲觀：整個部位提早賣出。絕不因時事認賠；送單回 True。"""
        limit = round_tick(price, False, symbol)
        lots = e.open_lots
        pnl = e.unrealized_pnl(limit, lots)
        min_pct = self.settings.smile_mood_min_profit_pct
        if pnl <= 0 or pnl < min_pct / 100.0 * e._lot_cost(lots):
            return False
        mood = self._current_mood()
        if not mood.pessimistic:
            return False
        self._smile_stop_selling.discard(symbol)
        self._smile_expiry_selling.pop(symbol, None)
        if not self._place_stop_sell(symbol, pos.quantity, "smile_mood", limit_price=limit):
            return False
        self._write_mood_log(
            "mood_sell", symbol, limit, mood=asdict(mood), net_pnl_twd=round(pnl, 2))
        self.logger.warning("%s 市場悲觀且持倉淨利 %.0f 元，情緒賣出 @ %.2f（%s）",
                            symbol, pnl, limit, "；".join(mood.reasons))
        return True

    def _log_news_block(self, symbol, day, price, bad):
        if (symbol, day) in self._news_block_logged:
            return
        self._news_block_logged.add((symbol, day))
        self._write_mood_log("news_block", symbol, price, bad_news=bad)
        self.logger.warning("%s 近期有公司層級壞消息，今日不買進：%s", symbol,
                            "；".join(b.get("title", "") for b in bad))

    def _write_mood_log(self, action, symbol, price, **detail):
        try:
            path = self._state_root / "data" / "smile_mood_log.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {"ts": now_tw().isoformat(timespec="seconds"), "symbol": symbol,
                   "action": action, "price": price, **detail}
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        except Exception as exc:
            self.logger.warning("寫入 smile_mood_log.jsonl 失敗：%s: %s", type(exc).__name__, exc)

    def _place_expiry_sell(self, symbol, pos, price, day):
        """持有到期：整個部位以現價限價賣出，不論賺賠；出清後重設標準價，不停用。"""
        self._smile_stop_selling.discard(symbol)
        entry = self._smile_expiry_selling.setdefault(symbol, [day, 0.0])
        if not self._place_stop_sell(symbol, pos.quantity, "smile_expiry",
                                     limit_price=round_tick(price, False, symbol)):
            if entry[1] == 0.0 and symbol in self.positions:
                self._smile_expiry_selling.pop(symbol, None)

    def _position_closure(self):
        # Smile is a swing strategy; daily closure must never sell losing batches.
        while self._running:
            if now_tw_time() >= dt.time(13, 30):
                self.stop()
                return
            time.sleep(1)
