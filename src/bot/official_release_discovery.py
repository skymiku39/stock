"""Discover issuer releases from official indexes; never infer a download URL."""
import io
import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from pypdf import PdfReader

INDEXES = {
    '4062 JP': ('https://www.ibiden.com/ir/', r'tannshinn(20\d{2})(?:Q([1-4]))?\.pdf'),
    '6857 JP': ('https://www.advantest.com/en/investors/ir-library/result/', r'E_FR_FY(20\d{2})_(?:([1-4])Q|FN)\.pdf'),
    '6981 JP': ('https://corporate.murata.com/en-global/ir/library/results', r'(\d{2})q([1-4])-e-fls\.ashx'),
    '009150 KS': ('https://www.samsungsem.com/global/about-us/investor-relations/earnings-release.do', r'([1-4])Q(\d{2})_Earnings_Release_eng\.pdf'),
    'IFX GY': ('https://www.infineon.com/about/investor/reports-presentations/financial-results', r'inf(?:pr|xx)(20\d{2})(\d{2})-\d+e\.pdf'),
    '6098 JP': ('https://recruit-holdings.com/en/ir/financials/', r'recruit_(20\d{2})03Q([1-4])_earnings_en/$'),
    '7167 JP': ('https://www.mebuki-fg.co.jp/eng/', r'/(20\d{2})(\d{2})\d{2}(?:_\d+)?\.pdf'),
    '8306 JT': ('https://www.mufg.jp/english/ir/index.html', r'summary(\d{2})(\d{2})_en\.pdf'),
    '8309 JP': ('https://www.smtg.jp/english/investors/report/fs', r'E(\d{2})(\d{2})_01\.pdf'),
    '6787 JP': ('https://www.meiko-elec.com/english/ir/library/presentations.html', r'/statements/(20\d{2})/Q([1-4])\.pdf'),
    '8750 JP': ('https://www.daiichilife-group.com/en/investor/library/download/', r'/earning/pdf/(20\d{2})/(?:20\d{2}_quarter_00([1-3])|index_005)\.pdf'),
    '8766 JP': ('https://www.tokiomarinehd.com/en/ir/download/', r'/presentation/(20\d{2})/[^/]+/([1-4])Q_FY\d{4}_Summary_Report_e\.pdf'),
}


def release_links(html, base, ticker):
    pattern = INDEXES[ticker][1]
    found = {}
    for a in BeautifulSoup(html, 'html.parser').select('a[href]'):
        if ticker == '7167 JP' and not a.get_text(' ', strip=True).startswith('Financial Results for'):
            continue
        url = urljoin(base, a['href'])
        match = re.search(pattern, url, re.I)
        if match and urlparse(url).hostname == urlparse(INDEXES[ticker][0]).hostname:
            year, period = match.groups()
            if ticker == '009150 KS':
                year, period = period, year
            found.setdefault(url, (int(year) + (2000 if len(year) == 2 else 0), int(period or 4)))
    return sorted(found, key=lambda url: found[url], reverse=True)


def publication(text):
    months = 'January February March April May June July August September October November December'.split()
    pattern = r'\b(?:' + '|'.join(months) + r')[.,]?\s+\d{1,2}[,.]?\s+20\d{2}\b|\b\d{1,2}\s+(?:' + '|'.join(months) + r')\s+20\d{2}\b'
    match = re.search(pattern, text)
    if not match:
        raise ValueError('publication date not recognized')
    original = match.group()
    clean = original.replace('.', '').replace(',', '')
    for fmt in ('%B %d %Y', '%d %B %Y'):
        try:
            return original, datetime.strptime(clean, fmt).date()
        except ValueError:
            pass
    raise ValueError('invalid publication date')


def collect_official(ticker, fetch, cutoff):
    # Imports here keep source replay usable without a discovery/network dependency.
    from bot.foreign_research_sources import official_text, OFFICIAL_IDENTITIES, OFFICIAL_ALIASES
    ticker = OFFICIAL_ALIASES.get(ticker, ticker)
    if ticker in {'6098 JP', '7167 JP', '8306 JT', '8309 JP', '6787 JP', '8750 JP', '8766 JP'}:
        from bot.extended_issuer_releases import collect_release
        return collect_release(ticker, fetch, cutoff)
    if ticker not in INDEXES:
        raise ValueError(f'official issuer release adapter not configured for {ticker}')
    index_url, _ = INDEXES[ticker]
    index_source, index_raw = fetch(index_url, 'foreign_official_index')
    discovery_ids = [index_source.document_id]
    html = index_raw
    if ticker == 'IFX GY':
        soup = BeautifulSoup(index_raw, 'html.parser')
        tables = soup.select('[data-component="generictable"][data-api]')
        if not tables:
            raise ValueError('official financial results index changed')
        html = ''
        # Latest fiscal year first; an unpublished year can be empty.
        for table in tables[:2]:
            table_source, table_raw = fetch(urljoin(index_url, table['data-api']), 'foreign_official_index')
            discovery_ids.append(table_source.document_id)
            payload = json.loads(table_raw)
            for row in payload['data']:
                cells = row['rowData']
                if cells and 'Press Release' in cells[0].get('value', ''):
                    html += ''.join(cell.get('value', '') for cell in cells)
            if release_links(html, index_url, ticker):
                break
    links = release_links(html, index_url, ticker)
    if not links:
        raise ValueError('no recognized release on official index')
    source, raw = fetch(links[0], 'foreign_official_issuer')
    if not raw.startswith(b'%PDF'):
        raise ValueError('release is not a PDF')
    pages = PdfReader(io.BytesIO(raw)).pages
    cover = official_text(raw, True, 0)
    date_excerpt, date = publication(cover)
    if date.isoformat() > cutoff:
        raise ValueError('future official release')
    identity_source, identity_raw = source, raw
    identity_pdf, identity_page = True, 0
    host, code, name = OFFICIAL_IDENTITIES[ticker]
    if ticker == '009150 KS':
        identity_source, identity_raw = fetch('https://www.samsungsem.com/kr/about-us/investor-relations/stock.do', 'foreign_official_issuer')
        identity_pdf = False
    else:
        identity_page = next((i for i in range(min(len(pages), 15))
                              if code in official_text(raw, True, i) and name in official_text(raw, True, i)), -1)
        if identity_page < 0:
            raise ValueError('ticker identity missing from release')
    page = 0
    if ticker == '009150 KS':
        page = next((i for i in range(min(len(pages), 8)) if 'Earnings Overview' in official_text(raw, True, i)
                     and 'KRW' in official_text(raw, True, i)), -1)
        if page < 0:
            raise ValueError('earnings table not recognized')
        financial = official_text(raw, True, page)
        period = re.search(r'Q[1-4] 20\d{2} Earnings Result', cover).group()
    elif ticker == 'IFX GY':
        financial = re.search(r'Q[1-4] FY 20\d{2}: Revenue .*?Margin [\d.]+ percent', cover).group()
        period = re.search(r'(?:first|second|third|fourth) quarter of the 20\d{2} fiscal year, ended \d+ \w+ 20\d{2}', cover).group()
    else:
        start = 'Net sales' if ticker != '6981 JP' else 'Revenue Operating profit'
        financial = cover[cover.index(start):cover.index('Basic earnings', cover.index(start))].strip()
        patterns = {'4062 JP': r'FY 20\d{2} (?:1st|2nd|3rd|4th) quarter',
                    '6857 JP': r'Period ended \w+ \d+, 20\d{2}',
                    '6981 JP': r'(?:Three|Six|Nine|Twelve) Months Ended \w+ \d+, 20\d{2}'}
        period = re.search(patterns[ticker], cover, re.I).group()
    return {'format': 'official_excerpt', 'source_document_id': source.document_id,
            'sha256': source.content_hash, 'url': source.url, 'pdf': True, 'page': page,
            'identity_source_id': identity_source.document_id, 'identity_sha256': identity_source.content_hash,
            'identity_pdf': identity_pdf, 'identity_page': identity_page,
            'discovery_source_ids': discovery_ids, 'publication_date': date.isoformat(),
            'row_pages': {'financial_excerpt': page, 'publication_excerpt': 0, 'period_excerpt': 0},
            'row': {'financial_excerpt': financial, 'publication_excerpt': date_excerpt, 'period_excerpt': period}}
