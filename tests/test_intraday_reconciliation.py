import datetime as dt
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from bot.intraday_history import fetch_and_store_intraday
from bot.intraday_reconciliation import reconcile_intraday_session, reconciled_day_available, VOLUME_REPORTS
from bot.source_registry import SourceRegistry
from bot.stock_db import PriceBar, StockDB


@pytest.fixture
def archived_session(tmp_path):
    db = StockDB.open(root=tmp_path)
    day = '2026-11-05'
    ticks = dict(ts=[pd.Timestamp(day+' 09:00:00').value, pd.Timestamp(day+' 13:30:00').value],
                 close=[100, 101], volume=[1, 2], bid_price=[99, 100], bid_volume=[2, 3],
                 ask_price=[100, 101], ask_volume=[3, 4], tick_type=[1, 1])
    minutes = dict(ts=[pd.Timestamp(day+' 09:01:00').value, pd.Timestamp(day+' 13:30:00').value],
                   Open=[100, 101], High=[100, 101], Low=[100, 101], Close=[100, 101],
                   Volume=[1, 2], Amount=[100000, 202000])
    broker = SimpleNamespace(fetch_kbars=lambda *a, **kw: minutes, fetch_ticks=lambda *a, **kw: ticks)
    for interval in ('1m', 'tick'):
        fetch_and_store_intraday(broker, '2330', dt.date.fromisoformat(day), dt.date.fromisoformat(day),
                                interval=interval, root=tmp_path, db=db, save_csv=False, request_delay_sec=0)
    source = SourceRegistry(tmp_path, db=db).register_document('twse_stock_day', json.dumps(dict(stat='OK',
        fields=['日期', '開盤價', '最高價', '最低價', '收盤價', '成交股數'],
        data=[['115/11/05', '100', '101', '100', '101', '3000']])), suffix='json')
    db.bulk_upsert_price_bars([PriceBar(symbol='2330', date=day, open=100, high=101, low=100, close=101,
                                      volume=3, source_record_id=source.document_id, quality_status='ok')])
    for name, (_, column, _) in VOLUME_REPORTS.items():
        SourceRegistry(tmp_path, db=db).register_document('twse_volume_'+name, json.dumps(dict(
            stat='OK', date='20261105', fields=['證券代號', column], data=[])), suffix='json', as_of=day,
            metadata={'market_scope':'all'})
    yield db, tmp_path, day
    db.close()


def test_reconcile_closing_auction_and_detect_later_quote_corruption(archived_session):
    db, root, day = archived_session
    result = reconcile_intraday_session(db, root, '2330', day)
    assert result['minutes'] == 2 and result['mismatches'] == 0
    assert reconciled_day_available(db, root, '2330', day)
    with db.transaction() as conn:
        conn.execute("UPDATE intraday_bars SET ask_price=999 WHERE interval='tick'")
    with pytest.raises(ValueError, match='raw/canonical mismatch'):
        reconcile_intraday_session(db, root, '2330', day)


def test_missing_bar_is_not_a_complete_session(archived_session):
    db, root, day = archived_session
    with db.transaction() as conn:
        conn.execute("DELETE FROM intraday_bars WHERE interval='1m' AND ts LIKE '%13:30:00'")
    assert not reconciled_day_available(db, root, '2330', day)
    with pytest.raises(ValueError, match='row count mismatch'):
        reconcile_intraday_session(db, root, '2330', day)


def test_canonical_daily_fact_must_match_official_raw(archived_session):
    db, root, day = archived_session
    with db.transaction() as conn:
        conn.execute('UPDATE price_history SET volume=4')
    with pytest.raises(ValueError, match='official daily raw/canonical volume'):
        reconcile_intraday_session(db, root, '2330', day)


def test_matching_ohlc_cannot_hide_missing_session_volume(archived_session):
    db, root, day = archived_session
    source = SourceRegistry(root, db=db).register_document('twse_stock_day', json.dumps(dict(stat='OK',
        fields=['日期', '開盤價', '最高價', '最低價', '收盤價', '成交股數'],
        data=[['115/11/05', '100', '101', '100', '101', '4000']])), suffix='json')
    with db.transaction() as conn:
        conn.execute('UPDATE price_history SET volume=4, source_record_id=?', (source.document_id,))
    with pytest.raises(ValueError, match='differs from official daily volume'):
        reconcile_intraday_session(db, root, '2330', day)


def test_completed_prefix_excludes_unfinished_minute_and_never_promotes_day(tmp_path):
    from bot.intraday_reconciliation import reconcile_observed_prefix
    day = '2026-11-06'
    ticks = dict(ts=[pd.Timestamp(day+' 09:00:01').value, pd.Timestamp(day+' 09:00:30').value,
                     pd.Timestamp(day+' 09:01:01').value], close=[100,101,102], volume=[1,2,3],
                 bid_price=[99,100,101], bid_volume=[1,2,3], ask_price=[100,101,102],
                 ask_volume=[1,2,3], tick_type=[1,1,1])
    minutes = dict(ts=[pd.Timestamp(day+' 09:01:00').value], Open=[100],High=[101],Low=[100],
                   Close=[101],Volume=[3],Amount=[302000])
    broker = SimpleNamespace(fetch_ticks=lambda *a, **kw:ticks, fetch_kbars=lambda *a, **kw:minutes)
    db = StockDB.open(root=tmp_path)
    try:
        for interval in ('tick','1m'):
            fetch_and_store_intraday(broker,'2330',dt.date.fromisoformat(day),dt.date.fromisoformat(day),
                interval=interval,root=tmp_path,db=db,save_csv=False,request_delay_sec=0)
        checked, receipt = reconcile_observed_prefix(db,tmp_path,'2330',day,observed_at=day+'T09:01:10+08:00')
        assert len(checked) == 2 and receipt['minutes'] == 1 and not receipt['session_complete']
        assert not reconciled_day_available(db,tmp_path,'2330',day)
        with pytest.raises(ValueError,match='future timestamp'):
            reconcile_observed_prefix(db,tmp_path,'2330',day,observed_at=day+'T09:00:59+08:00')
        with pytest.raises(ValueError,match='explicit timezone'):
            reconcile_observed_prefix(db,tmp_path,'2330',day,observed_at=day+'T09:01:10')
    finally:
        db.close()
