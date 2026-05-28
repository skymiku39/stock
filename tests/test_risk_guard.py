"""RiskGuard 風控守門員測試。

涵蓋:
  - 基本資金/張數上限調整
  - 黑名單、價格區間、漲幅上限
  - 每日下單次數 / 單檔次數上限
  - 再進場冷卻
  - 同時持倉檔數
  - 每日損失熔斷 (自動拉 kill switch)
  - Kill switch 檔案存在 → 拒絕新進場
  - 出場永遠允許
  - 狀態持久化
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bot.config import Settings
from bot.risk_guard import KILL_SWITCH_FILENAME, RiskGuard


def _make_settings(**overrides):
    base = dict(
        symbols=["2330", "0050"],
        max_fund=500_000,
        max_lot_per_symbol=2,
        per_order_max_cost_twd=0,
        max_open_positions=0,
        daily_max_orders=0,
        per_symbol_daily_max_orders=0,
        reentry_cooldown_seconds=0,
        max_pct_chg_on_entry=0.0,
        min_price=0.0,
        max_price=0.0,
        blacklist_symbols=[],
        daily_max_loss_twd=0,
        daily_max_loss_pct=0.0,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture
def guard(tmp_path: Path) -> RiskGuard:
    s = _make_settings()
    return RiskGuard(settings=s, project_root=tmp_path)


class TestEntryBasics:
    def test_allows_normal_entry(self, guard: RiskGuard) -> None:
        # 100 元 1 張 = 100,000 < max_fund 500,000 ✓
        d = guard.check_entry("2330", price=100, requested_lots=1)
        assert d.allowed
        assert d.adjusted_lots == 1

    def test_rejects_zero_lots(self, guard: RiskGuard) -> None:
        d = guard.check_entry("2330", price=100, requested_lots=0)
        assert not d.allowed

    def test_clamps_to_max_lot_per_symbol(self, guard: RiskGuard) -> None:
        d = guard.check_entry("2330", price=50, requested_lots=10)
        assert d.allowed
        assert d.adjusted_lots == 2  # max_lot_per_symbol default

    def test_insufficient_fund_rejects(self, tmp_path: Path) -> None:
        s = _make_settings(max_fund=50_000)
        g = RiskGuard(settings=s, project_root=tmp_path)
        d = g.check_entry("2330", price=600, requested_lots=1)
        assert not d.allowed
        assert "insufficient_fund" in d.blocking_rule

    def test_per_order_max_cost_clamps(self, tmp_path: Path) -> None:
        s = _make_settings(per_order_max_cost_twd=100_000, max_fund=1_000_000)
        g = RiskGuard(settings=s, project_root=tmp_path)
        # 100/股 × 1 張 = 100,000 → 剛好 1 張
        d = g.check_entry("2330", price=100, requested_lots=2)
        assert d.allowed and d.adjusted_lots == 1
        # 200/股 × 1 張 = 200,000 > 100,000 → 0 張被擋
        d2 = g.check_entry("2330", price=200, requested_lots=2)
        assert not d2.allowed


class TestBlacklistAndPrice:
    def test_blacklist_rejects(self, tmp_path: Path) -> None:
        s = _make_settings(blacklist_symbols=["2498"])
        g = RiskGuard(settings=s, project_root=tmp_path)
        d = g.check_entry("2498", price=20, requested_lots=1)
        assert not d.allowed and "blacklist" in d.blocking_rule

    def test_min_price_rejects(self, tmp_path: Path) -> None:
        s = _make_settings(min_price=20.0)
        g = RiskGuard(settings=s, project_root=tmp_path)
        d = g.check_entry("9999", price=8, requested_lots=1)
        assert not d.allowed and "below_min_price" in d.blocking_rule

    def test_max_price_rejects(self, tmp_path: Path) -> None:
        s = _make_settings(max_price=500.0)
        g = RiskGuard(settings=s, project_root=tmp_path)
        d = g.check_entry("2330", price=1000, requested_lots=1)
        assert not d.allowed and "above_max_price" in d.blocking_rule


class TestPctChgGuard:
    def test_max_pct_chg_blocks_chase(self, tmp_path: Path) -> None:
        s = _make_settings(max_pct_chg_on_entry=4.0)
        g = RiskGuard(settings=s, project_root=tmp_path)
        d = g.check_entry("2330", price=100, requested_lots=1, pct_chg=5.5)
        assert not d.allowed and "pct_chg_too_large" in d.blocking_rule

    def test_within_pct_chg_ok(self, tmp_path: Path) -> None:
        s = _make_settings(max_pct_chg_on_entry=4.0)
        g = RiskGuard(settings=s, project_root=tmp_path)
        d = g.check_entry("2330", price=100, requested_lots=1, pct_chg=3.0)
        assert d.allowed


class TestOrderFrequency:
    def test_daily_max_orders(self, tmp_path: Path) -> None:
        s = _make_settings(daily_max_orders=2)
        g = RiskGuard(settings=s, project_root=tmp_path)
        for _ in range(2):
            g.on_entry_filled("2330", 100, 1)
        d = g.check_entry("2330", price=100, requested_lots=1)
        assert not d.allowed and "daily_orders_exceeded" in d.blocking_rule

    def test_per_symbol_daily_orders(self, tmp_path: Path) -> None:
        s = _make_settings(per_symbol_daily_max_orders=1)
        g = RiskGuard(settings=s, project_root=tmp_path)
        g.on_entry_filled("2330", 100, 1)
        d = g.check_entry("2330", price=100, requested_lots=1)
        assert not d.allowed and "per_symbol" in d.blocking_rule

    def test_reentry_cooldown(self, tmp_path: Path) -> None:
        s = _make_settings(reentry_cooldown_seconds=60)
        g = RiskGuard(settings=s, project_root=tmp_path)
        g.on_entry_filled("2330", 100, 1)
        g.on_exit_filled("2330", 100, 102, 1)
        # 剛平倉，馬上進不准
        d = g.check_entry("2330", price=102, requested_lots=1)
        assert not d.allowed and "cooldown" in d.blocking_rule


class TestOpenPositions:
    def test_max_open_positions(self, tmp_path: Path) -> None:
        s = _make_settings(max_open_positions=2)
        g = RiskGuard(settings=s, project_root=tmp_path)
        g.on_entry_filled("2330", 100, 1)
        g.on_entry_filled("0050", 100, 1)
        d = g.check_entry("2317", price=100, requested_lots=1)
        assert not d.allowed and "max_open_positions" in d.blocking_rule
        # 平掉一檔後可以再開
        g.on_exit_filled("2330", 100, 100, 1)
        d2 = g.check_entry("2317", price=100, requested_lots=1)
        assert d2.allowed


class TestLossCircuitBreaker:
    def test_daily_loss_twd_breaker(self, tmp_path: Path) -> None:
        s = _make_settings(daily_max_loss_twd=5000, max_fund=200_000)
        g = RiskGuard(settings=s, project_root=tmp_path)
        g.on_entry_filled("2330", 100, 1)
        g.on_exit_filled("2330", 100, 94, 1)  # 虧 6000
        d = g.check_entry("0050", price=50, requested_lots=1)
        assert not d.allowed and "circuit_breaker" in d.blocking_rule
        assert g.is_kill_switch_engaged()

    def test_daily_loss_pct_breaker(self, tmp_path: Path) -> None:
        s = _make_settings(daily_max_loss_pct=2.0, max_fund=500_000)
        g = RiskGuard(settings=s, project_root=tmp_path)
        # 500k * 2% = 10k 是門檻
        g.on_entry_filled("2330", 100, 1)
        g.on_exit_filled("2330", 100, 88, 1)  # 虧 12000
        d = g.check_entry("0050", price=50, requested_lots=1)
        assert not d.allowed
        assert g.is_kill_switch_engaged()


class TestKillSwitch:
    def test_engage_blocks_entry(self, guard: RiskGuard) -> None:
        assert not guard.is_kill_switch_engaged()
        guard.engage_kill_switch("test")
        assert guard.is_kill_switch_engaged()
        d = guard.check_entry("2330", price=100, requested_lots=1)
        assert not d.allowed and d.blocking_rule == "kill_switch_engaged"

    def test_file_based_engage(self, tmp_path: Path) -> None:
        s = _make_settings()
        g = RiskGuard(settings=s, project_root=tmp_path)
        (tmp_path / "data" / KILL_SWITCH_FILENAME).write_text("manual")
        assert g.is_kill_switch_engaged()

    def test_release(self, guard: RiskGuard) -> None:
        guard.engage_kill_switch("test")
        guard.release_kill_switch()
        assert not guard.is_kill_switch_engaged()


class TestExitAndState:
    def test_exit_always_allowed(self, guard: RiskGuard) -> None:
        guard.engage_kill_switch("emergency")
        assert guard.check_exit("2330", "stop_loss") is True

    def test_state_persists_to_file(self, tmp_path: Path) -> None:
        s = _make_settings()
        g = RiskGuard(settings=s, project_root=tmp_path)
        g.on_entry_filled("2330", 100, 1)
        files = list((tmp_path / "data").glob("risk_state_*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["today_orders"] == 1
        assert data["today_orders_per_symbol"] == {"2330": 1}

    def test_snapshot_keys(self, guard: RiskGuard) -> None:
        guard.on_entry_filled("2330", 100, 1)
        snap = guard.snapshot()
        for key in (
            "fund_used", "fund_remaining", "open_positions", "today_orders",
            "realized_pnl_twd", "kill_switch", "blacklist",
        ):
            assert key in snap
        assert snap["fund_used"] == 100 * 1 * 1000
        assert snap["open_positions"] == 1
