"""Durable real-quote follower for existing, evidenced Smile paper accounts.

No time limit and no forced sale. Quotes are saved before local matching;
restart replays the immutable journal from verified parent accounts. The SDK
connection has no connection to the order adapter (which is TickPaperApi).
"""
from __future__ import annotations

from bisect import bisect_right
from contextlib import ExitStack, contextmanager
import csv
import datetime as dt
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
from shioaji.constant import Status

from bot.intraday_history import intraday_ts_to_datetime
from bot.models import MarketTick
from bot.risk_guard import RiskGuard
from bot.runtime_state import FileLock, StateBusy, atomic_json
from bot.source_registry import SourceRegistry
from bot.stock_db import IntradayBar, PriceBar
from bot.tick_replay import replay_ticks

TW = ZoneInfo('Asia/Taipei')
PENDING = (Status.Submitted, Status.PartFilled)


@contextmanager
def paper_clock(clock):
    with ExitStack() as stack:
        for name in ('bot.strategy.now_tw', 'bot.risk_guard.now_tw', 'bot.recorder.now_tw'):
            stack.enter_context(patch(name, side_effect=lambda: clock[0]))
        for name in ('bot.strategy.now_tw_time', 'bot.strategy_smile.now_tw_time'):
            stack.enter_context(patch(name, side_effect=lambda: clock[0].time()))
        yield


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('nonfinite market number')
    return result


class QuoteDecoder:
    """Validate each real callback and retain only causally available books."""
    def __init__(self, symbol):
        self.symbol = symbol
        self.previous = None
        self.book_times, self.books = [], []
        self.day = None
        self.checks = self.ticks = self.duplicates = self.ignored = 0
        self.gaps = []
        self.last_connection = None

    def feed(self, row, sequence, source, safe_gap):
        event = row['payload']
        if event['code'] != self.symbol:
            return None
        # Simulated auction prints and odd lots are stored, never executed.
        flags = [event.get(k) for k in ('simtrade', 'intraday_odd', 'suspend')]
        if any(v is True for v in flags):
            self.ignored += 1
            return None
        if any(v is not False for v in flags):
            raise ValueError('unknown market event eligibility')
        when, received = pd.Timestamp(event['datetime']), pd.Timestamp(row['received_at'])
        if when.tzinfo is not None or received.tzinfo is None:
            raise ValueError('unknown callback timezone')
        if when > received.tz_convert('Asia/Taipei').tz_localize(None):
            raise ValueError('future market event')
        if not dt.time(9) <= when.time() <= dt.time(13, 30):
            self.ignored += 1
            return None
        day = str(when.date())
        if self.day and day < self.day:
            raise ValueError('market day moved backwards')
        if day != self.day:
            self.day, self.previous = day, None
            self.book_times, self.books = [], []
        connection = row.get('connection_id', 'seed')
        if row['kind'] == 'bidask':
            for side in ('bid', 'ask'):
                prices, volumes = event[side+'_price'], event[side+'_volume']
                if not prices or len(prices) != len(volumes):
                    raise ValueError('incomplete order book')
                for price, volume in zip(prices, volumes):
                    price, volume = number(price), number(volume)
                    if price < 0 or volume < 0 or volume != int(volume):
                        raise ValueError('invalid order book')
            pos = bisect_right(self.book_times, when.value)
            self.book_times.insert(pos, when.value)
            self.books.insert(pos, (event, dict(source_document_id=source, sequence=sequence,
                timestamp=str(when), received_at=row['received_at'])))
            # Old books cannot affect the one-second causal join.
            cutoff = bisect_right(self.book_times, when.value-2_000_000_000)
            if cutoff:
                del self.book_times[:cutoff]
                del self.books[:cutoff]
            return None
        if row['kind'] != 'tick':
            raise ValueError('unknown quote event')
        price, volume, amount = (number(event[k]) for k in ('close', 'volume', 'amount'))
        high, low, total, total_amount = (number(event[k]) for k in ('high', 'low', 'total_volume', 'total_amount'))
        if (price <= 0 or volume <= 0 or volume != int(volume) or amount != price*volume*1000
                or not 0 < low <= price <= high or total < volume or total != int(total) or total_amount < amount):
            raise ValueError('invalid trade or cumulative market values')
        gap = False
        if self.previous:
            old_when, old = self.previous
            if total <= number(old['total_volume']) and connection != self.last_connection:
                # A reconnect may replay old snapshots. They are not new liquidity.
                self.duplicates += 1
                return None
            if when < old_when:
                raise ValueError('out-of-order real ticks')
            if high < number(old['high']) or low > number(old['low']):
                raise ValueError('day high/low moved backwards')
            gap = total-number(old['total_volume']) != volume or total_amount-number(old['total_amount']) != amount
            if not gap:
                self.checks += 2
        else:
            gap = total != volume or total_amount != amount
        if gap:
            if not safe_gap(float(high), day):
                raise ValueError('unobserved trades may contain a strategy action')
            self.gaps.append(dict(day=day, through=str(when), source_document_id=source,
                sequence=sequence, reason='held_at_cap_and_day_high_below_original_sell_reference',
                full_market_coverage=False))
        self.previous, self.last_connection = (when, event), connection
        self.ticks += 1
        pos = bisect_right(self.book_times, when.value)-1
        book = self.books[pos] if pos >= 0 and when.value-self.book_times[pos] <= 1_000_000_000 else None
        tick = IntradayBar(symbol=self.symbol, ts=str(when), interval='tick', sequence=sequence,
            open=float(price), high=float(price), low=float(price), close=float(price), volume=int(volume),
            amount=float(amount), tick_type=int(event['tick_type']), quality_status='partial', source_record_id=source,
            bid_price=float(book[0]['bid_price'][0]) if book else None,
            ask_price=float(book[0]['ask_price'][0]) if book else None,
            bid_volume=int(book[0]['bid_volume'][0]) if book else None,
            ask_volume=int(book[0]['ask_volume'][0]) if book else None)
        tick.book_evidence = book[1] if book else None
        return tick


class SmileAccount:
    def __init__(self, db, root, settings, parent_path, output):
        self.parent_path, self.output = Path(parent_path), Path(output)
        raw = self.parent_path.read_bytes()
        self.parent, self.parent_sha256 = json.loads(raw), hashlib.sha256(raw).hexdigest()
        self.symbol, self.day = self.parent['symbol'], self.parent['day']
        if self.parent['strategy'] != 'smile_curve' or not self.parent['positions'].get(self.symbol) or self.parent['pending_orders']:
            raise ValueError('requires an evidenced open Smile parent without unresolved orders')
        runtime = {}
        result = replay_ticks(db, settings, self.symbol, self.day, self.output, root=root, runtime_sink=runtime)
        for key in ('final_cash', 'realized_pnl', 'cost_basis'):
            if abs(result[key]-self.parent[key]) > 1e-6:
                raise ValueError('parent ledger reconstruction mismatch: '+key)
        for key in ('cost_model', 'positions', 'fills', 'pending_orders'):
            if result[key] != self.parent[key]:
                raise ValueError('parent reconstruction mismatch: '+key)
        original = json.loads((self.parent_path.parent/'data/simulation/data/smile_state.json').read_text('utf-8'))
        restored = json.loads((self.output/'data/simulation/data/smile_state.json').read_text('utf-8'))
        if original != restored:
            raise ValueError('parent Smile state reconstruction mismatch')
        self.api, self.strategy, self.settings = runtime['api'], runtime['strategy'], runtime['settings']
        self.reference = self.strategy.engines[self.symbol].state.reference
        self.daily_pnl = {self.day: result['realized_pnl']}
        self.previous_close = result['previous_close']
        self.closed_at, self.last_tick, self.blocked = None, None, None
        self.ended_days = set()
        self.decoder = QuoteDecoder(self.symbol)
        self.db, self.root = db, Path(root)

    def safe_gap(self, high, day):
        return (not self.closed_at and not self.pending() and high < self.reference
                and self.api.positions.get(self.symbol) == self.settings.max_lot_per_symbol*1000)

    def pending(self):
        return [t for t in self.api.trades if t.status.status in PENDING]

    def change_day(self, day, clock, previous_close):
        if day <= self.day:
            raise ValueError('continuation day must move forward')
        self.end_day(self.day, clock)
        cost = self.strategy.risk.fund_used
        clock[0] = dt.datetime.fromisoformat(day+'T09:00:00').replace(tzinfo=TW)
        self.strategy.risk = RiskGuard(self.settings, project_root=self.strategy._state_root, logger=self.strategy.logger)
        if abs(cost-self.strategy.risk.fund_used) > 1e-6:
            raise ValueError('overnight cost carry mismatch')
        self.day, self.previous_close = day, previous_close
        contract = self.api.contract(self.symbol)
        contract.reference, contract.limit_up, contract.limit_down = previous_close, previous_close*1.1, previous_close*.9
        self.strategy.broker._contracts[self.symbol] = contract
        self.strategy._prev_close[self.symbol] = previous_close

    def end_day(self, day, clock):
        if day != self.day:
            raise ValueError('session expiry does not match account day')
        clock[0] = dt.datetime.fromisoformat(self.day+'T13:30:00').replace(tzinfo=TW)
        for trade in self.pending():
            self.api.cancel_order(trade)
            self.strategy._refresh_order_state(trade)
        self.daily_pnl[self.day] = self.strategy.risk.state.realized_pnl_twd
        self.strategy.risk._persist()
        self.ended_days.add(day)

    def advance(self, tick, clock):
        if self.closed_at or self.blocked:
            return
        clock[0] = intraday_ts_to_datetime(tick.ts).replace(tzinfo=TW)
        self.api.advance(tick)
        for trade in self.api.trades:
            self.strategy._refresh_order_state(trade)
        self.last_tick = tick.ts
        if not self.api.positions.get(self.symbol) and self.symbol not in self.strategy.positions:
            self.closed_at = tick.ts
        else:
            self.strategy.on_tick(MarketTick(clock[0], self.symbol, tick.close, tick.volume,
                (tick.close/self.previous_close-1)*100, self.previous_close, 'shioaji_observed', quality_status='ok'))
        self.daily_pnl[self.day] = self.strategy.risk.state.realized_pnl_twd

    def snapshot(self):
        # Independently rebuild cash, weighted acquisition cost, inventory and
        # realized profit using Decimal, then compare persisted fill CSVs too.
        cash, cost, pnl, shares = number(self.settings.effective_fund_cap()), Decimal(0), Decimal(0), 0
        model = self.parent['cost_model']
        for fill in self.api.fill_evidence:
            qty = int(fill['quantity'])*1000
            notional = number(fill['price'])*qty
            fee = max(number(model['minimum_fee']), notional*number(model['broker_fee_rate']))
            if fill['side'] == 'Buy':
                cash -= notional+fee
                cost += notional+fee
                shares += qty
            else:
                if shares < qty:
                    raise ValueError('sell exceeds independently recorded inventory')
                tax = notional*number(model['sell_tax_rate'])
                allocated = cost*qty/shares
                cash += notional-fee-tax
                pnl += notional-fee-tax-allocated
                cost -= allocated
                shares -= qty
        differences = dict(cash=float(number(self.api.cash)-cash), cost=float(number(self.strategy.risk.fund_used)-cost),
            profit=float(number(sum(self.daily_pnl.values()))-pnl))
        if any(abs(v) > 1e-6 for v in differences.values()) or shares != self.api.positions.get(self.symbol, 0):
            raise ValueError('independent cash/cost/profit/inventory reconciliation failed')
        rows = []
        for path in sorted((self.output/'data/simulation/data').glob('trades_*.csv')):
            with path.open(encoding='utf-8-sig', newline='') as handle:
                rows.extend(csv.DictReader(handle))
        if len(rows) != len(self.api.fill_evidence):
            raise ValueError('persisted fill count mismatch')
        for row, fill in zip(rows, self.api.fill_evidence):
            if (row['action'] != fill['side'] or number(row['price']) != number(fill['price'])
                    or int(row['quantity']) != fill['quantity'] or row['symbol'] != self.symbol or row['unit'] != 'lot'):
                raise ValueError('persisted fill content mismatch')
        engine = self.strategy.engines[self.symbol]
        saved_smile = json.loads((self.output/'data/simulation/data/smile_state.json').read_text('utf-8'))[self.symbol]
        saved_shares = sum(lot['quantity']*(1000 if lot['unit']=='lot' else 1) for lot in saved_smile['lots'])
        if saved_shares != shares or saved_smile['reference'] != engine.state.reference:
            raise ValueError('persisted Smile inventory/reference mismatch')
        complete = bool(self.closed_at and not self.blocked and not self.pending() and not shares
            and not cost and not engine.open_lots and self.symbol not in self.strategy.positions)
        return dict(symbol=self.symbol, parent_sha256=self.parent_sha256, parent_state_reconstructed=True,
            day=self.day, last_tick=self.last_tick, last_price=self.api.prices.get(self.symbol),
            original_sell_reference=self.reference, shares=shares, cash=str(cash), cost_basis=str(cost), realized_pnl=str(pnl),
            independent_ledger_differences=differences, persisted_fills_checked=len(rows),
            pending_orders=[t.order.id for t in self.pending()], fills=self.api.fill_evidence,
            closed_at=self.closed_at, lifecycle_complete=complete, blocked_reason=self.blocked,
            expired_sessions=sorted(self.ended_days),
            cumulative_volume_amount_checks=self.decoder.checks, validated_ticks=self.decoder.ticks,
            overlap_events_ignored=self.decoder.duplicates, ineligible_events_ignored=self.decoder.ignored,
            coverage_gaps=self.decoder.gaps, full_market_coverage=False, external_orders=0)


def previous_close(db, root, symbol, last_day, day):
    """Use verified official closes; require fresh full-month evidence after gaps."""
    cur = dt.date.fromisoformat(last_day)+dt.timedelta(days=1)
    weekdays = []
    while cur < dt.date.fromisoformat(day):
        if cur.weekday() < 5:
            weekdays.append(cur.isoformat())
        cur += dt.timedelta(days=1)
    rows = db.get_price_history(symbol, end=(dt.date.fromisoformat(day)-dt.timedelta(days=1)).isoformat(), ascending=False, limit=1)
    # The immediate next weekday can use an already verified prior close.
    if not weekdays and rows and rows[0].date == last_day and rows[0].quality_status == 'ok' and rows[0].source_record_id:
        return rows[0].close
    from bot.technicals import fetch_monthly_kline_with_meta
    months = {(d[:4], d[5:7]) for d in [last_day, *weekdays]}
    official = []
    for year, month in sorted(months):
        result = fetch_monthly_kline_with_meta(symbol, int(year), int(month), root=root)
        if result.quality_status != 'ok' or not result.source_document_id or not result.rows:
            raise ValueError('verified previous official close unavailable')
        for row in result.rows:
            if row['date'] < day:
                if row['close'] <= 0 or not row['low'] <= row['close'] <= row['high']:
                    raise ValueError('invalid official daily price')
                official.append(row)
                db.upsert_price_bar(PriceBar(symbol=symbol, **row, source='twse', source_url=result.source_url,
                    source_date=result.source_date, retrieved_at=result.retrieved_at, quality_status='ok', source_record_id=result.source_document_id))
    if any(last_day < row['date'] < day for row in official):
        raise ValueError('unobserved intervening trading session requires recovery')
    known = [r for r in official if r['date'] == last_day]
    if not known:
        raise ValueError('last observed session has no official close')
    return known[0]['close']


class QuoteConnection:
    """Quote-only SDK, immediately durable raw callbacks, reconnectable."""
    def __init__(self, root, path, symbols):
        self.root, self.path, self.symbols = root, Path(path), symbols
        self.api, self.errors = None, []
        self.guard = threading.Lock()
        self.last_callback = 0
        self.connection_id = None

    def save(self, kind, event):
        try:
            payload = event.to_dict() if hasattr(event, 'to_dict') else event.dict()
            row = dict(kind=kind, received_at=dt.datetime.now(TW).isoformat(), payload=payload, connection_id=self.connection_id)
            with self.guard:
                with self.path.open('a', encoding='utf-8', newline='\n') as handle:
                    handle.write(json.dumps(row, default=str)+'\n')
                self.last_callback = time.monotonic()
        except Exception as exc:
            self.errors.append(type(exc).__name__)

    def connect(self):
        import shioaji as sj
        from bot.config import Settings
        settings = Settings(_env_file=str(Path(self.root)/'.env'), simulation=True, run_mode='watch')
        self.connection_id = uuid4().hex
        self.api = sj.Shioaji(simulation=True)
        self.api.login(api_key=settings.api_key, secret_key=settings.secret_key, contracts_timeout=60000)
        self.api.on_tick_stk_v1()(lambda exchange, event: self.save('tick', event))
        self.api.on_bidask_stk_v1()(lambda exchange, event: self.save('bidask', event))
        for symbol in self.symbols:
            for kind in (sj.constant.QuoteType.Tick, sj.constant.QuoteType.BidAsk):
                self.api.quote.subscribe(self.api.Contracts.Stocks[symbol], quote_type=kind, version=sj.constant.QuoteVersion.v1)
        self.last_callback = time.monotonic()

    def disconnect(self):
        if self.api:
            try:
                self.api.logout()
            except Exception as exc:
                self.errors.append('logout:'+type(exc).__name__)
            finally:
                self.api = None


def recover_incomplete_writes(output):
    """Preserve crash tails, then replay only complete durable observations."""
    output = Path(output)
    inbox = output/'inbox.jsonl'
    recovery = output/'recovery'
    if inbox.exists() and inbox.stat().st_size:
        with inbox.open('r+b') as handle:
            size = handle.seek(0, 2)
            start = max(0, size-65536)
            handle.seek(start)
            tail = handle.read()
            if not tail.endswith(b'\n'):
                boundary = tail.rfind(b'\n')+1
                if not boundary and start:
                    raise ValueError('incomplete quote exceeds safe recovery window')
                recovery.mkdir(exist_ok=True)
                (recovery/f'inbox-tail-{uuid4().hex}.bin').write_bytes(tail[boundary:])
                handle.truncate(start+boundary)
                handle.flush()
                os.fsync(handle.fileno())
    paths = sorted((output/'observations').glob('*.jsonl'))
    if paths and (not paths[-1].stat().st_size or not paths[-1].read_bytes().endswith(b'\n')):
        recovery.mkdir(exist_ok=True)
        paths[-1].replace(recovery/f'incomplete-{paths[-1].name}-{uuid4().hex}.bin')


def run(root, output, settings_path, parent_paths, seed_stream=None):
    from bot.config import Settings
    from bot.stock_db import StockDB
    root, output = Path(root), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with FileLock(output/'follow.lock'):
        config = dict(settings=json.loads(Path(settings_path).read_text('utf-8')),
            parents=[dict(path=str(Path(p).resolve()), sha256=hashlib.sha256(Path(p).read_bytes()).hexdigest()) for p in parent_paths])
        config_path = output/'configuration.json'
        if config_path.exists() and json.loads(config_path.read_text('utf-8')) != config:
            raise ValueError('saved configuration or parent evidence changed')
        atomic_json(config_path, config)
        inbox, chunks = output/'inbox.jsonl', output/'observations'
        chunks.mkdir(exist_ok=True)
        recover_incomplete_writes(output)
        if not inbox.exists():
            raw = Path(seed_stream).read_bytes() if seed_stream else b''
            raw = raw[:raw.rfind(b'\n')+1]
            inbox.write_bytes(raw)
            atomic_json(output/'seed.json', dict(path=str(seed_stream), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        generation = output/'runs'/uuid4().hex
        db = StockDB.open(root=root)
        connection = QuoteConnection(root, inbox, [json.loads(Path(p).read_bytes())['symbol'] for p in parent_paths])
        try:
            atomic_json(output/'status.json', dict(state='reconstructing', pid=os.getpid(), observed_at=dt.datetime.now(TW).isoformat(), external_orders=0))
            accounts = [SmileAccount(db, root, Settings(_env_file=None, **config['settings']), p, generation/Path(p).parent.name) for p in parent_paths]
            by_symbol = {a.symbol:a for a in accounts}
            registry = SourceRegistry(root, db=db)
            offset = sequence = chunk_index = 0
            clock = [dt.datetime.now(TW)]
            checkpoint_path = output/'checkpoint.json'
            checkpoint = json.loads(checkpoint_path.read_text('utf-8')) if checkpoint_path.exists() else None
            def process(raw, index):
                nonlocal sequence
                source = registry.register_document('shioaji_follow', raw, kind='sdk_response', suffix='jsonl',
                    mime_type='application/x-ndjson', quality_status='partial', source_ref=f'{output.name}:{index}',
                    metadata=dict(simulation_login=True, external_orders=0, full_session=False))
                with paper_clock(clock):
                    for line in raw.splitlines():
                        row = json.loads(line)
                        account = by_symbol.get(row['payload']['code'])
                        if account and not account.closed_at and not account.blocked:
                            try:
                                if row['kind'] == 'session_end':
                                    received = dt.datetime.fromisoformat(row['received_at'])
                                    day = row['payload']['day']
                                    if received.tzinfo is None or received.astimezone(TW) < dt.datetime.fromisoformat(day+'T13:31:00').replace(tzinfo=TW):
                                        raise ValueError('premature session expiration')
                                    account.end_day(day, clock)
                                    tick = None
                                else:
                                    tick = account.decoder.feed(row, sequence, source.document_id, account.safe_gap)
                                if tick:
                                    day = tick.ts[:10]
                                    if day != account.day:
                                        prior = previous_close(db, root, account.symbol, account.day, day)
                                        account.change_day(day, clock, prior)
                                    account.advance(tick, clock)
                            except ValueError as exc:
                                account.blocked = str(exc)  # Only local validation errors, no SDK exceptions.
                            except Exception as exc:
                                account.blocked = 'processing:'+type(exc).__name__
                        sequence += 1
                    for account in accounts:
                        clock[0] = dt.datetime.fromisoformat(account.day+'T13:30:00').replace(tzinfo=TW)
                        account.strategy.risk._persist()
                        account.strategy.recorder.export_csv()

            with inbox.open('rb') as handle:
                for chunk_index, path in enumerate(sorted(chunks.glob('*.jsonl')), 1):
                    raw = path.read_bytes()
                    if handle.read(len(raw)) != raw:
                        raise ValueError('saved journal differs from immutable observation chunks')
                    process(raw, chunk_index-1)
                    offset += len(raw)
                    if checkpoint and offset == checkpoint['processed_bytes']:
                        for actual, expected in zip([a.snapshot() for a in accounts], checkpoint['accounts']):
                            for key in ('parent_sha256', 'cash', 'shares', 'cost_basis', 'realized_pnl',
                                        'pending_orders', 'fills', 'closed_at', 'last_tick'):
                                if actual[key] != expected[key]:
                                    raise ValueError('restart checkpoint mismatch: '+key)
                        checkpoint = None
            if checkpoint:
                raise ValueError('restart checkpoint does not end at an immutable chunk')
            next_flush, next_connect = 0, 0
            while True:
                now = dt.datetime.now(TW)
                market = now.weekday() < 5 and dt.time(8, 50) <= now.time() <= dt.time(13, 31)
                if market and not connection.api and time.monotonic() >= next_connect:
                    try:
                        connection.connect()
                    except Exception as exc:
                        connection.errors.append('connect:'+type(exc).__name__)
                        connection.disconnect()
                        next_connect = time.monotonic()+60
                if connection.api and (not market or (dt.time(9, 1) <= now.time() <= dt.time(13, 24)
                        and time.monotonic()-connection.last_callback > 120)):
                    connection.disconnect()
                    next_connect = time.monotonic()+5
                if time.monotonic() >= next_flush:
                    if now.time() >= dt.time(13, 31):
                        with connection.guard, inbox.open('a', encoding='utf-8', newline='\n') as handle:
                            for account in accounts:
                                if (account.day == str(now.date()) and account.day not in account.ended_days
                                        and not account.closed_at and not account.blocked):
                                    handle.write(json.dumps(dict(kind='session_end', received_at=now.isoformat(),
                                        payload=dict(code=account.symbol, day=account.day)))+'\n')
                    with inbox.open('rb') as handle:
                        handle.seek(offset)
                        raw = handle.read(8*1024*1024)
                    raw = raw[:raw.rfind(b'\n')+1]
                    if raw:
                        path = chunks/f'{chunk_index:08d}.jsonl'
                        # Commit the exact source before producing any fill.
                        with path.open('xb') as handle:
                            handle.write(raw)
                            handle.flush()
                            os.fsync(handle.fileno())
                        process(raw, chunk_index)
                        offset += len(raw)
                        chunk_index += 1
                    snapshots = [a.snapshot() for a in accounts]
                    complete = all(a['lifecycle_complete'] for a in snapshots)
                    state = ('complete' if complete else 'data_blocked' if any(a['blocked_reason'] for a in snapshots)
                        else 'following' if market and connection.api else 'waiting_session' if not market else 'reconnecting')
                    status = dict(state=state, pid=os.getpid(), observed_at=now.isoformat(), generation=str(generation),
                        quote_connected=bool(connection.api), last_callback_age_seconds=round(time.monotonic()-connection.last_callback, 1),
                        processed_bytes=offset, observed_events=sequence, chunks=chunk_index,
                        accounts=snapshots, errors=connection.errors[-20:], external_orders=0,
                        lifecycle_complete=complete, duration_limit=None,
                        limitation='Paper matches only; exchange queue priority and broker settlement are not asserted.')
                    atomic_json(output/'status.json', status)
                    atomic_json(generation/'result.json', status)
                    atomic_json(checkpoint_path, status)
                    if complete:
                        return status
                    next_flush = time.monotonic()+(1 if inbox.stat().st_size-offset > 8*1024*1024 else 15)
                time.sleep(1)
        finally:
            connection.disconnect()
            db.close()


def main(argv=None):
    import argparse
    import contextlib
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--parents', type=Path, nargs='+', required=True)
    parser.add_argument('--seed-stream', type=Path)
    args = parser.parse_args(argv)
    # Never retain SDK exception text or login payloads; suppress SDK logging.
    with open(os.devnull, 'w', encoding='utf-8') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            run(args.root, args.output, args.settings, args.parents, args.seed_stream)
        except StateBusy:
            return 73
        except Exception as exc:
            atomic_json(args.output/'failure.json', dict(error_type=type(exc).__name__, observed_at=dt.datetime.now(TW).isoformat()))
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
