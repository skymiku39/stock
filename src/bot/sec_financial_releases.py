"""Replay a supported financial 6-K when SEC companyfacts has no accounting facts."""
import re

from bs4 import BeautifulSoup


def release_row(raw, issuer):
    text = ' '.join(BeautifulSoup(raw, 'html.parser').get_text(' ', strip=True).split())
    if issuer.casefold() not in text.casefold():
        raise ValueError('financial release issuer identity missing')
    # Preserve the entire table, its units, comparative columns, footnotes,
    # and preliminary-results warning. Do not invent numeric column mappings.
    start = text.find('The preliminary results')
    end = text.find('2. Source of Data', start)
    if start < 0 or end < 0:
        raise ValueError('supported financial release table not found')
    excerpt = text[start:end].strip()
    if not all(word in excerpt for word in ('Consolidated', 'Current Period', 'Revenue', 'Operating Profit', 'Won')):
        raise ValueError('financial release context missing')
    period = re.search(r'Current Period .*?(?= Previous Period)', excerpt)
    publication = re.search(r'Date: ([A-Z][a-z]+ \d{1,2}, 20\d{2})', text)
    if not period or not publication:
        raise ValueError('financial release period or publication date missing')
    return {'financial_excerpt': excerpt, 'period_excerpt': period.group(),
            'publication_excerpt': publication.group(1)}


def filing_url(cik, accession, document):
    if not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.html?', document):
        raise ValueError('invalid SEC filing path')
    return f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace("-", "")}/{document}'


def collect_release(fetch, submissions, identity, cutoff):
    recent = submissions['filings']['recent']
    # A bounded discovery scan; never infer a release URL from its title.
    for i, form in enumerate(recent['form'][:60]):
        filed = recent['filingDate'][i]
        if form not in {'6-K', '6-K/A'} or filed > cutoff:
            continue
        accession, document = recent['accessionNumber'][i], recent['primaryDocument'][i]
        source, raw = fetch(filing_url(identity['cik'], accession, document), 'sec_financial_filing')
        try:
            row = release_row(raw, submissions['name'])
            from bot.official_release_discovery import publication
            if publication(row['publication_excerpt'])[1].isoformat() != filed:
                continue
        except ValueError:
            continue
        return {'format': 'sec_filing_excerpt', 'source_document_id': source.document_id,
                'sha256': source.content_hash, 'url': source.url, 'row': row,
                'accession': accession, 'primary_document': document, 'filing_date': filed}
    raise ValueError('no accounting facts or supported financial release with exact filing links')


def validate_entry(root, db, ticker, entry):
    import json
    from bot.foreign_research_sources import read_source, issuer_identity
    from bot.official_release_discovery import publication
    source, raw = read_source(root, db, entry['source_document_id'], entry['sha256'])
    directory_source, directory = read_source(root, db, entry['directory_source_id'], entry['directory_sha256'])
    submissions_source, submissions_raw = read_source(root, db, entry['submissions_source_id'], entry['submissions_sha256'])
    submissions = json.loads(submissions_raw)
    identity = issuer_identity(json.loads(directory), submissions, ticker)
    recent = submissions['filings']['recent']
    indices = [i for i, accession in enumerate(recent['accessionNumber']) if accession == entry['accession']]
    if len(indices) != 1:
        raise ValueError('financial release filing link missing or ambiguous')
    i = indices[0]
    if (entry['identity'] != identity or source.source != 'sec_financial_filing'
            or directory_source.url != 'https://www.sec.gov/files/company_tickers_exchange.json'
            or submissions_source.url != f'https://data.sec.gov/submissions/CIK{int(identity["cik"]):010d}.json'
            or recent['form'][i] not in {'6-K', '6-K/A'}
            or recent['primaryDocument'][i] != entry['primary_document']
            or recent['filingDate'][i] != entry['filing_date']
            or entry['filing_date'] > entry['cutoff']
            or source.url != filing_url(identity['cik'], entry['accession'], entry['primary_document'])
            or entry['url'] != source.url):
        raise ValueError('financial release identity, endpoint or date mismatch')
    if entry['row'] != release_row(raw, submissions['name']):
        raise ValueError('financial release excerpt cannot be replayed')
    if publication(entry['row']['publication_excerpt'])[1].isoformat() != entry['filing_date']:
        raise ValueError('financial release publication date mismatch')
