from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bot.query_plan import plan_query
from bot.resource_adapters import ProfessionalKnowledgeAdapter
from bot.source_registry import SourceRegistry
from bot.web_search import PageContent, SearchResult


def test_professional_adapter_prefers_allowlisted_page_text(tmp_path: Path) -> None:
    plan = plan_query("台積電 官方文件 專業研究", ticker="2330")
    with SourceRegistry(tmp_path) as registry:
        source_document = registry.capture_document(
            "web_page",
            "這是可供核對的官方原文內容。".encode("utf-8"),
            source_url="https://www.twse.com.tw/example",
            source_ref="https://www.twse.com.tw/example",
            suffix="html",
        )
    search_rows = [
        SearchResult(
            title="TWSE official material",
            url="https://www.twse.com.tw/example",
            snippet="官方摘要",
            source="ddg",
        ),
        SearchResult(
            title="Generic commentary",
            url="https://example.com/commentary",
            snippet="不應成為專業證據",
            source="ddg",
        ),
    ]
    page = PageContent(
        url="https://www.twse.com.tw/example",
        title="TWSE official material",
        text="這是可供核對的官方原文內容。",
        fetched_at="2026-09-20T03:00:00+08:00",
        source_document_id=source_document.document_id,
    )
    with (
        patch("bot.web_search.search_web", return_value=search_rows),
        patch("bot.web_search.fetch_page_text", return_value=page) as fetch,
    ):
        result = ProfessionalKnowledgeAdapter(root=tmp_path).collect(plan)

    assert result.status == "ok"
    assert len(result.evidence) == 1
    item = result.evidence[0]
    assert item["kind"] == "professional_knowledge"
    assert item["source_document_id"] == source_document.document_id
    assert item["provenance_status"] == "exact"
    assert item["quality_status"] == "ok"
    assert item["metadata"]["authority_domain"] == "twse.com.tw"
    assert item["metadata"]["evidence_mode"] == "page_text"
    fetch.assert_called_once()
    fetch_args, fetch_kwargs = fetch.call_args
    assert fetch_args == ("https://www.twse.com.tw/example",)
    assert fetch_kwargs["max_chars"] == 3000
    assert 1 <= fetch_kwargs["timeout"] <= 8
    assert fetch_kwargs["root"] == tmp_path


def test_professional_adapter_rejects_generic_search_results(tmp_path: Path) -> None:
    plan = plan_query("台積電 專業研究", ticker="2330")
    rows = [
        SearchResult(
            title="Unverified commentary",
            url="https://example.com/commentary",
            snippet="一般網路摘要",
            source="ddg",
        ),
    ]
    with patch("bot.web_search.search_web", return_value=rows):
        result = ProfessionalKnowledgeAdapter(root=tmp_path).collect(plan)

    assert result.status == "partial"
    assert result.evidence == []
    assert "一般搜尋結果不列為專業證據" in result.warnings[0]
