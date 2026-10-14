"""Input linkage is independent of execution success and output verification."""

from typing import Any, Mapping


def evidence_ids(value: Any) -> list[str]:
    """Accept an ID or an ID list, never dictionary keys or arbitrary objects."""
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip()))


def evidence_receipt(db: Any, metadata: Mapping[str, Any], prompt_id: str) -> dict:
    expected = evidence_ids(metadata.get("evidence_ids"))
    linked = [value for value in expected if db.get_evidence(value) is not None]
    missing = [value for value in expected if value not in linked]
    task = str(metadata.get("task") or prompt_id)
    # Only the known planner is exempt. Unknown/legacy calls are not silently
    # reclassified as tasks that do not need market evidence.
    if task == prompt_id == "query_plan" and not expected:
        state = "not_required"
    elif "evidence_ids" not in metadata:
        state = "unassessed"
    elif expected and not missing:
        state = "linked"
    elif linked:
        state = "partial"
    else:
        state = "missing"
    return {"version": 1, "state": state, "expected_ids": expected,
            "linked_ids": linked, "missing_ids": missing}
