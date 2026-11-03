"""Deterministic comparisons and explicit objectives for research inputs.

These calculations consume exact source rows. They do not certify model prose
or turn a short excerpt into a complete financial analysis.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json


VERSION = 1


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value).replace(',', '').strip())
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def _change(current, previous):
    current, previous = number(current), number(previous)
    if current is None or previous is None:
        return None
    return {'current': float(current), 'previous': float(previous),
            'change': float(current - previous),
            'change_pct': float((current - previous) / abs(previous) * 100)
            if previous != 0 else None}


def input_fingerprint(item):
    """Count identical issuer data once even when vendor symbols differ."""
    rows = sorted(json.dumps({'source': s['source_document_id'], 'row': s['row']},
                             ensure_ascii=False, sort_keys=True) for s in item.get('sources', []))
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def _sec_comparisons(sources):
    groups = defaultdict(list)
    for source in sources:
        if source.get('format') != 'sec_fact':
            continue
        row = source['row']
        groups[(row.get('taxonomy'), row.get('concept'), row.get('unit'))].append(source)
    out = []
    for (_, concept, unit), entries in groups.items():
        entries.sort(key=lambda s: (s['row'].get('end', ''), s['row'].get('filed', ''),
                                    s['row'].get('start', '')), reverse=True)
        latest = entries[0]
        row = latest['row']
        try:
            end = date.fromisoformat(row['end'])
            duration = (end - date.fromisoformat(row['start'])).days if row.get('start') else None
        except (ValueError, KeyError):
            continue
        for prior in entries[1:]:
            old = prior['row']
            try:
                old_end = date.fromisoformat(old['end'])
                old_duration = (old_end - date.fromisoformat(old['start'])).days if old.get('start') else None
            except (ValueError, KeyError):
                continue
            if (old_end >= end or (duration is None) != (old_duration is None)
                    or (duration is not None and abs(duration - old_duration) > 7)):
                continue
            values = _change(row.get('val'), old.get('val'))
            if values is None:
                continue
            out.append({'metric': concept, 'unit': unit,
                        'comparison': 'year_over_year' if 350 <= (end - old_end).days <= 380 else 'prior_period',
                        'start': row.get('start', ''), 'end': row['end'],
                        'previous_start': old.get('start', ''), 'previous_end': old['end'],
                        **values, 'evidence_ids': [latest['evidence_id'], prior['evidence_id']]})
    return out


def _domestic_comparisons(sources):
    out = []
    for source in sources:
        row = source['row']
        current = row.get('營業收入-當月營收')
        for field, label in [('營業收入-上月營收', 'month_over_month'),
                             ('營業收入-去年當月營收', 'year_over_year'),
                             ('營業收入-去年同月營收', 'year_over_year')]:
            values = _change(current, row.get(field))
            if values is None:
                continue
            period = str(row.get('資料年月', ''))
            if not period:
                continue
            comparison = {'metric': 'monthly_revenue', 'unit': 'TWD_thousand',
                          'comparison': label, 'period': period, **values,
                          'evidence_ids': [source['evidence_id']]}
            if comparison not in out:
                out.append(comparison)
    return out


def _fund_comparisons(sources):
    prices = {}
    for source in sources:
        if source.get('format') != 'twse_fund_price':
            continue
        row = source['row']
        try:
            year, month, day = map(int, row['日期'].split('/'))
            day = date(year + 1911 if year < 1911 else year, month, day).isoformat()
        except (KeyError, ValueError, AttributeError):
            continue
        value = number(row.get('收盤價'))
        if value is not None and value > 0:
            prices[day] = (value, source['evidence_id'])
    out = []
    for source in sources:
        if source.get('format') != 'fund_nav':
            continue
        row = source['row']
        nav = number(row.get('nav'))
        day = row.get('as_of', '')
        if nav is not None and nav > 0 and day in prices:
            close, price_id = prices[day]
            out.append({'metric': 'premium_to_nav', 'unit': 'pct',
                'comparison': 'same_day_close_to_nav', 'period': day,
                'close': float(close), 'nav': float(nav),
                'premium_pct': float((close - nav) / nav * 100),
                'evidence_ids': [price_id, source['evidence_id']]})
    if len(prices) < 2:
        return out
    days = sorted(prices)
    first, last = days[0], days[-1]
    return out + [{'metric': 'close', 'unit': 'TWD', 'comparison': 'observed_price_change',
             'start': first, 'end': last, 'observations': len(prices),
             **_change(prices[last][0], prices[first][0]),
             'evidence_ids': [prices[first][1], prices[last][1]],
             'basis': 'unadjusted_close; excludes distributions and split adjustments'}]


def analyze(item):
    sources = item.get('sources', [])
    formats = {s.get('format', 'official_company_row') for s in sources}
    comparisons = _sec_comparisons(sources) + _domestic_comparisons(sources) + _fund_comparisons(sources)
    objectives = []

    def objective(name, met):
        objectives.append({'objective': name, 'met': bool(met)})

    if 'twse_fund_profile' in formats or 'ETF' in item.get('asset_class', ''):
        profile = next((s['row'] for s in sources if s.get('format') == 'twse_fund_profile'), {})
        objective('fund_identity', bool(profile.get('基金代號')))
        objective('price_comparison', any(c['metric'] == 'close' for c in comparisons))
        objective('holdings', any(s.get('format') == 'fund_holdings' for s in sources))
        objective('nav_and_premium', any(c['metric'] == 'premium_to_nav' for c in comparisons))
        kind = 'fund_research'
    elif 'sec_fact' in formats:
        concepts = {s['row'].get('concept') for s in sources if s.get('format') == 'sec_fact'}
        from bot.foreign_research_sources import CONCEPTS
        for label, alternatives in zip(('revenue', 'earnings', 'assets', 'liabilities', 'operating_cash_flow', 'eps'), CONCEPTS):
            objective(label, concepts.intersection(alternatives))
        objective('comparable_financial_periods', any(c['metric'] in set(CONCEPTS[0] + CONCEPTS[1]) for c in comparisons))
        objective('valuation', any('PEratio' in s['row'] or s.get('format') == 'valuation' for s in sources))
        kind = 'company_research'
    elif 'official_company_row' in formats:
        rows = [s['row'] for s in sources]
        objective('monthly_revenue', any('營業收入-當月營收' in r for r in rows))
        objective('financial_statement', any(('季別' in r or 'Season' in r) for r in rows))
        objective('valuation', any(number(r.get('PEratio')) is not None or number(r.get('PBratio')) is not None for r in rows))
        objective('revenue_comparison', bool(comparisons))
        objective('financial_history', len({(r.get('年度', r.get('Year')), r.get('季別', r.get('Season')))
                                           for r in rows if '季別' in r or 'Season' in r}) >= 2)
        kind = 'company_research'
    else:
        objective('structured_financials', False)
        objective('comparable_financial_periods', False)
        objective('valuation', False)
        kind = 'financial_excerpt' if sources else 'missing'
    ready = bool(objectives) and all(o['met'] for o in objectives)
    return {'version': VERSION, 'kind': kind, 'research_ready': ready,
            'status': 'ready' if ready else 'limited' if sources else 'missing',
            'objectives': objectives, 'missing': [o['objective'] for o in objectives if not o['met']],
            'comparisons': comparisons, 'input_fingerprint': input_fingerprint(item)}


def validate_analysis(report, item):
    if 'analysis' in report and report['analysis'] != analyze(item):
        raise ValueError('saved calculations or research objectives do not match source inputs')
