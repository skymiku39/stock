"""Tests for market_movers scanner."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot.market_movers import (
    _parse_mis_items,
    fetch_market_movers,
    list_scan_tickers,
)


def test_parse_mis_items_pct_chg():
    items = [{
        "c": "2330",
        "n": "台積電",
        "z": "1000",
        "y": "980",
        "v": "12345",
        "ex": "tse",
        "t": "10:00:00",
        "d": "20260610",
    }]
    rows = _parse_mis_items(items)
    assert "2330" in rows
    row = rows["2330"]
    assert row.pct_chg == round(100 * (1000 - 980) / 980, 2)
    assert row.volume == 12345


@patch("bot.market_movers.load_market_map")
def test_list_scan_tickers_excludes_etf(mock_map):
    mock_map.return_value = {
        "2330": "twse",
        "0050": "twse",
        "3443": "tpex",
    }
    tickers = list_scan_tickers(exclude_etf=True)
    assert "2330" in tickers
    assert "3443" in tickers
    assert "0050" not in tickers


@patch("bot.market_movers.time.sleep")
@patch("bot.market_movers._session")
@patch("bot.market_movers.load_market_map")
def test_fetch_market_movers_sorts_gainers(mock_map, mock_session_fn, _sleep):
    mock_map.return_value = {"2330": "twse", "2317": "twse"}
    sess = MagicMock()
    mock_session_fn.return_value = sess
    sess.get.side_effect = [
        MagicMock(),  # index.jsp
        MagicMock(
            json=lambda: {
                "msgArray": [
                    {"c": "2330", "n": "A", "z": "110", "y": "100", "v": "1", "ex": "tse"},
                    {"c": "2317", "n": "B", "z": "95", "y": "100", "v": "1", "ex": "tse"},
                ],
            },
        ),
    ]
    result = fetch_market_movers(limit=1, direction="gainers", batch_size=10)
    assert result.rows[0].ticker == "2330"
    assert result.rows[0].pct_chg == 10.0
    assert result.scanned == 2
