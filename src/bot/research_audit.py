"""Read-only checks for the raw -> research provenance contract.

This module deliberately does not repair rows.  A research document is useful
only when its exact source document and immutable raw bytes can be resolved.
The audit therefore reports date/type/version problems without guessing a
replacement source or silently rewriting a derived document.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


def _parse_temporal(value: Any) -> Optional[dt.datetime]:
    """Parse the date formats used by TWSE/MOPS manifests.

    Month-only values are normalized to the first day for ordering checks.
    Unknown non-empty values are intentionally rejected by the caller rather
    than interpreted by fuzzy matching.
    """
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text, text.replace("/", "-")]
    for candidate in candidates:
        try:
            parsed = dt.datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%Y-%m", "%Y%m%d", "%Y%m", "%Y/%m/%d"):
            try:
                parsed_date = dt.datetime.strptime(candidate, fmt)
                return parsed_date.replace(tzinfo=dt.timezone.utc)
            except ValueError:
                pass
        if len(candidate) == 6 and candidate[:4].isdigit() and candidate[4] == "Q":
            quarter = candidate[5]
            if quarter in {"1", "2", "3", "4"}:
                return dt.datetime(
                    int(candidate[:4]), (int(quarter) - 1) * 3 + 1, 1,
                    tzinfo=dt.timezone.utc,
                )
    # ROC dates occasionally appear in MOPS schedules.  Convert only the
    # exact numeric forms; never infer a missing day from arbitrary text.
    compact = text.replace("/", "-")
    parts = compact.split("-")
    if len(parts) in {2, 3} and parts[0].isdigit() and len(parts[0]) == 3:
        try:
            year = int(parts[0]) + 1911
            month = int(parts[1])
            day = int(parts[2]) if len(parts) == 3 else 1
            return dt.datetime(year, month, day, tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


def _finding(
    code: str,
    severity: str,
    message: str,
    *,
    document_id: str = "",
    source_document_id: str = "",
    details: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "document_id": document_id,
        "source_document_id": source_document_id,
        "details": dict(details or {}),
    }


def _safe_path(root: Path, relative: str) -> Optional[Path]:
    if not relative:
        return None
    root_resolved = root.resolve()
    candidate = (root / relative.replace("\\", "/")).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    return candidate


def _load_json(value: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def audit_research_database(
    path: Path,
    *,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Audit research/source identity without changing SQLite or raw files."""
    db_path = Path(path)
    if not db_path.exists():
        return {
            "exists": False,
            "path": str(db_path),
            "quality_status": "empty",
            "documents_checked": 0,
            "chunks_checked": 0,
            "findings": [],
            "versioning": {
                "groups_checked": 0,
                "unversioned_groups": [],
                "capture_versioned_groups": [],
                "period_unpinned_groups": [],
                "conflicts": [],
            },
        }

    project_root = Path(root) if root is not None else db_path.parent.parent
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    findings: List[Dict[str, Any]] = []
    source_cache: Dict[str, sqlite3.Row] = {}
    source_replay_cache: Dict[str, Tuple[bool, str]] = {}
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {"research_documents", "research_chunks", "source_documents"}
        if not required.issubset(tables):
            missing = sorted(required - tables)
            return {
                "exists": True,
                "path": str(db_path),
                "quality_status": "partial",
                "documents_checked": 0,
                "chunks_checked": 0,
                "findings": [_finding(
                    "research_schema_incomplete", "warning",
                    "研究稽核所需資料表不存在：" + ", ".join(missing),
                )],
                "versioning": {
                    "groups_checked": 0,
                    "unversioned_groups": [],
                    "capture_versioned_groups": [],
                    "period_unpinned_groups": [],
                    "conflicts": [],
                },
            }

        source_rows = conn.execute("SELECT * FROM source_documents").fetchall()
        source_cache = {str(row["document_id"]): row for row in source_rows}

        research_rows = conn.execute(
            "SELECT * FROM research_documents ORDER BY document_id"
        ).fetchall()
        chunk_counts = {
            str(row["document_id"]): int(row["n"])
            for row in conn.execute(
                "SELECT document_id, COUNT(*) n FROM research_chunks GROUP BY document_id"
            ).fetchall()
        }
        chunks_checked = int(
            conn.execute("SELECT COUNT(*) FROM research_chunks").fetchone()[0]
        )

        for research in research_rows:
            document_id = str(research["document_id"] or "")
            source_id = str(research["source_document_id"] or "").strip()
            canonical_text = str(research["canonical_text"] or "").strip()
            research_metadata = _load_json(research["metadata_json"])
            quarantined = isinstance(research_metadata.get("quarantine"), dict)
            if not source_id:
                findings.append(_finding(
                    "research_source_document_missing", "error",
                    "研究文件沒有 exact source_document_id，不能追溯原始資料。",
                    document_id=document_id,
                ))
                continue
            source = source_cache.get(source_id)
            if source is None:
                findings.append(_finding(
                    "research_source_document_orphan", "error",
                    "研究文件指向不存在的 source_document_id。",
                    document_id=document_id, source_document_id=source_id,
                ))
                continue

            if not canonical_text:
                findings.append(_finding(
                    "research_text_empty", "error",
                    "研究文件 canonical_text 為空。",
                    document_id=document_id, source_document_id=source_id,
                ))
            if not chunk_counts.get(document_id):
                findings.append(_finding(
                    "research_chunks_missing", "error",
                    "研究文件沒有可檢索的 research_chunks。",
                    document_id=document_id, source_document_id=source_id,
                ))

            for field in ("as_of", "published_at", "retrieved_at", "valid_from", "valid_to"):
                value = str(source[field] or "").strip()
                if value and _parse_temporal(value) is None:
                    findings.append(_finding(
                        "source_date_unparseable", "warning",
                        f"來源 {field} 不是受支援的日期格式，未將它猜測成日期。",
                        document_id=document_id, source_document_id=source_id,
                        details={"field": field, "value": value},
                    ))
            source_pub = _parse_temporal(source["published_at"])
            source_retrieved = _parse_temporal(source["retrieved_at"])
            if source_pub and source_retrieved and source_pub > source_retrieved:
                findings.append(_finding(
                    "source_date_order", "error",
                    "來源 published_at 晚於 retrieved_at，時間欄位互相矛盾。",
                    document_id=document_id, source_document_id=source_id,
                ))
            valid_from = _parse_temporal(source["valid_from"])
            valid_to = _parse_temporal(source["valid_to"])
            if valid_from and valid_to and valid_from > valid_to:
                findings.append(_finding(
                    "source_validity_order", "error",
                    "來源 valid_from 晚於 valid_to。",
                    document_id=document_id, source_document_id=source_id,
                ))

            research_pub = str(research["published_at"] or "").strip()
            if research_pub and _parse_temporal(research_pub) is None:
                findings.append(_finding(
                    "research_date_unparseable", "warning",
                    "研究文件 published_at 無法解析，回答不可依它做時間排序。",
                    document_id=document_id, source_document_id=source_id,
                    details={"value": research_pub},
                ))
            if research_pub and source["published_at"]:
                left = _parse_temporal(research_pub)
                right = _parse_temporal(source["published_at"])
                if left and right and left != right:
                    findings.append(_finding(
                        "research_source_date_mismatch", "error",
                        "研究文件 published_at 與 raw source published_at 不一致。",
                        document_id=document_id, source_document_id=source_id,
                        details={"research_published_at": research_pub,
                                 "source_published_at": str(source["published_at"])},
                    ))

            relative = str(source["content_path"] or "").strip()
            raw_path = _safe_path(project_root, relative)
            if raw_path is None or not raw_path.is_file():
                source_replay_cache[source_id] = (False, "raw_missing_or_outside_root")
                findings.append(_finding(
                    "research_raw_missing", "error",
                    "研究文件的 exact raw archive 不存在或超出專案根目錄。",
                    document_id=document_id, source_document_id=source_id,
                    details={"content_path": relative},
                ))
            elif source_id not in source_replay_cache:
                digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
                expected = str(source["content_hash"] or "").strip().lower()
                ok = bool(expected and digest == expected)
                source_replay_cache[source_id] = (ok, "" if ok else "hash_mismatch")
                if not ok:
                    findings.append(_finding(
                        "research_raw_hash_mismatch", "error",
                        "研究文件的 raw archive SHA-256 與 source manifest 不一致。",
                        document_id=document_id, source_document_id=source_id,
                        details={"content_path": relative},
                    ))
            elif not source_replay_cache[source_id][0]:
                findings.append(_finding(
                    "research_raw_hash_mismatch", "error",
                    "研究文件共用的 raw archive 未通過 SHA-256 驗證。",
                    document_id=document_id, source_document_id=source_id,
                ))

            suffix = Path(relative).suffix.lower()
            mime = str(source["mime_type"] or "").lower()
            if suffix == ".pdf" and "html" in mime:
                severity = (
                    "warning"
                    if quarantined
                    else "error" if str(research["document_type"] or "") == "mops_presentation"
                    else "warning"
                )
                findings.append(_finding(
                    "source_type_quarantined" if quarantined else "source_type_mismatch",
                    severity,
                    "raw manifest 的 PDF 副檔名與 HTML MIME 不一致；已隔離的衍生鏈不可作為已驗證 PDF。"
                    if quarantined else
                    "raw manifest 的 PDF 副檔名與 HTML MIME 不一致；不可把它當成已驗證 PDF。",
                    document_id=document_id, source_document_id=source_id,
                    details={"content_path": relative, "mime_type": str(source["mime_type"] or "")},
                ))

            replacement_count = canonical_text.count("\ufffd")
            if replacement_count and replacement_count / max(1, len(canonical_text)) >= 0.01:
                findings.append(_finding(
                    "research_text_encoding_suspect", "warning",
                    "研究文字含有大量 Unicode replacement character，不能直接視為可讀原文。",
                    document_id=document_id, source_document_id=source_id,
                    details={"replacement_characters": replacement_count},
                ))

        version_groups: Dict[Tuple[str, str], List[sqlite3.Row]] = defaultdict(list)
        for source in source_rows:
            source_name = str(source["source"] or "").strip()
            source_ref = str(source["source_ref"] or "").strip()
            if source_name and source_ref:
                version_groups[(source_name, source_ref)].append(source)
        unversioned_groups: List[Dict[str, Any]] = []
        capture_versioned_groups: List[Dict[str, Any]] = []
        period_unpinned_groups: List[Dict[str, Any]] = []
        conflicts: List[Dict[str, Any]] = []
        for (source_name, source_ref), rows in sorted(version_groups.items()):
            hashes = sorted({str(row["content_hash"] or "") for row in rows})
            if len(hashes) <= 1:
                continue
            as_of_values = sorted({str(row["as_of"] or "").strip() for row in rows})
            retrieved_at_values = sorted({str(row["retrieved_at"] or "").strip() for row in rows})
            retrieval_version_is_safe = (
                len(retrieved_at_values) == len(rows)
                and all(value and _parse_temporal(value) is not None for value in retrieved_at_values)
            )
            payload = {
                "source": source_name,
                "source_ref": source_ref,
                "document_count": len(rows),
                "content_hashes": hashes,
                "as_of_values": as_of_values,
                "retrieved_at_values": retrieved_at_values,
                "document_ids": [str(row["document_id"]) for row in rows],
            }
            # Different hashes are expected for snapshots.  retrieved_at can
            # preserve raw capture identity, but it is not a substitute for
            # as_of (the period the source data actually describes).
            if len(as_of_values) == 1 and not as_of_values[0]:
                payload["version_basis"] = "retrieved_at" if retrieval_version_is_safe else "unresolved"
                period_unpinned_groups.append(payload)
                if retrieval_version_is_safe:
                    capture_versioned_groups.append(payload)
                else:
                    unversioned_groups.append(payload)
            else:
                conflicts.append(payload)

        if conflicts:
            findings.append(_finding(
                "source_version_conflict", "warning",
                f"有 {len(conflicts)} 組同一來源參照對應多個版本，回答必須保留版本而不可覆蓋。",
                details={"groups": conflicts[:100]},
            ))
        if unversioned_groups:
            findings.append(_finding(
                "source_version_unpinned", "warning",
                f"有 {len(unversioned_groups)} 組來源參照有多份 raw 內容，且沒有可用的 as_of 或 distinct retrieved_at；不可依來源參照單獨選最新版本。",
                details={"groups": unversioned_groups[:100]},
            ))
        if period_unpinned_groups:
            findings.append(_finding(
                "source_period_unpinned", "warning",
                f"有 {len(period_unpinned_groups)} 組多版本 raw 內容沒有 as_of；可依 retrieved_at 保存抓取版本，但不可把它當成資料有效期間。",
                details={"groups": period_unpinned_groups[:100]},
            ))

        errors = sum(1 for item in findings if item["severity"] == "error")
        warnings = sum(1 for item in findings if item["severity"] == "warning")
        quality_status = "invalid" if errors else "partial" if warnings else "ok"
        return {
            "exists": True,
            "path": str(db_path),
            "quality_status": quality_status,
            "documents_checked": len(research_rows),
            "chunks_checked": chunks_checked,
            "invalid_documents": sum(
                1 for item in findings
                if item["severity"] == "error" and item.get("document_id")
            ),
            "warnings": warnings,
            "errors": errors,
            "findings": findings,
            "versioning": {
                "groups_checked": len(version_groups),
                "unversioned_groups": unversioned_groups,
                "capture_versioned_groups": capture_versioned_groups,
                "period_unpinned_groups": period_unpinned_groups,
                "conflicts": conflicts,
            },
        }
    finally:
        conn.close()


__all__ = ["audit_research_database"]
