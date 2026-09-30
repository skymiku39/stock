"""MOPS 法說會簡報的 raw -> research -> evidence -> claim 切片。

PDF/PPTX 本身是 immutable raw source；文字抽取是 deterministic/derived
結果。這裡只接受 exact ``source_document_id``，並將抽取方法、頁數與原始
來源一起保存。抽取文字的 claim 只描述可重播的原文摘錄，不自動產生市場
結論或投資建議。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.evidence import make_evidence, persist_evidence_items
from bot.mops_presentation_validator import validate_mops_presentation
from bot.source_registry import (
    SourceRegistry,
    is_source_document_replayable,
    resolve_source_document_path,
)
from bot.stock_db import (
    ClaimEvidenceRow,
    ClaimRow,
    LlmRunEvidenceRow,
    LlmRunRow,
    StockDB,
)
from bot.utils import now_tw


PIPELINE = "mops_presentation_vertical_slice"
_WHITESPACE_RE = re.compile(r"\s+")


def _read_source_bytes(root: Path, source: Any) -> bytes:
    path = resolve_source_document_path(root, source)
    if not path.is_file():
        return b""
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _assert_source_replayable(root: Path, source: Any, source_document_id: str) -> str:
    """切片前確認 exact raw 仍可回放；失敗時回傳錯誤字串，空字串＝通過。"""
    if source is None:
        return f"unknown source_document_id: {source_document_id}"
    if is_source_document_replayable(root, source):
        return ""
    path = resolve_source_document_path(root, source)
    if not path.is_file():
        return f"raw archive missing: {source.content_path}"
    if source.content_hash:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != source.content_hash:
            return (
                f"raw hash mismatch: expected {source.content_hash[:12]}…"
                f" got {digest[:12]}…"
            )
    return f"raw archive 無法回放: {source_document_id}"


def _source_metadata(source: Any) -> Dict[str, Any]:
    try:
        value = json.loads(str(getattr(source, "metadata_json", "") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


@dataclass
class MopsPresentationSlice:
    source_document_id: str
    symbol: str = ""
    conference_date: str = ""
    research_document_id: str = ""
    evidence_id: str = ""
    claim_id: str = ""
    run_id: str = ""
    canonical_text: str = ""
    chunk_ids: List[str] = field(default_factory=list)
    status: str = "ok"  # ok / reused / failed / empty
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _normal(value: Any) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def _claim_parts(text: str, *, max_chars: int = 240) -> tuple[str, str]:
    snippet = _normal(text)[:max_chars].strip()
    return (
        f"MOPS 法說會簡報原文摘錄：{snippet}" if snippet else "",
        snippet,
    )


def _merge_metadata(raw: Any, updates: Dict[str, Any]) -> str:
    try:
        metadata = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    for key, value in updates.items():
        if value not in ("", None, 0, []):
            metadata[key] = value
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)


def materialize_mops_presentation_slice(
    *,
    root: Path,
    source_document_id: str,
    canonical_text: str,
    ticker: str = "",
    conference_date: str = "",
    title: str = "",
    pages: int = 0,
    extraction_method: str = "pypdf_or_text",
    calendar_conference_date: str = "",
    db: Optional[StockDB] = None,
) -> MopsPresentationSlice:
    """由 exact raw manifest 建立法說簡報的研究鏈。"""
    source_id = str(source_document_id or "").strip()
    text = str(canonical_text or "").strip()
    base = MopsPresentationSlice(
        source_document_id=source_id,
        symbol=str(ticker or ""),
        conference_date=str(conference_date or ""),
    )
    owns_db = db is None
    database = db or StockDB.open(root=Path(root))
    try:
        if not source_id.startswith("source-doc:"):
            base.status = "failed"
            base.error = "法說簡報缺少 exact source_document_id"
            return base
        source = database.get_source_document(source_id)
        if source is None:
            base.status = "failed"
            base.error = f"unknown source_document_id: {source_id}"
            return base

        replay_error = _assert_source_replayable(Path(root), source, source_id)
        if replay_error:
            base.status = "failed"
            base.error = replay_error
            return base

        payload = _read_source_bytes(Path(root), source)
        metadata = _source_metadata(source)
        calendar_date = str(
            calendar_conference_date
            or metadata.get("conference_date")
            or metadata.get("calendar_conference_date")
            or ""
        )
        validation = validate_mops_presentation(
            content=payload,
            content_path=str(source.content_path or ""),
            mime_type=str(source.mime_type or ""),
            claimed_conference_date=str(conference_date or ""),
            source_as_of=str(source.as_of or ""),
            calendar_conference_date=calendar_date,
            page_count=pages if pages else None,
            source_quality_status=str(source.quality_status or ""),
        )
        if not validation.usable:
            base.status = "failed"
            base.error = "; ".join(validation.errors) or (
                "raw source 無法通過簡報驗證，拒絕建立簡報切片"
            )
            return base

        accepted_conference_date = validation.conference_date
        base.conference_date = accepted_conference_date
        resolved_pages = (
            validation.page_count
            if validation.page_count is not None
            else max(0, int(pages or 0))
        )

        if not text:
            base.status = "empty"
            base.error = "法說簡報抽取文字為空"
            return base

        research_id = "research-doc:" + hashlib.sha256(
            (source_id + "|" + text).encode("utf-8")
        ).hexdigest()[:24]
        run_id = "mops-presentation-slice:" + hashlib.sha256(
            (source_id + "|" + research_id).encode("utf-8")
        ).hexdigest()[:24]
        claim_id = f"{run_id}:claim:0"
        claim_text, snippet = _claim_parts(text)
        evidence = make_evidence(
            "mops_presentation",
            source="mops",
            source_ref=str(source.source_ref or source_id),
            title=title or source.title or "MOPS 法說會簡報",
            url=str(source.url or ""),
            observed_at=str(
                accepted_conference_date or source.as_of or source.retrieved_at
            ),
            as_of=str(accepted_conference_date or source.as_of),
            published_at=str(source.published_at or source.as_of),
            retrieved_at=str(source.retrieved_at or ""),
            excerpt=text[:2000],
            quality_status=validation.quality_status,
            source_document_id=source_id,
            provenance_status="exact",
            metadata={
                "pipeline": PIPELINE,
                "research_document_id": research_id,
                "ticker": str(ticker or ""),
                "conference_date": accepted_conference_date,
                "conference_date_aligned": validation.conference_date_aligned,
                "pages": max(0, int(resolved_pages or 0)),
                "extraction_method": extraction_method,
                "validation": {
                    "quality_status": validation.quality_status,
                    "errors": list(validation.errors),
                    "warnings": list(validation.warnings),
                    "content_kind": validation.content_kind,
                    "is_pdf": validation.is_pdf,
                    "page_count": validation.page_count,
                },
            },
        )
        existing = (
            database.get_research_document(research_id) is not None
            and database.get_llm_run(run_id) is not None
            and database.get_evidence(str(evidence["id"])) is not None
            and database.get_claim(claim_id) is not None
        )
        if existing:
            chunks = database.list_research_chunks(research_id)
            existing_research = database.get_research_document(research_id)
            if existing_research is not None:
                existing_research.metadata_json = _merge_metadata(
                    existing_research.metadata_json,
                    {
                        "pipeline": PIPELINE,
                        "ticker": str(ticker or ""),
                        "conference_date": accepted_conference_date,
                        "conference_date_aligned": validation.conference_date_aligned,
                        "pages": max(0, int(resolved_pages or 0)),
                        "extraction_method": extraction_method,
                    },
                )
                existing_research.updated_at = now_tw().isoformat(timespec="seconds")
                database.upsert_research_document(existing_research)
            existing_evidence = database.get_evidence(str(evidence["id"]))
            if existing_evidence is not None:
                existing_evidence.observed_at = str(evidence.get("observed_at") or existing_evidence.observed_at)
                existing_evidence.as_of = str(evidence.get("as_of") or existing_evidence.as_of)
                existing_evidence.published_at = str(evidence.get("published_at") or existing_evidence.published_at)
                existing_evidence.retrieved_at = str(evidence.get("retrieved_at") or existing_evidence.retrieved_at)
                existing_evidence.quality_status = str(
                    evidence.get("quality_status") or existing_evidence.quality_status
                )
                existing_evidence.metadata_json = json.dumps(
                    dict(evidence.get("metadata") or {}),
                    ensure_ascii=False, sort_keys=True, default=str,
                )
                existing_evidence.updated_at = now_tw().isoformat(timespec="seconds")
                database.upsert_evidence(existing_evidence)
            return MopsPresentationSlice(
                source_document_id=source_id,
                symbol=str(ticker or ""),
                conference_date=accepted_conference_date,
                research_document_id=research_id,
                evidence_id=str(evidence["id"]),
                claim_id=claim_id,
                run_id=run_id,
                canonical_text=text,
                chunk_ids=[chunk.chunk_id for chunk in chunks],
                status="reused",
            )

        research = SourceRegistry(Path(root), db=database).materialize_research_document(
            source_id,
            text,
            document_type="mops_presentation",
            symbol=str(ticker or ""),
            title=title or source.title or "MOPS 法說會簡報",
            published_at=str(source.published_at or source.as_of),
            metadata={
                "pipeline": PIPELINE,
                "ticker": str(ticker or ""),
                "conference_date": accepted_conference_date,
                "conference_date_aligned": validation.conference_date_aligned,
                "pages": max(0, int(resolved_pages or 0)),
                "extraction_method": extraction_method,
            },
        )
        persist_evidence_items(database, [evidence])
        now = now_tw().isoformat(timespec="seconds")
        database.upsert_llm_run(LlmRunRow(
            run_id=run_id,
            task_type="mops_presentation_ingest",
            model="",
            prompt_id=PIPELINE,
            prompt_version="1.0",
            started_at=now,
            finished_at=now,
            status="source_ingested",
            input_hash=hashlib.sha256(source_id.encode("utf-8")).hexdigest(),
            output_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            trace_id=source_id,
            metadata_json=json.dumps({
                "pipeline": PIPELINE,
                "source_document_id": source_id,
                "research_document_id": research.document_id,
                "run_kind": "ingest",
            }, ensure_ascii=False, sort_keys=True),
        ))
        evidence_id = str(evidence["id"])
        database.link_llm_run_evidence(LlmRunEvidenceRow(
            run_id=run_id,
            evidence_id=evidence_id,
            ordinal=0,
            role="supporting",
        ))
        supported = bool(snippet and _normal(snippet) in _normal(text))
        database.upsert_claim(ClaimRow(
            claim_id=claim_id,
            run_id=run_id,
            text=claim_text or "MOPS 法說會簡報抽取文字已登錄",
            claim_type="source_excerpt",
            confidence=1.0 if supported else 0.0,
            status="verified" if supported else "unverified",
            uncertainty="" if supported else "簡報摘錄無法核對",
            created_at=now,
            updated_at=now,
        ))
        database.link_claim_evidence(ClaimEvidenceRow(
            claim_id=claim_id,
            evidence_id=evidence_id,
            ordinal=0,
        ))
        return MopsPresentationSlice(
            source_document_id=source_id,
            symbol=str(ticker or ""),
            conference_date=accepted_conference_date,
            research_document_id=research.document_id,
            evidence_id=evidence_id,
            claim_id=claim_id,
            run_id=run_id,
            canonical_text=text,
            chunk_ids=[
                chunk.chunk_id
                for chunk in database.list_research_chunks(research.document_id)
            ],
            status="ok",
        )
    except Exception as exc:  # noqa: BLE001
        base.status = "failed"
        base.error = f"{type(exc).__name__}: {exc}"
        return base
    finally:
        if owns_db:
            database.close()


__all__ = [
    "MopsPresentationSlice",
    "PIPELINE",
    "materialize_mops_presentation_slice",
]
