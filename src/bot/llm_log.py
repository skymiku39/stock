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
import json
import logging
import threading
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


class LlmCallLogger:
    """以日為單位切檔的 JSONL writer。"""

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.log_dir = log_dir or (Path.cwd() / "log" / "llm_calls")
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
            metadata=metadata or {},
        )
        path = self._path_for(ts)
        try:
            with self._lock:
                with path.open("a", encoding="utf-8") as f:
                    json.dump(rec.__dict__, f, ensure_ascii=False)
                    f.write("\n")
        except Exception:
            self.logger.exception("LLM call log 寫入失敗")
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
# Singleton
# ----------------------------------------------------------------------

_logger_instance: Optional[LlmCallLogger] = None
_lock = threading.Lock()


def get_call_logger(log_dir: Optional[Path] = None) -> LlmCallLogger:
    global _logger_instance
    with _lock:
        if _logger_instance is None:
            _logger_instance = LlmCallLogger(log_dir=log_dir)
    return _logger_instance


__all__ = ["LlmCallLogger", "LlmCallRecord", "get_call_logger"]
