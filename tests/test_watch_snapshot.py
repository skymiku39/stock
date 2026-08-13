"""Tests for watch_snapshot."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bot.hot_stock_futures import HotStockFuturesResult
from bot.price_band_heat import PriceBandHeatResult, PriceBandHeatRow
from bot.watch_snapshot import save_watch_snapshot


@patch("bot.watch_snapshot.fetch_hot_stock_futures")
@patch("bot.watch_snapshot.fetch_price_band_heat")
def test_save_watch_snapshot_writes_files(mock_band, mock_fut, tmp_path: Path):
    mock_band.return_value = PriceBandHeatResult(
        asof="2026-06-12T10:00:00",
        price_low=9.0,
        price_high=12.5,
        band_count=1,
        rows=[PriceBandHeatRow(ticker="2323", name="A", band_rank=1, volume=100)],
    )
    mock_fut.return_value = HotStockFuturesResult(
        asof="2026-06-12T10:00:00",
        direction="volume",
        limit=5,
        rows=[],
    )
    summary = save_watch_snapshot(root=tmp_path, band_limit=5)
    assert len(summary["files"]) == 2
    assert (tmp_path / "data" / "watch_snapshots").exists()
