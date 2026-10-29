from __future__ import annotations

from pathlib import Path
import pytest

from bot.stock_db import StockDB
from bot.web_search import fetch_page_text, search_news, search_web
from bot.web_search import SearchUnavailable, _ddg_destination


class _Response:
    def __init__(self, content: bytes, *, status_code: int = 200):
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status_code
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.apparent_encoding = "utf-8"
        self.encoding = "utf-8"


class _SearchSession:
    headers = {}

    def post(self, _url, **_kwargs):
        return _Response(
            b'<div class="result">'
            b'<a class="result__a" href="https://example.test/result">'
            b'Test result</a>'
            b'<a class="result__snippet">A sufficiently long search snippet.</a>'
            b"</div>"
        )

    def get(self, url, **_kwargs):
        if "news.google.com" in url:
            return _Response(
                b"<rss><channel><item>"
                b"<title>Test news</title>"
                b"<link>https://example.test/news</link>"
                b"<pubDate>Wed, 16 Sep 2026 12:00:00 GMT</pubDate>"
                b"<description>News description</description>"
                b"</item></channel></rss>"
            )
        return _Response(
            b"<html><head><title>Example page</title></head><body>"
            b"<main><p>This is a sufficiently long source paragraph for replay.</p>"
            b"</main></body></html>"
        )


def test_search_and_page_raw_responses_are_replayable(tmp_path: Path):
    session = _SearchSession()
    assert search_web("test query", session=session, root=tmp_path)
    assert search_news("test query", session=session, root=tmp_path)
    page = fetch_page_text(
        "https://example.test/page", session=session, root=tmp_path,
    )
    assert "source paragraph" in page.text
    assert page.source_document_id.startswith("source-doc:web_page:")

    db = StockDB.open(root=tmp_path)
    sources = {d.source for d in db.list_source_documents(limit=100)}
    assert {"duckduckgo_search", "google_news_rss", "web_page"} <= sources
    research_types = {
        row.document_type
        for row in db.list_research_documents(limit=100)
    }
    assert {"web_search", "news_search"} <= research_types
    assert db.fetch_all_rows("claims")


def test_blocked_search_is_not_reported_as_no_results():
    class Blocked(_SearchSession):
        def post(self, *args, **kwargs):
            return _Response(b'<form id="challenge-form"></form>', status_code=202)
    with pytest.raises(SearchUnavailable, match="驗證頁"):
        search_web("test", session=Blocked(), strict=True)
    assert search_web("test", session=Blocked()) == []


def test_ddg_redirects_resolve_to_source_and_reject_non_web_urls():
    assert _ddg_destination('//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.test%2Freport&amp;rut=x') == 'https://example.test/report'
    assert _ddg_destination('/l/?uddg=https%3A%2F%2Fexample.test%2Fa') == 'https://example.test/a'
    assert _ddg_destination('javascript:alert(1)') == ''
