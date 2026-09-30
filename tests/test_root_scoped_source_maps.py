import json
from pathlib import Path

from bot import company_info, market_meta
from bot.stock_db import StockDB
from bot.utils import now_tw


def test_company_map_cache_isolated_by_root(monkeypatch, tmp_path: Path) -> None:
    company_info._COMPANY_MAP_CACHE.clear()
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"

    def fake_build(*, root=None, **_kwargs):
        symbol = "1111" if Path(root) == root_a else "2222"
        return {
            symbol: company_info.StockInfo(
                symbol=symbol,
                name=f"company-{symbol}",
                market="TWSE",
            )
        }

    monkeypatch.setattr(company_info, "_build_company_map", fake_build)
    first = company_info.load_company_map(root=root_a, force_refresh=True)
    second = company_info.load_company_map(root=root_b, force_refresh=True)

    assert set(first) == {"1111"}
    assert set(second) == {"2222"}
    assert company_info.load_company_map(root=root_a)["1111"].name == "company-1111"


def test_market_map_cache_isolated_by_root(tmp_path: Path) -> None:
    market_meta._MARKET_MAP_CACHE.clear()
    today = now_tw().date().isoformat()
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    for root, symbol, market in (
        (root_a, "1111", market_meta.TWSE),
        (root_b, "2222", market_meta.TPEX),
    ):
        path = root / "data" / "meta" / "market_map.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"date": today, "map": {symbol: market}}),
            encoding="utf-8",
        )

    assert market_meta.load_market_map(root=root_a) == {"1111": market_meta.TWSE}
    assert market_meta.load_market_map(root=root_b) == {"2222": market_meta.TPEX}


class _Response:
    status_code = 200

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.content = json.dumps(rows).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self):
        return self._rows


class _Session:
    def get(self, url: str, **_kwargs):
        if "STOCK_DAY_AVG_ALL" in url:
            return _Response([{"Code": "2330", "ClosingPrice": "1000"}])
        return _Response([{"SecuritiesCompanyCode": "6488", "CompanyName": "test"}])


def test_market_map_captures_both_raw_responses(tmp_path: Path) -> None:
    market_meta._MARKET_MAP_CACHE.clear()
    result = market_meta._build_market_map(session=_Session(), root=tmp_path)

    assert result == {"2330": market_meta.TWSE, "6488": market_meta.TPEX}
    db = StockDB.open(root=tmp_path)
    try:
        documents = db.list_source_documents(limit=20)
        assert {row.source for row in documents} == {
            "market_meta_twse",
            "market_meta_tpex",
        }
        assert all(row.content_path and row.content_hash for row in documents)
    finally:
        db.close()
