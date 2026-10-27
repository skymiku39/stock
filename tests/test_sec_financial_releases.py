import json
from types import SimpleNamespace

import pytest

from bot.sec_financial_releases import collect_release, filing_url, release_row, validate_entry
from bot.source_registry import SourceRegistry

HTML = b'''<p>Issuer Inc.</p><p>The preliminary results may differ from final results.
1. Basis: Consolidated Classification (unit: in millions of Won or %)
Current Period (Second quarter of 2026) Previous Period (First quarter of 2026)
Revenue 100 80 Operating Profit 20 10 2. Source of Data Investor Relations
Date: July 29, 2026</p>'''


def test_release_keeps_preliminary_warning_units_and_comparison_columns():
    row = release_row(HTML, 'Issuer Inc.')
    assert 'may differ from final results' in row['financial_excerpt']
    assert 'millions of Won' in row['financial_excerpt']
    assert 'Revenue 100 80' in row['financial_excerpt']
    assert row['period_excerpt'] == 'Current Period (Second quarter of 2026)'
    with pytest.raises(ValueError, match='issuer'):
        release_row(HTML, 'Other issuer')
    with pytest.raises(ValueError, match='table'):
        release_row(b'<p>Issuer Inc. share repurchase notice</p>', 'Issuer Inc.')
    with pytest.raises(ValueError, match='path'):
        filing_url(1, '0000000001-26-000001', '../report.htm')


def test_discovery_uses_actual_filing_links_and_ignores_future_publications():
    submissions = {'name': 'Issuer Inc.', 'filings': {'recent': {
        'form': ['6-K', '6-K'], 'filingDate': ['2050-01-01', '2026-07-29'],
        'accessionNumber': ['0000000001-50-000001', '0000000001-26-000001'],
        'primaryDocument': ['future.htm', 'actual.htm']}}}
    calls = []
    def fetch(url, kind):
        calls.append(url)
        return SimpleNamespace(document_id='doc', content_hash='sha', url=url), HTML
    entry = collect_release(fetch, submissions, {'cik': 1}, '2026-10-27')
    assert len(calls) == 1 and calls[0].endswith('/actual.htm')
    assert entry['filing_date'] == '2026-07-29'


def test_release_replay_requires_sec_identity_link_exact_text_and_cutoff(tmp_path):
    directory = {'fields': ['cik', 'name', 'ticker', 'exchange'],
                 'data': [[1, 'Issuer Inc.', 'AAA', 'Nasdaq']]}
    submissions = {'cik': '1', 'name': 'Issuer Inc.', 'tickers': ['AAA'], 'exchanges': ['Nasdaq'],
                   'filings': {'recent': {'form': ['6-K'], 'filingDate': ['2026-07-29'],
                       'accessionNumber': ['0000000001-26-000001'], 'primaryDocument': ['actual.htm']}}}
    with SourceRegistry(tmp_path) as registry:
        directory_doc = registry.capture_document('sec_ticker_directory', json.dumps(directory),
            source_url='https://www.sec.gov/files/company_tickers_exchange.json', suffix='json')
        submissions_doc = registry.capture_document('sec_submissions', json.dumps(submissions),
            source_url='https://data.sec.gov/submissions/CIK0000000001.json', suffix='json')
        def fetch(url, kind):
            return registry.capture_document(kind, HTML, source_url=url, suffix='html'), HTML
        identity = {'cik': 1, 'name': 'Issuer Inc.', 'ticker': 'AAA', 'exchange': 'Nasdaq'}
        entry = collect_release(fetch, submissions, identity, '2026-10-27')
        entry.update(identity=identity, cutoff='2026-10-27', directory_source_id=directory_doc.document_id,
            directory_sha256=directory_doc.content_hash, submissions_source_id=submissions_doc.document_id,
            submissions_sha256=submissions_doc.content_hash)
        validate_entry(tmp_path, registry.db, 'AAA US', entry)
        with pytest.raises(ValueError, match='identity'):
            validate_entry(tmp_path, registry.db, 'BBB US', entry)
        entry['cutoff'] = '2026-01-01'
        with pytest.raises(ValueError, match='date'):
            validate_entry(tmp_path, registry.db, 'AAA US', entry)
        entry['cutoff'] = '2026-10-27'
        entry['row']['financial_excerpt'] += ' invented'
        with pytest.raises(ValueError, match='replayed'):
            validate_entry(tmp_path, registry.db, 'AAA US', entry)
