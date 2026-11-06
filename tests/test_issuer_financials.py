import hashlib
import pytest
from bot.issuer_financials import reviewed_pdf_rows
from bot.financial_history import comparative_revenue_rows


def test_comparative_columns_are_shifted_without_inventing_growth():
    raw = ('公司代號,資料年月,營業收入-當月營收,營業收入-去年當月營收,累計營業收入-去年累計營收\n'
           '7856,115/01,214313,158741,158741\n').encode('utf-8')
    row = comparative_revenue_rows(raw)[0]
    assert row['資料年月'] == '114/01'
    assert row['營業收入-當月營收'] == '158741'
    assert row['營業收入-去年同月增減(%)'] == ''


def test_monthly_answer_keeps_full_amount_and_explicit_unknown_rates():
    from bot.monthly_revenue_research import canonical_summary_from_payload
    text = canonical_summary_from_payload(dict(symbol='7856', year_month='2025-01',
        revenue=158741, yoy_pct=None, mom_pct=None, cumulative=158741, cum_yoy_pct=None))
    assert '月營收 158741 新台幣千元' in text
    assert '營收年增率未提供' in text
    assert '0%' not in text


def test_reviewed_pdf_keeps_loss_sign_and_checks_hash_and_locator():
    raw = b'%PDF-reviewed-test'
    meta = dict(pdf_sha256=hashlib.sha256(raw).hexdigest(), review_method='rendered_page_transcription',
                currency_unit='TWD_thousand', extractions=[dict(symbol='7828', period='2024Q2',
                pages=[8],column='113年1月至6月',revenue=81453,gross_profit=60946,
                operating_profit=-9156,net_profit=-1509,eps=-.05)])
    assert reviewed_pdf_rows(raw,meta)[0]['基本每股盈餘'] == -.05
    with pytest.raises(ValueError,match='hash'):
        reviewed_pdf_rows(raw+b'changed',meta)
    meta['extractions'][0]['pages'] = []
    with pytest.raises(ValueError,match='page'):
        reviewed_pdf_rows(raw,meta)


def test_two_periods_from_one_pdf_keep_both_replay_locators(tmp_path, monkeypatch):
    import json
    from bot.issuer_financials import import_reviewed_quarterly_pdf
    from bot.stock_db import StockDB
    monkeypatch.setenv('GOOGLE_CACHE_DIR', str(tmp_path/'cloud'))
    raw = b'%PDF-reviewed-two-periods'
    digest = hashlib.sha256(raw).hexdigest()
    for period, revenue in [('2024Q1', 1000), ('2024Q2', 2000)]:
        meta = dict(pdf_sha256=digest, review_method='rendered_page_transcription',
                    currency_unit='TWD_thousand', extractions=[dict(symbol='7828', period=period,
                    pages=[8], column=period, revenue=revenue, gross_profit=100,
                    operating_profit=-10, net_profit=-5, eps=-.05)])
        import_reviewed_quarterly_pdf(tmp_path, raw, url='https://example.test/issuer.pdf', metadata=meta)
    db = StockDB.open(root=tmp_path)
    try:
        source = db.get_source_document('source-doc:issuer_quarterly_pdf:'+digest)
        rows = reviewed_pdf_rows(raw, json.loads(source.metadata_json))
        assert len(rows) == 2
        assert {r['季別'] for r in rows} == {'1', '2'}
    finally:
        db.close()
