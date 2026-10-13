"""MOPS 法說會簡報的來源專用 domain validator。

檢查 HTML 冒充 PDF、PDF magic／MIME／副檔名一致性、可取得時的頁數，以及
``conference_date`` 是否有 raw／行事曆證據對齊。回傳結構化 errors／warnings
與 ``quality_status``；不臆造來源 ID，也不把較差來源品質自動升格。
"""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.data_quality import (
    QUALITY_INVALID,
    QUALITY_OK,
    QUALITY_PARTIAL,
    combine_quality_status,
)
from bot.source_audit import AuditFinding


_PDF_MAGIC = b"%PDF"
_HTML_MARKERS = (
    b"<html",
    b"<!doctype html",
    b"<head",
    b"<body",
    b"<table",
)
_DATE_RE = re.compile(r"^(?:19|20)\d{2}-\d{2}-\d{2}$")


@dataclass
class MopsPresentationValidationResult:
    """法說簡報驗證結果。"""

    quality_status: str = QUALITY_OK
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    findings: List[AuditFinding] = field(default_factory=list)
    page_count: Optional[int] = None
    conference_date: str = ""
    conference_date_aligned: bool = False
    is_pdf: bool = False
    content_kind: str = "unknown"  # pdf / html / other / unknown

    @property
    def usable(self) -> bool:
        return self.quality_status in {QUALITY_OK, QUALITY_PARTIAL}

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["findings"] = [finding.to_dict() for finding in self.findings]
        return payload


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _looks_like_html(payload: bytes) -> bool:
    sample = payload[:4096].lstrip().lower()
    # UTF-16 LE/BE HTML often still contains ASCII tag bytes with nulls.
    compacted = sample.replace(b"\x00", b"")
    return any(marker in sample or marker in compacted for marker in _HTML_MARKERS)


def _looks_like_pdf(payload: bytes) -> bool:
    return payload[:1024].lstrip().startswith(_PDF_MAGIC) or payload.startswith(_PDF_MAGIC)


def _suffix_from_path(content_path: str, suffix: str = "") -> str:
    if suffix:
        return _lower(suffix).lstrip(".")
    path = _lower(content_path)
    if "." in path:
        return path.rsplit(".", 1)[-1]
    return ""


def _count_pdf_pages(payload: bytes) -> Optional[int]:
    if not _looks_like_pdf(payload):
        return None
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None
    try:
        reader = PdfReader(io.BytesIO(payload))
        return len(reader.pages)
    except Exception:
        return None


def resolve_conference_date(
    claimed: str = "",
    *,
    source_as_of: str = "",
    calendar_conference_date: str = "",
) -> tuple[str, bool, List[AuditFinding]]:
    """只在 raw／行事曆證據支援時接受 conference_date。

    不會把沒有證據的呼叫端日期寫進對齊結果，也不會憑空發明日期。
    """
    findings: List[AuditFinding] = []
    claimed_date = _text(claimed)
    evidence = {
        value for value in (_text(source_as_of), _text(calendar_conference_date))
        if value
    }
    for value in list(evidence):
        if not _DATE_RE.match(value):
            findings.append(AuditFinding(
                "conference_date", "warning",
                f"證據日期格式無法辨識：{value}",
                {"value": value},
            ))
            evidence.discard(value)
    if not claimed_date:
        return "", False, findings
    if not _DATE_RE.match(claimed_date):
        findings.append(AuditFinding(
            "conference_date", "error",
            f"conference_date 格式無效：{claimed_date}",
            {"conference_date": claimed_date},
        ))
        return "", False, findings
    if claimed_date in evidence:
        return claimed_date, True, findings
    if not evidence:
        findings.append(AuditFinding(
            "conference_date", "warning",
            "conference_date 缺少 raw／行事曆證據，拒絕對齊",
            {"conference_date": claimed_date},
        ))
        return "", False, findings
    findings.append(AuditFinding(
        "conference_date", "error",
        (
            f"conference_date {claimed_date} 與證據 "
            f"{sorted(evidence)} 不一致"
        ),
        {
            "conference_date": claimed_date,
            "evidence_dates": sorted(evidence),
        },
    ))
    return "", False, findings


def validate_mops_presentation(
    *,
    content: bytes = b"",
    content_path: str = "",
    mime_type: str = "",
    suffix: str = "",
    claimed_conference_date: str = "",
    source_as_of: str = "",
    calendar_conference_date: str = "",
    page_count: Optional[int] = None,
    source_quality_status: str = "",
) -> MopsPresentationValidationResult:
    """驗證法說簡報位元組與中繼資料；失敗不拋例外。"""
    result = MopsPresentationValidationResult()
    findings: List[AuditFinding] = []
    payload = bytes(content or b"")
    mime = _lower(mime_type)
    path = _lower(content_path)
    ext = _suffix_from_path(content_path, suffix)
    claims_pdf = (
        ext == "pdf"
        or "pdf" in mime
        or path.endswith(".pdf")
    )

    is_html = _looks_like_html(payload) or "html" in mime or ext in {"html", "htm"}
    is_pdf = _looks_like_pdf(payload)

    if is_html and claims_pdf:
        findings.append(AuditFinding(
            "html_as_pdf", "error",
            "raw source 是 HTML 冒充 PDF，拒絕進入簡報驗證鏈",
            {
                "mime_type": mime_type,
                "content_path": content_path,
                "suffix": ext,
            },
        ))
        result.content_kind = "html"
    elif is_html:
        findings.append(AuditFinding(
            "content_type", "error",
            "raw source 是 HTML 頁面，不是可驗證的簡報檔",
            {"mime_type": mime_type, "content_path": content_path},
        ))
        result.content_kind = "html"
    elif is_pdf:
        result.content_kind = "pdf"
        result.is_pdf = True
    elif payload:
        result.content_kind = "other"
    else:
        result.content_kind = "unknown"
        if claims_pdf:
            findings.append(AuditFinding(
                "pdf_empty", "error",
                "宣稱 PDF 但內容為空",
            ))

    if claims_pdf and payload and not is_html:
        if not is_pdf:
            findings.append(AuditFinding(
                "pdf_magic", "error",
                "缺少 %PDF magic，無法確認為 PDF",
                {"content_path": content_path, "mime_type": mime_type},
            ))
        if mime and "pdf" not in mime and "octet-stream" not in mime:
            findings.append(AuditFinding(
                "pdf_mime", "warning",
                f"MIME 不是 PDF：{mime_type}",
                {"mime_type": mime_type},
            ))
        if ext and ext != "pdf":
            findings.append(AuditFinding(
                "pdf_suffix", "error",
                f"副檔名不是 .pdf：.{ext}",
                {"suffix": ext},
            ))

    if is_pdf and not claims_pdf:
        findings.append(AuditFinding(
            "pdf_suffix", "warning",
            "內容為 PDF magic，但 MIME／副檔名未標示 pdf",
            {"mime_type": mime_type, "suffix": ext},
        ))

    resolved_pages = page_count if page_count is not None else None
    if resolved_pages is None and is_pdf and payload:
        resolved_pages = _count_pdf_pages(payload)
    if resolved_pages is not None:
        try:
            resolved_pages = int(resolved_pages)
        except (TypeError, ValueError):
            findings.append(AuditFinding(
                "page_count", "warning",
                f"page_count 無法解析：{page_count}",
            ))
            resolved_pages = None
        if resolved_pages is not None and resolved_pages < 0:
            findings.append(AuditFinding(
                "page_count", "error",
                f"page_count 不可為負：{resolved_pages}",
            ))
            resolved_pages = None
    result.page_count = resolved_pages

    accepted, aligned, date_findings = resolve_conference_date(
        claimed_conference_date,
        source_as_of=source_as_of,
        calendar_conference_date=calendar_conference_date,
    )
    findings.extend(date_findings)
    result.conference_date = accepted
    result.conference_date_aligned = aligned

    errors = [f.message for f in findings if f.severity == "error"]
    warnings = [f.message for f in findings if f.severity == "warning"]
    result.findings = findings
    result.errors = errors
    result.warnings = warnings
    if errors:
        result.quality_status = QUALITY_INVALID
    elif warnings:
        result.quality_status = QUALITY_PARTIAL
    else:
        result.quality_status = QUALITY_OK

    if source_quality_status:
        result.quality_status = combine_quality_status(
            source_quality_status, result.quality_status,
        )
    return result


def validate_mops_presentation_file(
    path: Path,
    *,
    mime_type: str = "",
    claimed_conference_date: str = "",
    source_as_of: str = "",
    calendar_conference_date: str = "",
    page_count: Optional[int] = None,
    source_quality_status: str = "",
) -> MopsPresentationValidationResult:
    """從檔案路徑讀取位元組後驗證。"""
    file_path = Path(path)
    try:
        payload = file_path.read_bytes() if file_path.is_file() else b""
    except OSError as exc:
        result = MopsPresentationValidationResult(
            quality_status=QUALITY_INVALID,
            errors=[f"無法讀取簡報檔：{exc}"],
            findings=[AuditFinding(
                "io", "error", f"無法讀取簡報檔：{exc}",
            )],
        )
        return result
    return validate_mops_presentation(
        content=payload,
        content_path=str(file_path),
        mime_type=mime_type,
        suffix=file_path.suffix.lstrip("."),
        claimed_conference_date=claimed_conference_date,
        source_as_of=source_as_of,
        calendar_conference_date=calendar_conference_date,
        page_count=page_count,
        source_quality_status=source_quality_status,
    )


__all__ = [
    "MopsPresentationValidationResult",
    "resolve_conference_date",
    "validate_mops_presentation",
    "validate_mops_presentation_file",
]


