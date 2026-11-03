"""Deterministic, network-free matching for testing the real order adapter.

Only the transport/matcher is replaced: SjBroker still constructs SDK orders,
and strategies still consume SDK-style partial-fill and order callbacks.
"""
from types import SimpleNamespace

from shioaji.account import StockAccount
from shioaji.constant import Action, Exchange, OrderState, Status, StockOrderLot, Unit
from shioaji.contracts import Stock
from shioaji.order import Deal, Order, OrderStatus, Trade

from bot.trade_cost import buy_cash_required, sell_cash_received


class PaperApi:
    Order = Order

    @property
    def simulation(self):
        return True

    def __init__(self, settings, callback, *, auto_fill=True):
        self.settings = settings
        self.callback = callback
        self.auto_fill = auto_fill
        self.cash = float(settings.effective_fund_cap())
        self.stock_account = StockAccount(person_id="LOCAL", broker_id="LOCAL",
                                         account_id="LOCAL", signed=True)
        self.trades = []
        self.positions = {}
        self.messages = []
        self.prices = {}

    @staticmethod
    def contract(symbol):
        return Stock(code=symbol, exchange=Exchange.TSE, name="Local simulation",
                     reference=100, limit_up=110, limit_down=90)

    def place_order(self, contract, order):
        order.id = order.seqno = f"P{len(self.trades) + 1:05d}"
        # Exchange order numbers can be blank before acknowledgement.
        order.ordno = "     "
        trade = Trade(contract=contract, order=order, status=OrderStatus(
            status=Status.Submitted, order_quantity=order.quantity, deals=[]))
        self.trades.append(trade)
        if self.auto_fill:
            self.fill(trade, order.quantity)
        return trade

    def fill(self, trade, quantity, price=None):
        order = trade.order
        symbol = trade.contract.code
        price = float(price if price is not None else
                      (order.price or self.prices.get(symbol, trade.contract.reference)))
        if quantity <= 0 or trade.status.deal_quantity + quantity > order.quantity:
            raise ValueError("Invalid simulated fill quantity")
        unit = "share" if order.order_lot in (StockOrderLot.IntradayOdd, StockOrderLot.Odd) else "lot"
        shares = quantity * (1 if unit == "share" else 1000)
        held = self.positions.get(symbol, 0)
        if order.action == Action.Buy:
            cost = buy_cash_required(price, quantity, unit, settings=self.settings)
            if cost > self.cash:
                raise ValueError("Insufficient paper cash")
            self.cash -= cost
            self.positions[symbol] = held + shares
        else:
            if held < shares:
                raise ValueError("Paper sell exceeds filled position")
            tax = .001 if symbol == "0050" else (
                self.settings.smile_regular_tax_rate if self.settings.strategy_type == "smile_curve"
                else self.settings.day_trade_tax_rate)
            self.cash += sell_cash_received(price, quantity, unit, settings=self.settings, sell_tax_rate=tax)
            self.positions[symbol] = held - shares
        seq = f"D{len(self.messages) + 1:06d}"
        trade.status.deal_quantity += quantity
        trade.status.status = (Status.Filled if trade.status.deal_quantity == order.quantity
                               else Status.PartFilled)
        trade.status.deals.append(Deal(seq=seq, price=price, quantity=quantity, ts=0))
        msg = dict(code=symbol, action=order.action.value, price=price, quantity=quantity,
                   seqno=order.seqno, ordno="", exchange_seq=seq,
                   order_lot=order.order_lot.value, custom_field=order.custom_field)
        self.messages.append(msg)
        self.callback(OrderState.StockDeal, msg)

    def update_status(self, *args, **kwargs):
        pass

    def list_trades(self):
        return self.trades

    def cancel_order(self, trade):
        if trade.status.status != Status.Filled:
            trade.status.status = Status.Cancelled
        return trade

    def list_positions(self, *args, unit=Unit.Share, **kwargs):
        return [SimpleNamespace(code=symbol, quantity=quantity if unit == Unit.Share else quantity / 1000)
                for symbol, quantity in self.positions.items() if quantity]

    def logout(self):
        pass
