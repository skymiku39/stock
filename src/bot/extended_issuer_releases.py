"""Reviewed issuer indexes with exact financial excerpts and separate date roles."""
import io
import re
from urllib.parse import urljoin, urlparse

from pypdf import PdfReader
from bs4 import BeautifulSoup


def publication_line(lines):
    from bot.official_release_discovery import publication
    for line in lines:
        line = ' '.join(line.split())
        try:
            excerpt, date = publication(line)
        except ValueError:
            continue
        if excerpt == line:
            return excerpt, date
    raise ValueError('standalone official publication date not found')


def financial_excerpt(ticker, text):
    bounds = {
        '6098 JP': ('Consolidated Operating Results', 'Consolidated Financial Position'),
        '8306 JT': ('(1) Results of Operations', '(2) Financial Conditions'),
        '8309 JP': ('(1) Operating Results', '(2) Financial Position'),
        '6787 JP': ('(1) Consolidated operating results', '(2) Consolidated financial position'),
        '8750 JP': ('(1) Consolidated results of operations', '(2) Consolidated financial condition'),
        '8766 JP': ('Insurance revenue', 'Total assets'),
    }
    if ticker == '7167 JP':
        # PDF reading order separates the numeric table from its headers.
        # Use only the self-contained comprehensive-income sentences.
        match = re.search(r'\(Note\) Comprehensive income.*?\[[^]]+\].*?\[[^]]+\]', text)
        if not match:
            raise ValueError('self-contained comprehensive income disclosure missing')
        return match.group()
    start, end = bounds[ticker]
    a = text.find(start)
    b = text.find(end, a)
    if a < 0 or b < 0:
        raise ValueError('reviewed issuer financial table changed')
    return text[a:b].strip()


def index_refresh_url(raw, base):
    for meta in BeautifulSoup(raw, 'html.parser').select('meta[http-equiv]'):
        if meta['http-equiv'].casefold() != 'refresh':
            continue
        match = re.search(r'\bURL\s*=\s*([^;]+)$', meta.get('content', ''), re.I)
        if match:
            url = urljoin(base, match.group(1).strip(' \"\''))
            if urlparse(url).scheme != 'https' or urlparse(url).hostname != urlparse(base).hostname:
                raise ValueError('official index refresh points to another origin')
            return url
    return None


def collect_release(ticker, fetch, cutoff):
    from bot.foreign_research_sources import OFFICIAL_IDENTITIES, official_text
    from bot.official_release_discovery import INDEXES, publication, release_links
    index_url, _ = INDEXES[ticker]
    index_source, index_raw = fetch(index_url, 'foreign_official_index')
    discovery = [index_source.document_id]
    refreshed = index_refresh_url(index_raw, index_url)
    if refreshed:
        index_url = refreshed
        index_source, index_raw = fetch(index_url, 'foreign_official_index')
        discovery.append(index_source.document_id)
    links = release_links(index_raw, index_url, ticker)
    if not links:
        raise ValueError('no reviewed financial release on official issuer index')
    source, raw = fetch(links[0], 'foreign_official_issuer')
    pdf = ticker != '6098 JP'
    if pdf != raw.startswith(b'%PDF'):
        raise ValueError('official release document format changed')
    cover = official_text(raw, pdf, 0)
    host, code, name = OFFICIAL_IDENTITIES[ticker]
    if code not in cover or name not in cover:
        raise ValueError('exact issuer ticker and name missing from release')
    if pdf:
        published, date = publication_line(PdfReader(io.BytesIO(raw)).pages[0].extract_text().splitlines())
    else:
        published, date = publication(cover)
    if date.isoformat() > cutoff:
        raise ValueError('future official release')
    period = re.search(r'(?:three|six|nine|twelve) months ended [A-Z][a-z]+ \d{1,2}, 20\d{2}', cover, re.I)
    if not period:
        raise ValueError('official financial period not found')
    page = 1 if ticker == '8306 JT' else 0
    financial = financial_excerpt(ticker, official_text(raw, pdf, page))
    return {'format': 'official_excerpt', 'source_document_id': source.document_id,
            'sha256': source.content_hash, 'url': source.url, 'pdf': pdf, 'page': page,
            'identity_source_id': source.document_id, 'identity_sha256': source.content_hash,
            'identity_pdf': pdf, 'identity_page': 0, 'discovery_source_ids': discovery,
            'publication_date': date.isoformat(),
            'row_pages': {'financial_excerpt': page, 'publication_excerpt': 0, 'period_excerpt': 0},
            'row': {'financial_excerpt': financial, 'publication_excerpt': published, 'period_excerpt': period.group()}}
