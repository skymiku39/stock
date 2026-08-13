"""啟動安全檢查 — 手動持股重疊、券商庫存與本工具 AI 紀錄對帳。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.ownership import effective_trading_blacklist
from bot.portfolio import (
    classify_bot_ownership,
    fetch_broker_positions,
    load_bot_portfolio,
)

if TYPE_CHECKING:
    from bot.broker import SjBroker
    from bot.config import Settings
    from bot.risk_guard import RiskGuard

logger = logging.getLogger("position_safety")


@dataclass
class OverlapIssue:
    symbol: str
    broker_qty: float
    bot_qty: float
    manual_qty: float
    message: str


@dataclass
class ReconcileIssue:
    symbol: str
    broker_qty: float
    bot_qty: float
    kind: str  # manual_overlap | bot_exceeds_broker
    message: str


@dataclass
class StartupSafetyReport:
    ok: bool
    overlap_issues: list[OverlapIssue] = field(default_factory=list)
    reconcile_issues: list[ReconcileIssue] = field(default_factory=list)
    broker_fetch_error: str = ""
    summary: str = ""

    @property
    def blocking_messages(self) -> list[str]:
        msgs = [i.message for i in self.overlap_issues] + [
            i.message for i in self.reconcile_issues
        ]
        if self.broker_fetch_error:
            msgs.insert(0, self.broker_fetch_error)
        return msgs


def audit_manual_overlap(
    settings: Settings,
    project_root: Path | None = None,
    *,
    broker_snapshot: Any | None = None,
) -> list[OverlapIssue]:
    """檢查監控標的是否與券商「非本工具」庫存重疊。"""
    root = project_root or Path.cwd()
    issues: list[OverlapIssue] = []

    _, bot_positions = load_bot_portfolio(root)
    monitored = set(settings.symbols or [])
    if not monitored:
        return issues

    if broker_snapshot is None:
        broker_snapshot = fetch_broker_positions(settings)

    if getattr(broker_snapshot, "error", ""):
        logger.warning("無法讀取券商庫存做重疊檢查: %s", broker_snapshot.error)
        return issues

    broker_map: dict[str, float] = {
        p.symbol: float(p.qty) for p in broker_snapshot.positions if p.symbol
    }

    for symbol in sorted(monitored):
        broker_qty = broker_map.get(symbol, 0.0)
        if broker_qty <= 0:
            continue
        bot_pos = bot_positions.get(symbol)
        ownership = classify_bot_ownership(broker_qty, bot_pos)
        if ownership.manual_qty > 1e-9:
            issues.append(OverlapIssue(
                symbol=symbol,
                broker_qty=broker_qty,
                bot_qty=ownership.bot_qty,
                manual_qty=ownership.manual_qty,
                message=(
                    f"{symbol} 券商總量 {broker_qty:g} 張，"
                    f"本工具紀錄 {ownership.bot_qty:g} 張，"
                    f"疑似手動/外部 {ownership.manual_qty:g} 張 — "
                    f"自動賣出可能影響手動持股"
                ),
            ))
    return issues


def reconcile_broker_vs_bot_records(
    settings: Settings,
    project_root: Path | None = None,
    *,
    broker_snapshot: Any | None = None,
) -> list[ReconcileIssue]:
    """比對券商庫存與本地 AI 成交紀錄（僅監控清單內標的）。"""
    root = project_root or Path.cwd()
    issues: list[ReconcileIssue] = []

    _, bot_positions = load_bot_portfolio(root)
    monitored = set(settings.symbols or [])

    if broker_snapshot is None:
        broker_snapshot = fetch_broker_positions(settings)

    if getattr(broker_snapshot, "error", ""):
        return issues

    broker_map: dict[str, float] = {
        p.symbol: float(p.qty) for p in broker_snapshot.positions if p.symbol
    }

    for symbol in sorted(monitored):
        broker_qty = broker_map.get(symbol, 0.0)
        bot_qty = float(bot_positions.get(symbol).qty) if symbol in bot_positions else 0.0

        if bot_qty > broker_qty + 1e-9:
            issues.append(ReconcileIssue(
                symbol=symbol,
                broker_qty=broker_qty,
                bot_qty=bot_qty,
                kind="bot_exceeds_broker",
                message=(
                    f"{symbol} 本工具紀錄 {bot_qty:g} 張 > 券商庫存 {broker_qty:g} 張，"
                    f"對帳不一致，暫停新進場"
                ),
            ))

    return issues


def _broker_snapshot_usable(snapshot: Any | None) -> bool:
    return snapshot is not None and not getattr(snapshot, "error", "")


def run_startup_safety_checks(
    settings: Settings,
    project_root: Path | None = None,
    *,
    broker: SjBroker | None = None,
    broker_snapshot: Any | None = None,
    engage_kill_switch: bool = True,
    risk: RiskGuard | None = None,
    require_broker_snapshot: bool = True,
) -> StartupSafetyReport:
    """trade 模式啟動前執行：重疊稽核 + 對帳。有阻擋項則拉 Kill Switch。"""
    root = project_root or Path.cwd()
    broker_fetch_error = ""
    fetch_exc: str | None = None

    if broker_snapshot is None and broker is not None and broker.api is not None:
        try:
            from shioaji.constant import Unit

            account = broker.api.stock_account
            if account is None:
                fetch_exc = "登入成功但沒有 stock_account"
            else:
                from bot.portfolio import (
                    BrokerPositionsSnapshot,
                    broker_position_from_shioaji,
                )

                positions = broker.api.list_positions(account, unit=Unit.Common)
                rows = [
                    broker_position_from_shioaji(p, account=str(getattr(account, "account_id", "")))
                    for p in positions
                ]
                broker_snapshot = BrokerPositionsSnapshot(
                    broker="shioaji",
                    account="",
                    asof="",
                    simulation=settings.simulation,
                    positions=[p for p in rows if p.symbol and p.qty != 0],
                )
        except Exception as exc:
            fetch_exc = str(exc)
            logger.warning("啟動對帳：讀取 broker 庫存失敗: %s", exc)

    if broker_snapshot is None and fetch_exc is None and broker is None:
        broker_snapshot = fetch_broker_positions(settings)
        if getattr(broker_snapshot, "error", ""):
            fetch_exc = str(broker_snapshot.error)

    if require_broker_snapshot and not _broker_snapshot_usable(broker_snapshot):
        broker_fetch_error = (
            "券商庫存讀取失敗，無法完成啟動安全檢查"
            f"{f': {fetch_exc}' if fetch_exc else ''}"
        )

    overlap: list[OverlapIssue] = []
    reconcile: list[ReconcileIssue] = []
    if _broker_snapshot_usable(broker_snapshot):
        overlap = audit_manual_overlap(
            settings, root, broker_snapshot=broker_snapshot,
        )
        reconcile = reconcile_broker_vs_bot_records(
            settings, root, broker_snapshot=broker_snapshot,
        )

    blocked_symbols = effective_trading_blacklist(settings)
    overlap = [o for o in overlap if o.symbol not in blocked_symbols]
    reconcile = [r for r in reconcile if r.symbol not in blocked_symbols]

    ok = not overlap and not reconcile and not broker_fetch_error
    summary_parts: list[str] = []
    if broker_fetch_error:
        summary_parts.append("券商庫存讀取失敗")
    if overlap:
        summary_parts.append(f"手動持股重疊 {len(overlap)} 檔")
    if reconcile:
        summary_parts.append(f"對帳異常 {len(reconcile)} 檔")
    if ok:
        summary = "啟動安全檢查通過"
    else:
        summary = "；".join(summary_parts)

    if not ok and engage_kill_switch and risk is not None:
        detail = " | ".join(
            ([broker_fetch_error] if broker_fetch_error else [])
            + [i.message for i in overlap]
            + [i.message for i in reconcile]
        )[:500]
        risk.engage_kill_switch(f"startup_safety: {detail}")

    return StartupSafetyReport(
        ok=ok,
        overlap_issues=overlap,
        reconcile_issues=reconcile,
        broker_fetch_error=broker_fetch_error,
        summary=summary,
    )


def symbols_to_exclude_for_trading(settings: Settings) -> list[str]:
    """回傳應從自動交易排除的代號（手動持股 + 黑名單）。"""
    return sorted(effective_trading_blacklist(settings))


__all__ = [
    "OverlapIssue",
    "ReconcileIssue",
    "StartupSafetyReport",
    "audit_manual_overlap",
    "reconcile_broker_vs_bot_records",
    "run_startup_safety_checks",
    "symbols_to_exclude_for_trading",
]
