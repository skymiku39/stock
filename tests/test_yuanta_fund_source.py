import copy
import json
from types import SimpleNamespace

import pytest

from bot import etf_research_sources as funds
from bot.research_analysis import analyze
from bot.source_registry import SourceRegistry
from bot.verified_research_batch import revalidate
from bot.yuanta_fund_source import _Literals, parse_snapshot


def serialized_page(*, ticker='0050', nav=100, weights=None):
    fund = {'STK_CD': ticker, 'FUND_ID': '1066'}
    portfolio = {'PCF': {'markcd': ticker, 'fundid': '1066', 'trandate': '20261027',
        'nav': nav, 'totalav': 100000, 'osunit': 1000},
        'FundWeights': {'Summary': {'code': '1066'}, 'StockWeights': weights or [
            {'code': '2330', 'name': '台積電', 'weights': 60, 'qty': 10},
            {'code': '2317', 'name': '鴻海', 'weights': 30, 'qty': 5}]}}
    return ('<script>window.__NUXT__=(function(f){return {fundData:f,weightData:'
        + json.dumps(portfolio, ensure_ascii=False) + '}}('
        + json.dumps(fund, ensure_ascii=False) + '));</script>').encode()


def test_literal_parser_rejects_executable_expressions_and_nonfinite_values():
    for value in ('process.exit()', 'globalThis.secret', '1+2', 'NaN', 'Infinity', '{x:1,x:2}'):
        with pytest.raises(ValueError):
            _Literals(value).parse()
    with pytest.raises(ValueError, match='NAV'):
        parse_snapshot(serialized_page(nav='NaN'), '0050')


def test_fund_parser_requires_exact_identity_and_unique_bounded_weights():
    result = parse_snapshot(serialized_page(), '0050')
    assert result['nav']['as_of'] == '2026-10-27'
    assert result['holdings']['count'] == 2
    with pytest.raises(ValueError, match='identity'):
        parse_snapshot(serialized_page(), '0051')
    for weights in ([{'code': '2330', 'weights': 90}, {'code': '2330', 'weights': 1}],
                    [{'code': '2330', 'weights': 102}], [{'code': '2330', 'weights': 'NaN'}]):
        with pytest.raises(ValueError):
            parse_snapshot(serialized_page(weights=weights), '0050')


def test_official_portfolio_is_persisted_replayed_and_joined_to_same_day_price(tmp_path, monkeypatch):
    profile = [{'基金代號': '0050', '基金中文名稱': '元大台灣50'}]
    page = serialized_page()
    def get(url, **kwargs):
        raw = json.dumps(profile).encode() if url == funds.PROFILE_URL else page
        return SimpleNamespace(status_code=200, url=url, content=raw,
            json=lambda: json.loads(raw), raise_for_status=lambda: None)
    monkeypatch.setattr(funds.requests, 'get', get)
    with SourceRegistry(tmp_path) as registry:
        registry.capture_document('twse_stock_day', json.dumps({'stat': 'OK',
            'fields': ['日期', '收盤價'], 'data': [['115/10/26', '100'], ['115/10/27', '101']]}),
            source_ref='0050:2026-10', source_url='https://www.twse.com.tw/exchangeReport/STOCK_DAY?stockNo=0050', suffix='json')
    inputs, gaps = funds.collect(tmp_path, ['0050'])
    assert not gaps
    item = {'ticker': '0050', 'sources': inputs['0050']}
    assert len(item['sources']) == 5
    revalidate(tmp_path, [item])
    result = analyze(item)
    assert result['research_ready']
    premium = next(c for c in result['comparisons'] if c['metric'] == 'premium_to_nav')
    assert premium['premium_pct'] == 1.0
    changed = copy.deepcopy(item)
    nav = next(s for s in changed['sources'] if s['format'] == 'fund_nav')
    nav['row']['as_of'] = '2026-10-25'
    assert 'nav_and_premium' in analyze(changed)['missing']
    nav['row']['nav'] = 99
    with pytest.raises(ValueError, match='replayed'):
        revalidate(tmp_path, [changed])


def test_issuer_failure_keeps_profile_but_does_not_meet_portfolio_objectives(tmp_path, monkeypatch):
    def get(url, **kwargs):
        raw = json.dumps([{'基金代號': '0050', '基金中文名稱': '元大台灣50'}]).encode() if url == funds.PROFILE_URL else b'<html>unavailable</html>'
        return SimpleNamespace(status_code=200, url=url, content=raw,
            json=lambda: json.loads(raw), raise_for_status=lambda: None)
    monkeypatch.setattr(funds.requests, 'get', get)
    inputs, gaps = funds.collect(tmp_path, ['0050'])
    assert inputs['0050'] and gaps
    assert 'holdings' in analyze({'ticker': '0050', 'sources': inputs['0050']})['missing']
