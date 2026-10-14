"""本地來源登錄與不可變 raw archive。

這個模組只提供來源生命週期的共用契約，不負責爬蟲、解析或品質判定：

``fetch -> register raw document -> parse/canonical upsert -> evidence``

raw 檔案以內容 hash 命名；同一份內容不覆蓋既有檔案，SQLite 只保存 manifest
與 ingestion run 狀態。這讓後續來源 adapter 可以重播，而不必依賴目前網頁仍然
可取得。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.data_quality import normalize_quality_status
from bot.stock_db import (
    IngestionRunRow,
    ResearchChunkRow,
    ResearchDocumentRow,
    SourceDocumentRow,
    SourceRun,
    StockDB,
)
from bot.utils import now_tw


def _safe_part(value: str, fallback: str = "unknown") -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return text.strip("._-") or fallback


def _as_bytes(content: bytes | bytearray | str) -> bytes:
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    return str(content).encode("utf-8")


class SourceRegistry:
    """以 SQLite manifest + data/raw archive 保存來源文件。"""

    def __init__(self, root: Path, db: Optional[StockDB] = None) -> None:
        self.root = Path(root)
        self._owns_db = db is None
        self.db = db or StockDB.open(root=self.root)
        self.raw_root = self.root / "data" / "raw"

    def close(self) -> None:
        """關閉由 registry 自己建立的 SQLite 連線。"""
        if self._owns_db and self.db is not None:
            self.db.close()

    def __enter__(self) -> "SourceRegistry":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def start_ingestion(
        self,
        source: str,
        *,
        source_url: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
        run_id: str = "",
    ) -> IngestionRunRow:
        row = IngestionRunRow(
            run_id=run_id or f"ingest:{_safe_part(source)}:{uuid.uuid4().hex[:16]}",
            source=str(source or "unknown"),
            source_run_id=run_id or "",
            status="running",
            metadata_json=json.dumps(
                {"source_url": source_url, **dict(metadata or {})},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )
        row.source_run_id = row.source_run_id or row.run_id
        self.db.upsert_ingestion_run(row)
        metadata_obj = json.loads(row.metadata_json or "{}")
        self.db.record_source_run(SourceRun(
            run_id=row.source_run_id,
            source=row.source,
            source_url=str(metadata_obj.get("source_url") or ""),
            status="running",
            started_at=row.started_at,
            details_json=row.metadata_json,
        ))
        return self.db.get_ingestion_run(row.run_id) or row

    def finish_ingestion(
        self,
        run_id: str,
        *,
        status: str,
        document_count: int = 0,
        error: str = "",
    ) -> Optional[IngestionRunRow]:
        row = self.db.get_ingestion_run(run_id)
        if row is None:
            return None
        row.status = str(status or "failed")
        row.document_count = max(0, int(document_count))
        row.error = str(error or "")[:2000]
        row.completed_at = now_tw().isoformat(timespec="seconds")
        self.db.upsert_ingestion_run(row)
        try:
            metadata_obj = json.loads(row.metadata_json or "{}")
        except json.JSONDecodeError:
            metadata_obj = {}
        documents = self.db.list_source_documents(
            ingestion_run_id=row.run_id,
            limit=100_000,
        )
        digest = ""
        hashes = sorted(
            str(document.content_hash)
            for document in documents
            if str(document.content_hash)
        )
        if hashes:
            digest = hashlib.sha256("\n".join(hashes).encode("utf-8")).hexdigest()
        self.db.record_source_run(SourceRun(
            run_id=row.source_run_id or row.run_id,
            source=row.source,
            source_url=str(metadata_obj.get("source_url") or ""),
            status=row.status,
            started_at=row.started_at,
            completed_at=row.completed_at,
            source_date=str(metadata_obj.get("source_date") or ""),
            row_count=row.document_count,
            raw_hash=digest,
            error=row.error,
            details_json=json.dumps(
                {**metadata_obj, "document_ids": [d.document_id for d in documents]},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        ))
        return self.db.get_ingestion_run(run_id) or row

    def register_document(
        self,
        source: str,
        content: bytes | bytearray | str,
        *,
        kind: str = "raw",
        source_ref: str = "",
        url: str = "",
        title: str = "",
        suffix: str = "bin",
        mime_type: str = "",
        as_of: str = "",
        published_at: str = "",
        retrieved_at: str = "",
        valid_from: str = "",
        valid_to: str = "",
        ingestion_run_id: str = "",
        quality_status: str = "ok",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> SourceDocumentRow:
        payload = _as_bytes(content)
        content_hash = hashlib.sha256(payload).hexdigest()
        source_part = _safe_part(source)
        suffix_part = _safe_part(suffix.lstrip("."), "bin")
        path = self.raw_root / source_part / f"{content_hash}.{suffix_part}"
        path.parent.mkdir(parents=True, exist_ok=True)
        retrieved = retrieved_at or now_tw().isoformat(timespec="seconds")
        document_id = f"source-doc:{source_part}:{content_hash}"
        row = SourceDocumentRow(
            document_id=document_id,
            source=str(source or "unknown"),
            kind=str(kind or "raw"),
            source_ref=str(source_ref or ""),
            url=str(url or ""),
            title=str(title or ""),
            content_path=str(path.relative_to(self.root)),
            content_hash=content_hash,
            mime_type=str(mime_type or ""),
            as_of=str(as_of or ""),
            published_at=str(published_at or ""),
            retrieved_at=retrieved,
            valid_from=str(valid_from or ""),
            valid_to=str(valid_to or ""),
            ingestion_run_id=str(ingestion_run_id or ""),
            quality_status=normalize_quality_status(quality_status),
            metadata_json=json.dumps(
                dict(metadata or {}), ensure_ascii=False, sort_keys=True, default=str,
            ),
        )
        # Persist the exact registration intent before publishing any raw bytes.
        # This survives a failed manifest transaction or a process restart.
        from bot.source_recovery import write_receipt, commit_registration
        receipt_id = write_receipt(self.root, row)
        # Hash-addressed paths are immutable.  Do not rewrite an existing
        # object; a changed response receives a new hash and a new manifest id.
        if not path.exists():
            # Write beside the final object and atomically publish it.  A
            # process interruption must not leave a truncated file at the
            # hash-addressed path that the manifest will later trust.
            fd, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{content_hash}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, path)
            except Exception:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        # raw archive 是跨機器重播研究鏈所需的 canonical artifact；若有
        # GOOGLE_CACHE_DIR，與其他 data cache 使用相同相對路徑鏡像。
        mirror_file_to_cloud(path, root=self.root)

        if hashlib.sha256(path.read_bytes()).hexdigest() != content_hash:
            raise ValueError(f"raw archive hash mismatch: {path}")
        commit_registration(self.db, receipt_id, row)
        return self.db.get_source_document(document_id) or row

    def capture_document(
        self,
        source: str,
        content: bytes | bytearray | str,
        *,
        source_url: str = "",
        source_ref: str = "",
        title: str = "",
        kind: str = "raw",
        suffix: str = "bin",
        mime_type: str = "",
        as_of: str = "",
        published_at: str = "",
        retrieved_at: str = "",
        quality_status: str = "ok",
        metadata: Optional[Mapping[str, Any]] = None,
        run_id: str = "",
    ) -> SourceDocumentRow:
        """以單一成功／失敗邊界保存一份文件並完成 ingestion run。"""
        try:
            run = self.start_ingestion(
                source,
                source_url=source_url,
                run_id=run_id,
                metadata={
                    "source_ref": source_ref,
                    "source_date": as_of,
                    **dict(metadata or {}),
                },
            )
            try:
                document = self.register_document(
                    source,
                    content,
                    kind=kind,
                    source_ref=source_ref,
                    url=source_url,
                    title=title,
                    suffix=suffix,
                    mime_type=mime_type,
                    as_of=as_of,
                    published_at=published_at,
                    retrieved_at=retrieved_at,
                    ingestion_run_id=run.run_id,
                    quality_status=quality_status,
                    metadata=metadata,
                )
                self.finish_ingestion(
                    run.run_id,
                    status=quality_status,
                    document_count=1,
                )
                return document
            except Exception as exc:
                self.finish_ingestion(
                    run.run_id,
                    status="failed",
                    error=str(exc),
                )
                raise
        finally:
            # Most raw captures use a short-lived ``SourceRegistry(root)``.
            # Do not leave a SQLite handle open on Windows after capture.
            self.close()

    def materialize_research_document(
        self,
        source_document_id: str,
        canonical_text: str,
        *,
        document_type: str = "research",
        symbol: str = "",
        title: str = "",
        published_at: str = "",
        chunk_chars: int = 1800,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ResearchDocumentRow:
        """由已存在 raw manifest 建立可重建 research document/chunks。

        ``source_document_id`` 必須是已存在的 exact identity；這個方法不會
        依 URL、標題或內容模糊尋找來源。
        """
        source = self.db.get_source_document(source_document_id)
        if source is None:
            raise ValueError(f"unknown source_document_id: {source_document_id}")
        if not is_source_document_replayable(self.root, source):
            raise ValueError(
                "source_document_id 無法回放 raw archive: "
                f"{source_document_id}"
            )
        text = str(canonical_text or "").strip()
        if not text:
            raise ValueError("canonical_text 不可為空")
        document_id = "research-doc:" + hashlib.sha256(
            (source_document_id + "|" + text).encode("utf-8")
        ).hexdigest()[:24]
        row = ResearchDocumentRow(
            document_id=document_id,
            document_type=document_type,
            symbol=symbol,
            title=title or source.title,
            published_at=published_at or source.published_at,
            source_document_id=source_document_id,
            canonical_text=text,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True, default=str),
        )
        self.db.upsert_research_document(row)
        size = max(200, int(chunk_chars))
        for ordinal, start in enumerate(range(0, len(text), size)):
            chunk = text[start:start + size]
            chunk_id = f"{document_id}:chunk:{ordinal}"
            self.db.upsert_research_chunk(ResearchChunkRow(
                chunk_id=chunk_id,
                document_id=document_id,
                ordinal=ordinal,
                text=chunk,
                token_count=len(chunk),
            ))
        return self.db.get_research_document(document_id) or row


def resolve_source_document_path(root: Path, source: Any) -> Path:
    """解析 manifest 的 raw path；只在本地缺檔時從 cloud cache 還原。

    不會以 cloud cache 覆蓋已存在的本地檔案，避免把 raw hash mismatch
    靜默掩蓋；呼叫端仍必須驗證 ``content_hash``。
    """
    project_root = Path(root)
    path = project_root / str(getattr(source, "content_path", "") or "")
    try:
        path.resolve().relative_to(project_root.resolve())
    except ValueError:
        # 不讓損壞或竄改的 manifest 透過 research replay 讀到專案外檔案。
        return project_root / "__invalid_source_document_path__"
    if not path.exists():
        restore_file_from_cloud(path, root=project_root, prefer_newer=False)
    return path


def is_source_document_replayable(root: Path, source: Any) -> bool:
    """判斷 manifest 是否仍能在本地精確回放原始文件。

    ``source_document_id`` 只代表 SQLite manifest 的身份；它本身不能證明
    raw archive 仍存在或沒有被改寫。問答與記憶升級的 ``exact`` 閘門採取
    保守規則：必須有相對路徑、SHA-256，檔案必須在專案根目錄內且 hash
    必須完全相同。這個檢查不會自動從 cloud cache 還原，避免讀取路徑把
    遺失的本地 artifact 靜默變成「已驗證」。
    """
    if source is None:
        return False
    project_root = Path(root).resolve()
    content_path = str(getattr(source, "content_path", "") or "").strip()
    expected_hash = str(getattr(source, "content_hash", "") or "").strip().lower()
    if not content_path or len(expected_hash) != 64:
        return False
    try:
        path = (project_root / content_path).resolve()
        path.relative_to(project_root)
        if not path.is_file():
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().lower() == expected_hash
    except (OSError, ValueError):
        return False


__all__ = [
    "SourceRegistry",
    "is_source_document_replayable",
    "resolve_source_document_path",
]
