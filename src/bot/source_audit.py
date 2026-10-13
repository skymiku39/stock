"""來源資料的語意品質稽核。

``data_quality`` 負責 HTTP/JSON/最低筆數契約；本模組再往前一步，檢查
解析後資料是否有合理的欄位、主鍵、日期與數值。它不判斷投資方向，只回答
「這批資料是否值得送進後續計算」以及「哪裡需要人工查看」。
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from bot.data_quality import (
    QUALITY_INVALID,
    QUALITY_OK,
    QUALITY_PARTIAL,
    schema_hash,
)


FieldSpec = Union[str, Sequence[str]]

# 公開市場資料用這些字串表示「該公司沒有可計算值」；它們不是格式錯誤。
_MISSING_NUMERIC_VALUES = frozenset({"N/A", "NA", "N.M.", "-", "--", "—", "–", "..."})


@dataclass
class AuditFinding:
    """單一可追蹤的品質問題。"""

    check: str
    severity: str  # error | warning | info
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DatasetAudit:
    source: str
    rows: int
    quality_status: str
    schema_hash: str = ""
    observed_date: str = ""
    findings: List[AuditFinding] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.quality_status in {QUALITY_OK, QUALITY_PARTIAL}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "rows": self.rows,
            "quality_status": self.quality_status,
            "schema_hash": self.schema_hash,
            "observed_date": self.observed_date,
            "findings": [f.to_dict() for f in self.findings],
        }


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _field_names(spec: FieldSpec) -> Tuple[str, ...]:
    if isinstance(spec, str):
        return (spec,)
    return tuple(str(v) for v in spec)


def _pick(row: Mapping[str, Any], spec: FieldSpec) -> Any:
    for name in _field_names(spec):
        value = row.get(name)
        if _text(value):
            return value
    return None


def _parse_date(value: Any) -> Optional[dt.date]:
    """解析 ISO、西元緊湊日期與台灣民國日期。

    TWSE/TPEx 公開 API 仍常回傳 ``YYYMMDD`` 或 ``YYY/MM/DD`` 的民國日期，
    不能把它當成 ISO 日期失敗後直接判定整批資料無效。
    """
    text = _text(value)
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]

    # 先處理帶分隔符的日期。三位數年份且小於 1911 時視為民國年。
    parts = [part for part in re.split(r"[-/.]", text) if part]
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        year, month, day = (int(part) for part in parts)
        if len(parts[0]) == 3 and year < 1911:
            year += 1911
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None

    # API 常見的西元 YYYYMMDD 與民國 YYYMMDD。
    compact = re.sub(r"\D", "", text)
    if compact.isdigit() and len(compact) in {7, 8}:
        if len(compact) == 7:
            year = int(compact[:3]) + 1911
        else:
            year = int(compact[:4])
        try:
            return dt.date(year, int(compact[-4:-2]), int(compact[-2:]))
        except ValueError:
            return None

    try:
        return dt.date.fromisoformat(text[:10].replace("/", "-"))
    except ValueError:
        return None


def _parse_number(value: Any) -> Optional[float]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_missing_number(value: Any) -> bool:
    return value is None or _text(value).upper() in _MISSING_NUMERIC_VALUES


def audit_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    source: str,
    required_fields: Sequence[FieldSpec] = (),
    key_fields: Sequence[FieldSpec] = (),
    date_fields: Sequence[FieldSpec] = (),
    numeric_fields: Sequence[FieldSpec] = (),
    numeric_ranges: Optional[Mapping[str, Tuple[Optional[float], Optional[float]]]] = None,
    min_rows: int = 1,
    max_future_days: int = 1,
) -> DatasetAudit:
    """對一批 dict 做可重複的結構與 domain 基本檢查。

    ``FieldSpec`` 可以是欄位名，也可以是候選欄位名 tuple；後者用於同一個
    API 在不同市場使用不同欄名的情況。日期只接受可解析的 ISO/常見日期前綴，
    數值欄位允許空值但不允許非數字或無限大。
    """
    raw_rows = list(rows)
    invalid_row_count = sum(1 for row in raw_rows if not isinstance(row, Mapping))
    materialized = [dict(row) for row in raw_rows if isinstance(row, Mapping)]
    findings: List[AuditFinding] = []
    if invalid_row_count:
        findings.append(AuditFinding(
            "row_type", "error",
            f"有 {invalid_row_count} 筆資料不是 object/map",
            {"invalid_row_count": invalid_row_count},
        ))
    if not materialized:
        findings.append(AuditFinding("row_count", "error", "沒有可稽核的資料列"))
    elif len(materialized) < min_rows:
        findings.append(AuditFinding(
            "row_count", "warning",
            f"資料列數 {len(materialized)} 小於最低期待 {min_rows}",
        ))

    columns = schema_hash(materialized) if materialized else ""
    observed_dates: List[dt.date] = []
    today = dt.date.today()

    for index, row in enumerate(materialized):
        for field_spec in required_fields:
            if _pick(row, field_spec) is None:
                findings.append(AuditFinding(
                    "required_field", "error",
                    f"第 {index + 1} 列缺少必要欄位：{'/'.join(_field_names(field_spec))}",
                    {"row_index": index, "fields": list(_field_names(field_spec))},
                ))

        for field_spec in date_fields:
            value = _pick(row, field_spec)
            if value is None:
                continue
            parsed = _parse_date(value)
            if parsed is None:
                findings.append(AuditFinding(
                    "date_format", "error",
                    f"第 {index + 1} 列日期無法解析：{'/'.join(_field_names(field_spec))}",
                    {"row_index": index, "value": _text(value)},
                ))
            else:
                observed_dates.append(parsed)
                if parsed > today + dt.timedelta(days=max_future_days):
                    findings.append(AuditFinding(
                        "future_date", "warning",
                        f"第 {index + 1} 列日期晚於今天：{parsed.isoformat()}",
                        {"row_index": index, "value": parsed.isoformat()},
                    ))

        for field_spec in numeric_fields:
            value = _pick(row, field_spec)
            if _is_missing_number(value):
                continue
            if _parse_number(value) is None:
                findings.append(AuditFinding(
                    "numeric_format", "error",
                    f"第 {index + 1} 列數值無法解析：{'/'.join(_field_names(field_spec))}",
                    {"row_index": index, "value": _text(value)},
                ))

    if key_fields and materialized:
        seen: Dict[Tuple[str, ...], int] = {}
        for index, row in enumerate(materialized):
            key = tuple(_text(_pick(row, field_spec)) for field_spec in key_fields)
            if not all(key):
                findings.append(AuditFinding(
                    "primary_key", "error",
                    f"第 {index + 1} 列主鍵不完整",
                    {"row_index": index, "key": key},
                ))
                continue
            if key in seen:
                findings.append(AuditFinding(
                    "duplicate_key", "error",
                    f"主鍵重複：{key}",
                    {"first_row": seen[key], "duplicate_row": index},
                ))
            else:
                seen[key] = index

    for field_name, (lower, upper) in (numeric_ranges or {}).items():
        for index, row in enumerate(materialized):
            value = row.get(field_name)
            if _is_missing_number(value):
                continue
            number = _parse_number(value)
            if number is None:
                continue
            if lower is not None and number < lower or upper is not None and number > upper:
                findings.append(AuditFinding(
                    "numeric_range", "error",
                    f"第 {index + 1} 列 {field_name} 超出合理範圍：{number}",
                    {"row_index": index, "field": field_name, "value": number,
                     "lower": lower, "upper": upper},
                ))

    has_error = any(f.severity == "error" for f in findings)
    has_warning = any(f.severity == "warning" for f in findings)
    status = QUALITY_INVALID if has_error else QUALITY_PARTIAL if has_warning else QUALITY_OK
    return DatasetAudit(
        source=source,
        rows=len(materialized),
        quality_status=status,
        schema_hash=columns,
        observed_date=max(observed_dates).isoformat() if observed_dates else "",
        findings=findings,
    )


__all__ = ["AuditFinding", "DatasetAudit", "audit_rows"]
