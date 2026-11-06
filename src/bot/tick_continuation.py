"""Continue an evidenced Smile position from its original causal tick replay.

Reconstruct the original state and compare its ledger and saved Smile batches
before consuming later observations. Stop only after the strategy's sell fills.
"""
from contextlib import ExitStack
from dataclasses import asdict
import datetime as dt
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from shioaji.constant import Status
from bot.broker import SjBroker
from bot.intraday_history import intraday_ts_to_datetime
from bot.intraday_reconciliation import reconcile_intraday_session, reconcile_observed_prefix
from bot.models import MarketTick
from bot.risk_guard import RiskGuard
from bot.runtime_state import atomic_json
from bot.simulation_check import _strategy
from bot.tick_replay import TickPaperApi


def continue_smile_replay(db, settings, parent_path, days, output, *, root, observed_at=None, stream_path=None):
    parent_path, output = Path(parent_path), Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('continuation output must be fresh')
    parent_bytes = parent_path.read_bytes()
    parent = json.loads(parent_bytes)
    symbol, first_day = parent['symbol'], parent['day']
    if parent['strategy'] != 'smile_curve' or not parent['positions'].get(symbol) or parent['pending_orders']:
        raise ValueError('parent must have an open Smile position with no unresolved order')
    expected_costs = dict(broker_fee_rate=.001425*settings.broker_fee_discount,
                          minimum_fee=settings.broker_min_fee,
                          sell_tax_rate=.001 if symbol == '0050' else settings.smile_regular_tax_rate)
    if any(abs(parent['cost_model'][key]-value) > 1e-12 for key,value in expected_costs.items()):
        raise ValueError('continuation cost model differs from parent')
    if not days or days != sorted(set(days)) or days[0] <= first_day:
        raise ValueError('continuation days must be unique and strictly later than parent')
    expected_days = []
    cur = dt.date.fromisoformat(first_day) + dt.timedelta(days=1)
    while cur <= dt.date.fromisoformat(days[-1]):
        if cur.weekday() < 5:
            expected_days.append(cur.isoformat())
        cur += dt.timedelta(days=1)
    if days != expected_days:
        raise ValueError('cannot skip intervening weekdays in a continuation')
    if stream_path and len(days) != 1:
        raise ValueError('a stream continuation currently supports one following session')
    settings = settings.model_copy(update=dict(strategy_type='smile_curve', simulation=True, run_mode='trade',
        symbols=[symbol], symbols_auto_merge=False, api_key='local-only', secret_key='local-only',
        use_odd_lot=False, telegram_bot_token='', telegram_chat_id='', llm_gate_enabled=False,
        llm_sell_gate_enabled=False, llm_intraday_review_enabled=False, check_account_balance=False))
    broker = SjBroker(settings, simulation_only=True)
    api = TickPaperApi(settings, broker._dispatch_order_callback)
    broker.api = api
    broker._contracts[symbol] = api.contract(symbol)
    clock = [dt.datetime.fromisoformat(first_day+'T09:00:00').replace(tzinfo=ZoneInfo('Asia/Taipei'))]
    receipts, checkpoints, pnl_by_day = [], [], {}
    closed_at = None
    with ExitStack() as stack:
        for name in ('bot.strategy.now_tw', 'bot.risk_guard.now_tw', 'bot.recorder.now_tw'):
            stack.enter_context(patch(name, side_effect=lambda: clock[0]))
        for name in ('bot.strategy.now_tw_time', 'bot.strategy_smile.now_tw_time'):
            stack.enter_context(patch(name, side_effect=lambda: clock[0].time()))
        strategy = _strategy(settings, broker, output)
        for day in [first_day, *days]:
            if stream_path and day != first_day:
                from bot.observed_stream import read_observed_stream
                ticks, receipt = read_observed_stream(db, root, stream_path, symbol, day)
                # A late subscription cannot silently skip trading decisions.
                # For these capped, already-held positions, a day high below the
                # original sell reference proves no earlier sell was possible;
                # the full position cap proves no earlier additional buy.
                held = api.positions.get(symbol, 0)
                if (receipt['first_day_high'] >= strategy.engines[symbol].state.reference
                        or held != settings.max_lot_per_symbol*1000):
                    raise ValueError('unobserved opening interval may contain a strategy action')
                receipt['pre_capture_no_action_proven'] = True
            elif observed_at and day == str(dt.datetime.fromisoformat(str(observed_at)).astimezone(ZoneInfo('Asia/Taipei')).date()):
                ticks, receipt = reconcile_observed_prefix(db, root, symbol, day, observed_at=observed_at)
            else:
                receipt = reconcile_intraday_session(db, root, symbol, day)
                ticks = [b for b in db.get_intraday_bars(symbol, interval='tick', start=day, end=day)
                         if dt.time(9) <= intraday_ts_to_datetime(b.ts).time() <= dt.time(13,30)]
            receipts.append(receipt)
            prev = db.get_price_history(symbol, end=(dt.date.fromisoformat(day)-dt.timedelta(days=1)).isoformat(), ascending=False, limit=1)
            if not prev or prev[0].quality_status != 'ok' or not prev[0].source_record_id:
                raise ValueError('verified previous close required for each continuation day')
            reference = prev[0].close
            if day != first_day:
                # Exchange day orders expire; cancellation is never a fill.
                for trade in api.trades:
                    if trade.status.status in (Status.Submitted, Status.PartFilled):
                        api.cancel_order(trade)
                        strategy._refresh_order_state(trade)
                prior_cost = strategy.risk.fund_used
                clock[0] = intraday_ts_to_datetime(ticks[0].ts).replace(tzinfo=ZoneInfo('Asia/Taipei'))
                strategy.risk = RiskGuard(settings, project_root=strategy._state_root, logger=strategy.logger)
                if abs(strategy.risk.fund_used-prior_cost) > 1e-6:
                    raise ValueError('overnight acquisition costs failed to carry forward')
            strategy._prev_close[symbol] = reference
            broker._contracts[symbol].reference = reference
            broker._contracts[symbol].limit_up = reference*1.1
            broker._contracts[symbol].limit_down = reference*.9
            for tick in ticks:
                clock[0] = intraday_ts_to_datetime(tick.ts).replace(tzinfo=ZoneInfo('Asia/Taipei'))
                api.advance(tick)
                for trade in api.trades:
                    strategy._refresh_order_state(trade)
                if day != first_day and not api.positions.get(symbol) and symbol not in strategy.positions:
                    closed_at = tick.ts
                    break
                strategy.on_tick(MarketTick(clock[0], symbol, tick.close, tick.volume,
                    (tick.close/reference-1)*100, reference, 'shioaji_observed', quality_status='ok'))
            pnl_by_day[day] = strategy.risk.state.realized_pnl_twd
            strategy.risk._persist()
            snapshot = dict(day=day, cash=api.cash, cost_basis=strategy.risk.fund_used,
                positions=dict(api.positions), reference=strategy.engines[symbol].state.reference,
                lots=[asdict(lot) for lot in strategy.engines[symbol].state.lots], fills=len(api.fill_evidence))
            checkpoints.append(snapshot)
            if day == first_day:
                for key, value in [('final_cash', api.cash), ('cost_basis', strategy.risk.fund_used),
                                   ('realized_pnl', strategy.risk.state.realized_pnl_twd)]:
                    if abs(parent[key]-value) > 1e-6:
                        raise ValueError('parent ledger reconstruction mismatch: '+key)
                if parent['positions'] != api.positions or len(parent['fills']) != len(api.fill_evidence):
                    raise ValueError('parent inventory/fill count reconstruction mismatch')
                for original, reconstructed in zip(parent['fills'], api.fill_evidence):
                    if any(reconstructed.get(k) != v for k, v in original.items()):
                        raise ValueError('parent fill reconstruction mismatch')
                original_state = json.loads((parent_path.parent/'data/simulation/data/smile_state.json').read_text(encoding='utf-8'))[symbol]
                current = strategy.engines[symbol].state
                if (original_state['reference'] != current.reference or original_state['lots'] != snapshot['lots']
                        or original_state['tiers_done'] != sorted(current.tiers_done)
                        or original_state['bottom_dates'] != sorted(current.bottom_dca_done_dates)):
                    raise ValueError('parent Smile cycle reconstruction mismatch')
            strategy.recorder.export_csv()
            if closed_at:
                break
        pending = [t.order.id for t in api.trades if t.status.status in (Status.Submitted, Status.PartFilled)]
        balance_difference = api.cash+strategy.risk.fund_used-settings.effective_fund_cap()-sum(pnl_by_day.values())
        if abs(balance_difference) > 1e-6:
            raise ValueError('continued cash, acquisition cost and cumulative profit do not reconcile')
        all_flat = (bool(closed_at) and not pending and not api.positions.get(symbol)
                    and symbol not in strategy.positions and not strategy.engines[symbol].open_lots
                    and abs(strategy.risk.fund_used)<1e-8)
        result = dict(symbol=symbol, strategy='smile_curve', parent_result=str(parent_path),
            parent_sha256=hashlib.sha256(parent_bytes).hexdigest(), parent_state_reconstructed=True,
            days=[c['day'] for c in checkpoints], initial_cash=settings.effective_fund_cap(), final_cash=api.cash,
            cost_basis=strategy.risk.fund_used, realized_pnl=sum(pnl_by_day.values()), daily_pnl=pnl_by_day,
            positions=api.positions, pending_orders=pending, closed_at=closed_at, lifecycle_complete=all_flat,
            ledger_balance_difference=balance_difference,
            fills=api.fill_evidence, records=list(strategy.recorder._records), checkpoints=checkpoints,
            orders=[dict(id=t.order.id, side=t.order.action.value, quantity=t.order.quantity,
                         price=t.order.price, status=t.status.status.value, submitted_at=api.submitted_at[t.order.id]) for t in api.trades],
            reconciliations=receipts, external_orders=0, cost_model=parent['cost_model'],
            limitation='Observed historical quote matching; no claim of exchange queue priority or broker settlement.')
    atomic_json(output/'result.json', result)
    return result


def main(argv=None):
    import argparse
    from bot.config import Settings
    from bot.stock_db import StockDB
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--parent', type=Path, required=True)
    parser.add_argument('--settings', type=Path, required=True,
                        help='Original replay configuration JSON; reconstruction must match')
    parser.add_argument('--days', nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    observation = parser.add_mutually_exclusive_group()
    observation.add_argument('--stream', type=Path, help='Archived real tick/bidask callbacks')
    observation.add_argument('--observed-at', help='Timezone-aware cutoff for archived minute/tick data')
    args = parser.parse_args(argv)
    settings = Settings(_env_file=None, **json.loads(args.settings.read_text(encoding='utf-8')))
    db = StockDB.open(root=args.root)
    try:
        result = continue_smile_replay(db, settings, args.parent, args.days, args.output,
            root=args.root, observed_at=args.observed_at, stream_path=args.stream)
        print(json.dumps({k: result[k] for k in ('symbol','days','lifecycle_complete','positions',
                                                'pending_orders','final_cash','realized_pnl')}))
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
