from __future__ import annotations

import datetime as dt
import json
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

from bot.global_events_fetcher import (
    APPLE_NEWSROOM_RSS,
    fetch_apple_newsroom_events,
    fetch_corporate_official_pages,
)
from bot.chip_distribution import fetch_distribution_all
from bot.hot_stock_futures import fetch_hot_stock_futures
from bot.market_movers import fetch_market_movers
from bot.mops_scraper import (
    MOPS_CONF_URL,
    download_file,
    fetch_conference_schedule,
    fetch_material_detail,
)
from bot.market_source import TwsePublicMarketSource
from bot.stock_db import StockDB


class _Response:
    def __init__(self, content: bytes, *, status_code: int = 200, headers=None):
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        import json

        return json.loads(self.text)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, _chunk_size: int):
        yield self.content


class _MopsSession:
    def __init__(self, html: bytes):
        self.html = html

    def post(self, url, **_kwargs):
        assert url == MOPS_CONF_URL
        return _Response(self.html)

    def get(self, url, **_kwargs):
        return _Response(self.html)


def test_mops_raw_responses_are_registered_without_changing_parser(tmp_path: Path):
    html = (
        b"<table><tr><td>115/09/17</td><td>10:00</td><td>2330</td>"
        b"<td>Test Co</td><td>Investor meeting</td></tr></table>"
    )
    session = _MopsSession(html)

    entries = fetch_conference_schedule(115, 9, session=session, root=tmp_path)
    assert len(entries) == 1
    assert entries[0].ticker == "2330"

    detail = fetch_material_detail(
        "https://example.test/detail", session=session, root=tmp_path,
    )
    assert "Investor meeting" in detail

    db = StockDB.open(root=tmp_path)
    docs = db.list_source_documents(limit=20)
    assert {d.source for d in docs} >= {
        "mops_conference_schedule",
        "mops_material_detail",
    }
    for doc in docs:
        assert (tmp_path / doc.content_path).read_bytes() == html


def test_mops_download_preserves_exact_stream_bytes(tmp_path: Path):
    payload = b"presentation-bytes-\x00\x01"

    class _DownloadSession:
        def get(self, _url, **_kwargs):
            return _Response(payload, headers={"Content-Type": "application/pdf"})

    dest = tmp_path / "data" / "mops_downloads" / "2330.pdf"
    result = download_file(
        "https://example.test/2330.pdf",
        dest,
        session=_DownloadSession(),
        root=tmp_path,
    )
    assert result == dest
    assert dest.read_bytes() == payload
    docs = StockDB.open(root=tmp_path).list_source_documents(
        source="mops_presentation",
    )
    assert len(docs) == 1
    assert (tmp_path / docs[0].content_path).read_bytes() == payload


def test_global_official_responses_are_registered(tmp_path: Path):
    published = format_datetime(dt.datetime.now(dt.timezone.utc))
    rss = (
        "<rss><channel><item>"
        "<title>WWDC Apple AI event</title>"
        f"<link>https://example.test/news</link><pubDate>{published}</pubDate>"
        "</item></channel></rss>"
    ).encode()
    official = b"<html><body>Event date 2026-10-01</body></html>"

    class _GlobalSession:
        headers = {}

        def get(self, url, **_kwargs):
            if url == APPLE_NEWSROOM_RSS:
                return _Response(rss, headers={"Content-Type": "application/rss+xml"})
            return _Response(official, headers={"Content-Type": "text/html"})

    rss_rows = fetch_apple_newsroom_events(
        root=tmp_path, session=_GlobalSession(), lookback_hours=48,
    )
    page_rows = fetch_corporate_official_pages(
        root=tmp_path, session=_GlobalSession(),
    )
    assert rss_rows
    assert page_rows

    db = StockDB.open(root=tmp_path)
    sources = {d.source for d in db.list_source_documents(limit=100)}
    assert "apple_newsroom_rss" in sources
    assert "corporate_official_page" in sources


def test_twse_mis_raw_response_is_saved_only_with_explicit_root(tmp_path: Path):
    payload = {
        "msgArray": [{
            "c": "2330", "n": "Test", "z": "100", "y": "99",
            "v": "10", "ex": "tse", "d": "20260917", "t": "10:00:00",
        }],
    }
    raw = json.dumps(payload, ensure_ascii=False).encode()

    class _MisSession:
        headers = {}

        def get(self, _url, **_kwargs):
            return _Response(raw, headers={"Content-Type": "application/json"})

    source = TwsePublicMarketSource(["2330"], root=tmp_path)
    source._session = _MisSession()
    source._initialized = True
    rows = source._fetch_raw(["2330"])
    assert rows and rows[0]["c"] == "2330"

    docs = StockDB.open(root=tmp_path).list_source_documents(
        source="twse_mis_quote",
    )
    assert len(docs) == 1
    assert (tmp_path / docs[0].content_path).read_bytes() == raw


def test_market_movers_and_tdcc_preserve_api_payloads(tmp_path: Path, monkeypatch):
    mis_payload = {
        "msgArray": [{
            "c": "2330", "n": "Test", "z": "100", "y": "99",
            "v": "10", "ex": "tse", "d": "20260917", "t": "10:00:00",
        }],
    }
    mis_raw = json.dumps(mis_payload).encode()
    tdcc_payload = [{
        "資料日期": "20260912", "證券代號": "2330", "持股分級": "1",
        "人數": "10", "股數": "100", "占集保庫存數比例%": "1.0",
    }]
    tdcc_raw = json.dumps(tdcc_payload, ensure_ascii=False).encode()

    class _MarketSession:
        headers = {}

        def __init__(self):
            self.responses = [
                _Response(b"<html>session</html>"),
                _Response(mis_raw, headers={"Content-Type": "application/json"}),
            ]

        def get(self, _url, **_kwargs):
            return self.responses.pop(0)

    monkeypatch.setattr(
        "bot.market_movers.load_market_map",
        lambda root=None: {"2330": "twse"},
    )
    with patch(
        "bot.market_movers.now_tw",
        return_value=dt.datetime(2026, 9, 17, 10, 0),
    ):
        movers = fetch_market_movers(
            root=tmp_path,
            session=_MarketSession(),
            batch_size=10,
            batch_pause_sec=0,
        )
    assert movers.rows and movers.rows[0].ticker == "2330"

    class _TdccSession:
        headers = {}

        def get(self, _url, **_kwargs):
            return _Response(tdcc_raw, headers={"Content-Type": "application/json"})

    rows = fetch_distribution_all(
        root=tmp_path, session=_TdccSession(), use_cache=False,
    )
    assert rows == tdcc_payload

    db = StockDB.open(root=tmp_path)
    sources = {d.source for d in db.list_source_documents(limit=100)}
    assert "twse_mis_market_movers" in sources
    assert "tdcc_distribution" in sources


def test_taifex_hot_futures_preserves_api_payload(tmp_path: Path):
    payload = {
        "RtCode": "0",
        "RtData": {
            "QuoteCount": "1",
            "QuoteList": [{
                "SymbolID": "2330F", "SpotID": "2330", "DispCName": "Test",
                "CLastPrice": "100", "CRefPrice": "99", "CTotalVolume": "3",
                "CDiffRate": "1.01", "CTime": "100000", "Status": "N",
            }],
        },
    }
    raw = json.dumps(payload).encode()

    class _TaifexSession:
        headers = {}

        def post(self, _url, **_kwargs):
            return _Response(raw, headers={"Content-Type": "application/json"})

    result = fetch_hot_stock_futures(
        root=tmp_path, session=_TaifexSession(), direction="gainers",
    )
    assert result.fetched == 1
    docs = StockDB.open(root=tmp_path).list_source_documents(
        source="taifex_hot_stock_futures",
    )
    assert len(docs) == 1
    assert (tmp_path / docs[0].content_path).read_bytes() == raw
