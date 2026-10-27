import pytest

from bot.extended_issuer_releases import financial_excerpt, publication_line, index_refresh_url
from bot.official_release_discovery import release_links


def test_publication_date_is_not_the_reporting_period():
    excerpt, date = publication_line(['Financial Results for the Three Months Ended June 30, 2026',
                                     'July 30, 2026', 'Company name'])
    assert excerpt == 'July 30, 2026' and date.isoformat() == '2026-07-30'
    assert publication_line(['August, 5 2026'])[1].isoformat() == '2026-08-05'
    with pytest.raises(ValueError):
        publication_line(['Results for the Three Months Ended June 30, 2026'])


def test_issuer_index_does_not_choose_forecast_or_other_host():
    html = '''<a href="/eng/pdf/release/20260831.pdf">Establishment of a Subsidiary</a>
    <a href="/eng/pdf/release/20260805.pdf">Financial Results for the 1st Quarter of FY2026</a>
    <a href="/eng/pdf/release/20260805_02.pdf">Revision of Earnings Forecast</a>
    <a href="https://other.invalid/20270101.pdf">Financial Results for FY2026</a>'''
    assert release_links(html, 'https://www.mebuki-fg.co.jp/eng/', '7167 JP') == [
        'https://www.mebuki-fg.co.jp/eng/pdf/release/20260805.pdf']


def test_financial_excerpt_excludes_forecast_sections_and_disordered_table():
    text = ('Consolidated Operating Results (billions of yen) Revenue 100 80 '
            'Consolidated Financial Position Assets 200 Forecast Revenue 300')
    assert financial_excerpt('6098 JP', text) == 'Consolidated Operating Results (billions of yen) Revenue 100 80'
    sentence = '(Note) Comprehensive income For 2026: ¥52,684 million [38.1%] For 2025: ¥38,130 million [-%]'
    assert financial_excerpt('7167 JP', sentence + ' Forecast 10000') == sentence
    with pytest.raises(ValueError, match='changed'):
        financial_excerpt('8309 JP', 'new unknown table')


def test_index_refresh_follows_only_explicit_same_origin_metadata():
    base = 'https://issuer.example/ir/'
    assert index_refresh_url('<meta http-equiv="refresh" content="0;URL=/ir/2026.html">', base) == 'https://issuer.example/ir/2026.html'
    assert index_refresh_url('<script>location.href="/hidden"</script>', base) is None
    with pytest.raises(ValueError, match='origin'):
        index_refresh_url('<meta http-equiv="refresh" content="0;URL=https://other.invalid/report">', base)
