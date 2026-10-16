"""Conservative daily-bar execution proxy for ordinary TW cash equities.

No same-day high/low/close/volume is used to decide an opening fill. Prior close
is only a price-band proxy: corporate-action reference prices need better data.
"""
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math


def tick(price: float) -> float:
    for boundary, step in ((10,.01),(50,.05),(100,.1),(500,.5),(1000,1)):
        if price < boundary:
            return step
    return 5.0


def round_tick(price: float, up: bool) -> float:
    step = Decimal(str(tick(price)))
    return float((Decimal(str(price))/step).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR)*step)


@dataclass(frozen=True)
class SmileExecution:
    slippage_bps: float = 10.0
    participation: float = .001
    limit_guard: bool = True
    tick_rounding: bool = True
    volume_guard: bool = True

    def __post_init__(self):
        if not math.isfinite(self.slippage_bps) or not 0 <= self.slippage_bps < 10000:
            raise ValueError("slippage_bps must be finite in [0,10000)")
        if not math.isfinite(self.participation) or not 0 < self.participation <= 1:
            raise ValueError("participation must be in (0,1]")

    @classmethod
    def ideal(cls):
        return cls(0, 1, False, False, False)

    @property
    def is_ideal(self):
        return self == self.ideal()

    def to_dict(self):
        return dict(**asdict(self), volume_basis="previous_session_lots",
                    limit_basis="previous_close_proxy_not_official_reference",
                    partial_sell_policy="whole_entry_lot_or_cancel")

    def capacity(self, previous_volume):
        if not self.volume_guard:
            return math.inf
        if previous_volume is None or not math.isfinite(previous_volume) or previous_volume <= 0:
            return 0
        return math.floor(previous_volume * 1000 * self.participation)

    def slipped(self, price, side):
        value = float(Decimal(str(price)) * (1 + Decimal(str(self.slippage_bps))/10000 * (1 if side == "buy" else -1)))
        return round_tick(value, side == "buy") if self.tick_rounding else value

    def quote(self, opening, side, previous_close):
        price = self.slipped(opening, side)
        if not self.limit_guard:
            return price, ""
        if previous_close is None or not math.isfinite(previous_close) or previous_close <= 0:
            return None, "missing_limit_reference"
        upper = round_tick(float(Decimal(str(previous_close)) * Decimal("1.10")), False)
        lower = round_tick(float(Decimal(str(previous_close)) * Decimal("0.90")), True)
        if opening > upper + 1e-8 or opening < lower - 1e-8:
            return None, "outside_proxy_band_requires_reference"
        if side == "buy" and opening >= upper - 1e-8:
            return None, "limit_up_buy_blocked"
        if side == "sell" and opening <= lower + 1e-8:
            return None, "limit_down_sell_blocked"
        if price > upper + 1e-8 or price < lower - 1e-8:
            return None, "slippage_outside_price_band"
        return price, ""
