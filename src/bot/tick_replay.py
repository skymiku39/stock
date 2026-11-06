"""Causal replay of real regular-lot ticks through the simulation order adapter.

Orders can match only a later timestamp, at observed opposite-side quotes and
bounded by displayed and traded lots. This models fills; it does not assert queue
priority or an exchange fill. Odd-lot quotes require a separate market feed.
"""
from contextlib import ExitStack
import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from shioaji.constant import Action, Status, StockOrderLot, OrderType

from bot.broker import SjBroker
from bot.models import MarketTick
from bot.paper_broker import PaperApi
from bot.simulation_check import _strategy
from bot.intraday_history import intraday_ts_to_datetime
from bot.runtime_state import atomic_json


class TickPaperApi(PaperApi):
    def __init__(self, settings, callback):
        super().__init__(settings, callback, auto_fill=False)
        self.current_tick = None
        self.submitted_at = {}
        self.fill_evidence = []

    def place_order(self, contract, order):
        if self.current_tick is None:
            raise ValueError('cannot submit before a market observation')
        if order.order_lot in (StockOrderLot.IntradayOdd, StockOrderLot.Odd):
            raise ValueError('regular-lot historical quotes cannot validate odd-lot execution')
        trade = super().place_order(contract, order)
        self.submitted_at[order.id] = self.current_tick.ts
        return trade

    def advance(self, tick):
        self.current_tick = tick
        self.prices[tick.symbol] = tick.close
        cur = intraday_ts_to_datetime(tick.ts).time()
        if not dt.time(9) <= cur <= dt.time(13,30):
            return
        remaining_volume = int(tick.volume)
        quote_remaining = {'Buy': int(tick.ask_volume or 0), 'Sell': int(tick.bid_volume or 0)}
        for trade in self.trades:
            order = trade.order
            if (trade.contract.code != tick.symbol or trade.status.status not in (Status.Submitted, Status.PartFilled)
                    or tick.ts <= self.submitted_at[order.id]):
                continue
            side = order.action.value
            immediate = order.order_type in (OrderType.IOC, OrderType.FOK)
            price = tick.ask_price if order.action == Action.Buy else tick.bid_price
            if not price or price <= 0:
                if immediate:
                    self.cancel_order(trade)
                continue
            if order.price and (price > order.price if side == 'Buy' else price < order.price):
                if immediate:
                    self.cancel_order(trade)
                continue
            qty = min(order.quantity-trade.status.deal_quantity, remaining_volume, quote_remaining[side])
            if qty <= 0 or (order.order_type == OrderType.FOK and qty < order.quantity):
                if immediate:
                    self.cancel_order(trade)
                continue
            super().fill(trade, qty, price)
            remaining_volume -= qty
            quote_remaining[side] -= qty
            self.fill_evidence.append(dict(order_id=order.id, submitted_at=self.submitted_at[order.id],
                ts=tick.ts, sequence=tick.sequence, source_document_id=tick.source_record_id,
                side=side, price=price, quantity=qty, unit='lot',
                bid_price=tick.bid_price, ask_price=tick.ask_price, bid_volume=tick.bid_volume,
                ask_volume=tick.ask_volume, traded_volume=tick.volume))
            if immediate and trade.status.status != Status.Filled:
                self.cancel_order(trade)


def replay_ticks(db, settings, symbol: str, day: str, output: Path, *, root=None) -> dict:
    if output.exists() and any(output.iterdir()):
        raise ValueError('replay output must be a fresh directory')
    from bot.intraday_reconciliation import reconcile_intraday_session
    reconciliation = reconcile_intraday_session(db, root or db.path.parent.parent, symbol, day)
    ticks = [b for b in db.get_intraday_bars(symbol, interval='tick', start=day, end=day)
             if dt.time(9) <= intraday_ts_to_datetime(b.ts).time() <= dt.time(13,30)]
    if not ticks or any(t.quality_status != 'ok' or not t.source_record_id for t in ticks):
        raise ValueError('replay requires reconciled real tick records')
    previous = db.get_price_history(symbol, end=(dt.date.fromisoformat(day)-dt.timedelta(days=1)).isoformat(), ascending=False, limit=1)
    if not previous or previous[0].close <= 0 or previous[0].quality_status != 'ok' or not previous[0].source_record_id:
        raise ValueError('verified previous daily close missing')
    prev_close = previous[0].close
    settings = settings.model_copy(update=dict(simulation=True, run_mode='trade', symbols=[symbol],
        api_key='local-only', secret_key='local-only', use_odd_lot=False, symbols_auto_merge=False,
        telegram_bot_token='', telegram_chat_id='', llm_gate_enabled=False, llm_sell_gate_enabled=False,
        llm_intraday_review_enabled=False, check_account_balance=False))
    broker = SjBroker(settings, simulation_only=True)
    api = TickPaperApi(settings, broker._dispatch_order_callback)
    broker.api = api
    broker._contracts[symbol] = api.contract(symbol)
    broker._contracts[symbol].reference = prev_close
    broker._contracts[symbol].limit_up = prev_close*1.1
    broker._contracts[symbol].limit_down = prev_close*.9
    clock = [intraday_ts_to_datetime(ticks[0].ts).replace(tzinfo=ZoneInfo('Asia/Taipei'))]
    with ExitStack() as stack:
        for name in ('bot.strategy.now_tw','bot.risk_guard.now_tw','bot.recorder.now_tw'):
            stack.enter_context(patch(name, side_effect=lambda: clock[0]))
        for name in ('bot.strategy.now_tw_time','bot.strategy_configurable.now_tw_time','bot.strategy_smile.now_tw_time'):
            stack.enter_context(patch(name, side_effect=lambda: clock[0].time()))
        strategy = _strategy(settings, broker, output)
        strategy._prev_close[symbol] = prev_close
        for bar in ticks:
            clock[0] = intraday_ts_to_datetime(bar.ts).replace(tzinfo=ZoneInfo('Asia/Taipei'))
            # Match pending orders before making the current observation available
            # to the strategy. Same-timestamp prints cannot fill a fresh signal.
            api.advance(bar)
            for trade in api.trades:
                strategy._refresh_order_state(trade)
            strategy.on_tick(MarketTick(clock[0], symbol, bar.close, bar.volume,
                (bar.close/prev_close-1)*100, prev_close, 'shioaji_historical', quality_status='ok'))
            if (settings.strategy_type != 'smile_curve' and clock[0].time() >= settings.exit_time
                    and symbol in strategy.positions and not strategy._has_pending(symbol)):
                strategy._place_stop_sell(symbol, strategy.positions[symbol].quantity, 'close')
        strategy.recorder.export_csv()
    result = dict(symbol=symbol, day=day, strategy=settings.strategy_type, tick_count=len(ticks),
        initial_cash=settings.effective_fund_cap(), final_cash=api.cash,
        realized_pnl=strategy.risk.state.realized_pnl_twd, cost_basis=strategy.risk.fund_used,
        positions=api.positions, pending_orders=[t.order.id for t in api.trades if t.status.status in (Status.Submitted,Status.PartFilled)],
        fills=api.fill_evidence, records=list(strategy.recorder._records),
        previous_close=prev_close, previous_close_source=previous[0].source_record_id,
        execution_model='next_timestamp_opposite_quote_capped_by_displayed_and_traded_lots',
        source_market='regular_lot', external_orders=0, reconciliation=reconciliation,
        cost_model=dict(broker_fee_rate=.001425*settings.broker_fee_discount,
                        minimum_fee=settings.broker_min_fee,
                        sell_tax_rate=.001 if symbol == '0050' else (settings.smile_regular_tax_rate if settings.strategy_type == 'smile_curve' else settings.day_trade_tax_rate),
                        rounding='unrounded model costs, not a broker settlement statement'),
        limitation='Historical quote matching models fills; queue priority is not observable.')
    atomic_json(output/'result.json',result)
    return result


def main(argv=None):
    import argparse
    from bot.config import Settings
    from bot.stock_db import StockDB
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--symbol',required=True)
    parser.add_argument('--date',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--strategy',choices=['configurable','smile_curve'],default='configurable')
    args=parser.parse_args(argv)
    settings=Settings(_env_file=str(args.root/'.env'),strategy_type=args.strategy)
    db=StockDB.open(root=args.root)
    try:
        result=replay_ticks(db,settings,args.symbol,args.date,args.output,root=args.root)
        print(json.dumps({k:result[k] for k in ('symbol','day','tick_count','final_cash','positions','pending_orders')},ensure_ascii=False))
        return 0
    finally:
        db.close()


if __name__=='__main__':
    raise SystemExit(main())
