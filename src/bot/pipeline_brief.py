"""Verified brief inputs, durable receipts and a fail-closed model boundary."""
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from bot.brief_sources import BriefSources, same_json_value
from bot.evidence import persist_evidence_items
from bot.source_registry import SourceRegistry, resolve_source_document_path


def prepare_brief_metadata(*, root: Path, task: str, run_id: str, variables: dict[str, Any],
                           macro_snapshot=None, latest=None, previous=None,
                           chip_summaries=None, etf_meta=None, min_consensus=2):
    """Replace caller variables with replayed facts BEFORE recording or calling."""
    if task not in {"daily_brief", "us_market_brief"}:
        raise ValueError("unsupported brief task")
    with SourceRegistry(root) as registry:
        verifier = BriefSources(root, registry.db)
        if task == "us_market_brief":
            for key in ("indices", "stocks", "adr_premiums"):
                if not same_json_value(json.loads(variables[key + "_json"]), (macro_snapshot or {}).get(key)):
                    raise ValueError(f"{key} differs from supplied macro snapshot")
        macro = verifier.macro(macro_snapshot)
        usable = bool(macro)
        if task == "us_market_brief":
            if not macro:
                raise ValueError("us_market_brief: no verifiable macro inputs")
            for key, default in (("indices", {}), ("stocks", {}), ("adr_premiums", [])):
                variables[key + "_json"] = json.dumps(macro.get(key, default), ensure_ascii=False)
            variables["supply_chain_json"] = "{}"
            verifier.omit("supply_chain", "editable configuration has no independently verified source links")
        else:
            from bot.etf_consensus import build_consensus, diff_snapshots, consensus_new_builds, consensus_additions
            current, current_ids = verifier.holdings(latest or {})
            prior, prior_ids = verifier.holdings(previous or {})
            consensus = build_consensus(current, etf_meta or {}, min_etf_count=1)
            top = [{"ticker": c.ticker, "name": c.name, "etf_count": c.etf_count,
                    "total_weight": round(c.total_weight, 2),
                    "holdings": [{"etf": w.etf_symbol, "date": current[w.etf_symbol].date.isoformat(),
                                  "weight_pct": w.weight_pct, "evidence_id": current_ids[w.etf_symbol]}
                                 for w in c.held_by]} for c in consensus[:20]]
            changes = []
            for symbol in current.keys() & prior.keys():
                if prior[symbol].date < current[symbol].date:
                    changes.extend(diff_snapshots(prior[symbol], current[symbol]))
            def signal_data(signal):
                data = asdict(signal)
                data["source_windows"] = [{"etf": s, "before": prior[s].date.isoformat(),
                    "after": current[s].date.isoformat(), "evidence_ids": [prior_ids[s], current_ids[s]]}
                    for s in signal.related_etfs]
                return data
            variables["consensus_signals_json"] = json.dumps({
                "scope": "Only raw-replayed ETF snapshots; source dates differ, not same-day flows",
                "consensus_top": top,
                "new_build_signals": [signal_data(s) for s in consensus_new_builds(changes, min_etfs=min_consensus)[:20]],
                "add_signals": [signal_data(s) for s in consensus_additions(changes, min_etfs=max(2, min_consensus + 1))[:20]],
            }, ensure_ascii=False)
            if variables.get("presentation_analyses_json") not in (None, "", "[]"):
                verifier.omit("presentation_analyses", "prior model output is not independently verified fact")
            variables["presentation_analyses_json"] = "[]"
            chips = verifier.chips(chip_summaries or [])
            variables["chips_summary_json"] = json.dumps(chips, ensure_ascii=False)
            variables["macro_summary"] = json.dumps(macro, ensure_ascii=False)
            usable = usable or bool(top) or bool(chips)
        if not usable or not verifier.packets:
            raise ValueError("brief has no verifiable evidence; model call refused")
        evidence_ids = list(verifier.packets)
        variables["input_provenance_json"] = json.dumps({
            "evidence": [{"id": p["id"], "url": p["url"], "as_of": p["as_of"],
                          "source_document_id": p["source_document_id"]} for p in verifier.packets.values()],
            "excluded": verifier.excluded,
            "rule": "Only supplied facts may be analyzed; missing data is unavailable, never zero. Cite evidence IDs and actual source dates. Model conclusions remain unverified.",
        }, ensure_ascii=False)
        persist_evidence_items(registry.db, verifier.packets.values())
        source_hashes = {p["source_document_id"]: verifier.raw(p["source_document_id"])[0].content_hash
                         for p in verifier.packets.values()}
        payload = json.dumps({"version": 2, "task": task, "run_id": run_id,
            "variables": variables, "evidence_ids": evidence_ids,
            "source_hashes": source_hashes, "excluded": verifier.excluded},
            ensure_ascii=False, sort_keys=True, allow_nan=False)
        receipt = registry.capture_document(
            "pipeline_llm_input", payload, source_ref=f"{run_id}:{task}",
            kind="llm_input_snapshot", suffix="json", mime_type="application/json",
            quality_status="partial" if verifier.excluded else "ok",
            metadata={"task": task, "pipeline_run_id": run_id, "input_gate_version": 2},
        )
    return {"task": task, "run_id": run_id, "evidence_ids": evidence_ids,
            "input_source_document_id": receipt.document_id,
            "input_content_hash": receipt.content_hash, "input_gate_version": 2,
            "market_verification": "replayed_inputs_only", "excluded_inputs": verifier.excluded}


def validate_brief_call(*, root, task, variables, metadata):
    """Every CLI/dashboard/pipeline call must consume the exact prepared input."""
    if metadata.get("input_gate_version") != 2:
        raise ValueError("brief requires verified input receipt")
    with SourceRegistry(root) as registry:
        source = registry.db.get_source_document(metadata.get("input_source_document_id", ""))
        if source is None or source.source != "pipeline_llm_input":
            raise ValueError("brief input receipt missing")
        raw = resolve_source_document_path(Path(root), source).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source.content_hash or source.content_hash != metadata.get("input_content_hash"):
            raise ValueError("brief input receipt hash mismatch")
        receipt = json.loads(raw)
        if (receipt.get("version") != 2 or receipt.get("task") != task
                or receipt.get("variables") != variables
                or not receipt.get("evidence_ids")
                or receipt["evidence_ids"] != metadata.get("evidence_ids")):
            raise ValueError("brief input differs from verified receipt")
        verifier = BriefSources(root, registry.db)
        for doc, digest in receipt["source_hashes"].items():
            if verifier.raw(doc)[0].content_hash != digest:
                raise ValueError("brief source changed since preparation")
        for eid in receipt["evidence_ids"]:
            evidence = registry.db.get_evidence(eid)
            if evidence is None or evidence.source_document_id not in receipt["source_hashes"]:
                raise ValueError("brief evidence link missing")


def validate_saved_brief(root, task, metadata):
    try:
        with SourceRegistry(root) as registry:
            if metadata.get("analysis_id"):
                analysis = registry.db.conn.execute(
                    "SELECT quality_status,success FROM llm_analysis_history WHERE id=?",
                    (metadata["analysis_id"],),
                ).fetchone()
                if analysis is None or analysis["quality_status"] == "invalid" or not analysis["success"]:
                    return False
            source = registry.db.get_source_document(metadata.get("input_source_document_id", ""))
            if source is None:
                return False
            receipt = json.loads(resolve_source_document_path(Path(root), source).read_bytes())
        validate_brief_call(root=root, task=task, variables=receipt["variables"], metadata=metadata)
        return True
    except (ValueError, OSError, KeyError, TypeError):
        return False
