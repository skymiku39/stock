"""Tests for hot_stock_futures fetcher."""
from __future__ import annotations

from unittest.mock import MagicMock

from bot.hot_stock_futures import _parse_quote_item, fetch_hot_stock_futures


def test_parse_quote_item_pct_chg_fallback():
    row = _parse_quote_item({
        "SymbolID": "CCFF6-F",
        "SpotID": "2303",
        "DispCName": "聯電期貨066",
        "DispEName": "CCF066",
        "CLastPrice": "138.00",
        "CRefPrice": "126.50",
        "CTotalVolume": "100",
        "CTime": "101026",
    })
    assert row.spot_id == "2303"
    assert row.last_price == 138.0
    assert row.pct_chg == round(100 * (138.0 - 126.5) / 126.5, 2)
    assert row.quote_time == "10:10:26"


def test_fetch_hot_stock_futures_sorts_gainers():
    sess = MagicMock()
    sess.post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "RtCode": "0",
            "RtData": {
                "QuoteCount": "2",
                "QuoteList": [
                    {
                        "SymbolID": "AAA-F",
                        "SpotID": "2330",
                        "DispCName": "A",
                        "CLastPrice": "110",
                        "CRefPrice": "100",
                        "CDiffRate": "10.00",
                        "CTotalVolume": "50",
                    },
                    {
                        "SymbolID": "BBB-F",
                        "SpotID": "2317",
                        "DispCName": "B",
                        "CLastPrice": "95",
                        "CRefPrice": "100",
                        "CDiffRate": "-5.00",
                        "CTotalVolume": "200",
                    },
                ],
            },
        },
    )
    result = fetch_hot_stock_futures(limit=1, direction="gainers", session=sess)
    assert result.rows[0].symbol_id == "AAA-F"
    assert result.rows[0].pct_chg == 10.0
