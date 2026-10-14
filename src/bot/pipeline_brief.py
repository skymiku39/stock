"""Persist exact pipeline model inputs without treating them as market proof."""

import hashlib
import json
from pathlib import Path
from typing import Any

from bot.evidence import make_evidence, persist_evidence_items
from bot.source_registry import SourceRegistry, resolve_source_document_path


def prepare_brief_metadata(
    *, root: Path, task: str, run_id: str, variables: dict[str, Any],
    macro_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence_ids = []
    with SourceRegistry(root) as registry:
        if task == "us_market_brief":
            snapshot = macro_snapshot or {}
            source_id = str(snapshot.get("source_document_id") or "")
            source = registry.db.get_source_document(source_id)
            if source is None:
                raise ValueError("us_market_brief: missing exact macro source")
            raw = resolve_source_document_path(root, source).read_bytes()
            if hashlib.sha256(raw).hexdigest() != source.content_hash:
                raise ValueError("us_market_brief: macro raw hash mismatch")
            archived = json.loads(raw)
            # Compare actual prompt values; a same-day source alone is not proof.
            for key in ("indices", "stocks", "adr_premiums"):
                if json.loads(variables[key + "_json"]) != archived.get(key):
                    raise ValueError(f"us_market_brief: {key} differs from macro raw")
            packet = make_evidence(
                "macro", source=source.source, source_ref=source.document_id,
                title="US brief exact macro input", url=source.url,
                as_of=source.as_of, retrieved_at=source.retrieved_at,
                excerpt=f"Macro raw sha256={source.content_hash}; source_dates={archived.get('source_dates', {})}",
                source_document_id=source.document_id, provenance_status="exact",
                quality_status=source.quality_status,
                metadata={"source_dates": archived.get("source_dates", {}),
                          "source_date_status": archived.get("source_date_status", "unknown")},
            )
            persist_evidence_items(registry.db, [packet])
            evidence_ids.append(packet["id"])
        payload = json.dumps({"task": task, "run_id": run_id, "variables": variables,
                              "evidence_ids": evidence_ids}, ensure_ascii=False, sort_keys=True)
        # This receipt proves what was supplied, not that configuration, derived
        # consensus or prior model conclusions are independently verified facts.
        receipt = registry.capture_document(
            "pipeline_llm_input", payload, source_ref=f"{run_id}:{task}",
            kind="llm_input_snapshot", suffix="json", mime_type="application/json",
            quality_status="partial", metadata={"task": task, "pipeline_run_id": run_id,
                "market_verification": "partial" if evidence_ids else "unverified"},
        )
    return {"task": task, "run_id": run_id, "evidence_ids": evidence_ids,
            "input_source_document_id": receipt.document_id,
            "input_content_hash": receipt.content_hash,
            "market_verification": "partial" if evidence_ids else "unverified"}
