"""Continuations must preserve held cost and require later market liquidity."""
import json
from types import SimpleNamespace

import pytest

from bot.config import Settings
from bot.stock_db import IntradayBar
from bot.tick_replay import replay_ticks
from bot.tick_continuation import continue_smile_replay


@pytest.fixture
def held_parent(tmp_path, monkeypatch):
    symbol = '0050'
    def tick(day, time, price, bid=None):
        return IntradayBar(symbol=symbol, ts=f'{day} {time}.000000000', interval='tick',
            close=price, volume=1, bid_price=bid or price, ask_price=price,
            bid_volume=1, ask_volume=1, source_record_id='test-fixture', quality_status='ok')
    initial = [tick('2026-11-05', '09:00:00', 100), tick('2026-11-05', '09:01:00', 99.4),
               tick('2026-11-05', '09:01:01', 99.4)]
    next_day = [tick('2026-11-06', '09:00:00', 100.5), tick('2026-11-06', '09:00:01', 100.5),
                tick('2026-11-06', '09:00:02', 99)]
    db = SimpleNamespace(get_intraday_bars=lambda *a, **kw: initial if kw['start']=='2026-11-05' else next_day,
        get_price_history=lambda *a, **kw: [SimpleNamespace(close=100, quality_status='ok', source_record_id='test-daily')])
    check = lambda *a, **kw: {'mismatches': 0}
    monkeypatch.setattr('bot.intraday_reconciliation.reconcile_intraday_session', check)
    monkeypatch.setattr('bot.tick_continuation.reconcile_intraday_session', check)
    settings = Settings(_env_file=None, strategy_type='smile_curve', max_fund=500000, daily_fund_budget=0,
        max_lot_per_symbol=1, smile_base_lot=1, smile_buy_tiers='0.5:1,1:1,2:1',
        min_pct_chg_on_entry=-10, max_pct_chg_on_entry=10, broker_min_fee=1, broker_fee_discount=.28)
    parent = tmp_path/'parent'
    original = replay_ticks(db, settings, symbol, '2026-11-05', parent, root=tmp_path)
    assert original['positions'] == {symbol:1000}
    return db, settings, parent/'result.json', next_day, tmp_path


def test_next_day_sell_requires_later_tick_and_preserves_full_ledger(held_parent):
    db, settings, parent, ticks, root = held_parent
    result = continue_smile_replay(db, settings, parent, ['2026-11-06'], root/'continued', root=root)
    assert result['parent_state_reconstructed'] and result['lifecycle_complete']
    assert result['closed_at'] == ticks[1].ts
    assert result['fills'][-1]['submitted_at'] == ticks[0].ts
    assert result['fills'][-1]['ts'] > result['fills'][-1]['submitted_at']
    assert not any(result['positions'].values()) and not result['pending_orders'] and result['cost_basis'] == 0
    assert result['final_cash'] == pytest.approx(result['initial_cash']+result['realized_pnl'])
    assert len(result['fills']) == 2  # The later dip must not start a new cycle.


def test_signal_without_later_liquidity_stays_open(held_parent):
    db, settings, parent, ticks, root = held_parent
    ticks[:] = ticks[:1]
    result = continue_smile_replay(db, settings, parent, ['2026-11-06'], root/'open', root=root)
    assert not result['lifecycle_complete'] and result['closed_at'] is None
    assert result['positions'] == {'0050':1000} and result['pending_orders']
    assert len(result['fills']) == 1


def test_changed_parent_cost_is_rejected_before_later_data(held_parent):
    db, settings, parent, ticks, root = held_parent
    original = json.loads(parent.read_text())
    original['final_cash'] += 1
    parent.write_text(json.dumps(original))
    with pytest.raises(ValueError, match='parent ledger reconstruction mismatch'):
        continue_smile_replay(db, settings, parent, ['2026-11-06'], root/'changed', root=root)


def test_cost_assumptions_cannot_change_for_later_fills(held_parent):
    db, settings, parent, ticks, root = held_parent
    settings = settings.model_copy(update={'broker_fee_discount':1})
    with pytest.raises(ValueError, match='cost model differs'):
        continue_smile_replay(db, settings, parent, ['2026-11-06'], root/'costs', root=root)


def test_late_stream_cannot_skip_an_earlier_sell_signal(held_parent, monkeypatch):
    db, settings, parent, ticks, root = held_parent
    monkeypatch.setattr('bot.observed_stream.read_observed_stream', lambda *a: (ticks, {'first_day_high':101}))
    with pytest.raises(ValueError, match='unobserved opening interval'):
        continue_smile_replay(db, settings, parent, ['2026-11-06'], root/'gap', root=root, stream_path='unused')


def test_late_stream_requires_full_position_cap(held_parent, monkeypatch):
    db, settings, parent, ticks, root = held_parent
    monkeypatch.setattr('bot.observed_stream.read_observed_stream', lambda *a: (ticks, {'first_day_high':99.9}))
    # Earlier buying could differ if more lots were allowed.
    settings = settings.model_copy(update={'max_lot_per_symbol':2})
    with pytest.raises(ValueError, match='unobserved opening interval'):
        continue_smile_replay(db, settings, parent, ['2026-11-06'], root/'cap', root=root, stream_path='unused')


def test_unfilled_day_order_expires_before_next_session_and_cost_survives(held_parent):
    from dataclasses import replace
    db, settings, parent, ticks, root = held_parent
    original_read = db.get_intraday_bars
    monday = [replace(t, ts=t.ts.replace('2026-11-06','2026-11-09')) for t in ticks[:2]]
    ticks[:] = ticks[:1]
    db.get_intraday_bars = lambda *a, **kw: monday if kw['start']=='2026-11-09' else original_read(*a, **kw)
    result = continue_smile_replay(db, settings, parent, ['2026-11-06','2026-11-09'], root/'weekend', root=root)
    assert result['lifecycle_complete']
    assert [o['status'] for o in result['orders']] == ['Filled','Cancelled','Filled']
    assert result['fills'][-1]['submitted_at'].startswith('2026-11-09')
    assert result['checkpoints'][0]['cost_basis'] == result['checkpoints'][1]['cost_basis']
    assert result['daily_pnl']['2026-11-06'] == 0
