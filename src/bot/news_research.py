"""新聞快取的 research／evidence／claim 垂直切片。

新聞日快取是一份 raw source document，單一新聞列是該文件中的可核對
derived evidence。這裡只建立可由原文回放的 source-excerpt claims，不把
新聞標題延伸成市場結論，也不依 URL／標題猜測來源。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from bot.evidence import make_evidence, persist_evidence_items
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.stock_db import (
    ClaimEvidenceRow,
    ClaimRow,
    LlmRunEvidenceRow,
    LlmRunRow,
    StockDB,
)
from bot.utils import now_tw

PIPELINE = "news_official_cache_slice"
INGEST_TASK_TYPE = "news_cache_ingest"
INGEST_RUN_STATUS = "source_ingested"
_WS_RE = re.compile(r"\s+")


@dataclass
class NewsCacheSlice:
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


def normalize_news_text(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def _as_item_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    try:
        return asdict(item)
    except TypeError:
        return {}


def _canonical_item_text(item: Mapping[str, Any]) -> str:
    title = normalize_news_text(item.get("title"))
    summary = normalize_news_text(item.get("summary"))
    published = normalize_news_text(item.get("published_at"))
    if not title:
        return ""
    line = f"[{published}] {title}" if published else title
    return f"{line}\n{summary}" if summary else line


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
    return bool(title) and normalize_news_text(title) in normalize_news_text(raw_text)


def materialize_news_cache_slice(
    *,
    root: Path,
    source_document_id: str,
    items: Iterable[Any],
    db: Optional[StockDB] = None,
) -> NewsCacheSlice:
    """由已知新聞 raw document 建立 research、evidence 與 claims。"""
    project_root = Path(root)
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    try:
        source = database.get_source_document(source_document_id)
        if source is None:
            return NewsCacheSlice(
                source_document_id=source_document_id,
                status="failed",
                error=f"unknown source_document_id: {source_document_id}",
            )
        valid, raw_text = _raw_archive_matches(project_root, source)
        if not valid:
            return NewsCacheSlice(
                source_document_id=source_document_id,
                status="failed",
                error=raw_text,
            )
        item_dicts = [
            item for item in (_as_item_dict(value) for value in items)
            if _canonical_item_text(item)
        ]
        if not item_dicts:
            return NewsCacheSlice(
                source_document_id=source_document_id,
                status="empty",
                error="新聞快取沒有可 materialize 的標題",
            )

        canonical_text = "\n\n".join(
            _canonical_item_text(item) for item in item_dicts
        )
        research_id = "research-doc:" + hashlib.sha256(
            (source_document_id + "|" + canonical_text).encode("utf-8")
        ).hexdigest()[:24]
        run_id = "news-slice:" + hashlib.sha256(
            f"{source_document_id}|{research_id}".encode("utf-8")
        ).hexdigest()[:24]
        evidence_packets: List[Dict[str, Any]] = []
        claim_specs: List[tuple[str, str, str, bool]] = []
        for ordinal, item in enumerate(item_dicts):
            title = normalize_news_text(item.get("title"))
            summary = normalize_news_text(item.get("summary"))
            news_id = normalize_news_text(item.get("news_id") or item.get("id"))
            source_ref = news_id or normalize_news_text(item.get("url")) or title
            excerpt = (summary or title)[:2000]
            packet = make_evidence(
                "news",
                source=normalize_news_text(item.get("source")) or "cnyes",
                source_ref=source_ref,
                title=title,
                url=normalize_news_text(item.get("url")),
                observed_at=normalize_news_text(item.get("published_at")) or source.as_of,
                published_at=normalize_news_text(item.get("published_at")),
                retrieved_at=source.retrieved_at,
                excerpt=excerpt,
                quality_status=source.quality_status or "ok",
                source_document_id=source_document_id,
                provenance_status="exact",
                metadata={
                    "pipeline": PIPELINE,
                    "news_id": news_id,
                    "ordinal": ordinal,
                    "category": normalize_news_text(item.get("category")),
                    "related_tickers": list(item.get("related_tickers") or []),
                },
            )
            evidence_packets.append(packet)
            claim_specs.append((
                f"{run_id}:claim:{ordinal}",
                f"新聞標題原文摘錄：{title}",
                str(packet["id"]),
                _claim_supported(title, raw_text),
            ))

        existing_run = database.get_llm_run(run_id)
        existing_research = database.get_research_document(research_id)
        existing_claims = all(
            database.get_claim(claim_id) is not None
            for claim_id, _text, _evidence_id, _supported in claim_specs
        )
        existing_evidence = all(
            database.get_evidence(str(packet["id"])) is not None
            for packet in evidence_packets
        )
        if existing_research and existing_run and existing_claims and existing_evidence:
            return NewsCacheSlice(
                source_document_id=source_document_id,
                research_document_id=research_id,
                run_id=run_id,
                evidence_ids=[str(packet["id"]) for packet in evidence_packets],
                claim_ids=[spec[0] for spec in claim_specs],
                chunk_ids=[
                    chunk.chunk_id
                    for chunk in database.list_research_chunks(research_id)
                ],
                status="reused",
            )

        registry = SourceRegistry(project_root, db=database)
        research = registry.materialize_research_document(
            source_document_id,
            canonical_text,
            document_type="news_cache",
            title=source.title or f"新聞快取 {source.as_of}",
            published_at=source.published_at or source.as_of,
            metadata={
                "pipeline": PIPELINE,
                "item_count": len(item_dicts),
            },
        )
        persist_evidence_items(database, evidence_packets)
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
        claim_ids: List[str] = []
        evidence_ids: List[str] = []
        for ordinal, (claim_id, claim_text, evidence_id, supported) in enumerate(claim_specs):
            database.link_llm_run_evidence(LlmRunEvidenceRow(
                run_id=run_id,
                evidence_id=evidence_id,
                ordinal=ordinal,
                role="supporting",
            ))
            status = "verified" if supported else "unverified"
            database.upsert_claim(ClaimRow(
                claim_id=claim_id,
                run_id=run_id,
                text=claim_text,
                claim_type="source_excerpt",
                confidence=1.0 if supported else 0.0,
                status=status,
                uncertainty="" if supported else "新聞標題無法在 raw archive 核對",
                created_at=now,
                updated_at=now,
            ))
            database.link_claim_evidence(ClaimEvidenceRow(
                claim_id=claim_id,
                evidence_id=evidence_id,
                ordinal=0,
            ))
            claim_ids.append(claim_id)
            evidence_ids.append(evidence_id)
        return NewsCacheSlice(
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
        return NewsCacheSlice(
            source_document_id=str(source_document_id or ""),
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_db:
            database.close()


__all__ = [
    "INGEST_RUN_STATUS",
    "INGEST_TASK_TYPE",
    "NewsCacheSlice",
    "materialize_news_cache_slice",
    "normalize_news_text",
]
