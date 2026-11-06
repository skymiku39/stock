"""Fetch and replay official MOPS historical revenue and YTD income statements.

Original CSV/HTML bytes are retained. Periods are explicit request parameters;
retrieval time is never substituted for a financial period or publication date.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup
import requests

from bot.fundamentals_fetcher import _parse_one_revenue, _parse_quarterly_raw, _decumulate_quarterlies
from bot.runtime_state import atomic_json
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.stock_db import QuarterlyReport


HOST = 'https://mopsov.twse.com.tw'


def revenue_rows(content: bytes) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')), strict=True)
    required = {'公司代號', '資料年月', '營業收入-當月營收'}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError('official revenue CSV headers missing')
    if len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError('duplicate revenue CSV header')
    rows = list(reader)
    if not rows or any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError('empty or malformed revenue CSV')
    return rows


def quarterly_rows(content: bytes, year: int, quarter: int, market: str) -> list[dict]:
    soup = BeautifulSoup(content, 'lxml')
    text = soup.get_text(' ', strip=True)
    market_label = {'sii': '上市', 'otc': '上櫃'}[market]
    quarter_label = {1: '第一季', 2: '第二季', 3: '第三季', 4: '第四季'}[quarter]
    headings = ' '.join(node.get_text(' ', strip=True) for node in soup.find_all(['h2', 'h3', 'h4']))
    if market_label not in headings or quarter_label not in headings:
        raise ValueError('MOPS response market/quarter does not match request')
    if '累計金額' not in text or '新台幣仟元' not in text:
        raise ValueError('MOPS response missing cumulative basis or currency unit')
    result = []
    seen = set()
    for table in soup.select('table.hasBorder'):
        headers = None
        for tr in table.find_all('tr'):
            if tr.find_parent('table') is not table:
                continue
            cells = tr.find_all(['th', 'td'], recursive=False)
            values = [re.sub(r'\s+', '', cell.get_text()) for cell in cells]
            if values and values[0] == '公司代號':
                headers = values
                continue
            if not headers or not values or not re.fullmatch(r'\d{4,6}', values[0]):
                continue
            if len(values) != len(headers):
                raise ValueError('MOPS income statement row width mismatch')
            if values[0] in seen:
                raise ValueError('duplicate company in income statement')
            row = dict(zip(headers, values))
            if not any(key.startswith('基本每股盈餘') for key in row):
                raise ValueError('MOPS income statement missing EPS')
            row.update(年度=str(year - 1911), 季別=str(quarter))
            seen.add(values[0])
            result.append(row)
    if not result:
        raise ValueError('MOPS income statement contains no company data')
    return result


def source_rows(source, content: bytes) -> list[dict]:
    if source.source == 'monthly_revenue_comparative':
        return comparative_revenue_rows(content)
    if source.source == 'issuer_quarterly_pdf':
        from bot.issuer_financials import reviewed_pdf_rows
        return reviewed_pdf_rows(content, json.loads(source.metadata_json))
    if source.source.startswith('monthly_revenue_history_'):
        return revenue_rows(content)
    if source.source.startswith('quarterly_financials_history_'):
        meta = json.loads(source.metadata_json)
        return quarterly_rows(content, int(meta['year']), int(meta['quarter']), meta['market'])
    payload = json.loads(content)
    return payload.get('rows', [payload]) if isinstance(payload, dict) else payload


def comparative_revenue_rows(content: bytes) -> list[dict]:
    """Replay the explicitly reported prior-year columns, without inventing growth rates."""
    rows = []
    for raw in revenue_rows(content):
        year, month = map(int, raw['資料年月'].split('/'))
        row = {'公司代號': raw['公司代號'], '公司名稱': raw.get('公司名稱', ''),
               '資料年月': f'{year-1}/{month:02d}',
               '營業收入-當月營收': raw['營業收入-去年當月營收'],
               '累計營業收入-當月累計營收': raw['累計營業收入-去年累計營收'],
               '營業收入-去年同月增減(%)': '', '營業收入-上月比較增減(%)': '',
               '累計營業收入-前期比較增減(%)': '',
               '備註': f"取自 {raw['資料年月']} 官方申報的去年同月及去年累計欄；未提供歷史成長率"}
        parsed = _parse_one_revenue(row)
        if parsed is not None:
            rows.append(row)
    return rows


def months_between(start: str, end: str):
    def ordinal(value):
        if not re.fullmatch(r'\d{4}-\d{2}', value):
            raise ValueError('month must be YYYY-MM')
        year, month = map(int, value.split('-'))
        if not 1 <= month <= 12:
            raise ValueError('invalid month')
        return year * 12 + month - 1
    a, b = ordinal(start), ordinal(end)
    if a > b:
        raise ValueError('start month is after end month')
    return [(n // 12, n % 12 + 1) for n in range(a, b + 1)]


def _fetch(registry, session, source, period, url, metadata, parser, *, refresh=False, data=None):
    if not refresh:
        prior = registry.db.conn.execute(
            'SELECT document_id FROM source_documents WHERE source=? AND as_of=? '
            "AND quality_status='ok' ORDER BY retrieved_at DESC LIMIT 1", (source, period)).fetchone()
        if prior:
            document = registry.db.get_source_document(prior[0])
            raw = resolve_source_document_path(registry.root, document).read_bytes()
            if hashlib.sha256(raw).hexdigest() != document.content_hash:
                raise ValueError('historical raw archive hash mismatch')
            return document, parser(raw)
    run = registry.start_ingestion(source, source_url=url, metadata=metadata)
    try:
        response = (session.post(url, data=data, timeout=30) if data is not None
                    else session.get(url, timeout=30))
        response.raise_for_status()
        rows = parser(response.content)
        document = registry.register_document(
            source, response.content, url=url, source_ref=period, as_of=period,
            suffix='html' if data is not None else 'csv',
            mime_type='text/html' if data is not None else 'text/csv',
            ingestion_run_id=run.run_id, metadata=metadata)
        registry.finish_ingestion(run.run_id, status='succeeded', document_count=1)
        return document, rows
    except Exception as exc:
        registry.finish_ingestion(run.run_id, status='failed', error=str(exc))
        raise


def backfill_history(root: Path, symbols: list[str], start: str, end: str, *,
                     markets=('sii', 'otc'), refresh=False, session=None, progress=None, quarterly_end=None):
    from bot.monthly_revenue_research import materialize_monthly_revenue_slice
    from bot.quarterly_research import materialize_quarterly_slice
    root = Path(root)
    targets = set(symbols)
    if not targets or any(not re.fullmatch(r'[1-9]\d{3,5}', symbol) for symbol in targets):
        raise ValueError('history requires company symbols (ETF has no company financial statements)')
    periods = months_between(start, end)
    if quarterly_end and not re.fullmatch(r'\d{4}Q[1-4]', quarterly_end):
        raise ValueError('quarterly end must be YYYYQn')
    sess = session or requests.Session()
    result = {'symbols': sorted(targets), 'start': start, 'end': end, 'monthly': [], 'quarterly': [], 'errors': []}
    histories = {symbol: {'monthly': {}, 'quarterly': {}} for symbol in targets}
    with SourceRegistry(root) as registry:
        for year, month in periods:
            period = f'{year:04d}-{month:02d}'
            for market in markets:
                try:
                    url = f'{HOST}/nas/t21/{market}/t21sc03_{year - 1911}_{month}.csv'
                    def parse_month(raw):
                        rows = revenue_rows(raw)
                        parsed = [_parse_one_revenue(row) for row in rows]
                        if any(row is None or (row.year, row.month) != (year, month) for row in parsed):
                            raise ValueError('monthly response contains invalid values or wrong period')
                        return rows
                    document, rows = _fetch(registry, sess, 'monthly_revenue_history_' + market, period,
                        url, {'year': year, 'month': month, 'market': market}, parse_month, refresh=refresh)
                    parsed_rows = [_parse_one_revenue(row) for row in rows]
                    for parsed in parsed_rows:
                        if parsed.ticker not in targets:
                            continue
                        item = materialize_monthly_revenue_slice(
                            root=root, db=registry.db, source_document_id=document.document_id,
                            symbol=parsed.ticker, year_month=period, revenue=parsed.revenue,
                            yoy_pct=parsed.yoy, mom_pct=parsed.mom, cumulative=parsed.cum_revenue,
                            cum_yoy_pct=parsed.cum_yoy, source_url=url, retrieved_at=document.retrieved_at)
                        if item.status not in {'ok', 'reused', 'stale_skipped'}:
                            raise ValueError(item.error)
                        histories[parsed.ticker]['monthly'][period] = asdict(parsed)
                        result['monthly'].append({'symbol': parsed.ticker, 'period': period,
                                                  'source_document_id': document.document_id})
                except Exception as exc:
                    result['errors'].append({'domain': 'monthly', 'period': period, 'market': market, 'error': str(exc)})
            if progress:
                progress({'domain': 'monthly', 'period': period, 'rows': len(result['monthly']), 'errors': len(result['errors'])})
        quarters = sorted({(year, (month - 1) // 3 + 1) for year, month in periods if month % 3 == 0})
        if quarterly_end:
            quarters = [(year, quarter) for year, quarter in quarters if f'{year}Q{quarter}' <= quarterly_end]
        result['quarterly_end'] = quarterly_end or (f'{quarters[-1][0]}Q{quarters[-1][1]}' if quarters else '')
        for year, quarter in quarters:
            period = f'{year}Q{quarter}'
            for market in markets:
                try:
                    data = {'encodeURIComponent': '1', 'step': '1', 'firstin': '1', 'off': '1',
                            'TYPEK': market, 'year': str(year - 1911), 'season': f'{quarter:02d}'}
                    url = f'{HOST}/mops/web/ajax_t163sb04?' + urlencode(data)
                    document, rows = _fetch(registry, sess, 'quarterly_financials_history_' + market, period,
                        url, {'year': year, 'quarter': quarter, 'market': market, 'request_method': 'POST'},
                        lambda raw: quarterly_rows(raw, year, quarter, market), refresh=refresh, data=data)
                    for raw in rows:
                        symbol = raw['公司代號']
                        if symbol not in targets:
                            continue
                        raw = {**raw, '_source_document_id': document.document_id, '_source_url': url,
                               '_retrieved_at': document.retrieved_at, '_quality_status': 'ok'}
                        parsed = _parse_quarterly_raw(raw, symbol)
                        if parsed is None:
                            raise ValueError('invalid historical quarterly company row')
                        histories[symbol]['quarterly'][period] = parsed
                        # Canonical facts preserve the source's YTD basis. Snapshot code can
                        # derive single-quarter deltas separately from the complete raw history.
                        item = _decumulate_quarterlies(symbol, [parsed])[0]
                        report = QuarterlyReport(symbol=symbol, period=period, eps=item.eps, revenue=item.revenue,
                            gross_margin=item.gross_margin, op_margin=item.operating_margin, net_margin=item.net_margin,
                            note=item.note, source_url=url, source_date=period, retrieved_at=document.retrieved_at,
                            quality_status='ok', source_record_id=document.document_id)
                        registry.db.upsert_quarterly_report(report)
                        sliced = materialize_quarterly_slice(root=root, db=registry.db, report=report)
                        if sliced.status not in {'ok', 'reused'}:
                            raise ValueError(sliced.error)
                        result['quarterly'].append({'symbol': symbol, 'period': period,
                                                    'source_document_id': document.document_id})
                except Exception as exc:
                    result['errors'].append({'domain': 'quarterly', 'period': period, 'market': market, 'error': str(exc)})
            if progress:
                progress({'domain': 'quarterly', 'period': period, 'rows': len(result['quarterly']), 'errors': len(result['errors'])})
    for symbol, history in histories.items():
        for domain, filename, key in (
            ('monthly', 'monthly_revenue.json', lambda row: f"{row['year']:04d}-{row['month']:02d}"),
            ('quarterly', 'quarterlies_raw.json', lambda row: f"{row['year']}Q{row['quarter']}"),
        ):
            path = root / 'data/fundamentals' / symbol / filename
            previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
            merged = {key(row): row for row in previous}
            merged.update(history[domain])
            if history[domain]:
                atomic_json(path, [merged[k] for k in sorted(merged)])
    for symbol in sorted(targets):
        for year, month in periods:
            period = f'{year:04d}-{month:02d}'
            if period not in histories[symbol]['monthly']:
                result['errors'].append({'domain': 'monthly', 'symbol': symbol, 'period': period, 'error': 'missing company data'})
        for year, quarter in quarters:
            period = f'{year}Q{quarter}'
            if period not in histories[symbol]['quarterly']:
                result['errors'].append({'domain': 'quarterly', 'symbol': symbol, 'period': period, 'error': 'missing company data'})
    result['complete'] = not result['errors']
    return result


def main(argv=None):
    from bot.cli_encoding import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--symbols', required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--quarterly-end', help='Explicit last required quarterly period, e.g. 2026Q2')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = backfill_history(args.root.resolve(), args.symbols.split(','), args.start, args.end,
                              refresh=args.refresh, quarterly_end=args.quarterly_end,
                              progress=lambda row: print(json.dumps(row), flush=True))
    atomic_json(args.output, result)
    print(json.dumps({'complete': result['complete'], 'monthly': len(result['monthly']),
                      'quarterly': len(result['quarterly']), 'errors': result['errors']}, ensure_ascii=False))
    return 0 if result['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
