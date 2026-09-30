"""搜尋結果的 raw -> research -> evidence -> claim 垂直切片。

DuckDuckGo 與 Google News 的搜尋結果不是正式新聞全文；這個模組只保存
可由搜尋回應 raw archive 核對的「搜尋結果標題／摘要」事實，不把搜尋摘要
升格成文章內容，也不由 URL、標題或相似度猜測來源文件。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import datetime as dt
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from bot.evidence import make_evidence, persist_evidence_items
from bot.source_registry import resolve_source_document_path
from bot.stock_db import (
    ClaimEvidenceRow,
    ClaimRow,
    LlmRunEvidenceRow,
    LlmRunRow,
    StockDB,
)
from bot.utils import now_tw

PIPELINE = "web_search_result_slice"
INGEST_TASK_TYPE = "web_search_result_ingest"
INGEST_RUN_STATUS = "source_ingested"
SUPPORTED_SOURCES = {"google_news_rss", "duckduckgo_search"}
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class WebSearchSlice:
    source_document_id: str
    research_document_id: str = ""
    run_id: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    claim_ids: List[str] = field(default_factory=list)
    chunk_ids: List[str] = field(default_factory=list)
    status: str = "ok"  # ok / reused / failed / empty
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _text(value: Any) -> str:
    value = html.unescape(str(value or ""))
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", value)).strip()


def _item_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    try:
        return asdict(item)
    except TypeError:
        return {}


def _canonical_item(item: Mapping[str, Any]) -> str:
    title = _text(item.get("title"))
    if not title:
        return ""
    published = _text(item.get("published_at"))
    snippet = _text(item.get("snippet") or item.get("summary"))
    url = _text(item.get("url"))
    line = f"[{published}] {title}" if published else title
    if snippet:
        line += f"\n{snippet}"
    if url:
        line += f"\n{url}"
    return line


def _raw_archive_matches(root: Path, source: Any) -> tuple[bool, str]:
    path = resolve_source_document_path(root, source)
    if not path.is_file():
        return False, f"raw archive 不存在: {source.content_path}"
    try:
        content = path.read_bytes()
    except OSError as exc:
        return False, f"raw archive 無法讀取: {exc}"
    digest = hashlib.sha256(content).hexdigest()
    if source.content_hash and digest != source.content_hash:
        return False, "raw archive hash mismatch"
    return True, content.decode("utf-8", errors="replace")


def _claim_supported(title: str, raw_text: str) -> bool:
    """核對標題是否真的存在 raw 回應；HTML/XML entity 先正規化。"""
    normalized_title = _text(title)
    normalized_raw = _text(raw_text)
    return bool(normalized_title and normalized_title in normalized_raw)


def materialize_web_search_slice(
    *,
    root: Path,
    source_document_id: str,
    items: Iterable[Any],
    db: Optional[StockDB] = None,
) -> WebSearchSlice:
    """由已知搜尋 raw document 建立 research、evidence 與 source-excerpt claims。"""
    project_root = Path(root)
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    try:
        source = database.get_source_document(source_document_id)
        if source is None:
            return WebSearchSlice(
                source_document_id=source_document_id,
                status="failed",
                error=f"unknown source_document_id: {source_document_id}",
            )
        if source.source not in SUPPORTED_SOURCES:
            return WebSearchSlice(
                source_document_id=source_document_id,
                status="failed",
                error=f"unsupported search source: {source.source}",
            )
        valid, raw_text = _raw_archive_matches(project_root, source)
        if not valid:
            return WebSearchSlice(
                source_document_id=source_document_id,
                status="failed",
                error=raw_text,
            )

        item_dicts: List[Dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        for value in items:
            item = _item_dict(value)
            title = _text(item.get("title"))
            if not title:
                continue
            key = (_text(item.get("url")), title)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            item_dicts.append(item)
        if not item_dicts:
            return WebSearchSlice(
                source_document_id=source_document_id,
                status="empty",
                error="搜尋回應沒有可 materialize 的結果",
            )

        canonical_text = "\n\n".join(
            _canonical_item(item) for item in item_dicts
        )
        document_type = (
            "news_search" if source.source == "google_news_rss" else "web_search"
        )
        research_id = "research-doc:" + hashlib.sha256(
            (source_document_id + "|" + canonical_text).encode("utf-8")
        ).hexdigest()[:24]
        run_id = "web-search-slice:" + hashlib.sha256(
            f"{source_document_id}|{research_id}".encode("utf-8")
        ).hexdigest()[:24]

        packets: List[Dict[str, Any]] = []
        claim_specs: List[tuple[str, str, str, bool]] = []
        default_kind = "news" if source.source == "google_news_rss" else "web_search"
        default_source = "google_news" if source.source == "google_news_rss" else "ddg"
        for ordinal, item in enumerate(item_dicts):
            title = _text(item.get("title"))
            snippet = _text(item.get("snippet") or item.get("summary"))
            url = _text(item.get("url"))
            published_at = _text(item.get("published_at"))
            source_ref = url or title
            excerpt = (snippet or title)[:2000]
            packet = make_evidence(
                default_kind,
                source=_text(item.get("source")) or default_source,
                source_ref=source_ref,
                title=title,
                url=url,
                observed_at=published_at or source.retrieved_at or source.as_of,
                published_at=published_at,
                retrieved_at=source.retrieved_at,
                excerpt=excerpt,
                quality_status=(
                    "ok" if snippet and source.quality_status == "ok" else "partial"
                ),
                source_document_id=source_document_id,
                provenance_status="exact",
                metadata={
                    "pipeline": PIPELINE,
                    "search_source": source.source,
                    "query": source.source_ref,
                    "ordinal": ordinal,
                    "site": _text(item.get("site")),
                },
            )
            packets.append(packet)
            supported = _claim_supported(title, raw_text)
            claim_specs.append((
                f"{run_id}:claim:{ordinal}",
                f"搜尋結果標題原文摘錄：{title}",
                str(packet["id"]),
                supported,
            ))

        existing_run = database.get_llm_run(run_id)
        existing_research = database.get_research_document(research_id)
        existing_claims = all(
            database.get_claim(claim_id) is not None
            for claim_id, _text_value, _evidence_id, _supported in claim_specs
        )
        existing_evidence = all(
            database.get_evidence(str(packet["id"])) is not None
            for packet in packets
        )
        if existing_research and existing_run and existing_claims and existing_evidence:
            return WebSearchSlice(
                source_document_id=source_document_id,
                research_document_id=research_id,
                run_id=run_id,
                evidence_ids=[str(packet["id"]) for packet in packets],
                claim_ids=[spec[0] for spec in claim_specs],
                chunk_ids=[
                    chunk.chunk_id
                    for chunk in database.list_research_chunks(research_id)
                ],
                status="reused",
            )

        from bot.source_registry import SourceRegistry

        registry = SourceRegistry(project_root, db=database)
        research = registry.materialize_research_document(
            source_document_id,
            canonical_text,
            document_type=document_type,
            title=source.title or f"搜尋結果 {source.source_ref}",
            published_at=source.published_at or source.as_of,
            metadata={
                "pipeline": PIPELINE,
                "search_source": source.source,
                "query": source.source_ref,
                "item_count": len(item_dicts),
            },
        )
        persist_evidence_items(database, packets)
        now = now_tw().isoformat(timespec="seconds")
        database.upsert_llm_run(LlmRunRow(
            run_id=run_id,
            task_type=INGEST_TASK_TYPE,
            model="",
            prompt_id=PIPELINE,
            prompt_version="1.0",
            started_at=now,
            finished_at=now,
            status=INGEST_RUN_STATUS,
            input_hash=hashlib.sha256(source_document_id.encode("utf-8")).hexdigest(),
            output_hash=hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
            trace_id=source_document_id,
            metadata_json=json.dumps({
                "pipeline": PIPELINE,
                "source_document_id": source_document_id,
                "research_document_id": research.document_id,
                "item_count": len(item_dicts),
                "run_kind": "ingest",
            }, ensure_ascii=False, sort_keys=True),
        ))

        evidence_ids: List[str] = []
        claim_ids: List[str] = []
        for ordinal, (claim_id, claim_text, evidence_id, supported) in enumerate(claim_specs):
            database.link_llm_run_evidence(LlmRunEvidenceRow(
                run_id=run_id,
                evidence_id=evidence_id,
                ordinal=ordinal,
                role="supporting",
            ))
            database.upsert_claim(ClaimRow(
                claim_id=claim_id,
                run_id=run_id,
                text=claim_text,
                claim_type="source_excerpt",
                confidence=1.0 if supported else 0.0,
                status="verified" if supported else "unverified",
                uncertainty="" if supported else "標題無法在搜尋 raw archive 核對",
                created_at=now,
                updated_at=now,
            ))
            database.link_claim_evidence(ClaimEvidenceRow(
                claim_id=claim_id,
                evidence_id=evidence_id,
                ordinal=0,
            ))
            evidence_ids.append(evidence_id)
            claim_ids.append(claim_id)

        return WebSearchSlice(
            source_document_id=source_document_id,
            research_document_id=research.document_id,
            run_id=run_id,
            evidence_ids=evidence_ids,
            claim_ids=claim_ids,
            chunk_ids=[
                chunk.chunk_id
                for chunk in database.list_research_chunks(research.document_id)
            ],
            status="ok",
        )
    except Exception as exc:
        return WebSearchSlice(
            source_document_id=str(source_document_id or ""),
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_db:
            database.close()


def _replay_items_from_raw(
    source: Any,
    raw_text: str,
    *,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """從 exact raw search response 重建 parser 結果，不從 URL 反查。"""
    from bot.web_search import (
        _clean_text,
        _extract_xml_tag,
        _HAS_BS4,
        _site_from_url,
    )

    items: List[Dict[str, Any]] = []
    if source.source == "google_news_rss":
        for raw_item in re.findall(r"<item>(.*?)</item>", raw_text, re.S | re.I):
            title = _clean_text(_extract_xml_tag(raw_item, "title"))
            url = _extract_xml_tag(raw_item, "link").strip()
            if not title or not url:
                continue
            published = _clean_text(_extract_xml_tag(raw_item, "pubDate"))
            try:
                published = dt.datetime.strptime(
                    published, "%a, %d %b %Y %H:%M:%S %Z"
                ).isoformat(timespec="seconds")
            except Exception:
                pass
            items.append({
                "title": title,
                "url": url,
                "snippet": _clean_text(_extract_xml_tag(raw_item, "description")),
                "source": "google_news",
                "site": _clean_text(_extract_xml_tag(raw_item, "source"))
                or _site_from_url(url),
                "published_at": published,
            })
            if len(items) >= limit:
                break
        return items

    if source.source != "duckduckgo_search":
        return items
    if _HAS_BS4:
        from bot.web_search import BeautifulSoup

        soup = BeautifulSoup(raw_text, "html.parser")
        for element in soup.select("div.result"):
            link = element.select_one("a.result__a")
            if link is None:
                continue
            title = link.get_text(" ", strip=True)
            url = str(link.get("href") or "")
            snippet_element = element.select_one(".result__snippet")
            snippet = (
                snippet_element.get_text(" ", strip=True)
                if snippet_element is not None else ""
            )
            if title and url:
                items.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "source": "ddg",
                    "site": _site_from_url(url),
                })
            if len(items) >= limit:
                break
        return items

    for match in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        raw_text,
        re.S,
    ):
        url, title_html, snippet_html = match.groups()
        title = _clean_text(title_html)
        if title and url:
            items.append({
                "title": title,
                "url": url,
                "snippet": _clean_text(snippet_html),
                "source": "ddg",
                "site": _site_from_url(url),
            })
        if len(items) >= limit:
            break
    return items


def replay_web_search_document(
    *,
    root: Path,
    source_document_id: str,
    db: Optional[StockDB] = None,
    limit: int = 100,
) -> WebSearchSlice:
    """以 exact manifest ID 重播一份既有搜尋 raw document。"""
    project_root = Path(root)
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    try:
        source = database.get_source_document(source_document_id)
        if source is None:
            return WebSearchSlice(
                source_document_id=source_document_id,
                status="failed",
                error=f"unknown source_document_id: {source_document_id}",
            )
        valid, raw_text = _raw_archive_matches(project_root, source)
        if not valid:
            return WebSearchSlice(
                source_document_id=source_document_id,
                status="failed",
                error=raw_text,
            )
        items = _replay_items_from_raw(source, raw_text, limit=max(1, int(limit)))
        return materialize_web_search_slice(
            root=project_root,
            source_document_id=source_document_id,
            items=items,
            db=database,
        )
    except Exception as exc:
        return WebSearchSlice(
            source_document_id=source_document_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_db:
            database.close()


def backfill_replayable_web_search(
    *,
    root: Path,
    db: Optional[StockDB] = None,
    limit: int = 100_000,
    items_per_document: int = 100,
) -> Dict[str, Any]:
    """exact-only 回填既有 Google News／DDG raw documents。"""
    project_root = Path(root)
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    report: Dict[str, Any] = {
        "documents": 0,
        "ok": 0,
        "reused": 0,
        "empty": 0,
        "failed": 0,
        "errors": [],
    }
    try:
        documents: List[Any] = []
        for source_name in sorted(SUPPORTED_SOURCES):
            documents.extend(database.list_source_documents(
                source=source_name,
                kind="search_response",
                limit=limit,
            ))
        for source in documents[: max(0, int(limit))]:
            report["documents"] += 1
            result = replay_web_search_document(
                root=project_root,
                source_document_id=source.document_id,
                db=database,
                limit=items_per_document,
            )
            status = result.status
            if status in {"ok", "reused", "empty", "failed"}:
                report[status] += 1
            if result.error:
                report["errors"].append({
                    "source_document_id": source.document_id,
                    "error": result.error,
                })
        return report
    finally:
        if owns_db:
            database.close()


__all__ = [
    "INGEST_RUN_STATUS",
    "INGEST_TASK_TYPE",
    "PIPELINE",
    "SUPPORTED_SOURCES",
    "WebSearchSlice",
    "backfill_replayable_web_search",
    "materialize_web_search_slice",
    "replay_web_search_document",
]
