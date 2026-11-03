"""Exact TWSE fund profiles and daily prices, kept separate from company accounts."""
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import requests

from bot.evidence import make_evidence, persist_evidence_items
from bot.source_registry import SourceRegistry
from bot.utils import now_tw

PROFILE_URL = 'https://openapi.twse.com.tw/v1/opendata/t187ap47_L'


def yuanta_url(ticker):
    if not ticker.isdigit() or not 4 <= len(ticker) <= 6:
        raise ValueError('invalid fund ticker')
    return f'https://www.yuantaetfs.com/product/detail/{ticker}/ratio'


def profile_row(raw, ticker):
    rows = [r for r in json.loads(raw) if isinstance(r, dict) and r.get('基金代號') == ticker]
    if len(rows) != 1:
        raise ValueError('missing or ambiguous TWSE fund identity')
    return rows[0]


def price_row(raw, index):
    data = json.loads(raw)
    fields = data.get('fields', [])
    row = data['data'][index]
    if data.get('stat') != 'OK' or len(fields) != len(row) or len(fields) != len(set(fields)):
        raise ValueError('invalid TWSE fund price response')
    return dict(zip(fields, row))


def validate_entry(root, db, ticker, entry):
    from bot.foreign_research_sources import read_source
    source, raw = read_source(root, db, entry['source_document_id'], entry['sha256'])
    if entry.get('url') != source.url:
        raise ValueError('fund source URL mismatch')
    identity, identity_raw = read_source(root, db, entry['identity_source_id'], entry['identity_sha256'])
    if identity.source != 'twse_fund_profile' or identity.url != PROFILE_URL:
        raise ValueError('wrong official fund identity source')
    profile = profile_row(identity_raw, ticker)
    if entry['format'] == 'twse_fund_profile':
        if source.document_id != identity.document_id or entry['row'] != profile:
            raise ValueError('fund profile cannot be replayed')
    elif entry['format'] == 'twse_fund_price':
        url = urlparse(source.url)
        if (source.source != 'twse_stock_day' or url.scheme != 'https' or url.hostname != 'www.twse.com.tw'
                or url.path != '/exchangeReport/STOCK_DAY' or parse_qs(url.query).get('stockNo') != [ticker]):
            raise ValueError('wrong fund price identity')
        if entry['row'] != price_row(raw, entry['row_index']):
            raise ValueError('fund price cannot be replayed')
    elif entry['format'] in {'fund_holdings', 'fund_nav'}:
        from bot.yuanta_fund_source import parse_snapshot
        if (not str(profile.get('基金中文名稱', '')).startswith('元大')
                or source.source != 'yuanta_fund_snapshot' or source.url != yuanta_url(ticker)):
            raise ValueError('wrong official fund portfolio identity')
        snapshot = parse_snapshot(raw, ticker)
        expected = snapshot['holdings' if entry['format'] == 'fund_holdings' else 'nav']
        if entry['row'] != expected:
            raise ValueError('fund portfolio cannot be replayed')
    else:
        raise ValueError('unsupported fund evidence format')


def collect(root, tickers):
    result, excluded = {t: [] for t in tickers}, []
    if not tickers:
        return result, excluded
    from bot.foreign_research_sources import read_source
    with SourceRegistry(root) as registry:
        try:
            latest = registry.db.conn.execute("SELECT document_id FROM source_documents WHERE source='twse_fund_profile' ORDER BY retrieved_at DESC LIMIT 1").fetchone()
            profile, raw = read_source(root, registry.db, latest['document_id']) if latest else (None, None)
            if profile is None or not profile.retrieved_at.startswith(now_tw().date().isoformat()):
                response = requests.get(PROFILE_URL, timeout=30)
                response.raise_for_status()
                if response.status_code != 200 or response.url != PROFILE_URL or not isinstance(response.json(), list):
                    raise ValueError('invalid TWSE fund profile response')
                raw = response.content
                profile = registry.capture_document('twse_fund_profile', raw, source_url=PROFILE_URL,
                    source_ref=PROFILE_URL, suffix='json', kind='http_response')
            for ticker in tickers:
                try:
                    row = profile_row(raw, ticker)
                    entries = [{'format': 'twse_fund_profile', 'source_document_id': profile.document_id,
                        'sha256': profile.content_hash, 'url': profile.url, 'row': row}]
                    prices = registry.db.conn.execute("SELECT document_id FROM source_documents WHERE source='twse_stock_day' AND source_ref LIKE ? ORDER BY retrieved_at DESC LIMIT 1", (ticker + ':%',)).fetchone()
                    if prices:
                        price, price_raw = read_source(root, registry.db, prices['document_id'])
                        if price.retrieved_at.startswith(now_tw().date().isoformat()):
                            for index in range(len(json.loads(price_raw).get('data', []))):
                                entries.append({'format': 'twse_fund_price', 'source_document_id': price.document_id,
                                    'sha256': price.content_hash, 'url': price.url, 'row_index': index,
                                    'row': price_row(price_raw, index)})
                    if str(row.get('基金中文名稱', '')).startswith('元大'):
                        try:
                            from bot.yuanta_fund_source import parse_snapshot
                            url = yuanta_url(ticker)
                            response = requests.get(url, timeout=30)
                            response.raise_for_status()
                            if response.status_code != 200 or response.url != url:
                                raise ValueError('invalid official fund portfolio response')
                            snapshot = parse_snapshot(response.content, ticker)
                            if snapshot['nav']['as_of'] > now_tw().date().isoformat():
                                raise ValueError('future fund portfolio date')
                            portfolio = registry.capture_document('yuanta_fund_snapshot', response.content,
                                source_url=url, source_ref=ticker, suffix='html', kind='http_response',
                                as_of=snapshot['nav']['as_of'])
                            for key, format_name in (('holdings', 'fund_holdings'), ('nav', 'fund_nav')):
                                entries.append({'format': format_name, 'source_document_id': portfolio.document_id,
                                    'sha256': portfolio.content_hash, 'url': url, 'row': snapshot[key]})
                        except (ValueError, KeyError, TypeError, OSError, requests.RequestException) as exc:
                            excluded.append({'ticker': ticker, 'reason': 'official fund portfolio: ' + str(exc)})
                    for entry in entries:
                        entry.update(identity_source_id=profile.document_id, identity_sha256=profile.content_hash)
                        validate_entry(root, registry.db, ticker, entry)
                        encoded = json.dumps(entry['row'], ensure_ascii=False, sort_keys=True, allow_nan=False)
                        packet = make_evidence('official_fund_row', source=entry['format'],
                            source_ref=entry['source_document_id'] + ':' + ticker + ':' + hashlib.sha256(encoded.encode()).hexdigest(),
                            title=ticker + ' official fund ' + entry['format'], url=entry['url'], excerpt=encoded,
                            source_document_id=entry['source_document_id'], quality_status='ok', provenance_status='composite_exact',
                            metadata={'symbol': ticker, 'format': entry['format'],
                                'row_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
                                'source_document_ids': [entry['source_document_id'], profile.document_id]})
                        entry['evidence_id'] = packet['id']
                        persist_evidence_items(registry.db, [packet])
                    result[ticker] = entries
                except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
                    excluded.append({'ticker': ticker, 'reason': str(exc)})
        except (ValueError, OSError, KeyError, TypeError, requests.RequestException) as exc:
            excluded.extend({'ticker': ticker, 'reason': 'TWSE fund source: ' + str(exc)} for ticker in tickers)
    return result, excluded
