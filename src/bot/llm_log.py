"""llm_log -- 所有 LLM 呼叫的 append-only JSONL 日誌。

每筆紀錄包含：
* ts (台灣時區 ISO8601)
* prompt_id / prompt_version
* model
* input (rendered prompt 全文)
* output (raw response)
* latency_ms
* tokens_in / tokens_out (若 SDK 有提供)
* success / error
* metadata (任意 key-value)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from bot.utils import get_logger, mk_folder, now_tw


@dataclass
class LlmCallRecord:
    ts: str
    prompt_id: str
    prompt_version: str
    model: str
    input: str
    output: str
    latency_ms: int = 0
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    success: bool = True
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


def analysis_record_id(record: LlmCallRecord) -> str:
    """回傳與 ``llm_analysis_history.id`` 相同的穩定識別。"""
    metadata = record.metadata or {}
    ticker = str(metadata.get("ticker") or metadata.get("symbol") or "")
    return f"{record.call_id or record.ts}_{record.prompt_id}_{ticker or 'NA'}"


def research_run_id(record: LlmCallRecord) -> str:
    """New calls have their own identity; replay preserves historical IDs."""
    if record.call_id:
        return f"llm:{record.call_id}"
    md = record.metadata or {}
    return str(md.get("run_id") or "llm:" + hashlib.sha256(
        "|".join((record.ts, record.prompt_id, str(md.get("task") or ""), record.input)).encode("utf-8")
    ).hexdigest()[:24])


def research_metadata(record: LlmCallRecord) -> Dict[str, Any]:
    md = dict(record.metadata or {})
    md["analysis_id"] = analysis_record_id(record)
    if record.call_id:
        md["call_id"] = record.call_id
        if md.get("run_id"):
            md["pipeline_run_id"] = md.pop("run_id")
    return md


class LlmCallLogger:
    """以日為單位切檔的 JSONL writer。"""

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
        root: Optional[Path] = None,
    ):
        # JSONL 測試／匯入目錄不一定屬於本專案；若沒有明確 root，不能
        # 猜測並把 audit record 寫入 cwd 的正式 stock.db。只有以下兩種
        # 情況允許 mirror：呼叫端明確指定 root，或 log_dir 明確是
        # <project-root>/log/llm_calls。
        if root is not None:
            self.root: Optional[Path] = Path(root)
        elif log_dir is None:
            self.root = Path.cwd()
        else:
            candidate = Path(log_dir).expanduser().resolve()
            if candidate.name == "llm_calls" and candidate.parent.name == "log":
                self.root = candidate.parent.parent
            else:
                self.root = None
        self.log_dir = Path(log_dir) if log_dir is not None else self.root / "log" / "llm_calls"
        self.logger = logger or get_logger("llm-log")
        mk_folder(str(self.log_dir))
        self._lock = threading.Lock()

    def _path_for(self, ts: dt.datetime) -> Path:
        return self.log_dir / f"llm_calls_{ts.strftime('%Y-%m-%d')}.jsonl"

    def record(
        self,
        *,
        prompt_id: str,
        prompt_version: str,
        model: str,
        input_text: str,
        output_text: str,
        latency_ms: int = 0,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        success: bool = True,
        error: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LlmCallRecord:
        ts = now_tw()
        rec = LlmCallRecord(
            ts=ts.isoformat(timespec="seconds"),
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            model=model,
            input=input_text,
            output=output_text,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            success=success,
            error=error,
            metadata=dict(metadata or {}),
            call_id=uuid.uuid4().hex,
        )
        path = self._path_for(ts)
        try:
            with self._lock:
                with path.open("a", encoding="utf-8") as f:
                    json.dump(rec.__dict__, f, ensure_ascii=False)
                    f.write("\n")
        except Exception:
            self.logger.exception("LLM call log 寫入失敗")

        # Best-effort: 同步寫入 stock_db.llm_analysis_history 供 Google Sheet 同步。
        # 自訂的孤立 log_dir 沒有資料庫邊界時，只落 JSONL，避免測試／匯入
        # 意外污染正式資料庫。失敗時也不影響呼叫流程 (JSONL 已落地)。
        if self.root is not None:
            try:
                _mirror_to_stock_db(rec, logger=self.logger, root=self.root)
            except Exception:
                self.logger.debug("LLM analysis mirror 失敗", exc_info=True)
        else:
            self.logger.debug("自訂 LLM log_dir 未指定 project root，略過 DB mirror")
        return rec

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------

    def list_dates(self) -> List[dt.date]:
        out: List[dt.date] = []
        if not self.log_dir.exists():
            return out
        for f in self.log_dir.glob("llm_calls_*.jsonl"):
            stem = f.stem.replace("llm_calls_", "")
            try:
                out.append(dt.date.fromisoformat(stem))
            except ValueError:
                continue
        return sorted(out, reverse=True)

    def read(
        self,
        date: Optional[dt.date] = None,
        limit: int = 500,
    ) -> List[LlmCallRecord]:
        if date is None:
            dates = self.list_dates()
            if not dates:
                return []
            date = dates[0]
        path = self.log_dir / f"llm_calls_{date.isoformat()}.jsonl"
        if not path.exists():
            return []
        records: List[LlmCallRecord] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        records.append(LlmCallRecord(**d))
                    except Exception:
                        continue
        except Exception:
            self.logger.exception("LLM call log 讀取失敗")
            return []
        return records[-limit:]

    def iter_records(
        self,
        dates: Optional[Iterable[dt.date]] = None,
    ) -> Iterable[LlmCallRecord]:
        ds = list(dates) if dates is not None else self.list_dates()
        for d in ds:
            for r in self.read(d, limit=10_000):
                yield r


# ----------------------------------------------------------------------
# Mirror to stock_db.llm_analysis_history (for Google Sheet sync)
# ----------------------------------------------------------------------


def _mirror_to_stock_db(
    rec: LlmCallRecord,
    *,
    logger: logging.Logger,
    root: Optional[Path] = None,
) -> None:
    """把單筆 LLM 呼叫鏡像到 stock_db.llm_analysis_history。

    解析 output 嘗試抽出 sentiment / sentiment_score / summary / drivers / risks。
    失敗時欄位留空。
    """
    try:
        from bot.stock_db import (
            LlmAnalysisRow,
            LlmOutputRow,
            LlmRunEvidenceRow,
            LlmRunRow,
            get_db,
        )
    except Exception:
        return  # stock_db 模組無法載入時 silently skip

    md = rec.metadata or {}
    ticker = str(md.get("ticker") or md.get("symbol") or "")

    sentiment = ""
    sentiment_score = 0.0
    confidence = 0.0
    summary = ""
    drivers = ""
    risks = ""
    parsed: Any = {}
    try:
        if rec.output:
            cleaned = rec.output.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`")
                if cleaned.lower().startswith("json"):
                    cleaned = cleaned[4:].strip()
            parsed = None
            try:
                parsed = json.loads(cleaned)
            except Exception:
                # try to find outermost {...}
                start = cleaned.find("{")
                end = cleaned.rfind("}")
                if start >= 0 and end > start:
                    try:
                        parsed = json.loads(cleaned[start : end + 1])
                    except Exception:
                        parsed = None
            if isinstance(parsed, dict):
                sentiment = str(parsed.get("sentiment", "") or parsed.get("report_tone", ""))
                try:
                    sentiment_score = float(
                        parsed.get("sentiment_score", parsed.get("report_score", 0.0)) or 0.0
                    )
                except Exception:
                    sentiment_score = 0.0
                try:
                    confidence = float(parsed.get("confidence", 0.0) or 0.0)
                except Exception:
                    confidence = 0.0
                summary = str(parsed.get("summary", "") or parsed.get("interpretation", ""))[:1500]
                ds = parsed.get("growth_drivers") or []
                if isinstance(ds, list):
                    drivers = "; ".join(str(d) for d in ds)[:1500]
                rs = parsed.get("risks") or []
                if isinstance(rs, list):
                    risks = "; ".join(str(r) for r in rs)[:1500]
    except Exception:
        logger.debug("LLM output 解析失敗 (mirror)", exc_info=True)

    row = LlmAnalysisRow(
        id=analysis_record_id(rec),
        ts=rec.ts,
        ticker=ticker,
        prompt_id=rec.prompt_id,
        prompt_version=rec.prompt_version,
        model=rec.model,
        sentiment=sentiment,
        sentiment_score=sentiment_score,
        confidence=confidence,
        summary=summary,
        drivers=drivers,
        risks=risks,
        raw_output=(rec.output or "")[:8000],
        latency_ms=int(rec.latency_ms or 0),
        tokens_in=int(rec.tokens_in or 0),
        tokens_out=int(rec.tokens_out or 0),
        success=1 if rec.success else 0,
        error=str(rec.error or "")[:1000],
        source=str(md.get("task") or md.get("source") or "llm_call"),
        evidence_json=json.dumps(
            list(dict.fromkeys(
                str(value) for value in (md.get("evidence_ids") or [])
                if str(value).strip()
            )),
            ensure_ascii=False,
        ),
        source_record_id=str(
            md.get("source_record_id") or md.get("trace_id") or ""
        ),
    )
    metadata_root = md.get("project_root")
    mirror_root = root
    if mirror_root is None and metadata_root:
        mirror_root = Path(str(metadata_root))
    db = get_db(root=mirror_root)
    db.upsert_llm_analysis(row)
    _persist_research_llm_run(
        db,
        rec=rec,
        parsed=parsed if isinstance(parsed, dict) else {},
        evidence_ids=row.evidence_json,
        run_id=research_run_id(rec),
    )
    # 完整 prompt/output 另外落到短期記憶；失敗呼叫仍保存，但搜尋層會排除
    # failed/invalid，避免錯誤輸出污染長期研究上下文。
    try:
        from bot.memory_store import MemoryStore

        MemoryStore(db).record_llm_call(rec, source_record_id=row.id)
    except Exception:
        logger.debug("LLM short memory mirror 失敗", exc_info=True)


def _persist_research_llm_run(
    db: Any,
    *,
    rec: LlmCallRecord,
    parsed: Dict[str, Any],
    evidence_ids: str,
    run_id: str,
) -> None:
    """Best-effort mirror into the relational research layer.

    The legacy JSONL and ``llm_analysis_history`` remain intact.  A missing
    evidence registration is recorded as an unlinked run rather than guessed
    or silently attached by URL/title.
    """
    from bot.stock_db import LlmOutputRow, LlmRunEvidenceRow, LlmRunRow

    md = research_metadata(rec)
    status = "ok" if rec.success else "failed"
    finished = rec.ts
    db.upsert_llm_run(LlmRunRow(
        run_id=run_id,
        task_type=str(md.get("task") or rec.prompt_id),
        model=rec.model,
        prompt_id=rec.prompt_id,
        prompt_version=rec.prompt_version,
        started_at=rec.ts,
        finished_at=finished,
        status=status,
        input_hash=hashlib.sha256((rec.input or "").encode("utf-8")).hexdigest(),
        output_hash=hashlib.sha256((rec.output or "").encode("utf-8")).hexdigest(),
        error=str(rec.error or "")[:2000],
        trace_id=str(md.get("trace_id") or ""),
        metadata_json=json.dumps(md, ensure_ascii=False, default=str),
    ))
    try:
        ids = json.loads(evidence_ids or "[]")
    except Exception:
        ids = []
    for ordinal, evidence_id in enumerate(ids if isinstance(ids, list) else []):
        try:
            db.link_llm_run_evidence(LlmRunEvidenceRow(
                run_id=run_id, evidence_id=str(evidence_id), ordinal=ordinal,
            ))
        except Exception:
            # Keep the run auditable; unresolved evidence is handled by audit.
            continue
    structured = json.dumps(parsed, ensure_ascii=False, default=str)
    try:
        output_confidence = float(parsed.get("confidence") or 0.0) if parsed else 0.0
    except (TypeError, ValueError):
        output_confidence = 0.0
    db.upsert_llm_output(LlmOutputRow(
        output_id=f"{run_id}:output",
        run_id=run_id,
        output_format="json" if parsed else "text",
        structured_json=structured,
        markdown=str(parsed.get("answer") or parsed.get("summary") or rec.output or "")[:20_000],
        confidence=output_confidence,
        limitation=str(parsed.get("uncertainty") or parsed.get("limitation") or "")[:4000],
        is_publishable=1 if rec.success and parsed else 0,
    ))


# ----------------------------------------------------------------------
# Singleton
# ----------------------------------------------------------------------

_logger_instance: Optional[LlmCallLogger] = None
_lock = threading.Lock()


def get_call_logger(
    log_dir: Optional[Path] = None,
    *,
    root: Optional[Path] = None,
) -> LlmCallLogger:
    global _logger_instance
    requested_dir = Path(log_dir) if log_dir is not None else (
        (Path(root) if root is not None else Path.cwd()) / "log" / "llm_calls"
    )
    with _lock:
        same_dir = (
            _logger_instance is not None
            and Path(_logger_instance.log_dir).resolve() == requested_dir.resolve()
        )
        if not same_dir:
            _logger_instance = LlmCallLogger(
                log_dir=requested_dir,
                root=root,
            )
    return _logger_instance


__all__ = ["LlmCallLogger", "LlmCallRecord", "analysis_record_id", "research_run_id", "get_call_logger"]
