import pytest

from bot.foreign_research_sources import issuer_identity, select_facts, fact_row


def test_identity_requires_exact_ticker_cik_and_exchange():
    directory = {'fields': ['cik', 'name', 'ticker', 'exchange'], 'data': [[1, 'Issuer', 'AAA', 'Nasdaq']]}
    submissions = {'cik': '1', 'tickers': ['AAA'], 'exchanges': ['Nasdaq']}
    assert issuer_identity(directory, submissions, 'AAA US')['cik'] == 1
    for bad in [{**submissions, 'cik': '2'}, {**submissions, 'tickers': ['AAB']},
                {**submissions, 'exchanges': ['NYSE']}]:
        with pytest.raises(ValueError):
            issuer_identity(directory, bad, 'AAA US')
    with pytest.raises(ValueError):
        issuer_identity(directory, submissions, 'AAA GY')


def test_facts_preserve_units_periods_and_reject_future_or_unlinked_values():
    records = [
        {'val': 100, 'end': '2026-06-30', 'start': '2026-01-01', 'filed': '2026-07-30', 'accn': 'a'},
        {'val': 50, 'end': '2026-06-30', 'start': '2026-04-01', 'filed': '2026-07-30', 'accn': 'a'},
        {'val': 999, 'end': '2026-09-30', 'filed': '2026-10-30', 'accn': 'b'},
        {'val': 888, 'end': '2026-09-30', 'filed': '2026-10-01', 'accn': 'unknown'},
    ]
    facts = {'cik': 1, 'entityName': 'Issuer', 'facts': {'us-gaap': {'Revenues': {'units': {'USD': records}}}}}
    submissions = {'filings': {'recent': {'accessionNumber': ['a', 'b']}}}
    pointers = select_facts(facts, submissions, '2026-10-16')
    assert pointers == [('us-gaap', 'Revenues', 'USD', 1)]
    row = fact_row(facts, *pointers[0])
    assert row['unit'] == 'USD' and row['val'] == 50 and row['start'] == '2026-04-01'


def test_conflicting_latest_facts_are_not_arbitrarily_selected():
    records = [{'val': v, 'end': '2026-06-30', 'filed': '2026-07-30', 'accn': 'a'} for v in [100, 200]]
    facts = {'facts': {'us-gaap': {'Assets': {'units': {'USD': records}}}}}
    assert select_facts(facts, {'filings': {'recent': {'accessionNumber': ['a']}}}, '2026-10-16') == []


def test_official_excerpt_requires_raw_identity_origin_and_exact_text(tmp_path):
    from bot.foreign_research_sources import validate_entry
    from bot.source_registry import SourceRegistry, resolve_source_document_path
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document('foreign_official_issuer',
            '<p>A009150 삼성전기 Q2 2026 2026.07.30 Sales KRW 100 billion</p>',
            source_url='https://www.samsungsem.com/report', suffix='html')
        entry = {'format': 'official_excerpt', 'source_document_id': source.document_id,
                 'sha256': source.content_hash, 'url': source.url, 'pdf': False,
                 'identity_source_id': source.document_id, 'identity_sha256': source.content_hash,
                 'identity_pdf': False, 'row': {'financial_excerpt': 'Sales KRW 100 billion',
                 'period_excerpt': 'Q2 2026', 'publication_excerpt': '2026.07.30'}}
        validate_entry(tmp_path, registry.db, '009150 KS', entry)
        entry['row']['financial_excerpt'] = 'Sales USD 100 billion'
        with pytest.raises(ValueError, match='replayed'):
            validate_entry(tmp_path, registry.db, '009150 KS', entry)
        entry['row']['financial_excerpt'] = 'Sales KRW 100 billion'
        with pytest.raises(ValueError, match='origin'):
            validate_entry(tmp_path, registry.db, '4062 JP', entry)
        resolve_source_document_path(tmp_path, source).write_bytes(b'<p>changed</p>')
        with pytest.raises(ValueError, match='hash'):
            validate_entry(tmp_path, registry.db, '009150 KS', entry)


def test_sec_replay_rejects_other_issuer_and_changed_value(tmp_path):
    import json
    from bot.foreign_research_sources import validate_entry
    from bot.source_registry import SourceRegistry
    directory = {'fields': ['cik', 'name', 'ticker', 'exchange'], 'data': [[1, 'Issuer', 'AAA', 'Nasdaq']]}
    submissions = {'cik': '1', 'tickers': ['AAA'], 'exchanges': ['Nasdaq'],
                   'filings': {'recent': {'accessionNumber': ['a']}}}
    facts = {'cik': 1, 'entityName': 'Issuer', 'facts': {'us-gaap': {'Assets': {'units': {'USD': [
        {'val': 100, 'end': '2026-06-30', 'filed': '2026-07-30', 'accn': 'a'}]}}}}}
    with SourceRegistry(tmp_path) as registry:
        docs = [registry.capture_document(name, json.dumps(payload), source_url=url, suffix='json')
                for name, payload, url in [
                    ('sec_ticker_directory', directory, 'https://www.sec.gov/files/company_tickers_exchange.json'),
                    ('sec_submissions', submissions, 'https://data.sec.gov/submissions/CIK0000000001.json'),
                    ('sec_companyfacts', facts, 'https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json')]]
        d, s, f = docs
        entry = {'format': 'sec_fact', 'source_document_id': f.document_id, 'sha256': f.content_hash,
                 'url': f.url, 'directory_source_id': d.document_id, 'directory_sha256': d.content_hash,
                 'submissions_source_id': s.document_id, 'submissions_sha256': s.content_hash,
                 'identity': issuer_identity(directory, submissions, 'AAA US'), 'cutoff': '2026-10-16',
                 'pointer': {'taxonomy': 'us-gaap', 'concept': 'Assets', 'unit': 'USD', 'index': 0},
                 'row': fact_row(facts, 'us-gaap', 'Assets', 'USD', 0)}
        validate_entry(tmp_path, registry.db, 'AAA US', entry)
        with pytest.raises(ValueError, match='identity'):
            validate_entry(tmp_path, registry.db, 'BBB US', entry)
        entry['row']['val'] = 200
        with pytest.raises(ValueError, match='linked'):
            validate_entry(tmp_path, registry.db, 'AAA US', entry)
