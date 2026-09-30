"""共用資料來源品質契約。

外部 API 的 ``HTTP 200`` 只代表傳輸成功，不代表內容可以安全使用。
本模組把常見的回應檢查集中起來，讓各 fetcher 至少能區分：

* ``ok``      — 有效且符合最低契約
* ``partial`` — 有資料，但來源可能不完整
* ``invalid`` — 回應格式或必要欄位不符合契約
* ``failed``  — 網路、HTTP 或 JSON 解析失敗

這是 ingestion 層的輕量契約，不負責判斷金融數值是否「合理」；數值範圍與
跨來源 reconciliation 應由各資料源的 domain validator 再補上。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from bot.utils import get_logger, now_tw


_LOG = get_logger("data-quality")


QUALITY_OK = "ok"
QUALITY_PARTIAL = "partial"
QUALITY_INVALID = "invalid"
QUALITY_FAILED = "failed"
QUALITY_STALE = "stale"
QUALITY_MISSING = "missing"
VALID_QUALITY_STATUSES = {
    QUALITY_OK,
    QUALITY_PARTIAL,
    QUALITY_INVALID,
    QUALITY_FAILED,
    QUALITY_STALE,
    QUALITY_MISSING,
    "unknown",
}


def _stable_hash(value: Any) -> str:
    """建立可比較的 payload hash；失敗時仍回傳可用的字串。"""
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def schema_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    """依欄位名稱建立 schema fingerprint，不把欄位值納入。"""
    keys = sorted({str(k) for row in rows if isinstance(row, Mapping) for k in row})
    return _stable_hash(keys)


def validate_ohlcv(
    *,
    open_price: Any,
    high: Any,
    low: Any,
    close: Any,
    volume: Any,
    amount: Any = 0,
) -> List[str]:
    """檢查 OHLCV 的基本 domain invariants；不判斷漲跌是否合理。"""
    errors: List[str] = []
    values: Dict[str, float] = {}
    for name, value in {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
    }.items():
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            errors.append(f"{name} is not numeric")
            continue
        values[name] = parsed
        if parsed < 0:
            errors.append(f"{name} is negative")
    if all(name in values for name in ("open", "high", "low", "close")):
        prices = [values[name] for name in ("open", "high", "low", "close")]
        # 允許全 0 代表來源明確回傳缺失，但不允許部分錯誤的 OHLC。
        if any(prices) and (
            values["high"] < max(values["open"], values["close"])
            or values["low"] > min(values["open"], values["close"])
            or values["high"] < values["low"]
        ):
            errors.append("OHLC invariant violated")
    return errors


def normalize_quality_status(value: Any) -> str:
    status = str(value or "unknown").strip().lower()
    return status if status in VALID_QUALITY_STATUSES else "unknown"


def combine_quality_status(
    source_quality_status: str,
    validation_quality_status: str,
) -> str:
    """合併來源與驗證品質；不把較差／未知來源自動升格為 ok。

    ``unknown`` 來源即使驗證通過也最多落到 ``partial``，避免 cache-only
    或尚未確認的來源被當成已驗證 ok。
    """
    source = normalize_quality_status(source_quality_status)
    validation = normalize_quality_status(validation_quality_status)
    rank = {
        QUALITY_OK: 0,
        QUALITY_PARTIAL: 1,
        QUALITY_STALE: 2,
        "unknown": 2,
        QUALITY_MISSING: 3,
        QUALITY_INVALID: 4,
        QUALITY_FAILED: 5,
    }
    if rank.get(source, 2) >= rank.get(validation, 2):
        if source == "unknown" and validation == QUALITY_OK:
            return QUALITY_PARTIAL
        return source
    return validation


@dataclass
class SourceFetchResult:
    """一次外部來源取得的可追蹤結果。"""

    source: str
    url: str = ""
    status: str = QUALITY_FAILED
    http_status: Optional[int] = None
    fetched_at: str = ""
    source_date: str = ""
    row_count: int = 0
    raw_hash: str = ""
    schema_hash: str = ""
    rows: List[Dict[str, Any]] = field(default_factory=list)
    # 可選的原始 HTTP payload。只供 raw archive 使用，不應直接送進
    # dashboard/LLM；to_dict() 會移除，避免把大型 bytes 帶進 metadata。
    raw_bytes: bytes = field(default=b"", repr=False)
    error: str = ""
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = now_tw().isoformat(timespec="seconds")

    @property
    def usable(self) -> bool:
        return self.status in {QUALITY_OK, QUALITY_PARTIAL}

    def to_dict(self, *, include_rows: bool = False) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("raw_bytes", None)
        if not include_rows:
            data.pop("rows", None)
        return data


def _rows_from_payload(payload: Any, list_key: str = "") -> Optional[List[Any]]:
    if isinstance(payload, list):
        return payload
    if list_key and isinstance(payload, Mapping):
        value = payload.get(list_key)
        return value if isinstance(value, list) else None
    return None


def validate_rows(
    rows: Iterable[Any],
    *,
    source: str,
    url: str = "",
    http_status: Optional[int] = 200,
    raw_payload: Any = None,
    raw_bytes: bytes = b"",
    required_keys: Sequence[str] = (),
    min_rows: int = 1,
    source_date_keys: Sequence[str] = (),
) -> SourceFetchResult:
    """驗證 list-of-dict API payload，回傳不拋例外的品質結果。"""
    result = SourceFetchResult(
        source=source,
        url=url,
        http_status=http_status,
        raw_hash=_stable_hash(raw_payload),
        raw_bytes=bytes(raw_bytes or b""),
    )
    if http_status is not None and not 200 <= int(http_status) < 300:
        result.error = f"HTTP {http_status}"
        return result

    materialized = list(rows)
    if not materialized:
        result.status = QUALITY_PARTIAL if min_rows <= 0 else QUALITY_INVALID
        result.error = "empty rows"
        return result
    if not all(isinstance(row, Mapping) for row in materialized):
        result.status = QUALITY_INVALID
        result.error = "rows must contain objects"
        return result

    result.rows = [dict(row) for row in materialized]
    result.row_count = len(result.rows)
    result.schema_hash = schema_hash(result.rows)
    missing = [
        key for key in required_keys
        if not any(str(row.get(key) or "").strip() for row in result.rows)
    ]
    if missing:
        result.status = QUALITY_INVALID
        result.error = f"missing required fields: {', '.join(missing)}"
        return result
    if len(result.rows) < min_rows:
        result.status = QUALITY_PARTIAL
        result.warnings.append(f"row_count={len(result.rows)} < min_rows={min_rows}")
    else:
        result.status = QUALITY_OK

    if source_date_keys:
        values = []
        for row in result.rows:
            for key in source_date_keys:
                value = str(row.get(key) or "").strip()
                if value:
                    values.append(value)
                    break
        if values:
            result.source_date = max(set(values))
    return result


def response_to_rows(
    response: Any,
    *,
    source: str,
    url: str = "",
    list_key: str = "",
    required_keys: Sequence[str] = (),
    min_rows: int = 1,
    source_date_keys: Sequence[str] = (),
) -> SourceFetchResult:
    """把 requests-like response 轉成 ``SourceFetchResult``。"""
    status_code = getattr(response, "status_code", None)
    raw_bytes = b""
    try:
        candidate = getattr(response, "content", b"")
        if isinstance(candidate, (bytes, bytearray)):
            raw_bytes = bytes(candidate)
        elif candidate:
            raw_bytes = str(candidate).encode("utf-8", errors="replace")
    except Exception:
        raw_bytes = b""
    result = SourceFetchResult(
        source=source,
        url=url,
        http_status=status_code,
        raw_bytes=raw_bytes,
    )
    if status_code is not None and not 200 <= int(status_code) < 300:
        result.error = f"HTTP {status_code}"
        return result
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        result.status = QUALITY_INVALID
        result.error = f"invalid JSON: {type(exc).__name__}"
        return result
    rows = _rows_from_payload(payload, list_key=list_key)
    if rows is None:
        result.status = QUALITY_INVALID
        result.raw_hash = _stable_hash(payload)
        result.error = "payload is not a JSON array"
        return result
    return validate_rows(
        rows,
        source=source,
        url=url,
        http_status=status_code,
        raw_payload=payload,
        raw_bytes=raw_bytes,
        required_keys=required_keys,
        min_rows=min_rows,
        source_date_keys=source_date_keys,
    )


def _persist_source_result_once(
    result: SourceFetchResult,
    *,
    root: Optional[Any] = None,
    run_id: str = "",
) -> Optional[str]:
    """把來源結果寫入統一 provenance 層；回傳 exact document ID。

    失敗不阻斷主要 fetcher，會回傳 ``None``。呼叫端若需要建立
    canonical/research slice，只有拿到這個 exact ID 才能繼續，不可依 URL、
    標題或時間自行猜測來源。

    若 adapter 是透過 ``response_to_rows``，優先保存它取得的原始 response
    bytes；若只有已解析的 rows，才退回 JSON raw envelope。兩者都先進
    hash-addressed archive，再由 canonical parser 使用，讓後續能重播品質稽核。
    """
    db = None
    document_id = ""
    try:
        from pathlib import Path

        from bot.source_registry import SourceRegistry
        from bot.stock_db import StockDB

        resolved_id = run_id or (
            f"{result.source}:{result.fetched_at}:{result.raw_hash[:12]}"
        )
        db = StockDB.open(root=root)
        project_root = Path(root) if root is not None else Path.cwd()
        registry = SourceRegistry(project_root, db=db)
        run = registry.start_ingestion(
            result.source,
            source_url=result.url,
            run_id=resolved_id,
            metadata={
                "http_status": result.http_status,
                "source_date": result.source_date,
                "raw_hash": result.raw_hash,
                "schema_hash": result.schema_hash,
                "warnings": result.warnings,
            },
        )
        raw_payload = bytes(result.raw_bytes or b"")
        raw_kind = "http_response"
        if not raw_payload:
            raw_payload = json.dumps(
                {
                    "source": result.source,
                    "url": result.url,
                    "fetched_at": result.fetched_at,
                    "source_date": result.source_date,
                    "http_status": result.http_status,
                    "rows": result.rows,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
            raw_kind = "api_rows"
        if raw_payload:
            document = registry.register_document(
                result.source,
                raw_payload,
                kind=raw_kind,
                source_ref=resolved_id,
                url=result.url,
                title=f"{result.source} API rows {result.source_date or result.fetched_at}",
                suffix="json",
                mime_type="application/json",
                as_of=result.source_date,
                retrieved_at=result.fetched_at,
                ingestion_run_id=run.run_id,
                quality_status=result.status,
                metadata={
                    "http_status": result.http_status,
                    "row_count": result.row_count,
                    "raw_hash": result.raw_hash,
                    "schema_hash": result.schema_hash,
                    "warnings": result.warnings,
                },
            )
            document_id = str(document.document_id or "")
        registry.finish_ingestion(
            run.run_id,
            status=result.status,
            document_count=1 if raw_payload else 0,
            error=result.error,
        )
        return document_id or None
    except Exception as exc:
        # provenance 不應讓既有資料流程因 DB 暫時不可寫而中斷。
        if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
            raise
        _LOG.warning(
            "provenance 保存失敗 source=%s run_id=%s error=%s: %s",
            result.source,
            run_id or "(auto)",
            type(exc).__name__,
            exc,
        )
        return None
    finally:
        if db is not None:
            db.close()


def persist_source_result(
    result: SourceFetchResult,
    *,
    root: Optional[Any] = None,
    run_id: str = "",
) -> Optional[str]:
    """保存來源結果；短暫 SQLite writer contention 時有限次重試。

    多個 live adapters 會在同一個問答 budget 內並行寫 raw manifest。SQLite
    的 busy timeout 能處理一般等待，但 schema 初始化／WAL writer 交接仍可能
    回傳 ``database is locked``；只對這種明確的暫時性錯誤重試，不對未知錯誤
    重試，也不會在失敗時猜造 source document ID。
    """
    for attempt in range(3):
        try:
            return _persist_source_result_once(result, root=root, run_id=run_id)
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message:
                _LOG.warning(
                    "provenance 保存失敗 source=%s run_id=%s error=%s: %s",
                    result.source,
                    run_id or "(auto)",
                    type(exc).__name__,
                    exc,
                )
                return None
            if attempt < 2:
                time.sleep(0.15 * (attempt + 1))
                continue
            _LOG.warning(
                "provenance 保存因 SQLite lock 放棄 source=%s run_id=%s",
                result.source,
                run_id or "(auto)",
            )
            return None
        except Exception as exc:
            _LOG.warning(
                "provenance 保存失敗 source=%s run_id=%s error=%s: %s",
                result.source,
                run_id or "(auto)",
                type(exc).__name__,
                exc,
            )
            return None
    return None


__all__ = [
    "QUALITY_FAILED",
    "QUALITY_INVALID",
    "QUALITY_MISSING",
    "QUALITY_OK",
    "QUALITY_PARTIAL",
    "QUALITY_STALE",
    "VALID_QUALITY_STATUSES",
    "SourceFetchResult",
    "combine_quality_status",
    "normalize_quality_status",
    "response_to_rows",
    "persist_source_result",
    "schema_hash",
    "validate_ohlcv",
    "validate_rows",
]
