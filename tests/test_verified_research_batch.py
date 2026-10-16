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
