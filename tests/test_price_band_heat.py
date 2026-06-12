"""Tests for price_band_heat."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot.market_movers import MoverRow
from bot.price_band_heat import _is_dr_like, fetch_price_band_heat


def test_is_dr_like():
    assert _is_dr_like(MoverRow(ticker="9105", name="致富-DR"))
    assert not _is_dr_like(MoverRow(ticker="2538", name="基隆"))


@patch("bot.price_band_heat._scan_all_quotes")
def test_fetch_price_band_heat_ranks_by_volume(mock_scan):
    mock_scan.return_value = (
        {
            "2538": MoverRow(ticker="2538", name="基隆", price=10.15, volume=200, pct_chg=0.5),
            "2323": MoverRow(ticker="2323", name="士林電", price=11.2, volume=5000, pct_chg=3.0),
            "2330": MoverRow(ticker="2330", name="台積電", price=1000.0, volume=99999, pct_chg=1.0),
            "9105": MoverRow(ticker="9105", name="致富-DR", price=9.74, volume=40000, pct_chg=5.0),
        },
        4,
        1,
        [],
    )
    result = fetch_price_band_heat(
        price_low=9.0,
        price_high=12.5,
        limit=10,
        exclude_dr=True,
        session=MagicMock(),
    )
    assert result.band_count == 2
    assert result.rows[0].ticker == "2323"
    assert result.rows[0].band_rank == 1
    assert result.rows[1].ticker == "2538"
    assert result.rows[1].band_rank == 2
