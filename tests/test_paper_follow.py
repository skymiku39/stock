"""Synthetic fixtures test failure boundaries; they are never market evidence."""
import datetime as dt
from decimal import Decimal

import pytest

from bot.paper_follow import QuoteDecoder, SmileAccount, paper_clock, recover_incomplete_writes, TW
from tests.test_tick_continuation import held_parent


def event(kind='tick', second=0, price=100.5, total=1, connection='one', **changes):
    payload = dict(code='0050', datetime=f'2026-11-06 09:00:{second:02d}',
        simtrade=False, intraday_odd=False, suspend=False)
    if kind == 'tick':
        payload.update(close=price, volume=1, amount=price*1000, high=price, low=price,
            total_volume=total, total_amount=total*price*1000, tick_type=1)
    else:
        payload.update(bid_price=[price], ask_price=[price], bid_volume=[1], ask_volume=[1])
    payload.update(changes)
    return dict(kind=kind, received_at=f'2026-11-06T09:01:00+08:00', payload=payload, connection_id=connection)


def test_decoder_never_uses_future_book_or_same_timestamp_fill(held_parent):
    db, settings, parent, _, root = held_parent
    account = SmileAccount(db, root, settings, parent, root/'follow')
    clock = [dt.datetime(2026,11,6,9,tzinfo=TW)]
    with paper_clock(clock):
        account.change_day('2026-11-06', clock, 100)
        account.decoder.feed(event('bidask', second=1), 0, 'future-book', account.safe_gap)
        first = account.decoder.feed(event(), 1, 'first-tick', account.safe_gap)
        assert first.bid_price is None
        account.advance(first, clock)
        assert account.pending() and account.snapshot()['shares'] == 1000
        same_time = account.decoder.feed(event(total=2), 2, 'same-time', account.safe_gap)
        account.advance(same_time, clock)
        assert account.snapshot()['shares'] == 1000
        later = account.decoder.feed(event(second=1, total=3), 3, 'later-tick', account.safe_gap)
        account.advance(later, clock)
        done = account.snapshot()
    assert done['lifecycle_complete'] and done['shares'] == 0
    assert done['fills'][-1]['book_evidence']['source_document_id'] == 'future-book'
    assert done['fills'][-1]['source_document_id'] == 'later-tick'
    assert all(abs(v) < 1e-6 for v in done['independent_ledger_differences'].values())


def test_reconstruct_and_replay_has_identical_fills_and_cash(held_parent):
    db, settings, parent, _, root = held_parent
    results = []
    for run in ('first', 'restart'):
        account = SmileAccount(db, root, settings, parent, root/run)
        clock = [dt.datetime(2026,11,6,9,tzinfo=TW)]
        rows = [event('bidask'), event(), event('bidask', second=1), event(second=1,total=2)]
        with paper_clock(clock):
            account.change_day('2026-11-06', clock, 100)
            for i, row in enumerate(rows):
                tick = account.decoder.feed(row, i, 'immutable-source', account.safe_gap)
                if tick:
                    account.advance(tick, clock)
        results.append(account.snapshot())
    assert results[0] == results[1]


def test_late_gap_requires_proof_and_does_not_claim_coverage(held_parent):
    db, settings, parent, _, root = held_parent
    account = SmileAccount(db, root, settings, parent, root/'follow')
    with pytest.raises(ValueError, match='unobserved trades'):
        account.decoder.feed(event(total=5), 1, 'missing', account.safe_gap)
    below = event(price=99, total=5)
    tick = account.decoder.feed(below, 1, 'observed', account.safe_gap)
    assert tick and account.decoder.gaps[0]['full_market_coverage'] is False
    assert account.decoder.checks == 0


def test_reconnect_overlap_never_becomes_new_liquidity():
    decoder = QuoteDecoder('0050')
    decoder.feed(event(), 0, 'a', lambda *a: False)
    assert decoder.feed(event(connection='two'), 1, 'b', lambda *a: False) is None
    assert decoder.duplicates == 1 and decoder.ticks == 1
    assert decoder.feed(event(second=1,total=2,connection='two'), 2, 'c', lambda *a: False)
    assert decoder.checks == 2


def test_gap_after_pending_sale_is_blocked(held_parent):
    db, settings, parent, _, root = held_parent
    account = SmileAccount(db, root, settings, parent, root/'follow')
    clock = [dt.datetime(2026,11,6,9,tzinfo=TW)]
    with paper_clock(clock):
        account.change_day('2026-11-06', clock, 100)
        account.advance(account.decoder.feed(event(), 0, 'signal', account.safe_gap), clock)
        with pytest.raises(ValueError, match='unobserved trades'):
            account.decoder.feed(event(second=2,total=3), 1, 'gap', account.safe_gap)
    assert account.snapshot()['shares'] == 1000


def test_persisted_csv_tampering_is_not_accepted(held_parent):
    db, settings, parent, _, root = held_parent
    account = SmileAccount(db, root, settings, parent, root/'follow')
    path = next((root/'follow/data/simulation/data').glob('trades_*.csv'))
    path.write_text(path.read_text('utf-8-sig').replace('99.4', '99.3'), encoding='utf-8-sig')
    with pytest.raises(ValueError, match='persisted fill content'):
        account.snapshot()


def test_fake_auction_and_odd_events_cannot_fill():
    decoder = QuoteDecoder('0050')
    for flag in ('simtrade', 'intraday_odd', 'suspend'):
        assert decoder.feed(event(**{flag:True}), 0, 'fake', lambda *a: True) is None
    assert decoder.ticks == 0 and decoder.ignored == 3
    with pytest.raises(ValueError, match='unknown market event'):
        decoder.feed(event(simtrade=None), 0, 'unknown', lambda *a: True)


def test_parent_strategy_or_cost_mutation_is_rejected(held_parent):
    db, settings, parent, _, root = held_parent
    with pytest.raises(ValueError, match='parent'):
        SmileAccount(db, root, settings.model_copy(update={'broker_fee_discount':1}), parent, root/'changed')


def test_cross_session_order_expiration_preserves_cost_until_later_sale(held_parent):
    from dataclasses import replace
    db, settings, parent, ticks, root = held_parent
    account = SmileAccount(db, root, settings, parent, root/'follow')
    clock = [dt.datetime(2026,11,6,9,tzinfo=TW)]
    with paper_clock(clock):
        account.change_day('2026-11-06', clock, 100)
        cost = account.strategy.risk.fund_used
        account.advance(ticks[0], clock)
        old_order = account.pending()[0]
        account.change_day('2026-11-09', clock, 100.5)
        assert old_order.status.status.value == 'Cancelled'
        assert account.strategy.risk.fund_used == cost
        assert not account.pending() and account.snapshot()['shares'] == 1000
        for tick in ticks[:2]:
            account.advance(replace(tick, ts=tick.ts.replace('2026-11-06','2026-11-09')), clock)
    final = account.snapshot()
    assert final['lifecycle_complete'] and Decimal(final['cost_basis']) == 0
    assert len(final['fills']) == 2


def test_session_expiry_cancels_order_without_fabricating_sale(held_parent):
    db, settings, parent, ticks, root = held_parent
    account = SmileAccount(db, root, settings, parent, root/'follow')
    clock = [dt.datetime(2026,11,6,9,tzinfo=TW)]
    with paper_clock(clock):
        account.change_day('2026-11-06', clock, 100)
        account.advance(ticks[0], clock)
        assert account.pending()
        before = account.snapshot()
        account.end_day('2026-11-06', clock)
        after = account.snapshot()
    assert not after['pending_orders'] and not after['lifecycle_complete']
    for key in ('cash','cost_basis','realized_pnl','shares','fills'):
        assert after[key] == before[key]
    assert '2026-11-06' in after['expired_sessions']


def test_crash_recovery_preserves_partial_bytes_and_replays_complete_rows(tmp_path):
    (tmp_path/'observations').mkdir()
    complete = b'{"kind":"example"}\n'
    partial = b'{"kind":"interrupted'
    (tmp_path/'inbox.jsonl').write_bytes(complete+partial)
    (tmp_path/'observations/00000000.jsonl').write_bytes(complete)
    (tmp_path/'observations/00000001.jsonl').write_bytes(partial)
    recover_incomplete_writes(tmp_path)
    assert (tmp_path/'inbox.jsonl').read_bytes() == complete
    assert len(list((tmp_path/'observations').glob('*.jsonl'))) == 1
    assert [p.read_bytes() for p in (tmp_path/'recovery').glob('*.bin')] == [partial,partial]
