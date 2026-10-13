"""MOPS 官方重大訊息詳情的 research／evidence／claim 垂直切片。

資料流（單一官方來源 ``mops_material_detail``）：

``raw archive → source_documents → research_documents/chunks
→ evidence_items → llm_runs → claims``

此模組不做來源猜測：``source_document_id`` 必須是呼叫端已知的 exact ID。
不呼叫 LLM、不下單、不改新聞／基本面／macro 流程。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.evidence import make_evidence, persist_evidence_items
from bot.source_registry import SourceRegistry, is_source_document_replayable
from bot.stock_db import (
    ClaimEvidenceRow,
    ClaimRow,
    LlmRunEvidenceRow,
    LlmRunRow,
    StockDB,
)
from bot.utils import now_tw

_WHITESPACE_RE = re.compile(r"\s+")

# ingest run 與 !ask answer run 的 status 必須區分：
# - ingest: source_ingested（資料登錄完成，非 LLM 回答驗證）
# - ask answer: verified（見 ask._persist_verified_answer）
INGEST_RUN_STATUS = "source_ingested"
INGEST_TASK_TYPE = "mops_material_detail_ingest"
SLICE_STATUSES = {"ok", "reused", "failed"}


@dataclass
class MopsOfficialSlice:
    """一次 MOPS 詳情垂直切片的可追蹤結果。"""

    source_document_id: str
    research_document_id: str = ""
    evidence_id: str = ""
    claim_id: str = ""
    run_id: str = ""
    canonical_text: str = ""
    chunk_ids: List[str] = field(default_factory=list)
    provenance_status: str = "exact"
    # ok=新建或寫入成功；reused=重跑冪等命中既有列；failed=失敗
    status: str = "ok"
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_claim_whitespace(value: str) -> str:
    """將換行、Tab、連續空白正規化後再做原文核對。"""
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def _extractive_claim_parts(
    canonical_text: str, *, max_chars: int = 180,
) -> tuple[str, str]:
    """回傳 (claim 全文, 用於核對的 snippet)。"""
    cleaned = normalize_claim_whitespace(canonical_text)
    if not cleaned:
        return "", ""
    snippet = cleaned[:max_chars].strip()
    return f"MOPS 重大訊息原文摘錄：{snippet}", snippet


def claim_snippet_supported(snippet: str, canonical_text: str) -> bool:
    """空白差異不影響核對；不做語意推論。"""
    needle = normalize_claim_whitespace(snippet)
    haystack = normalize_claim_whitespace(canonical_text)
    return bool(needle) and needle in haystack


def materialize_mops_detail_slice(
    *,
    root: Path,
    source_document_id: str,
    canonical_text: str,
    detail_url: str = "",
    ticker: str = "",
    title: str = "",
    db: Optional[StockDB] = None,
) -> MopsOfficialSlice:
    """由已知 raw ``source_document_id`` 建立 research／evidence／claim。

    ``source_document_id`` 必須已存在於 ``source_documents``；本函式不會依
    URL、標題或文字相似度猜測來源。同一輸入重跑為冪等（相同 ID，status=reused）。
    """
    project_root = Path(root)
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    try:
        source = database.get_source_document(source_document_id)
        if source is None:
            error = f"unknown source_document_id: {source_document_id}"
        elif not is_source_document_replayable(project_root, source):
            error = (
                f"source_document_id 無法回放 raw archive: "
                f"{source_document_id}"
            )
        else:
            error = ""
        if error:
            return MopsOfficialSlice(
                source_document_id=source_document_id,
                status="failed",
                error=error,
            )
        text = str(canonical_text or "").strip()
        if not text:
            return MopsOfficialSlice(
                source_document_id=source_document_id,
                status="failed",
                error="canonical_text 不可為空",
            )

        research_document_id = "research-doc:" + hashlib.sha256(
            (source_document_id + "|" + text).encode("utf-8")
        ).hexdigest()[:24]
        run_id = "mops-slice:" + hashlib.sha256(
            f"{source_document_id}|{research_document_id}".encode("utf-8")
        ).hexdigest()[:24]
        claim_id = f"{run_id}:claim:0"
        existing_research = database.get_research_document(research_document_id)
        if (
            existing_research is not None
            and database.get_llm_run(run_id) is not None
            and database.get_claim(claim_id) is not None
        ):
            chunks = database.list_research_chunks(research_document_id)
            excerpt = (chunks[0].text if chunks else text)[:2000]
            evidence_packet = make_evidence(
                "mops_material",
                source="mops",
                source_ref=detail_url or source.source_ref or source_document_id,
                title=existing_research.title or "MOPS 重大訊息詳情",
                url=detail_url or source.url,
                observed_at=source.as_of or source.retrieved_at,
                published_at=source.published_at or source.as_of,
                retrieved_at=source.retrieved_at,
                excerpt=excerpt,
                quality_status=source.quality_status or "ok",
                source_document_id=source_document_id,
                provenance_status="exact",
                metadata={
                    "research_document_id": research_document_id,
                    "pipeline": "mops_official_vertical_slice",
                    "ticker": ticker,
                },
            )
            evidence_id = str(evidence_packet["id"])
            if database.get_evidence(evidence_id) is not None:
                return MopsOfficialSlice(
                    source_document_id=source_document_id,
                    research_document_id=research_document_id,
                    evidence_id=evidence_id,
                    claim_id=claim_id,
                    run_id=run_id,
                    canonical_text=text,
                    chunk_ids=[chunk.chunk_id for chunk in chunks],
                    provenance_status="exact",
                    status="reused",
                )

        registry = SourceRegistry(project_root, db=database)
        research = registry.materialize_research_document(
            source_document_id,
            text,
            document_type="mops_material_detail",
            symbol=str(ticker or ""),
            title=title or source.title or "MOPS 重大訊息詳情",
            published_at=source.published_at,
            metadata={
                "source": "mops_material_detail",
                "detail_url": detail_url or source.url,
                "pipeline": "mops_official_vertical_slice",
            },
        )
        chunks = database.list_research_chunks(research.document_id)
        chunk_ids = [chunk.chunk_id for chunk in chunks]
        excerpt = (chunks[0].text if chunks else text)[:2000]
        evidence_packet = make_evidence(
            "mops_material",
            source="mops",
            source_ref=detail_url or source.source_ref or source_document_id,
            title=research.title or "MOPS 重大訊息詳情",
            url=detail_url or source.url,
            observed_at=source.as_of or source.retrieved_at,
            published_at=source.published_at or source.as_of,
            retrieved_at=source.retrieved_at,
            excerpt=excerpt,
            quality_status=source.quality_status or "ok",
            source_document_id=source_document_id,
            provenance_status="exact",
            metadata={
                "research_document_id": research.document_id,
                "pipeline": "mops_official_vertical_slice",
                "ticker": ticker,
            },
        )
        evidence_id = str(evidence_packet["id"])
        persist_evidence_items(database, [evidence_packet])
        now = now_tw().isoformat(timespec="seconds")
        database.upsert_llm_run(LlmRunRow(
            run_id=run_id,
            task_type=INGEST_TASK_TYPE,
            model="",
            prompt_id="mops_official_vertical_slice",
            prompt_version="1.0",
            started_at=now,
            finished_at=now,
            status=INGEST_RUN_STATUS,
            input_hash=hashlib.sha256(source_document_id.encode("utf-8")).hexdigest(),
            output_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            trace_id=source_document_id,
            metadata_json=json.dumps(
                {
                    "pipeline": "mops_official_vertical_slice",
                    "source_document_id": source_document_id,
                    "research_document_id": research.document_id,
                    "run_kind": "ingest",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ))
        database.link_llm_run_evidence(LlmRunEvidenceRow(
            run_id=run_id,
            evidence_id=evidence_id,
            ordinal=0,
            role="supporting",
        ))

        claim_body, snippet = _extractive_claim_parts(text)
        claim_ok = claim_snippet_supported(snippet, text)
        claim_status = "verified" if claim_body and claim_ok else "unverified"
        database.upsert_claim(ClaimRow(
            claim_id=claim_id,
            run_id=run_id,
            text=claim_body or "MOPS 重大訊息原文已登錄",
            claim_type="source_excerpt",
            confidence=1.0 if claim_status == "verified" else 0.0,
            status=claim_status,
            uncertainty="" if claim_status == "verified" else "原文摘錄無法核對",
            created_at=now,
            updated_at=now,
        ))
        database.link_claim_evidence(ClaimEvidenceRow(
            claim_id=claim_id,
            evidence_id=evidence_id,
            ordinal=0,
        ))
        return MopsOfficialSlice(
            source_document_id=source_document_id,
            research_document_id=research.document_id,
            evidence_id=evidence_id,
            claim_id=claim_id,
            run_id=run_id,
            canonical_text=text,
            chunk_ids=chunk_ids,
            provenance_status="exact",
            status="ok",
        )
    except Exception as exc:
        return MopsOfficialSlice(
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
    "MopsOfficialSlice",
    "claim_snippet_supported",
    "materialize_mops_detail_slice",
    "normalize_claim_whitespace",
]
