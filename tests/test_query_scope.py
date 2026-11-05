from unittest.mock import patch
from types import SimpleNamespace

import pytest

from bot.ask import AskEngine
from bot.query_plan import plan_query
from bot.query_scope import requested_months, requested_quarters, requested_dates, matches_query_scope
from bot.resource_adapters import MonthlyRevenueAdapter
from bot.stock_db import MonthlyRevenue, QuarterlyReport, PriceBar, StockDB


def test_model_busy_waits_for_actual_answer_without_bypassing_client():
    from bot.ask import _QueuedAskClient
    from unittest.mock import Mock
    client = SimpleNamespace(generate_raw=Mock(side_effect=[
        (None, {'error_kind': 'busy', 'deferred': True}), ('real answer', {'tokens_out': 10})]))
    with patch('bot.ask.time.sleep'):
        assert _QueuedAskClient(client, 1).generate_raw('query') == ('real answer', {'tokens_out': 10})
    assert client.generate_raw.call_count == 2


def test_model_quota_does_not_retry_as_busy():
    from bot.ask import _QueuedAskClient
    from unittest.mock import Mock
    client = SimpleNamespace(generate_raw=Mock(return_value=(None, {'error_kind': 'quota', 'deferred': True})))
    assert _QueuedAskClient(client, 1).generate_raw('query')[0] is None
    assert client.generate_raw.call_count == 1


@pytest.mark.parametrize('query,expected', [('2330 2025年9月營收', ['2025-09']),
    ('2330 114/9 營收', ['2025-09']), ('2025-09 與 2026-09 的2330營收', ['2025-09', '2026-09'])])
def test_explicit_revenue_periods(query, expected):
    assert requested_months(query) == expected


@pytest.mark.parametrize('query,expected', [('2330 2025Q2 EPS', ['2025Q2']),
    ('2330 2025年第二季EPS', ['2025Q2']), ('2330 2025年全年EPS', ['2025Q4'])])
def test_quarterly_periods(query, expected):
    assert requested_quarters(query) == expected


def test_year_before_symbol_is_not_a_ticker():
    assert plan_query('2025年9月2330的營收').ticker == '2330'
    assert plan_query('2025-09 2330營收').ticker == '2330'


def test_revenue_adapter_returns_requested_month_and_never_latest_substitute(tmp_path):
    db = StockDB.open(root=tmp_path)
    for month, value in [('2025-09', 330981542), ('2026-09', 511857399)]:
        db.upsert_monthly_revenue(MonthlyRevenue(symbol='2330', year_month=month, revenue=value, quality_status='ok'))
    adapter = MonthlyRevenueAdapter(root=tmp_path)
    result = adapter.collect(plan_query('2330 2025年9月營收'))
    assert len(result.evidence) == 1
    assert result.evidence[0]['as_of'] == '2025-09'
    assert '330981542' in result.evidence[0]['excerpt']
    missing = adapter.collect(plan_query('2330 2024年9月營收'))
    assert not missing.evidence
    assert missing.status == 'partial'
    db.close()


def test_unrelated_fundamental_data_cannot_answer_missing_revenue_period():
    query = '2330 2024年9月營收'
    assert not matches_query_scope({'kind': 'monthly_revenue', 'as_of': '2026-09'}, query)
    assert not matches_query_scope({'kind': 'fundamental', 'title': '2330 季報 2024Q3'}, query)
    assert not matches_query_scope({'kind': 'valuation_daily'}, query)


def test_one_month_cannot_satisfy_two_month_comparison():
    from bot.query_scope import missing_requested_periods
    assert missing_requested_periods([{'kind': 'monthly_revenue', 'as_of': '2025-09'}],
        '2330 2025年9月與2026年9月營收') == ['fundamental:2026-09']


def test_quarterly_answer_evidence_preserves_year_to_date_basis_and_unit(tmp_path):
    db = StockDB.open(root=tmp_path)
    db.upsert_quarterly_report(QuarterlyReport(symbol='2330', period='2025Q2', eps=30.09,
        revenue=1773011437, note='公開資料自動 (累計，缺前季)', quality_status='ok'))
    db.upsert_quarterly_report(QuarterlyReport(symbol='2330', period='2026Q2', eps=49.33,
        note='公開資料自動 (累計，缺前季)', quality_status='ok'))
    engine = AskEngine(root=tmp_path, db=db, client=None)
    with patch('bot.ask_adapters.collect_snapshot_resource_evidence', return_value=[]):
        pack = engine._collect_evidence(plan_query('2330 2025Q2 EPS'))
    rows = [item for item in pack.items if item['kind'] == 'fundamental']
    assert len(rows) == 1
    assert '2025Q2 年度累計 EPS 30.09 元' in rows[0]['excerpt']
    assert '1773011437 新台幣千元' in rows[0]['excerpt']
    db.close()


def test_date_specific_price_uses_that_session(tmp_path):
    db = StockDB.open(root=tmp_path)
    for date, price in [('2026-11-04', 2260), ('2026-11-05', 2240)]:
        db.bulk_upsert_price_bars([PriceBar(symbol='2330', date=date, open=price, high=price,
                                           low=price, close=price, quality_status='ok')])
    engine = AskEngine(root=tmp_path, db=db, client=None)
    with patch('bot.ask_adapters.collect_snapshot_resource_evidence', return_value=[]):
        pack = engine._collect_evidence(plan_query('2330 2026年11月4日收盤價'))
    assert requested_dates('2330 2026年11月4日收盤價') == ['2026-11-04']
    assert len(pack.items) == 1
    assert '收 2260' in pack.items[0]['excerpt']
    db.close()
