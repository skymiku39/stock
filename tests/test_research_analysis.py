import copy

import pytest

from bot.research_analysis import analyze, input_fingerprint, validate_analysis


def fact(value, start, end, *, unit='USD', concept='Revenues', evidence='fact'):
    return {'format': 'sec_fact', 'evidence_id': evidence, 'source_document_id': 'source',
            'row': {'taxonomy': 'us-gaap', 'concept': concept, 'unit': unit,
                    'start': start, 'end': end, 'filed': '2026-08-01', 'val': value}}


def test_growth_requires_matching_units_and_period_length():
    item = {'ticker': 'AAA', 'sources': [
        fact(150, '2026-04-01', '2026-06-30', evidence='new'),
        fact(100, '2025-04-01', '2025-06-30', evidence='prior'),
        fact(500, '2025-01-01', '2025-06-30', evidence='ytd'),
        fact(600, '2025-04-01', '2025-06-30', unit='TWD', evidence='fx'),
    ]}
    analysis = analyze(item)
    assert len(analysis['comparisons']) == 1
    comparison = analysis['comparisons'][0]
    assert comparison['change_pct'] == 50
    assert comparison['comparison'] == 'year_over_year'
    assert comparison['evidence_ids'] == ['new', 'prior']
    assert not analysis['research_ready']
    assert 'valuation' in analysis['missing']


def test_zero_base_remains_undefined_and_changed_calculation_is_rejected():
    item = {'sources': [fact(10, '2026-04-01', '2026-06-30'),
                        fact(0, '2025-04-01', '2025-06-30')]}
    analysis = analyze(item)
    assert analysis['comparisons'][0]['change_pct'] is None
    broken = copy.deepcopy(analysis)
    broken['comparisons'][0]['change_pct'] = 0
    with pytest.raises(ValueError, match='calculations'):
        validate_analysis({'analysis': broken}, item)


def test_single_excerpt_and_one_fund_price_are_not_complete_research():
    excerpt = {'sources': [{'format': 'official_excerpt', 'source_document_id': 's',
                           'row': {'financial_excerpt': 'Profit 100'}}]}
    assert analyze(excerpt)['status'] == 'limited'
    fund = {'sources': [
        {'format': 'twse_fund_profile', 'source_document_id': 's', 'row': {'基金代號': '0050'}},
        {'format': 'twse_fund_price', 'source_document_id': 's', 'evidence_id': 'p',
         'row': {'日期': '115/10/01', '收盤價': '100'}},
    ]}
    assert 'price_comparison' in analyze(fund)['missing']
    fund['sources'].append({'format': 'twse_fund_price', 'source_document_id': 's', 'evidence_id': 'p2',
                            'row': {'日期': '115/10/02', '收盤價': '105'}})
    analysis = analyze(fund)
    assert analysis['comparisons'][0]['change_pct'] == 5
    assert not analysis['research_ready']  # price change is not holdings/NAV research


def test_vendor_aliases_with_identical_financials_count_once():
    sources = [fact(100, '2026-04-01', '2026-06-30')]
    assert input_fingerprint({'ticker': 'AAA', 'sources': sources}) == input_fingerprint(
        {'ticker': 'AAA US', 'sources': sources})
