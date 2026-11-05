"""Explicit accounting periods and dates requested in a market question."""
import re


def _year(value):
    year = int(value)
    return year + 1911 if year < 1911 else year


def requested_months(query):
    return list(dict.fromkeys(f'{_year(year):04d}-{int(month):02d}' for year, month in re.findall(
        r'(?<!\d)((?:19|20)\d{2}|1\d{2})\s*(?:年\s*|[-/])(1[0-2]|0?[1-9])(?:月|(?=\D|$))', query)))


def requested_quarters(query):
    values = []
    for year, numeric, chinese in re.findall(
        r'(?<!\d)((?:19|20)\d{2}|1\d{2})\s*(?:年\s*)?(?:[Qq]([1-4])|第?([一二三四1-4])季)', query):
        quarter = numeric or (str('一二三四'.index(chinese) + 1) if chinese in ('一', '二', '三', '四') else chinese)
        values.append(f'{_year(year)}Q{quarter}')
    if not values and any(word in query for word in ('全年', '年度')):
        values = [f'{_year(year)}Q4' for year in re.findall(r'(?<!\d)((?:19|20)\d{2}|1\d{2})年', query)]
    return list(dict.fromkeys(values))


def requested_dates(query):
    return list(dict.fromkeys(f'{_year(y):04d}-{int(m):02d}-{int(d):02d}' for y, m, d in re.findall(
        r'(?<!\d)((?:19|20)\d{2}|1\d{2})\s*(?:年|[-/])\s*(\d{1,2})\s*(?:月|[-/])\s*(\d{1,2})(?:日|(?=\D|$))', query)))


def financial_domains(query):
    domains = set()
    if any(word in query for word in ('營收', '營業收入')):
        domains.add('quarterly' if requested_quarters(query) or '季營收' in query else 'monthly')
    if any(word in query.upper() for word in ('EPS', '財報', '毛利', '營業利益', '淨利')):
        domains.add('quarterly')
    if any(word in query for word in ('估值', '本益比', '股價淨值比', '殖利率')):
        domains.add('valuation')
    if any(word in query for word in ('股利', '股息', '配息', '除息', '除權')):
        domains.add('dividend')
    return domains


def matches_query_scope(item, query):
    """Discard another period/domain rather than answering from a convenient row."""
    kind = item.get('kind', '')
    domains = financial_domains(query)
    text = ' '.join(str(item.get(key, '')) for key in ('title', 'as_of', 'source_ref', 'observed_at'))
    if kind == 'monthly_revenue':
        periods = requested_months(query)
        return (not domains or 'monthly' in domains) and (not periods or any(p in text for p in periods))
    if kind in {'quarterly_report', 'fundamental'}:
        periods = requested_quarters(query)
        if kind == 'fundamental' and not ('季報' in text or re.search(r'20\d{2}Q[1-4]', text)):
            months = requested_months(query)
            return not domains or ('monthly' in domains and '營收' in text
                                   and (not months or any(month in text for month in months)))
        return (not domains or 'quarterly' in domains) and (not periods or any(p in text for p in periods))
    if kind == 'valuation_daily':
        return not domains or 'valuation' in domains
    if kind == 'dividend_history':
        return not domains or 'dividend' in domains
    if kind == 'ticker_snapshot':
        dates = requested_dates(query)
        return not dates or any(date in text for date in dates)
    return True


def missing_requested_periods(items, query):
    domains = financial_domains(query)
    missing = []
    requirements = []
    if 'monthly' in domains:
        requirements.extend(('fundamental', period, {'monthly_revenue', 'fundamental'}) for period in requested_months(query))
    if 'quarterly' in domains:
        requirements.extend(('fundamental', period, {'quarterly_report', 'fundamental'}) for period in requested_quarters(query))
    if any(word in query for word in ('收盤價', '開盤價', '日 K', '日K', 'OHLCV')):
        requirements.extend(('ticker_snapshot', date, {'ticker_snapshot'}) for date in requested_dates(query))
    for resource, period, kinds in requirements:
        if not any(item.get('kind') in kinds and period in ' '.join(
                str(item.get(key) or '') for key in ('title', 'as_of', 'source_ref', 'observed_at')) for item in items):
            missing.append(f'{resource}:{period}')
    return missing
