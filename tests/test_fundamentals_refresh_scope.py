from bot import fundamentals_refresh as refresh


def test_foreign_queue_preserved_but_cannot_consume_twse_retry_slots(tmp_path, monkeypatch):
    refresh.save_refresh_queue({"MSFTUS": {"symbol": "MSFTUS", "attempts": 2},
                                "2330": {"symbol": "2330"}}, root=tmp_path)
    calls = []
    def fetch(symbol, **kwargs):
        calls.append(symbol)
        raise RuntimeError("source unavailable")
    monkeypatch.setattr(refresh, "build_fundamental_snapshot", fetch)
    stats = refresh.run_refresh_queue(root=tmp_path, limit=1, delay_seconds=0)
    assert calls == ["2330"]
    assert stats["due"] == 1
    item = refresh.load_refresh_queue(tmp_path)["MSFTUS"]
    assert item["status"] == "unsupported_instrument" and item["attempts"] == 2


def test_seed_excludes_foreign_symbols(tmp_path):
    assert refresh.seed_refresh_queue(["MSFT US", "MUUS", "NETUS", "0050", "00929", "2330"], root=tmp_path, force=True) == 1
    assert list(refresh.load_refresh_queue(tmp_path)) == ["2330"]
