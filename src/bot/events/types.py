"""領域事件型別 — 不可變、可序列化。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    """所有領域事件的基底（值物件）。"""

    event_id: str = field(default_factory=lambda: uuid4().hex)
    occurred_at: str = field(
        default_factory=lambda: dt.datetime.now(dt.UTC).isoformat(),
    )

    @property
    def event_type(self) -> str:
        return type(self).__name__


@dataclass(frozen=True, kw_only=True)
class DailyKlineFetched(DomainEvent):
    symbol: str
    bar_count: int
    start: str
    end: str
    source: str = "twse"


@dataclass(frozen=True, kw_only=True)
class QuantDataFetchCompleted(DomainEvent):
    symbols: tuple[str, ...]
    start: str
    end: str
    price_bars: dict[str, int] = field(default_factory=dict)
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, kw_only=True)
class SmileScreenCompleted(DomainEvent):
    total_fund: float
    top_n: int
    selected_symbols: tuple[str, ...]
    candidate_count: int


@dataclass(frozen=True, kw_only=True)
class SmileAuditCompleted(DomainEvent):
    symbols: tuple[str, ...]
    scenario_count: int
    report_path: str | None = None


@dataclass(frozen=True, kw_only=True)
class TickReceived(DomainEvent):
    """策略層行情事件（broker / market_source → 策略解耦）。"""

    symbol: str
    price: float
    pct_chg: float = 0.0
    source: str = "shioaji"
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 交易生命週期
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class BotStarted(DomainEvent):
    symbols: tuple[str, ...]
    run_mode: str
    simulation: bool


@dataclass(frozen=True, kw_only=True)
class BotShutdownRequested(DomainEvent):
    run_mode: str
    trade_summary: str = ""


@dataclass(frozen=True, kw_only=True)
class NotifyRequested(DomainEvent):
    text: str


@dataclass(frozen=True, kw_only=True)
class RiskEntryBlocked(DomainEvent):
    symbol: str
    reason: str
    blocking_rule: str


@dataclass(frozen=True, kw_only=True)
class TradeBuyFilled(DomainEvent):
    symbol: str
    price: float
    quantity: int
    unit: str
    order_msg: dict[str, Any]
    trade_reason: str = "enter"


@dataclass(frozen=True, kw_only=True)
class TradeSellFilled(DomainEvent):
    symbol: str
    price: float
    quantity: int
    unit: str
    order_msg: dict[str, Any]
    trade_reason: str
    entry_price: float
    pnl_pct: float = 0.0
    pnl_twd: float = 0.0
    position_closed: bool = False


@dataclass(frozen=True, kw_only=True)
class ClosureCompleted(DomainEvent):
    trade_summary: str


@dataclass(frozen=True, kw_only=True)
class SignalRecorded(DomainEvent):
    """watch/report 虛擬訊號（供訂閱者擴充，如儀表板即時更新）。"""

    symbol: str
    action: str
    price: float
    quantity: int
    reason: str
    run_mode: str


# ---------------------------------------------------------------------------
# 管線編排
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class PipelineStepCompleted(DomainEvent):
    pipeline: str
    step: str
    step_index: int
    total_steps: int
    success: bool
    message: str = ""
    duration_sec: float = 0.0


@dataclass(frozen=True, kw_only=True)
class PipelineCompleted(DomainEvent):
    pipeline: str
    run_id: str
    output_dir: str
    success: bool
    error_count: int = 0
    duration_sec: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 排程器
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SchedulerStarted(DomainEvent):
    job_names: tuple[str, ...]
    dry_run: bool = False
    run_once: bool = False


@dataclass(frozen=True, kw_only=True)
class SchedulerJobCompleted(DomainEvent):
    job_name: str
    success: bool
    exit_code: int = 0
    dry_run: bool = False
    log_path: str | None = None
