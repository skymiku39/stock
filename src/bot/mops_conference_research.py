"""MOPS 法說會行事曆的 research／evidence／claim 垂直切片。

資料流：
``raw archive -> source_documents -> research_documents/chunks
-> evidence_items -> llm_runs -> source-fact claims``

這裡只接受已知的 exact ``source_document_id``，並從該 raw manifest 回放
JSON；不依 URL、月份或文字相似度猜測來源。行事曆是一份月快照，但每一場
活動都會建立獨立 claim，方便之後按日期／ticker 驗證與引用。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from bot.evidence import make_evidence, persist_evidence_items
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


PIPELINE = "mops_conference_calendar_vertical_slice"
INGEST_TASK_TYPE = "mops_conference_calendar_ingest"
INGEST_RUN_STATUS = "source_ingested"


@dataclass
class MopsConferenceCalendarSlice:
    source_document_id: str
    research_document_id: str = ""
    evidence_id: str = ""
    run_id: str = ""
    claim_ids: List[str] = field(default_factory=list)
    chunk_ids: List[str] = field(default_factory=list)
    valid_entries: int = 0
    invalid_entries: int = 0
    duplicate_entries: int = 0
    status: str = "ok"  # ok / reused / partial / empty / failed
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _raw_json(root: Path, source: Any) -> Tuple[bool, Any, str]:
    path = resolve_source_document_path(root, source)
    if not path.is_file():
        return False, None, f"raw archive 不存在: {source.content_path}"
    try:
        raw_text = path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, None, f"行事曆 raw JSON 無法讀取或解析: {exc}"
    if not isinstance(raw, dict):
        return False, None, "行事曆 raw JSON 根節點不是 object"
    return True, raw, ""


def _normalize_entries(raw_entries: Any) -> Tuple[List[Dict[str, str]], int, int]:
    """正規化活動欄位，回傳 (entries, invalid_count, duplicate_count)。"""
    if not isinstance(raw_entries, list):
        return [], 1, 0
    normalized: List[Dict[str, str]] = []
    seen: set[Tuple[str, str, str, str]] = set()
    invalid = 0
    duplicates = 0
    for item in raw_entries:
        if not isinstance(item, Mapping):
            invalid += 1
            continue
        date_text = str(item.get("date") or "").strip()[:10]
        try:
            parsed_date = dt.date.fromisoformat(date_text)
        except ValueError:
            invalid += 1
            continue
        event = {
            "date": parsed_date.isoformat(),
            "time": str(item.get("time") or "").strip(),
            "ticker": str(item.get("ticker") or "").strip(),
            "company": str(item.get("company") or "").strip(),
            "note": str(item.get("note") or "").strip(),
            "presentation_url": str(item.get("presentation_url") or "").strip(),
        }
        key = (event["date"], event["time"], event["ticker"], event["company"])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        normalized.append(event)
    normalized.sort(key=lambda row: (
        row["date"], row["time"], row["ticker"], row["company"],
    ))
    return normalized, invalid, duplicates


def _claim_text(entry: Mapping[str, str]) -> str:
    subject = " ".join(
        value for value in (entry.get("ticker", ""), entry.get("company", ""))
        if value
    ) or "未標示公司"
    when = " ".join(value for value in (entry.get("date", ""), entry.get("time", "")) if value)
    return f"{when} {subject} 法說會行事曆紀錄".strip()


def materialize_mops_conference_calendar_slice(
    *,
    root: Path,
    source_document_id: str,
    db: Optional[StockDB] = None,
) -> MopsConferenceCalendarSlice:
    """由 exact raw manifest 建立行事曆研究鏈，並支援冪等重跑。"""
    project_root = Path(root)
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    try:
        source = database.get_source_document(source_document_id)
        if source is None:
            return MopsConferenceCalendarSlice(
                source_document_id=source_document_id,
                status="failed",
                error=f"unknown source_document_id: {source_document_id}",
            )
        if source.source != "mops_conference_calendar":
            return MopsConferenceCalendarSlice(
                source_document_id=source_document_id,
                status="failed",
                error=(
                    "source_document_id 來源域不符: "
                    f"{source.source} != mops_conference_calendar"
                ),
            )
        if not is_source_document_replayable(project_root, source):
            return MopsConferenceCalendarSlice(
                source_document_id=source_document_id,
                status="failed",
                error=f"raw archive 無法回放: {source_document_id}",
            )
        valid, raw, error = _raw_json(project_root, source)
        if not valid:
            return MopsConferenceCalendarSlice(
                source_document_id=source_document_id,
                status="failed",
                error=error,
            )

        entries, invalid_count, duplicate_count = _normalize_entries(raw.get("entries"))
        if not entries:
            return MopsConferenceCalendarSlice(
                source_document_id=source_document_id,
                valid_entries=0,
                invalid_entries=invalid_count,
                duplicate_entries=duplicate_count,
                status="empty",
                error="行事曆沒有可驗證的活動列",
            )

        year = str(raw.get("year") or source.as_of or "")[:4]
        month = str(raw.get("month") or "").zfill(2)[:2]
        canonical = {
            "year": year,
            "month": month,
            "source_ref": source.source_ref,
            "entries": entries,
        }
        canonical_text = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, indent=2,
        )
        quality_status = "partial" if invalid_count else (source.quality_status or "ok")
        research_id = "research-doc:" + hashlib.sha256(
            (source_document_id + "|" + canonical_text).encode("utf-8")
        ).hexdigest()[:24]
        run_id = "mops-calendar-slice:" + hashlib.sha256(
            f"{source_document_id}|{research_id}".encode("utf-8")
        ).hexdigest()[:24]
        claim_ids = [f"{run_id}:claim:{index}" for index in range(len(entries))]
        evidence = make_evidence(
            "mops_conference_calendar",
            source="mops",
            source_ref=source.source_ref or source_document_id,
            title=source.title or f"MOPS 法說會行事曆 {year}-{month}",
            url=source.url,
            observed_at=source.as_of or source.retrieved_at,
            as_of=source.as_of or f"{year}-{month}",
            retrieved_at=source.retrieved_at,
            excerpt=(
                f"{source.title or 'MOPS 法說會行事曆'}：有效活動 {len(entries)} 場；"
                f"無效列 {invalid_count}；重複列 {duplicate_count}。"
            ),
            quality_status=quality_status,
            source_document_id=source_document_id,
            provenance_status="exact",
            metadata={
                "pipeline": PIPELINE,
                "research_document_id": research_id,
                "entry_count": len(entries),
                "invalid_entry_count": invalid_count,
                "duplicate_entry_count": duplicate_count,
                "temporal_status": "unknown",
            },
        )
        evidence_id = str(evidence["id"])
        persist_evidence_items(database, [evidence])

        existing = (
            database.get_research_document(research_id) is not None
            and database.get_llm_run(run_id) is not None
            and database.get_evidence(evidence_id) is not None
            and all(database.get_claim(claim_id) is not None for claim_id in claim_ids)
        )
        chunks = database.list_research_chunks(research_id)
        if existing:
            return MopsConferenceCalendarSlice(
                source_document_id=source_document_id,
                research_document_id=research_id,
                evidence_id=evidence_id,
                run_id=run_id,
                claim_ids=claim_ids,
                chunk_ids=[chunk.chunk_id for chunk in chunks],
                valid_entries=len(entries),
                invalid_entries=invalid_count,
                duplicate_entries=duplicate_count,
                status="reused",
            )

        registry = SourceRegistry(project_root, db=database)
        research = registry.materialize_research_document(
            source_document_id,
            canonical_text,
            document_type="mops_conference_calendar",
            title=source.title or f"MOPS 法說會行事曆 {year}-{month}",
            published_at=source.published_at or source.as_of,
            metadata={
                "pipeline": PIPELINE,
                "entry_count": len(entries),
                "invalid_entry_count": invalid_count,
                "duplicate_entry_count": duplicate_count,
            },
        )
        chunks = database.list_research_chunks(research.document_id)
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
                "run_kind": "ingest",
            }, ensure_ascii=False, sort_keys=True),
        ))
        database.link_llm_run_evidence(LlmRunEvidenceRow(
            run_id=run_id, evidence_id=evidence_id, ordinal=0, role="supporting",
        ))
        for ordinal, entry in enumerate(entries):
            claim_id = claim_ids[ordinal]
            database.upsert_claim(ClaimRow(
                claim_id=claim_id,
                run_id=run_id,
                text=_claim_text(entry),
                claim_type="source_fact",
                confidence=1.0,
                status="verified",
                uncertainty="",
                created_at=now,
                updated_at=now,
            ))
            database.link_claim_evidence(ClaimEvidenceRow(
                claim_id=claim_id,
                evidence_id=evidence_id,
                ordinal=ordinal,
            ))
        return MopsConferenceCalendarSlice(
            source_document_id=source_document_id,
            research_document_id=research.document_id,
            evidence_id=evidence_id,
            run_id=run_id,
            claim_ids=claim_ids,
            chunk_ids=[chunk.chunk_id for chunk in chunks],
            valid_entries=len(entries),
            invalid_entries=invalid_count,
            duplicate_entries=duplicate_count,
            status="partial" if invalid_count else "ok",
        )
    except Exception as exc:
        return MopsConferenceCalendarSlice(
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
    "MopsConferenceCalendarSlice",
    "materialize_mops_conference_calendar_slice",
]
