import copy
import json

import pytest

from bot.observed_stream import read_observed_stream
from bot.stock_db import StockDB


def event(kind, when, **data):
    return dict(kind=kind, received_at='2026-11-06T09:01:05+08:00', payload=dict(code='0050',
        datetime='2026-11-06 '+when, simtrade=False, intraday_odd=False, suspend=False, **data))


@pytest.fixture
def stream(tmp_path):
    rows = [event('bidask', '09:01:00', bid_price=['99.9'], ask_price=['100'], bid_volume=[1], ask_volume=[2]),
            event('tick', '09:01:00.100000', close='100', volume=1, amount='100000',
                  total_volume=100, total_amount='10000000', high='100', low='99', tick_type=1),
            event('bidask', '09:01:02', bid_price=['150'], ask_price=['151'], bid_volume=[2], ask_volume=[2]),
            event('tick', '09:01:00.200000', close='100', volume=2, amount='200000',
                  total_volume=102, total_amount='10200000', high='100', low='99', tick_type=1)]
    db = StockDB.open(root=tmp_path)
    yield tmp_path, db, rows
    db.close()


def read(stream, rows=None):
    root, db, original = stream
    p = root/'callbacks.jsonl'
    p.write_text('\n'.join(json.dumps(row) for row in (rows or original)), encoding='utf-8')
    return read_observed_stream(db, root, p, '0050', '2026-11-06')


def test_volume_amount_reconciliation_and_no_future_quote_join(stream):
    ticks, receipt = read(stream)
    assert len(ticks) == 2 and ticks[-1].bid_price == 99.9
    assert receipt['cumulative_volume_amount_checks'] == 2
    assert receipt['session_complete'] is False and receipt['mismatches'] == 0
    assert ticks[-1].quality_status == 'partial'
    assert stream[1].get_source_document(ticks[-1].source_record_id).quality_status == 'partial'


@pytest.mark.parametrize('field,value,match', [
    ('total_volume', 103, 'missing or duplicate'), ('total_amount', '10300000', 'missing or duplicate'),
    ('amount', '100', 'price/volume/amount'), ('simtrade', True, 'actual regular-lot'),
    ('intraday_odd', True, 'actual regular-lot'), ('datetime', '2026-11-07 09:01:01', 'date/session/future'),
])
def test_rejects_nonactual_or_incomplete_data(stream, field, value, match):
    rows = copy.deepcopy(stream[2]);rows[-1]['payload'][field] = value
    with pytest.raises(ValueError, match=match):
        read(stream, rows)


def test_stale_quotes_cannot_supply_fill_liquidity(stream):
    rows = copy.deepcopy(stream[2]);rows.pop(2);rows[-1]['payload']['datetime']='2026-11-06 09:01:03'
    ticks, _ = read(stream, rows)
    assert ticks[-1].bid_price is None and ticks[-1].ask_volume is None
