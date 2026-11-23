"""把券商「先進先出」的庫存，改用「賣出先扣最近買進的」重新認定剩下的是哪幾筆。

券商賣出時一律扣最舊的一筆，帳面均價因此往近期的買價靠攏。使用者短進短出時認定賣掉的是
最近買的那筆，舊的仍然留著；兩種認定的「已實現＋未實現」總和相同，但剩餘部位的成本不同，
損益兩平與停損價也跟著不同。

做法：把券商目前剩下的逐筆庫存，加上指定日期以後每筆賣出被券商扣掉的那些買進，還原成賣出前
的全部買進；再依賣出日期先後，每筆賣出改扣「賣出當天（含）以前、日期最近」的買進。

限制：只有日期沒有時間，同一天先賣後買時，那筆賣出會被當成賣掉當天買的（當沖本來就是如此）。
金額一律是含手續費的成本、扣掉費稅後的賣出淨額，和券商帳面的算法一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence


@dataclass(frozen=True)
class Lot:
    """一筆買進（或它還沒賣掉的部分）。cost 是這筆的總成本（元，含手續費）。"""
    date: str  # YYYY-MM-DD
    qty: float
    cost: float
    ref: str = ""


@dataclass(frozen=True)
class Sale:
    """一筆賣出。proceeds 是扣掉手續費與交易稅後實收的金額；matched 是券商扣掉的買進。"""
    date: str
    qty: float
    proceeds: float
    matched: Sequence[Lot] = ()
    ref: str = ""


@dataclass(frozen=True)
class SaleResult:
    date: str
    qty: float
    proceeds: float
    cost: float         # 改扣最近買進後，這筆賣出對應的成本
    broker_cost: float  # 券商先進先出扣掉的成本
    ref: str = ""

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost

    @property
    def broker_pnl(self) -> float:
        return self.proceeds - self.broker_cost


@dataclass
class LotView:
    lots: List[Lot] = field(default_factory=list)       # 重新認定後還留著的買進，舊到新
    sales: List[SaleResult] = field(default_factory=list)  # 被重新認定的賣出

    @property
    def qty(self) -> float:
        return sum(lot.qty for lot in self.lots)

    @property
    def cost(self) -> float:
        return sum(lot.cost for lot in self.lots)

    @property
    def realized_pnl(self) -> float:
        return sum(sale.pnl for sale in self.sales)

    @property
    def broker_realized_pnl(self) -> float:
        return sum(sale.broker_pnl for sale in self.sales)


def rematch_latest_first(remaining: Iterable[Lot], sales: Iterable[Sale], since: str) -> LotView:
    """since（含）以後的賣出改扣最近買進的；更早的賣出維持券商的認定。"""
    chosen = sorted((s for s in sales if s.date >= since and s.qty > 0), key=lambda s: s.date)
    pool = [lot for lot in remaining if lot.qty > 0]
    for sale in chosen:
        pool.extend(lot for lot in sale.matched if lot.qty > 0)
    pool.sort(key=lambda lot: lot.date)  # 穩定排序：同一天的維持原本順序

    results = []
    for sale in chosen:
        need, cost = float(sale.qty), 0.0
        for index in range(len(pool) - 1, -1, -1):
            if need <= 1e-9:
                break
            lot = pool[index]
            if lot.date > sale.date:
                continue
            take = min(lot.qty, need)
            part = lot.cost * take / lot.qty
            cost += part
            need -= take
            if lot.qty - take <= 1e-9:
                pool.pop(index)
            else:
                pool[index] = Lot(lot.date, lot.qty - take, lot.cost - part, lot.ref)
        if need > 1e-9:
            raise ValueError(f"{sale.date} 賣出 {sale.qty:g}，但當天以前的買進不夠扣（還差 {need:g}）")
        results.append(SaleResult(sale.date, float(sale.qty), float(sale.proceeds), cost,
                                  sum(lot.cost for lot in sale.matched), sale.ref))
    return LotView(lots=pool, sales=results)
