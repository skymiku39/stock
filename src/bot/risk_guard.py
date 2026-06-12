"""risk_guard -- 資金/風險控制器。

設計目的
========
不論策略邏輯怎麼變動，所有「實際下單」都必須先通過 `RiskGuard.check_entry()`。
這層守門員集中管理：

A. **總資金上限**
   * `max_fund` — 已用資金不可超過此總額 (沿用既有 setting)
   * `per_order_max_cost_twd` — 單筆委託金額上限 (新增)

B. **持倉控制**
   * `max_open_positions` — 同時最多 N 檔
   * `max_lot_per_symbol` — 單檔最大張數
   * `blacklist_symbols` — 強制不交易

C. **損失熔斷**
   * `daily_max_loss_twd` — 當日**已實現**累計虧損超過此值 → 停止開新倉並拉 Kill Switch
   * `daily_max_loss_pct` — 同上，以 effective_fund_cap 百分比表示
   * 未實現浮虧不計入熔斷（`update_unrealized_pnl` 僅供快照顯示）

D. **下單頻率**
   * `daily_max_orders` — 當日進場單數上限
   * `per_symbol_daily_max_orders` — 同一檔當日進場上限
   * `reentry_cooldown_seconds` — 同檔平倉後 N 秒內不得再進

E. **進場條件**
   * `max_pct_chg_on_entry` — 漲幅超過此值不進場 (防追高)
   * `min_price` / `max_price` — 價格區間

F. **手動 Kill Switch**
   * 偵測到 `data/.kill_switch` 檔案存在 → 立刻禁止新進場
   * (出場永遠允許，因為要平倉控制風險)
   * dashboard 提供「拉閘」按鈕 / CLI: `touch data/.kill_switch`

G. **每日狀態**
   * 持久化到 `data/risk_state_YYYY-MM-DD.json` (每日清零)
   * 內含: today_orders, realized_pnl_twd, last_exit_ts, kill_switch_engaged

依賴注入
========
``RiskGuard`` 不依賴 Shioaji；可在 watch / report 模式同樣作用。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Set, Tuple

EntryKind = Literal["enter", "reenter", "rebuy"]

from bot.models import QtyUnit, qty_multiplier
from bot.trade_cost import buy_cash_required, max_affordable_qty, net_pnl_twd

from bot.utils import get_logger, now_tw


KILL_SWITCH_FILENAME = ".kill_switch"
DAILY_STATE_PREFIX = "risk_state_"


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------


@dataclass
class EntryDecision:
    """進場檢查結果。"""
    allowed: bool
    reason: str = ""
    adjusted_lots: int = 0
    blocking_rule: str = ""    # 觸發的規則 id (用於 metrics / 通知)
    unit: QtyUnit = "lot"


@dataclass
class DailyState:
    """當日累積狀態 (持久化到 risk_state_*.json)。"""
    date: str
    today_orders: int = 0
    today_orders_per_symbol: Dict[str, int] = field(default_factory=dict)
    realized_pnl_twd: float = 0.0
    last_exit_ts: Dict[str, float] = field(default_factory=dict)
    kill_switch_engaged: bool = False
    blocked_attempts: List[Dict[str, str]] = field(default_factory=list)
    fund_used: float = 0.0
    open_positions: int = 0


# ----------------------------------------------------------------------
# 風控核心
# ----------------------------------------------------------------------


class RiskGuard:
    """資金 / 風險控制器。

    所有需要實際下單的路徑都必須先呼叫 `check_entry()`。
    成交後呼叫 `on_fill()` 更新累計狀態。
    """

    def __init__(
        self,
        settings,                           # bot.config.Settings (避免循環 import)
        project_root: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.settings = settings
        self.logger = logger or get_logger("risk_guard")
        self.project_root = project_root or Path.cwd()
        self.data_dir = self.project_root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._fund_used: float = 0.0
        self._unrealized_pnl: float = 0.0
        self._open_positions: int = 0
        self._open_symbols: Set[str] = set()
        self._pending_exposure: Dict[str, float] = {}

        self.state = self._load_today_state()
        self._fund_used = float(self.state.fund_used)
        self._open_positions = int(self.state.open_positions)
        from bot.ownership import effective_trading_blacklist

        self._blacklist = effective_trading_blacklist(settings)
        # 同步 fund_used 上限做為虧損百分比基準
        self._loss_base = max(1.0, float(settings.effective_fund_cap()))

    # ------------------------------------------------------------------
    # 狀態持久化
    # ------------------------------------------------------------------

    def _state_path(self, date: Optional[dt.date] = None) -> Path:
        d = date or now_tw().date()
        return self.data_dir / f"{DAILY_STATE_PREFIX}{d.isoformat()}.json"

    def _load_today_state(self) -> DailyState:
        today_iso = now_tw().date().isoformat()
        p = self._state_path()
        if not p.exists():
            return DailyState(date=today_iso)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("date") != today_iso:
                return DailyState(date=today_iso)
            return DailyState(
                date=data["date"],
                today_orders=data.get("today_orders", 0),
                today_orders_per_symbol=data.get("today_orders_per_symbol", {}),
                realized_pnl_twd=float(data.get("realized_pnl_twd", 0.0)),
                last_exit_ts=data.get("last_exit_ts", {}),
                kill_switch_engaged=bool(data.get("kill_switch_engaged", False)),
                blocked_attempts=data.get("blocked_attempts", []),
                fund_used=float(data.get("fund_used", 0.0)),
                open_positions=int(data.get("open_positions", 0)),
            )
        except Exception as e:
            self.logger.warning("讀取風控狀態失敗，重設今日狀態: %s", e)
            return DailyState(date=today_iso)

    def _persist(self) -> None:
        p = self._state_path()
        try:
            self.state.fund_used = self._fund_used
            self.state.open_positions = self._open_positions
            p.write_text(
                json.dumps(asdict(self.state), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            self.logger.exception("寫入風控狀態失敗")

    @property
    def fund_used(self) -> float:
        return self._fund_used

    @property
    def open_positions_count(self) -> int:
        return self._open_positions

    def set_fund_used(
        self,
        amount: float,
        *,
        open_positions: Optional[int] = None,
        open_symbols: Optional[List[str]] = None,
    ) -> None:
        """同步已用資金（啟動恢復部位時與策略對齊）。"""
        with self._lock:
            self._fund_used = max(0.0, float(amount))
            if open_symbols is not None:
                self._open_symbols = {s for s in open_symbols if s}
                self._open_positions = len(self._open_symbols)
            elif open_positions is not None:
                self._open_positions = max(0, int(open_positions))
            self._persist()

    def effective_fund_remaining(self) -> float:
        """可用預算（扣除已用資金與委託暫扣）。"""
        with self._lock:
            pending = sum(self._pending_exposure.values())
            return max(
                0.0,
                float(self.settings.effective_fund_cap()) - self._fund_used - pending,
            )

    def reserve_entry_exposure(self, symbol: str, cost: float) -> None:
        """trade 模式送單後暫扣額度，成交或取消時釋放。"""
        with self._lock:
            self._pending_exposure[symbol] = max(0.0, float(cost))

    def release_entry_exposure(self, symbol: str) -> None:
        """委託取消/失敗時釋放暫扣。"""
        with self._lock:
            self._pending_exposure.pop(symbol, None)

    # ------------------------------------------------------------------
    # Kill Switch
    # ------------------------------------------------------------------

    @property
    def kill_switch_path(self) -> Path:
        return self.data_dir / KILL_SWITCH_FILENAME

    def is_kill_switch_engaged(self) -> bool:
        """拉閘條件：檔案存在 或 當日狀態已標記。"""
        return self.kill_switch_path.exists() or self.state.kill_switch_engaged

    def engage_kill_switch(self, reason: str = "manual") -> None:
        """主動拉閘 (dashboard 按鈕 / 程式邏輯觸發)。"""
        with self._lock:
            self.kill_switch_path.write_text(
                f"engaged_at={now_tw().isoformat()}\nreason={reason}\n",
                encoding="utf-8",
            )
            self.state.kill_switch_engaged = True
            self._persist()
            self.logger.warning("🔴 Kill switch ENGAGED (%s)", reason)

    def release_kill_switch(self) -> None:
        with self._lock:
            try:
                self.kill_switch_path.unlink(missing_ok=True)
            except Exception:
                pass
            self.state.kill_switch_engaged = False
            self._persist()
            self.logger.info("🟢 Kill switch released")

    # ------------------------------------------------------------------
    # 對外: 進場檢查
    # ------------------------------------------------------------------

    def check_entry(
        self,
        symbol: str,
        price: float,
        requested_lots: int,
        *,
        unit: QtyUnit = "lot",
        pct_chg: Optional[float] = None,
        available_balance: Optional[float] = None,
        enforce_account_balance: bool = False,
        entry_kind: EntryKind = "enter",
    ) -> EntryDecision:
        """檢查是否可以進場。回傳是否允許 + 原因 + 調整後數量。

        呼叫端必須使用 `decision.adjusted_lots` 與 `decision.unit`，
        因為風控可能會把數量壓低 (例如剩餘資金不夠買原請求數)。
        """
        if requested_lots <= 0:
            return EntryDecision(False, "request_lots<=0", 0, "input", unit)

        if unit == "share":
            return self._check_entry_shares(
                symbol, price, requested_lots, pct_chg=pct_chg,
                available_balance=available_balance,
                enforce_account_balance=enforce_account_balance,
                entry_kind=entry_kind,
            )

        # 1) Kill switch 最高優先
        if self.is_kill_switch_engaged():
            return self._reject(symbol, "kill_switch_engaged", "🔴 Kill switch 已拉起", 0)

        # 2) 黑名單
        if symbol in self._blacklist:
            return self._reject(symbol, "blacklist", f"{symbol} 在黑名單", 0)

        # 3) 白名單 (settings.symbols 已是篩過的，這裡不再 enforce 給策略彈性)

        # 4) 價格區間
        if self.settings.min_price > 0 and price < self.settings.min_price:
            return self._reject(symbol, "below_min_price",
                                f"{symbol} 價格 {price} < min_price {self.settings.min_price}", 0)
        if self.settings.max_price > 0 and price > self.settings.max_price:
            return self._reject(symbol, "above_max_price",
                                f"{symbol} 價格 {price} > max_price {self.settings.max_price}", 0)

        # 5) 漲幅 (防追高)
        if (
            pct_chg is not None
            and self.settings.max_pct_chg_on_entry > 0
            and abs(pct_chg) > self.settings.max_pct_chg_on_entry
        ):
            return self._reject(
                symbol, "pct_chg_too_large",
                f"{symbol} 漲幅 {pct_chg:.2f}% > 上限 {self.settings.max_pct_chg_on_entry}%",
                0,
            )

        # 6) 每日下單次數
        if (
            self.settings.daily_max_orders > 0
            and self.state.today_orders >= self.settings.daily_max_orders
        ):
            return self._reject(
                symbol, "daily_orders_exceeded",
                f"今日已下 {self.state.today_orders} 單，達上限 {self.settings.daily_max_orders}",
                0,
            )

        if entry_kind != "rebuy":
            per_sym = self.state.today_orders_per_symbol.get(symbol, 0)
            if (
                self.settings.per_symbol_daily_max_orders > 0
                and per_sym >= self.settings.per_symbol_daily_max_orders
            ):
                return self._reject(
                    symbol, "per_symbol_orders_exceeded",
                    f"{symbol} 今日已進場 {per_sym} 次，達單檔上限 "
                    f"{self.settings.per_symbol_daily_max_orders}",
                    0,
                )

        # 7) 同檔再進場冷卻
        cooldown = float(self.settings.reentry_cooldown_seconds)
        if cooldown > 0:
            last = self.state.last_exit_ts.get(symbol)
            if last is not None:
                gap = now_tw().timestamp() - float(last)
                if gap < cooldown:
                    return self._reject(
                        symbol, "reentry_cooldown",
                        f"{symbol} 平倉後僅 {gap:.0f}s，需等 {cooldown:.0f}s 才能再進",
                        0,
                    )

        # 8) 同時持倉檔數（加碼既有標的不佔新檔位）
        if (
            self.settings.max_open_positions > 0
            and symbol not in self._open_symbols
            and self._open_positions >= self.settings.max_open_positions
        ):
            return self._reject(
                symbol, "max_open_positions",
                f"已有 {self._open_positions} 檔持倉，達上限 {self.settings.max_open_positions}",
                0,
            )

        # 9) 每日累計虧損熔斷 (含已實現)
        loss_cap_twd = self.settings.daily_max_loss_twd
        if self.settings.daily_max_loss_pct > 0:
            implied = self._loss_base * (self.settings.daily_max_loss_pct / 100.0)
            if loss_cap_twd <= 0 or implied < loss_cap_twd:
                loss_cap_twd = implied
        if loss_cap_twd > 0 and self.state.realized_pnl_twd <= -abs(loss_cap_twd):
            # 自動拉閘，免得繼續挖坑
            self.engage_kill_switch(
                f"daily_loss_circuit_breaker (PnL={self.state.realized_pnl_twd:.0f})"
            )
            return self._reject(
                symbol, "daily_loss_circuit_breaker",
                f"今日已實現虧損 {self.state.realized_pnl_twd:,.0f} TWD，超過熔斷門檻 "
                f"{loss_cap_twd:,.0f}，自動拉閘",
                0,
            )

        # 10) 單檔最大張數 + 單筆/預算上限（含手續費）→ 壓張數
        if price <= 0:
            return self._reject(symbol, "invalid_price", f"price={price}", 0)

        adjusted = min(requested_lots, self.settings.max_lot_per_symbol)

        if self.settings.per_order_max_cost_twd > 0:
            max_by_order = max_affordable_qty(
                price, float(self.settings.per_order_max_cost_twd), "lot",
                max_qty=adjusted, settings=self.settings,
            )
            adjusted = min(adjusted, max_by_order)

        remaining = self.effective_fund_remaining()
        adjusted = max_affordable_qty(
            price, remaining, "lot", max_qty=adjusted, settings=self.settings,
        )

        if adjusted <= 0:
            min_cost = buy_cash_required(price, 1, "lot", settings=self.settings)
            return self._reject(
                symbol, "insufficient_fund",
                f"剩餘資金 {remaining:,.0f} < 含費每張約 {min_cost:,.0f}",
                0,
            )

        balance_decision = self._check_account_balance_gate(
            symbol, price, adjusted, "lot",
            available_balance=available_balance,
            enforce_account_balance=enforce_account_balance,
        )
        if balance_decision is not None:
            return balance_decision

        return EntryDecision(
            allowed=True,
            adjusted_lots=adjusted,
            reason="ok",
            unit="lot",
        )

    def _check_account_balance_gate(
        self,
        symbol: str,
        price: float,
        qty: int,
        unit: QtyUnit,
        *,
        available_balance: Optional[float],
        enforce_account_balance: bool,
    ) -> Optional[EntryDecision]:
        if not enforce_account_balance or not self.settings.check_account_balance:
            return None
        if available_balance is None:
            return self._reject(
                symbol, "account_balance_unavailable",
                "無法讀取券商 account_balance，拒絕進場",
                0, unit,
            )
        cost = buy_cash_required(price, qty, unit, settings=self.settings)
        if cost > available_balance + 1e-9:
            return self._reject(
                symbol, "insufficient_account_balance",
                f"含費成本 {cost:,.0f} > 帳戶可用 {available_balance:,.0f}",
                0, unit,
            )
        return None

    def _check_entry_shares(
        self,
        symbol: str,
        price: float,
        requested_shares: int,
        *,
        pct_chg: Optional[float] = None,
        available_balance: Optional[float] = None,
        enforce_account_balance: bool = False,
        entry_kind: EntryKind = "enter",
    ) -> EntryDecision:
        """零股進場檢查 (unit=share)。"""
        if requested_shares <= 0:
            return EntryDecision(False, "request_shares<=0", 0, "input", "share")

        if self.is_kill_switch_engaged():
            return self._reject(symbol, "kill_switch_engaged", "🔴 Kill switch 已拉起", 0, "share")

        if symbol in self._blacklist:
            return self._reject(symbol, "blacklist", f"{symbol} 在黑名單", 0, "share")

        if self.settings.min_price > 0 and price < self.settings.min_price:
            return self._reject(
                symbol, "below_min_price",
                f"{symbol} 價格 {price} < min_price {self.settings.min_price}", 0, "share",
            )
        if self.settings.max_price > 0 and price > self.settings.max_price:
            return self._reject(
                symbol, "above_max_price",
                f"{symbol} 價格 {price} > max_price {self.settings.max_price}", 0, "share",
            )

        if (
            pct_chg is not None
            and self.settings.max_pct_chg_on_entry > 0
            and abs(pct_chg) > self.settings.max_pct_chg_on_entry
        ):
            return self._reject(
                symbol, "pct_chg_too_large",
                f"{symbol} 漲幅 {pct_chg:.2f}% > 上限 {self.settings.max_pct_chg_on_entry}%",
                0, "share",
            )

        if (
            self.settings.daily_max_orders > 0
            and self.state.today_orders >= self.settings.daily_max_orders
        ):
            return self._reject(
                symbol, "daily_orders_exceeded",
                f"今日已下 {self.state.today_orders} 單，達上限 {self.settings.daily_max_orders}",
                0, "share",
            )

        if entry_kind != "rebuy":
            per_sym = self.state.today_orders_per_symbol.get(symbol, 0)
            if (
                self.settings.per_symbol_daily_max_orders > 0
                and per_sym >= self.settings.per_symbol_daily_max_orders
            ):
                return self._reject(
                    symbol, "per_symbol_orders_exceeded",
                    f"{symbol} 今日已進場 {per_sym} 次，達單檔上限 "
                    f"{self.settings.per_symbol_daily_max_orders}",
                    0, "share",
                )

        cooldown = float(self.settings.reentry_cooldown_seconds)
        if cooldown > 0:
            last = self.state.last_exit_ts.get(symbol)
            if last is not None:
                gap = now_tw().timestamp() - float(last)
                if gap < cooldown:
                    return self._reject(
                        symbol, "reentry_cooldown",
                        f"{symbol} 平倉後僅 {gap:.0f}s，需等 {cooldown:.0f}s 才能再進",
                        0, "share",
                    )

        if (
            self.settings.max_open_positions > 0
            and symbol not in self._open_symbols
            and self._open_positions >= self.settings.max_open_positions
        ):
            return self._reject(
                symbol, "max_open_positions",
                f"已有 {self._open_positions} 檔持倉，達上限 {self.settings.max_open_positions}",
                0, "share",
            )

        loss_cap_twd = self.settings.daily_max_loss_twd
        if self.settings.daily_max_loss_pct > 0:
            implied = self._loss_base * (self.settings.daily_max_loss_pct / 100.0)
            if loss_cap_twd <= 0 or implied < loss_cap_twd:
                loss_cap_twd = implied
        if loss_cap_twd > 0 and self.state.realized_pnl_twd <= -abs(loss_cap_twd):
            self.engage_kill_switch(
                f"daily_loss_circuit_breaker (PnL={self.state.realized_pnl_twd:.0f})"
            )
            return self._reject(
                symbol, "daily_loss_circuit_breaker",
                f"今日已實現虧損 {self.state.realized_pnl_twd:,.0f} TWD，超過熔斷門檻 "
                f"{loss_cap_twd:,.0f}，自動拉閘",
                0, "share",
            )

        if price <= 0:
            return self._reject(symbol, "invalid_price", f"price={price}", 0, "share")

        max_shares = min(requested_shares, self.settings.odd_lot_max_shares)

        if self.settings.per_order_max_cost_twd > 0:
            max_by_order = max_affordable_qty(
                price, float(self.settings.per_order_max_cost_twd), "share",
                max_qty=max_shares, settings=self.settings,
            )
            max_shares = min(max_shares, max_by_order)

        remaining = self.effective_fund_remaining()
        max_shares = max_affordable_qty(
            price, remaining, "share", max_qty=max_shares, settings=self.settings,
        )

        if max_shares <= 0:
            min_cost = buy_cash_required(price, 1, "share", settings=self.settings)
            return self._reject(
                symbol, "insufficient_fund",
                f"剩餘資金 {remaining:,.0f} < 含費 1 股約 {min_cost:,.2f}",
                0, "share",
            )

        balance_decision = self._check_account_balance_gate(
            symbol, price, max_shares, "share",
            available_balance=available_balance,
            enforce_account_balance=enforce_account_balance,
        )
        if balance_decision is not None:
            return balance_decision

        return EntryDecision(
            allowed=True,
            adjusted_lots=max_shares,
            reason="ok",
            unit="share",
        )

    def record_blocked_attempt(self, symbol: str, rule: str, msg: str) -> None:
        """外部模組 (如 LlmGate) 記錄進場拒絕。"""
        self._reject(symbol, rule, msg, 0)

    def _reject(
        self,
        symbol: str,
        rule: str,
        msg: str,
        _lots: int,
        unit: QtyUnit = "lot",
    ) -> EntryDecision:
        self.logger.info("⛔ 進場拒絕 [%s] %s — %s", rule, symbol, msg)
        with self._lock:
            self.state.blocked_attempts.append({
                "ts": now_tw().isoformat(timespec="seconds"),
                "symbol": symbol,
                "rule": rule,
                "msg": msg,
            })
            # 只保留最近 200 筆
            self.state.blocked_attempts = self.state.blocked_attempts[-200:]
            self._persist()
        return EntryDecision(False, msg, 0, rule, unit)

    # ------------------------------------------------------------------
    # 對外: 出場永遠允許 (但記錄)
    # ------------------------------------------------------------------

    def check_exit(self, symbol: str, reason: str) -> bool:
        """出場一律允許 — 風控的目的是控制曝險，而非阻止平倉。"""
        self.logger.debug("✅ 出場允許 [%s] %s", reason, symbol)
        return True

    # ------------------------------------------------------------------
    # 對外: 事件回呼
    # ------------------------------------------------------------------

    def on_entry_filled(
        self,
        symbol: str,
        price: float,
        lots: int,
        *,
        unit: QtyUnit = "lot",
    ) -> None:
        """成交 (買進) 後呼叫。"""
        with self._lock:
            self._pending_exposure.pop(symbol, None)
            cost = buy_cash_required(price, lots, unit, settings=self.settings)
            self._fund_used += cost
            if symbol not in self._open_symbols:
                self._open_symbols.add(symbol)
            self._open_positions = len(self._open_symbols)
            self.state.today_orders += 1
            self.state.today_orders_per_symbol[symbol] = (
                self.state.today_orders_per_symbol.get(symbol, 0) + 1
            )
            self._persist()
            unit_label = "股" if unit == "share" else "張"
            self.logger.info(
                "📈 部位新增 %s @ %.2f x %d %s (已用資金 %.0f / %d 檔在倉)",
                symbol, price, lots, unit_label, self._fund_used, self._open_positions,
            )

    def on_exit_filled(
        self,
        symbol: str,
        avg_entry_price: float,
        exit_price: float,
        lots: int,
        *,
        unit: QtyUnit = "lot",
        position_closed: bool = False,
    ) -> None:
        """成交 (賣出) 後呼叫；自動累計實現損益。"""
        with self._lock:
            released = buy_cash_required(
                avg_entry_price, lots, unit, settings=self.settings,
            )
            self._fund_used = max(0.0, self._fund_used - released)
            if position_closed:
                self._open_symbols.discard(symbol)
            self._open_positions = len(self._open_symbols)
            pnl = net_pnl_twd(
                avg_entry_price, exit_price, lots, unit,
                settings=self.settings,
            )
            self.state.realized_pnl_twd += pnl
            if position_closed:
                self.state.last_exit_ts[symbol] = now_tw().timestamp()
            self._persist()
            self.logger.info(
                "📉 部位平倉 %s entry=%.2f exit=%.2f x %d → PnL=%.0f (累計 %.0f)",
                symbol, avg_entry_price, exit_price, lots, pnl,
                self.state.realized_pnl_twd,
            )

    def update_unrealized_pnl(self, total_unrealized_pnl_twd: float) -> None:
        """策略可定期回報未實現損益（僅供儀表板快照，不參與熔斷）。"""
        with self._lock:
            self._unrealized_pnl = total_unrealized_pnl_twd

    # ------------------------------------------------------------------
    # 對外: 給 dashboard 用
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, object]:
        cap = self.settings.daily_max_loss_twd
        if self.settings.daily_max_loss_pct > 0:
            implied = self._loss_base * (self.settings.daily_max_loss_pct / 100.0)
            if cap <= 0 or implied < cap:
                cap = implied
        return {
            "date": self.state.date,
            "kill_switch": self.is_kill_switch_engaged(),
            "fund_used": self._fund_used,
            "fund_remaining": max(
                0.0, self.settings.effective_fund_cap() - self._fund_used,
            ),
            "fund_cap": self.settings.effective_fund_cap(),
            "daily_fund_budget": self.settings.daily_fund_budget,
            "max_fund": self.settings.max_fund,
            "open_positions": self._open_positions,
            "max_open_positions": self.settings.max_open_positions,
            "today_orders": self.state.today_orders,
            "daily_max_orders": self.settings.daily_max_orders,
            "today_orders_per_symbol": dict(self.state.today_orders_per_symbol),
            "realized_pnl_twd": self.state.realized_pnl_twd,
            "unrealized_pnl_twd": self._unrealized_pnl,
            "daily_loss_cap": cap,
            "blocked_recent": list(self.state.blocked_attempts[-20:]),
            "blacklist": sorted(self._blacklist),
            "reentry_cooldown_seconds": self.settings.reentry_cooldown_seconds,
            "max_pct_chg_on_entry": self.settings.max_pct_chg_on_entry,
            "min_price": self.settings.min_price,
            "max_price": self.settings.max_price,
            "per_order_max_cost_twd": self.settings.per_order_max_cost_twd,
        }


__all__ = [
    "RiskGuard",
    "EntryDecision",
    "EntryKind",
    "DailyState",
    "KILL_SWITCH_FILENAME",
]
