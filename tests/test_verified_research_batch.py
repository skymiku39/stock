import hashlib
import json

import pytest

from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.verified_research_batch import backlog, collect, matching_rows, revalidate, validate_report


def test_backlog_is_exact_and_deduplicated():
    assert backlog({'focus_tickers': ['1', '2', '2', '3'],
                    'auto_research_batch': {'selected_tickers': ['1']}}) == ['2', '3']


def test_exact_company_identity_not_name_substring():
    raw = [{'公司代號': '2330', '備註': '2382'}, {'Code': '2382', 'PEratio': ''}]
    assert matching_rows(raw, '2382') == [raw[1]]
    assert matching_rows(raw, '238') == []


def test_raw_replay_rejects_tampering(tmp_path):
    raw = [{'公司代號': '2382', '資料年月': '11509', '營收': '100'}]
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document('monthly_revenue_上市', json.dumps(raw),
            source_url='https://openapi.twse.com.tw/v1/opendata/t187ap05_L', suffix='json')
        path = resolve_source_document_path(tmp_path, source)
    inputs, excluded = collect(tmp_path, ['2382', 'NVDA US'])
    assert not excluded and not inputs['NVDA US']
    item = {'ticker': '2382', 'sources': inputs['2382']}
    revalidate(tmp_path, [item])
    path.write_text('[]', encoding='utf-8')
    with pytest.raises(ValueError, match='changed'):
        revalidate(tmp_path, [item])


def test_output_cannot_substitute_other_company_or_numeric_value():
    item = {'ticker': '2382', 'sources': [{'evidence_id': 'a', 'row': {'營收': '100'}}]}
    report = {'ticker': '2382', 'summary': 'limited', 'limitations': ['historical'],
              'observations': [{'evidence_id': 'a', 'field': '營收', 'value': '100'}]}
    validate_report(report, item)
    report['observations'][0]['value'] = 100
    with pytest.raises(ValueError):
        validate_report(report, item)
    report['observations'][0].update(evidence_id='other', value='100')
    with pytest.raises(ValueError):
        validate_report(report, item)


def test_official_csv_replays_exact_strings_and_rejects_changed_value(tmp_path):
    raw = '\ufeff"出表日期","公司代號","現金股利"\n"1151026","1240","3.00000000"\n'
    with SourceRegistry(tmp_path) as registry:
        registry.capture_document('dividend_tpex', raw.encode('utf-8'),
            source_url='https://mopsfin.twse.com.tw/opendata/t187ap45_O.csv',
            suffix='csv', mime_type='text/csv')
    inputs, excluded = collect(tmp_path, ['1240', '124'])
    assert not excluded and not inputs['124']
    assert inputs['1240'][0]['row'] == {'出表日期': '1151026', '公司代號': '1240', '現金股利': '3.00000000'}
    item = {'ticker': '1240', 'sources': inputs['1240']}
    revalidate(tmp_path, [item])
    item['sources'][0]['row']['現金股利'] = '3'
    with pytest.raises(ValueError, match='changed'):
        revalidate(tmp_path, [item])


def test_bare_us_symbols_use_foreign_collector_and_keep_specific_gap(tmp_path, monkeypatch):
    from bot import verified_research_batch as batch, foreign_research_sources as foreign
    domestic_calls, foreign_calls = [], []
    def domestic(root, tickers):
        domestic_calls.append(tickers)
        return {t: [] for t in tickers}, []
    def external(root, tickers, **kwargs):
        foreign_calls.append(tickers)
        return {t: [] for t in tickers}, [{'ticker': t, 'reason': 'fixture identity missing'} for t in tickers]
    monkeypatch.setattr(batch, 'collect', domestic)
    monkeypatch.setattr(foreign, 'collect', external)
    out = tmp_path / 'out'
    assert batch.main(['--root', str(tmp_path), '--tickers', '2330', 'NVDA', 'NBIS UW',
                       '--output-dir', str(out)]) == 3
    assert domestic_calls == [['2330']] and foreign_calls == [['NVDA', 'NBIS UW']]
    result = json.loads((out / 'verification-summary.json').read_text())
    assert result['results']['NVDA']['reason'] == 'fixture identity missing'
