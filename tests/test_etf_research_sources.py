import json
from types import SimpleNamespace

import pytest

from bot import etf_research_sources as funds
from bot.source_registry import SourceRegistry
from bot.verified_research_batch import revalidate


def test_fund_identity_profile_price_and_exact_replay(tmp_path, monkeypatch):
    profile = [{'基金代號': '0050', '基金中文名稱': 'Test fund', '出表日期': '1151026', '發行單位數/轉換數': '100'}]
    raw = json.dumps(profile).encode()
    monkeypatch.setattr(funds.requests, 'get', lambda url, **kw: SimpleNamespace(
        status_code=200, url=url, content=raw, json=lambda: profile, raise_for_status=lambda: None))
    with SourceRegistry(tmp_path) as registry:
        registry.capture_document('twse_stock_day', json.dumps({'stat': 'OK',
            'fields': ['日期', '收盤價'], 'data': [['115/10/22', '106.90']]}),
            source_ref='0050:2026-10', source_url='https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&stockNo=0050', suffix='json')
    inputs, gaps = funds.collect(tmp_path, ['0050', '00500'])
    assert len(inputs['0050']) == 2 and not inputs['00500']
    assert gaps == [{'ticker': '00500', 'reason': 'missing or ambiguous TWSE fund identity'}]
    item = {'ticker': '0050', 'sources': inputs['0050']}
    revalidate(tmp_path, [item])
    with SourceRegistry(tmp_path) as registry:
        with pytest.raises(ValueError, match='identity'):
            funds.validate_entry(tmp_path, registry.db, '0051', inputs['0050'][1])
    inputs['0050'][1]['row']['收盤價'] = '100'
    with pytest.raises(ValueError, match='replayed'):
        revalidate(tmp_path, [item])


def test_fund_source_failure_does_not_become_an_empty_success(tmp_path, monkeypatch):
    import requests
    def fail(*args, **kwargs):
        raise requests.ConnectionError('offline')
    monkeypatch.setattr(funds.requests, 'get', fail)
    inputs, gaps = funds.collect(tmp_path, ['0050'])
    assert inputs == {'0050': []} and 'offline' in gaps[0]['reason']


def test_fund_profile_does_not_reuse_another_symbols_price(tmp_path, monkeypatch):
    profile = [{'基金代號': '0050'}]
    with SourceRegistry(tmp_path) as registry:
        registry.capture_document('twse_fund_profile', json.dumps(profile), source_url=funds.PROFILE_URL, suffix='json')
        registry.capture_document('twse_stock_day', json.dumps({'stat': 'OK',
            'fields': ['日期', '收盤價'], 'data': [['115/10/22', '100']]}),
            source_ref='0050:2026-10', source_url='https://www.twse.com.tw/exchangeReport/STOCK_DAY?stockNo=2330', suffix='json')
    inputs, gaps = funds.collect(tmp_path, ['0050'])
    assert not inputs['0050'] and gaps[0]['reason'] == 'wrong fund price identity'
