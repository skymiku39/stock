from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pandas as pd

from bot.stock_db import StockDB, default_db_path
from bot.watch_data_fetch import _backfill_yfinance_if_sparse, resolve_symbols


def test_resolve_symbols_reads_explicit_root_env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("SYMBOLS=2330, 2317\n", encoding="utf-8")
    monkeypatch.delenv("SYMBOLS", raising=False)
    assert resolve_symbols(root=tmp_path) == ["2330", "2317"]


def test_watch_yfinance_fallback_preserves_normalized_provenance(
    tmp_path: Path, monkeypatch,
) -> None:
    class FakeTicker:
        def history(self, **_kwargs):
            return pd.DataFrame(
                {
                    "Open": [100.0],
                    "High": [105.0],
                    "Low": [99.0],
                    "Close": [103.0],
                    "Volume": [12_500.0],
                },
                index=pd.to_datetime(["2026-09-18"]),
            )

    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        SimpleNamespace(Ticker=lambda _symbol: FakeTicker()),
    )
    monkeypatch.setenv("GOOGLE_CACHE_DIR", str(tmp_path / "cloud-cache"))
    db = StockDB.open(path=default_db_path(tmp_path))
    try:
        count = _backfill_yfinance_if_sparse(
            "2330",
            start="2026-09-01",
            end="2026-09-30",
            root=tmp_path,
            db=db,
            logger=__import__("logging").getLogger("test-watch-yfinance"),
            min_bars=400,
        )
        assert count == 1
        bar = db.get_price_history("2330", limit=1, ascending=False)[0]
        assert bar.source == "yfinance"
        assert bar.quality_status == "partial"
        assert bar.source_record_id.startswith("source-doc:yfinance_stock_day:")
        source = db.get_source_document(bar.source_record_id)
        assert source is not None
        assert "2026-09-01" in source.content_path or source.as_of == "2026-09-01"
        assert (tmp_path / "data" / "technicals" / "2330" / "daily_kline.csv").exists()
        assert (
            tmp_path / "cloud-cache" / "data" / "technicals" / "2330" / "daily_kline.csv"
        ).exists()
    finally:
        db.close()


def test_watch_cli_kline_only_honors_explicit_root(tmp_path: Path, monkeypatch, capsys) -> None:
    from bot import watch_data_fetch as module
    from bot import app_bootstrap

    monkeypatch.setattr(module, "resolve_symbols", lambda _, **kwargs: ["2330"])
    monkeypatch.setattr(app_bootstrap, "get_or_create_bus", lambda **kwargs: None)
    monkeypatch.setattr(module, "_ensure_daily_data", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_backfill_yfinance_if_sparse", lambda *args, **kwargs: 7)
    monkeypatch.setattr(module, "_publish_kline_events", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "publish_if_bus", lambda *args, **kwargs: None)

    assert module.main(["--kline-only", "--root", str(tmp_path)]) == 0
    report = capsys.readouterr().out
    assert str(tmp_path / "data" / "watch_snapshots") in report
    reports = list((tmp_path / "data" / "watch_snapshots").glob("quant_fetch_*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["price_bars"] == {"2330": 7}
