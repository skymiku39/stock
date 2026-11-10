from types import SimpleNamespace
import json

import pytest

from bot.financial_history import months_between, quarterly_rows, revenue_rows, source_rows
from bot.fundamentals_fetcher import (
    FundamentalSnapshot, MonthlyRevenue, _parse_one_revenue, _parse_quarterly_raw, _decumulate_quarterlies,
)


def test_monthly_history_preserves_official_period_amount_and_unit():
    raw = ('\ufeff公司代號,資料年月,營業收入-當月營收,營業收入-去年當月營收\n'
           '2330,114/9,330981542,251872622\n').encode('utf-8')
    row = _parse_one_revenue(revenue_rows(raw)[0])
    assert (row.ticker, row.year, row.month, row.revenue) == ('2330', 2025, 9, 330981542)
    assert row.revenue_last_year == 251872622


@pytest.mark.parametrize('period,amount', [('114/13', '10'), ('garbage', '10'), ('11409', '--'), ('11409', 'nan')])
def test_missing_or_invalid_financial_values_are_not_zero(period, amount):
    assert _parse_one_revenue({'公司代號': '2330', '資料年月': period, '當月營收': amount}) is None


def test_yoy_streak_stops_at_missing_month():
    snapshot = FundamentalSnapshot('2330', revenues=[
        MonthlyRevenue('2330', year=2025, month=7, yoy=10),
        MonthlyRevenue('2330', year=2025, month=9, yoy=10),
    ])
    assert snapshot.revenue_yoy_streak() == 1


def _income_html():
    return '''<h2>上市公司第一季資料</h2><p>截至該季止之累計金額。單位：新台幣仟元</p>
    <table class="hasBorder"><tr><th>公司 代號</th><th>公司名稱</th><th>營業收入</th>
    <th>營業毛利（毛損）</th><th>營業毛利（毛損）淨額</th><th>營業利益（損失）</th>
    <th>本期淨利（淨損）</th><th>基本每股盈餘（元）</th></tr>
    <tr><td>2330</td><td>台積電</td><td>839,253,664</td><td>493,395,076</td>
    <td>493,395,076</td><td>407,080,808</td><td>360,732,661</td><td>13.95</td></tr></table>'''.encode('utf-8')


def test_quarterly_history_retains_ytd_values_and_replays_from_raw_html():
    source = SimpleNamespace(source='quarterly_financials_history_sii',
                             metadata_json=json.dumps({'year': 2025, 'quarter': 1, 'market': 'sii'}))
    rows = source_rows(source, _income_html())
    parsed = _parse_quarterly_raw(rows[0], '2330')
    assert parsed['cum_revenue'] == 839253664
    assert parsed['cum_eps'] == 13.95
    item = _decumulate_quarterlies('2330', [parsed])[0]
    assert (item.year, item.quarter, item.gross_margin, item.operating_margin) == (2025, 1, 58.79, 48.51)


def test_quarterly_rejects_other_requested_quarter():
    with pytest.raises(ValueError, match='does not match'):
        quarterly_rows(_income_html(), 2025, 2, 'sii')


def test_margin_uses_net_gross_profit_after_unrealized_adjustments():
    parsed = _parse_quarterly_raw({'公司代號': '2317', '年度': '114', '季別': '1',
        '營業收入': '1000', '營業毛利（毛損）': '150', '營業毛利（毛損）淨額': '120',
        '基本每股盈餘（元）': '1.25'}, '2317')
    assert _decumulate_quarterlies('2317', [parsed])[0].gross_margin == 12


def test_month_range_includes_year_boundary():
    assert months_between('2024-12', '2025-02') == [(2024, 12), (2025, 1), (2025, 2)]


# --- throttle / retry for _fetch (fake session and sleep; no network) ---
import requests

from bot.financial_history import _fetch
from bot.source_registry import SourceRegistry

_CSV = '公司代號,資料年月,營業收入-當月營收\n2330,114/9,100\n'.encode('utf-8')


class _Resp:
    def __init__(self, status=200, content=_CSV):
        self.status_code, self.content = status, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'HTTP {self.status_code}', response=self)


class _Session:
    def __init__(self, *items):
        self.items, self.calls = list(items), 0

    def get(self, url, timeout=None):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _runs(registry, source):
    return [r[0] for r in registry.db.conn.execute(
        'SELECT status FROM ingestion_runs WHERE source=? ORDER BY started_at', (source,))]


def _call(registry, session, sleeps, parser=lambda raw: raw, **kw):
    return _fetch(registry, session, 'monthly_revenue_history_sii', '2025-09', 'http://x/a.csv', {},
                  parser, sleep=sleeps.append, **kw)


def test_fetch_retries_503_then_succeeds(tmp_path):
    sleeps = []
    session = _Session(_Resp(503), requests.ConnectionError('boom'), _Resp(200))
    with SourceRegistry(tmp_path) as registry:
        document, rows = _call(registry, session, sleeps, delay_seconds=2.0, retries=3)
        assert rows == _CSV and session.calls == 3
        assert _runs(registry, 'monthly_revenue_history_sii') == ['succeeded']
    # pre-request delay on every attempt, plus exponential backoff before each retry
    assert sleeps == [2.0, 2.0, 2.0, 4.0, 2.0]


def test_fetch_does_not_retry_404(tmp_path):
    sleeps = []
    session = _Session(_Resp(404))
    with SourceRegistry(tmp_path) as registry:
        with pytest.raises(requests.HTTPError):
            _call(registry, session, sleeps, delay_seconds=2.0, retries=3)
        assert session.calls == 1 and sleeps == [2.0]
        assert _runs(registry, 'monthly_revenue_history_sii') == ['failed']


def test_fetch_does_not_retry_parser_error(tmp_path):
    sleeps = []
    session = _Session(_Resp(200))

    def bad(raw):
        raise ValueError('content invalid')
    with SourceRegistry(tmp_path) as registry:
        with pytest.raises(ValueError, match='content invalid'):
            _call(registry, session, sleeps, parser=bad, delay_seconds=2.0, retries=3)
        assert session.calls == 1 and sleeps == [2.0]
        assert _runs(registry, 'monthly_revenue_history_sii') == ['failed']


def test_fetch_exhausts_retries_and_fails_once(tmp_path):
    sleeps = []
    session = _Session(_Resp(500), _Resp(429))
    with SourceRegistry(tmp_path) as registry:
        with pytest.raises(requests.HTTPError):
            _call(registry, session, sleeps, retries=1)
        assert session.calls == 2 and sleeps == [1.0]
        assert _runs(registry, 'monthly_revenue_history_sii') == ['failed']


def test_fetch_reuse_raw_does_not_sleep(tmp_path):
    sleeps = []
    with SourceRegistry(tmp_path) as registry:
        _call(registry, _Session(_Resp(200)), sleeps, delay_seconds=2.0)
        sleeps.clear()
        session = _Session()
        document, rows = _call(registry, session, sleeps, delay_seconds=2.0, retries=3)
        assert rows == _CSV and session.calls == 0 and sleeps == []
