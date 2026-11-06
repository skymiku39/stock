"""Import reviewed issuer PDF comparison columns and official CSV comparatives.

PDF extraction is explicitly reviewed transcription, not automatic OCR. The raw
PDF hash, page numbers, exact column heading and source values travel together.
"""
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from bot.financial_history import comparative_revenue_rows
from bot.fundamentals_fetcher import _parse_one_revenue, _parse_quarterly_raw
from bot.monthly_revenue_research import materialize_monthly_revenue_slice
from bot.quarterly_research import materialize_quarterly_slice
from bot.runtime_state import atomic_json
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.stock_db import QuarterlyReport


def reviewed_pdf_rows(content: bytes, metadata: dict) -> list[dict]:
    if not content.startswith(b'%PDF') or hashlib.sha256(content).hexdigest() != metadata['pdf_sha256']:
        raise ValueError('reviewed PDF hash mismatch')
    if metadata.get('review_method') != 'rendered_page_transcription' or metadata.get('currency_unit') != 'TWD_thousand':
        raise ValueError('PDF requires reviewed page and explicit unit')
    rows = []
    for item in metadata['extractions']:
        if not re.fullmatch(r'\d{4}Q[1-4]', item['period']) or not item['pages'] or not item['column']:
            raise ValueError('PDF extraction requires period/page/column')
        if any(not isinstance(p, int) or p < 1 for p in item['pages']):
            raise ValueError('invalid PDF page locator')
        for key in ('revenue', 'gross_profit', 'operating_profit', 'net_profit', 'eps'):
            if not Decimal(str(item[key])).is_finite():
                raise ValueError('missing or nonfinite reviewed financial value')
        rows.append({'公司代號': item['symbol'], '年度': item['period'][:4], '季別': item['period'][-1],
            '營業收入': item['revenue'], '營業毛利': item['gross_profit'],
            '營業利益': item['operating_profit'], '本期淨利': item['net_profit'],
            '基本每股盈餘': item['eps']})
    return rows


def _merge_cache(root, symbol, filename, row, key):
    path = Path(root) / 'data/fundamentals' / symbol / filename
    prior = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    merged = {key(r): r for r in prior}
    merged[key(row)] = row
    atomic_json(path, [merged[k] for k in sorted(merged)])


def import_monthly_comparative(root, source_document_id: str, symbol: str, period: str):
    with SourceRegistry(root) as registry:
        original = registry.db.get_source_document(source_document_id)
        if original is None or not original.source.startswith('monthly_revenue_history_'):
            raise ValueError('requires original official monthly CSV')
        content = resolve_source_document_path(root, original).read_bytes()
        if hashlib.sha256(content).hexdigest() != original.content_hash:
            raise ValueError('monthly original hash mismatch')
        rows = [_parse_one_revenue(r) for r in comparative_revenue_rows(content)]
        item = next((r for r in rows if r and r.ticker == symbol and f'{r.year}-{r.month:02d}' == period), None)
        if item is None:
            raise ValueError('requested comparison period missing')
        document = registry.register_document('monthly_revenue_comparative', content,
            suffix='csv', mime_type='text/csv', url=original.url, as_of=period,
            metadata={'original_source_document_id': original.document_id,
                      'comparison_as_of': original.as_of, 'column_basis': 'prior_year',
                      'currency_unit': 'TWD_thousand'})
        result = materialize_monthly_revenue_slice(root=root, db=registry.db,
            source_document_id=document.document_id, symbol=symbol, year_month=period,
            revenue=item.revenue, cumulative=item.cum_revenue, yoy_pct=None, mom_pct=None,
            cum_yoy_pct=None, source_url=original.url, retrieved_at=document.retrieved_at)
        if result.status not in {'ok','reused'}:
            raise ValueError(result.error or result.status)
        _merge_cache(root, symbol, 'monthly_revenue.json', asdict(item), lambda r: (r['year'],r['month']))
        return result


def import_reviewed_quarterly_pdf(root, content: bytes, *, url: str, metadata: dict):
    rows = reviewed_pdf_rows(content, metadata)
    results = []
    with SourceRegistry(root) as registry:
        # One immutable PDF may supply several periods in successive imports.
        # Keep all reviewed locators so earlier canonical facts remain replayable.
        previous = registry.db.get_source_document('source-doc:issuer_quarterly_pdf:' + metadata['pdf_sha256'])
        if previous is not None:
            prior = json.loads(previous.metadata_json)
            reviewed_pdf_rows(content, prior)
            merged = {(r['symbol'], r['period']): r for r in prior['extractions']}
            merged.update({(r['symbol'], r['period']): r for r in metadata['extractions']})
            metadata = {**metadata, 'extractions': list(merged.values())}
        document = registry.register_document('issuer_quarterly_pdf', content,
            suffix='pdf', mime_type='application/pdf', url=url, metadata=metadata)
        locators = {(r['symbol'], r['period']): r for r in metadata['extractions']}
        for raw in rows:
            locator = locators[(raw['公司代號'], f"{raw['年度']}Q{raw['季別']}")]
            raw.update(_source_document_id=document.document_id, _source_url=url,
                       _retrieved_at=document.retrieved_at, _quality_status='ok')
            symbol, period = raw['公司代號'], locator['period']
            parsed = _parse_quarterly_raw(raw, symbol)
            def ratio(key):
                rev = Decimal(str(locator['revenue']))
                return float((Decimal(str(locator[key])) / rev * 100).quantize(Decimal('.01'))) if rev else None
            note = ('公司正式合併財報比較欄；' + ('單季' if period.endswith('Q1') else '年度累計')
                    + f"；PDF頁 {','.join(map(str,locator['pages']))}，欄 {locator['column']}；"
                      'EPS依該報告所列比較數字（可能已追溯調整），非當時已知值')
            report = QuarterlyReport(symbol=symbol, period=period, eps=locator['eps'],
                revenue=locator['revenue'], gross_margin=ratio('gross_profit'),
                op_margin=ratio('operating_profit'), net_margin=ratio('net_profit'),
                note=note, source_url=url, source_date=period, retrieved_at=document.retrieved_at,
                quality_status='ok', source_record_id=document.document_id)
            registry.db.upsert_quarterly_report(report)
            result = materialize_quarterly_slice(root=root, db=registry.db, report=report)
            if result.status not in {'ok','reused'}:
                raise ValueError(result.error or result.status)
            _merge_cache(root, symbol, 'quarterlies_raw.json', parsed, lambda r: (r['year'],r['quarter']))
            results.append(result)
    return results
