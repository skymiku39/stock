"""Exact foreign issuer identity and raw financial evidence; no ticker guessing."""
import hashlib
import json
import math
import os
import time
import io
from pathlib import Path
from urllib.parse import urlparse

import requests

from bot.evidence import make_evidence, persist_evidence_items
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.utils import now_tw
from bot.verified_research_batch import write_json


CONCEPTS = (
    ('RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet', 'Revenue'),
    ('NetIncomeLoss', 'ProfitLoss'),
    ('Assets',), ('Liabilities',),
    ('NetCashProvidedByUsedInOperatingActivities', 'CashFlowsFromUsedInOperatingActivities'),
    ('EarningsPerShareDiluted', 'DilutedEarningsLossPerShare'),
)

OFFICIAL_IDENTITIES = {
    '4062 JP': ('www.ibiden.com', '4062', 'IBIDEN CO., LTD.'),
    '6857 JP': ('www.advantest.com', '6857', 'Advantest Corporation'),
    '6981 JP': ('corporate.murata.com', '6981', 'Murata Manufacturing Co., Ltd.'),
    '009150 KS': ('www.samsungsem.com', 'A009150', '삼성전기'),
    'IFX GY': ('www.infineon.com', 'ticker symbol: IFX', 'Infineon'),
}


def official_text(raw, pdf, page=0):
    if pdf:
        from pypdf import PdfReader
        text = PdfReader(io.BytesIO(raw)).pages[page].extract_text()
    else:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(raw, 'html.parser').get_text(' ', strip=True)
    return ' '.join(text.split())


def validate_official_excerpt(root, db, ticker, entry, source, raw):
    host, code, name = OFFICIAL_IDENTITIES[ticker]
    identity, identity_raw = read_source(root, db, entry['identity_source_id'], entry['identity_sha256'])
    for document in (source, identity):
        if document.source != 'foreign_official_issuer' or urlparse(document.url).hostname != host:
            raise ValueError('unapproved official issuer origin')
    identity_text = official_text(identity_raw, entry['identity_pdf'], entry.get('identity_page', 0))
    if code not in identity_text or name not in identity_text:
        raise ValueError('official ticker identity missing')
    texts = {key: official_text(raw, entry['pdf'], entry.get('row_pages', {}).get(key, entry.get('page', 0)))
             for key in entry['row']}
    if not entry['row'] or any(not isinstance(v, str) or not v or v not in texts[key] for key, v in entry['row'].items()):
        raise ValueError('official excerpt cannot be replayed')
    if not {'financial_excerpt', 'period_excerpt', 'publication_excerpt'} <= entry['row'].keys():
        raise ValueError('financial period or publication context missing')
    if entry.get('publication_date'):
        from bot.official_release_discovery import publication
        _, date = publication(entry['row']['publication_excerpt'])
        if date.isoformat() != entry['publication_date'] or date > now_tw().date():
            raise ValueError('invalid official publication date')
    for document_id in entry.get('discovery_source_ids', []):
        discovery, _ = read_source(root, db, document_id)
        if discovery.source != 'foreign_official_index' or urlparse(discovery.url).hostname != host:
            raise ValueError('invalid official discovery source')


def read_source(root, db, document_id, expected_hash=None):
    source = db.get_source_document(document_id)
    if source is None or source.quality_status not in {'ok', 'partial'}:
        raise ValueError('missing or unusable source')
    content = resolve_source_document_path(root, source).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != source.content_hash or (expected_hash and digest != expected_hash):
        raise ValueError('raw hash mismatch')
    return source, content


def issuer_identity(directory, submissions, ticker):
    if not ticker.endswith(' US'):
        raise ValueError('SEC mapping requires explicit US market suffix')
    symbol = ticker[:-3]
    matches = [dict(zip(directory['fields'], r)) for r in directory['data'] if dict(zip(directory['fields'], r)).get('ticker') == symbol]
    if len(matches) != 1:
        raise ValueError('missing or ambiguous SEC ticker identity')
    identity = matches[0]
    if int(submissions['cik']) != int(identity['cik']):
        raise ValueError('submission CIK mismatch')
    symbols, exchanges = submissions.get('tickers', []), submissions.get('exchanges', [])
    if symbol not in symbols or len(symbols) != len(exchanges):
        raise ValueError('submission does not confirm traded ticker')
    if str(exchanges[symbols.index(symbol)]).casefold() != str(identity['exchange']).casefold():
        raise ValueError('exchange mismatch')
    return identity


def fact_row(facts, taxonomy, concept, unit, index):
    value = facts['facts'][taxonomy][concept]['units'][unit][index]
    return {'issuer': facts['entityName'], 'CIK': facts['cik'], 'taxonomy': taxonomy,
            'concept': concept, 'unit': unit, **value}


def select_facts(facts, submissions, cutoff):
    accessions = set(submissions.get('filings', {}).get('recent', {}).get('accessionNumber', []))
    selected = []
    for alternatives in CONCEPTS:
        candidates = []
        for taxonomy in ('us-gaap', 'ifrs-full'):
            for concept in alternatives:
                units = facts.get('facts', {}).get(taxonomy, {}).get(concept, {}).get('units', {})
                for unit, records in units.items():
                    for index, record in enumerate(records):
                        value = record.get('val')
                        if not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value):
                            continue
                        if not record.get('end') or not record.get('filed') or record['end'] > cutoff or record['filed'] > cutoff:
                            continue
                        if record.get('accn') not in accessions:
                            continue
                        candidates.append((taxonomy, concept, unit, index, record))
        if candidates:
            # Same ending date can contain annual/YTD/quarter facts. Keep its
            # exact start/end rather than silently calling every fact quarterly.
            best = max(candidates, key=lambda x: (x[4]['end'], x[4]['filed'], x[4].get('start', '')))
            latest = (best[4]['end'], best[4]['filed'], best[4].get('start', ''))
            tied = [c for c in candidates if (c[4]['end'], c[4]['filed'], c[4].get('start', '')) == latest]
            if len({(c[2], c[4]['val']) for c in tied}) != 1:
                # Different concepts/currencies are distinct facts, not a
                # conversion or a conflict. Reject disagreement only within
                # the same taxonomy, concept, unit and exact period.
                groups = {}
                for candidate in tied:
                    groups.setdefault(candidate[:3], []).append(candidate)
                for records in groups.values():
                    if len({c[4]['val'] for c in records}) == 1:
                        selected.append(records[0][:4])
                continue
            selected.append(best[:4])
    return selected


def validate_entry(root, db, ticker, entry):
    source, raw = read_source(root, db, entry['source_document_id'], entry['sha256'])
    if entry.get('url') != source.url:
        raise ValueError('source URL mismatch')
    if entry['format'] == 'sec_fact':
        if source.source != 'sec_companyfacts':
            raise ValueError('wrong raw source type')
        directory_source, directory_raw = read_source(root, db, entry['directory_source_id'], entry['directory_sha256'])
        submissions_source, submissions_raw = read_source(root, db, entry['submissions_source_id'], entry['submissions_sha256'])
        identity = issuer_identity(json.loads(directory_raw), json.loads(submissions_raw), ticker)
        cik = int(identity['cik'])
        if (entry['identity'] != identity
                or directory_source.url != 'https://www.sec.gov/files/company_tickers_exchange.json'
                or submissions_source.url != f'https://data.sec.gov/submissions/CIK{cik:010d}.json'
                or source.url != f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json'):
            raise ValueError('SEC identity or official endpoint mismatch')
        facts = json.loads(raw)
        if int(facts['cik']) != int(identity['cik']):
            raise ValueError('financial facts CIK mismatch')
        pointer = entry['pointer']
        row = fact_row(facts, **pointer)
        if not isinstance(row.get('val'), (int, float)) or isinstance(row['val'], bool) or not math.isfinite(row['val']):
            raise ValueError('non-finite financial fact')
        if row != entry['row'] or row['accn'] not in json.loads(submissions_raw)['filings']['recent']['accessionNumber']:
            raise ValueError('financial fact is not linked to the issuer filing')
        if row['end'] > entry['cutoff'] or row['filed'] > entry['cutoff']:
            raise ValueError('future fact')
    elif entry['format'] == 'official_excerpt':
        validate_official_excerpt(root, db, ticker, entry, source, raw)
    else:
        raise ValueError('unsupported foreign evidence format')


def collect(root, tickers, *, refresh=False, cache_dir=None):
    out = Path(cache_dir) if cache_dir else Path(root) / 'data/foreign_sources' / now_tw().date().isoformat()
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / 'sec-downloads.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    result = {ticker: [] for ticker in tickers}
    exclusions = []
    from dotenv import dotenv_values
    agent = os.environ.get('SEC_USER_AGENT') or dotenv_values(Path(root) / '.env').get('SEC_USER_AGENT', '')
    session = requests.Session()
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    session.mount('https://', HTTPAdapter(max_retries=Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
    cutoff = now_tw().date().isoformat()

    def fetch(url, source_name):
        if url in manifest and not refresh:
            with SourceRegistry(root) as registry:
                source, raw = read_source(root, registry.db, manifest[url])
            if source.url != url or source.source != source_name:
                raise ValueError('cache source identity mismatch')
            return source, raw
        is_sec = urlparse(url).hostname in {'www.sec.gov', 'data.sec.gov'}
        if is_sec and not agent:
            raise ValueError('SEC_USER_AGENT with authorized contact is required')
        time.sleep(0.25)
        response = session.get(url, timeout=45, headers={'User-Agent': agent if is_sec else 'Mozilla/5.0'})
        response.raise_for_status()
        if response.status_code != 200 or not response.content:
            raise ValueError('empty or incomplete official response')
        if urlparse(response.url).hostname != urlparse(url).hostname:
            raise ValueError('official request redirected to another host')
        if is_sec:
            response.json()
        with SourceRegistry(root) as registry:
            source = registry.capture_document(source_name, response.content, source_url=url,
                source_ref=url, suffix='json' if is_sec else 'pdf' if response.content.startswith(b'%PDF') else 'html', kind='http_response')
        manifest[url] = source.document_id
        write_json(manifest_path, manifest)
        return source, response.content

    directory_error = None
    if any(t.endswith(' US') for t in tickers):
        try:
            directory_source, raw = fetch('https://www.sec.gov/files/company_tickers_exchange.json', 'sec_ticker_directory')
            directory_id, directory = directory_source.document_id, json.loads(raw)
        except (ValueError, OSError, requests.RequestException) as exc:
            directory_error = str(exc)
    for ticker in tickers:
        if not ticker.endswith(' US'):
            try:
                from bot.official_release_discovery import collect_official
                entries = [collect_official(ticker, fetch, cutoff)]
                with SourceRegistry(root) as registry:
                    for entry in entries:
                        validate_entry(root, registry.db, ticker, entry)
                        encoded = json.dumps(entry['row'], ensure_ascii=False, allow_nan=False)
                        row_hash = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
                        packet = make_evidence('official_financial_excerpt', source='foreign_official_issuer',
                            source_ref=entry['source_document_id'] + ':' + ticker + ':' + row_hash,
                            title=ticker + ' official financial excerpt', url=entry['url'],
                            excerpt=encoded,
                            source_document_id=entry['source_document_id'], quality_status='ok', provenance_status='composite_exact',
                            metadata={'row_sha256': row_hash, 'source_document_ids': [entry['source_document_id'], entry['identity_source_id'], *entry.get('discovery_source_ids', [])]})
                        entry['evidence_id'] = packet['id']
                        persist_evidence_items(registry.db, [packet])
                    result[ticker] = entries
            except (ValueError, OSError, TypeError, KeyError, AttributeError, IndexError, requests.RequestException) as exc:
                exclusions.append({'ticker': ticker, 'reason': str(exc)})
            continue
        try:
            if directory_error:
                raise ValueError(directory_error)
            matches = [dict(zip(directory['fields'], row)) for row in directory['data'] if dict(zip(directory['fields'], row)).get('ticker') == ticker[:-3]]
            if len(matches) != 1:
                raise ValueError('missing or ambiguous directory identity')
            cik = int(matches[0]['cik'])
            submissions_source, submissions = fetch(f'https://data.sec.gov/submissions/CIK{cik:010d}.json', 'sec_submissions')
            submissions = json.loads(submissions)
            identity = issuer_identity(directory, submissions, ticker)
            facts_source, facts = fetch(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json', 'sec_companyfacts')
            facts = json.loads(facts)
            if int(facts['cik']) != cik:
                raise ValueError('companyfacts identity mismatch')
            pointers = select_facts(facts, submissions, cutoff)
            if not pointers:
                raise ValueError('no verifiable financial facts with filing links')
            with SourceRegistry(root) as registry:
                for taxonomy, concept, unit, index in pointers:
                    row = fact_row(facts, taxonomy, concept, unit, index)
                    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)
                    packet = make_evidence('sec_company_fact', source='sec_companyfacts',
                        source_ref=facts_source.document_id + ':' + ticker + ':' + hashlib.sha256(encoded.encode()).hexdigest(),
                        title=ticker + ' SEC financial fact', url=facts_source.url, excerpt=encoded,
                        as_of=row['end'], retrieved_at=facts_source.retrieved_at,
                        source_document_id=facts_source.document_id, quality_status='ok', provenance_status='composite_exact',
                        metadata={'source_document_ids': [facts_source.document_id, directory_id, submissions_source.document_id]})
                    entry = {'format': 'sec_fact', 'evidence_id': packet['id'], 'source_document_id': facts_source.document_id,
                        'sha256': facts_source.content_hash, 'url': facts_source.url, 'row': row,
                        'directory_source_id': directory_id, 'directory_sha256': directory_source.content_hash,
                        'submissions_source_id': submissions_source.document_id, 'submissions_sha256': submissions_source.content_hash,
                        'identity': identity, 'cutoff': cutoff,
                        'pointer': {'taxonomy': taxonomy, 'concept': concept, 'unit': unit, 'index': index}}
                    validate_entry(root, registry.db, ticker, entry)
                    persist_evidence_items(registry.db, [packet])
                    result[ticker].append(entry)
            print(f'SEC {ticker}: {len(result[ticker])} verified facts', flush=True)
        except (ValueError, OSError, TypeError, KeyError, AttributeError, IndexError, requests.RequestException) as exc:
            result[ticker] = []
            exclusions.append({'ticker': ticker, 'reason': str(exc)})
            print(f'SEC {ticker}: excluded ({type(exc).__name__})', flush=True)
    write_json(out / 'sec-collection.json', {'inputs': result, 'excluded': exclusions})
    return result, exclusions
