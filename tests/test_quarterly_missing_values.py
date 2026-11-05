import pytest

from bot.fundamentals_fetcher import _parse_quarterly_raw, _decumulate_quarterlies
from bot.quarterly_research import _payload, _claims
from bot.stock_db import QuarterlyReport, StockDB
from bot.ask import _financial_value


def test_bank_missing_industry_fields_are_not_zero_and_eps_is_preserved(tmp_path):
    raw = {'公司代號': '2881', '年度': '115', '季別': '2', '淨收益': '172,468,262',
           '本期稅後淨利（淨損）': '97,780,594', '基本每股盈餘（元）': '6.67'}
    item = _decumulate_quarterlies('2881', [_parse_quarterly_raw(raw, '2881')])[0]
    assert item.eps == 6.67 and item.net_income == 97780594
    assert item.revenue is item.gross_margin is item.operating_margin is item.net_margin is None
    db = StockDB.open(root=tmp_path)
    report = QuarterlyReport('2881', '2026Q2', eps=item.eps, revenue=item.revenue,
                             gross_margin=item.gross_margin, note=item.note)
    db.upsert_quarterly_report(report)
    stored = db.get_quarterly_report('2881')[0]
    assert stored.revenue is None and stored.eps == 6.67
    payload = _payload(stored, 'source-doc:bank')
    assert payload['revenue'] is None
    assert not any('營收為' in c['text'] or '毛利率為' in c['text'] for c in _claims(payload))
    assert _financial_value('毛利率', None, '%') == '毛利率未提供／不適用'
    db.close()


def test_reported_zero_margin_is_still_a_real_zero():
    raw = {'公司代號': '2330', '年度': '115', '季別': '1', '營業收入': '100',
           '營業毛利（毛損）淨額': '0', '基本每股盈餘（元）': '0'}
    item = _decumulate_quarterlies('2330', [_parse_quarterly_raw(raw, '2330')])[0]
    assert item.gross_margin == 0 and item.eps == 0
    assert item.operating_margin is None


def test_missing_preceding_value_cannot_be_subtracted_as_zero():
    first = _parse_quarterly_raw({'公司代號':'2330','年度':'115','季別':'1','基本每股盈餘（元）':'1'}, '2330')
    second = _parse_quarterly_raw({'公司代號':'2330','年度':'115','季別':'2','基本每股盈餘（元）':'3','營業收入':'100'}, '2330')
    result = _decumulate_quarterlies('2330',[first,second])[-1]
    assert result.revenue is None and result.eps == 2


def test_annual_eps_cannot_treat_unreported_quarter_as_zero():
    from bot.quarterly import cumulative_eps
    from bot.fundamentals_fetcher import QuarterlyFinancials
    quarters={q:QuarterlyFinancials('2330',year=2026,quarter=q,eps=1 if q<4 else None, note='單季') for q in range(1,5)}
    assert cumulative_eps(quarters,4) is None


def test_first_reported_month_has_no_month_over_month_percentage(tmp_path):
    from bot.fundamentals_fetcher import _parse_one_revenue
    from bot.monthly_revenue_research import build_canonical_payload, _extractive_claims
    from bot.stock_db import MonthlyRevenue
    raw={'公司代號':'7856','資料年月':'114/8','營業收入-當月營收':'100','營業收入-上月比較增減(%)':'-'}
    value=_parse_one_revenue(raw)
    assert value.mom is None
    payload=build_canonical_payload(symbol='7856',year_month='2025-08',revenue=value.revenue,
        yoy_pct=value.yoy,mom_pct=value.mom,source_document_id='original')
    assert payload['mom_pct'] is None
    assert not any('月增率' in claim['text'] for claim in _extractive_claims(payload))
    db=StockDB.open(root=tmp_path)
    db.upsert_monthly_revenue(MonthlyRevenue('7856','2025-08',revenue=100,mom_pct=None))
    assert db.get_monthly_revenue('7856')[0].mom_pct is None
    db.close()
