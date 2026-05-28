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
   * `daily_max_loss_twd` — 當日累計虧損 (含未實現) 超過此值 → 停止開新倉
   * `daily_max_loss_pct` — 同上，以 max_fund 百分比表示

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
   * 內含: today_orders, today_pnl, last_exit_ts, kill_switch_engaged

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
from typing import Dict, List, Optional, Tuple

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

        self.state = self._load_today_state()
        # 黑名單常態化 (轉為 set)
        self._blacklist = {s.strip() for s in (settings.blacklist_symbols or []) if s.strip()}
        # 同步 fund_used 上限做為虧損百分比基準
        self._loss_base = max(1.0, float(settings.max_fund))

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
            )
        except Exception as e:
            self.logger.warning("讀取風控狀態失敗，重設今日狀態: %s", e)
            return DailyState(date=today_iso)

    def _persist(self) -> None:
        p = self._state_path()
        try:
            p.write_text(
                json.dumps(asdict(self.state), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            self.logger.exception("寫入風控狀態失敗")

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
        pct_chg: Optional[float] = None,
    ) -> EntryDecision:
        """檢查是否可以進場。回傳是否允許 + 原因 + 調整後張數。

        呼叫端必須使用 `decision.adjusted_lots` 而非原本 requested_lots，
        因為風控可能會把張數壓低 (例如剩餘資金不夠買原請求數)。
        """
        if requested_lots <= 0:
            return EntryDecision(False, "request_lots<=0", 0, "input")

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

        per_sym = self.state.today_orders_per_symbol.get(symbol, 0)
        if (
            self.settings.per_symbol_daily_max_orders > 0
            and per_sym >= self.settings.per_symbol_daily_max_orders
        ):
            return self._reject(
                symbol, "per_symbol_orders_exceeded",
                f"{symbol} 今日已進場 {per_sym} 次，達單檔上限 {self.settings.per_symbol_daily_max_orders}",
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

        # 8) 同時持倉檔數
        if (
            self.settings.max_open_positions > 0
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

        # 10) 單檔最大張數 + 單筆最大成本 → 壓張數
        cost_per_lot = price * 1000  # 1 張 = 1000 股
        if cost_per_lot <= 0:
            return self._reject(symbol, "invalid_price", f"price={price}", 0)

        adjusted = min(requested_lots, self.settings.max_lot_per_symbol)

        if self.settings.per_order_max_cost_twd > 0:
            max_lots_by_per_order = int(self.settings.per_order_max_cost_twd / cost_per_lot)
            if max_lots_by_per_order < adjusted:
                adjusted = max_lots_by_per_order

        # 11) 剩餘可用資金
        remaining = self.settings.max_fund - self._fund_used
        if remaining < cost_per_lot:
            return self._reject(
                symbol, "insufficient_fund",
                f"剩餘資金 {remaining:,.0f} < 每張 {cost_per_lot:,.0f}",
                0,
            )
        max_lots_by_fund = int(remaining / cost_per_lot)
        if max_lots_by_fund < adjusted:
            adjusted = max_lots_by_fund

        if adjusted <= 0:
            return self._reject(symbol, "adjusted_to_zero",
                                "經風控調整後張數為 0", 0)

        return EntryDecision(
            allowed=True,
            adjusted_lots=adjusted,
            reason="ok",
        )

    def _reject(self, symbol: str, rule: str, msg: str, _lots: int) -> EntryDecision:
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
        return EntryDecision(False, msg, 0, rule)

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

    def on_entry_filled(self, symbol: str, price: float, lots: int) -> None:
        """成交 (買進) 後呼叫。"""
        with self._lock:
            cost = price * lots * 1000
            self._fund_used += cost
            self._open_positions += 1
            self.state.today_orders += 1
            self.state.today_orders_per_symbol[symbol] = (
                self.state.today_orders_per_symbol.get(symbol, 0) + 1
            )
            self._persist()
            self.logger.info(
                "📈 部位新增 %s @ %.2f x %d (已用資金 %.0f / %d 檔在倉)",
                symbol, price, lots, self._fund_used, self._open_positions,
            )

    def on_exit_filled(
        self,
        symbol: str,
        avg_entry_price: float,
        exit_price: float,
        lots: int,
    ) -> None:
        """成交 (賣出) 後呼叫；自動累計實現損益。"""
        with self._lock:
            released = exit_price * lots * 1000
            self._fund_used = max(0.0, self._fund_used - released)
            self._open_positions = max(0, self._open_positions - 1)
            pnl = (exit_price - avg_entry_price) * lots * 1000
            self.state.realized_pnl_twd += pnl
            self.state.last_exit_ts[symbol] = now_tw().timestamp()
            self._persist()
            self.logger.info(
                "📉 部位平倉 %s entry=%.2f exit=%.2f x %d → PnL=%.0f (累計 %.0f)",
                symbol, avg_entry_price, exit_price, lots, pnl,
                self.state.realized_pnl_twd,
            )

    def update_unrealized_pnl(self, total_unrealized_pnl_twd: float) -> None:
        """策略可定期回報未實現損益，用於熔斷判斷。"""
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
            "fund_remaining": max(0.0, self.settings.max_fund - self._fund_used),
            "fund_cap": self.settings.max_fund,
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
    "DailyState",
    "KILL_SWITCH_FILENAME",
]
