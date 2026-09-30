"""唯讀的整合層前置檢查。

``stock-preflight`` 的責任是回答「交易設定現在能不能下單」，不適合再
塞入研究資料庫、LLM 與 Google Sheets 的狀態。本模組提供另一個明確的
系統檢查入口，回答「本地研究核心是否完整、可追溯，以及可選服務是否
已配置」。

設計邊界：

* 只讀 SQLite、raw manifest 與設定檔；不執行 fetch、LLM、同步或下單。
* 不把缺少 Gemini／Google 憑證當成本地核心故障。
* provenance 只接受現有 exact manifest，絕不依 URL、標題或相似度補猜。
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from bot.config import Settings
from bot.evidence import resolve_provenance_status
from bot.memory_store import LONG_MEMORY_EVIDENCE_REQUIRED_TYPES, MemoryPolicy
from bot.source_audit_cli import audit_project
from bot.source_registry import is_source_document_replayable
from bot.utils import now_tw


@dataclass
class SystemCheck:
    name: str
    status: str  # ok | warn | fail | info
    detail: str = ""
    suggestion: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemPreflightReport:
    fetched_at: str
    root: str
    status: str = "ready"  # ready | partial | blocked
    checks: List[SystemCheck] = field(default_factory=list)
    summary: str = ""

    def has_fail(self) -> bool:
        return any(item.status == "fail" for item in self.checks)

    def has_warn(self) -> bool:
        return any(item.status == "warn" for item in self.checks)


def _credential_file_state(value: str, root: Path) -> tuple[str, str]:
    """Return ``(status, detail)`` without exposing credential contents."""
    raw = str(value or "").strip()
    if not raw:
        return "missing", "未設定"
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return "invalid", "Service Account JSON 不是有效 JSON"
        if not isinstance(parsed, dict) or not parsed.get("client_email"):
            return "invalid", "Service Account JSON 缺少 client_email"
        return "ok", "已設定 JSON 內容（不顯示密鑰）"

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (root / path).resolve()
    if not path.is_file():
        return "missing", f"檔案不存在：{path}"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return "invalid", "Service Account 檔案無法解析為 JSON"
    if not isinstance(parsed, dict) or not parsed.get("client_email"):
        return "invalid", "Service Account JSON 缺少 client_email"
    return "ok", f"檔案存在且格式正確：{path.name}（不顯示密鑰）"


def _audit_checks(root: Path, report: Dict[str, Any]) -> List[SystemCheck]:
    checks: List[SystemCheck] = []
    database = report.get("database") or {}
    if not database.get("exists"):
        return [SystemCheck(
            "本地 SQLite 核心",
            "fail",
            "data/stock.db 不存在，無法提供本地研究與記憶核心",
            "先執行一次資料初始化或使用正確的 --root／STOCK_DB_PATH",
        )]

    integrity = database.get("integrity") or {}
    sqlite_integrity = integrity.get("sqlite") or {}
    sqlite_status = str(sqlite_integrity.get("status") or "unknown")
    if sqlite_status == "ok":
        checks.append(SystemCheck(
            "SQLite 完整性",
            "ok",
            "PRAGMA integrity_check=ok",
            extra={"details": sqlite_integrity.get("details") or []},
        ))
    else:
        checks.append(SystemCheck(
            "SQLite 完整性",
            "fail",
            "PRAGMA integrity_check 未通過",
            "停止寫入，先從最近備份還原並重新稽核",
            extra={"details": sqlite_integrity.get("details") or []},
        ))

    raw = report.get("raw_archive") or {}
    missing = list(raw.get("missing") or [])
    mismatched = list(raw.get("mismatched") or [])
    if missing or mismatched:
        checks.append(SystemCheck(
            "raw archive 可重播性",
            "fail",
            f"missing={len(missing)}、hash_mismatch={len(mismatched)}",
            "不可把缺失或 hash 不符的 raw 當成可驗證來源；先從備份或來源重新建立 exact archive",
            extra={"missing": missing[:20], "mismatched": mismatched[:20]},
        ))
    else:
        checks.append(SystemCheck(
            "raw archive 可重播性",
            "ok",
            f"已檢查 {int(raw.get('checked') or 0)} 份 raw，無缺檔或 hash 不符",
            extra={"checked": int(raw.get("checked") or 0)},
        ))
    hash_unavailable = list(raw.get("hash_unavailable") or [])
    if hash_unavailable:
        checks.append(SystemCheck(
            "raw archive hash 覆蓋率",
            "warn",
            f"有 {len(hash_unavailable)} 份 manifest 沒有 content_hash",
            "未取得 hash 前只能視為不可完整驗證的歷史來源，不要升格為 exact evidence",
            extra={"hash_unavailable": hash_unavailable[:20]},
        ))

    orphan_counts = integrity.get("orphan_counts") or {}
    orphan_total = sum(int(value or 0) for value in orphan_counts.values())
    if orphan_total:
        checks.append(SystemCheck(
            "provenance 關聯",
            "fail",
            f"發現 {orphan_total} 筆孤立連結",
            "保留原始證據，修復 exact foreign-key 關聯；不得用相似度猜補",
            extra={"orphan_counts": orphan_counts},
        ))
    else:
        checks.append(SystemCheck("provenance 關聯", "ok", "canonical／research／evidence 無孤立連結"))

    fts = integrity.get("fts") or {}
    inconsistent = [
        name for name, value in fts.items()
        if isinstance(value, dict) and value.get("consistent") is False
    ]
    if inconsistent:
        checks.append(SystemCheck(
            "FTS 檢索索引",
            "warn",
            "索引與基礎表筆數不一致：" + ", ".join(inconsistent),
            "重建 FTS 後再把它用於正式回答；SQLite 基礎資料仍保留",
            extra={"inconsistent": inconsistent},
        ))
    else:
        checks.append(SystemCheck("FTS 檢索索引", "ok", "可用索引與基礎表計數一致"))

    gaps = list(report.get("gaps") or [])
    warnings = [gap for gap in gaps if str(gap.get("severity")) == "warning"]
    errors = [gap for gap in gaps if str(gap.get("severity")) == "error"]
    # SQLite/raw/orphan 已個別列出；這裡仍保留完整 gap 摘要，讓管理者知道
    # legacy source run 或 research semantic warning 沒有被藏起來。
    if errors:
        checks.append(SystemCheck(
            "資料稽核缺口",
            "fail",
            f"有 {len(errors)} 個 error gap",
            "先處理 error gap，再允許依賴完整 provenance 的回答流程",
            extra={"codes": [str(gap.get("code") or "") for gap in errors]},
        ))
    elif warnings:
        checks.append(SystemCheck(
            "資料稽核缺口",
            "warn",
            f"有 {len(warnings)} 個 warning gap；不代表資料不存在",
            "保留 exact-only 與不確定性標示；legacy rows 不要臆造來源",
            extra={"codes": [str(gap.get("code") or "") for gap in warnings]},
        ))
    else:
        checks.append(SystemCheck("資料稽核缺口", "ok", "沒有 provenance gap"))

    return checks


def _service_checks(settings: Any, root: Path) -> List[SystemCheck]:
    checks: List[SystemCheck] = []
    llm_provider = str(getattr(settings, "llm_provider", "gemini") or "gemini").strip()
    if llm_provider == "codex_app_server":
        model = str(getattr(settings, "codex_app_server_model", "") or "").strip()
        checks.append(SystemCheck(
            "Codex App Server LLM 設定",
            "ok" if getattr(settings, "llm_enabled", False) else "warn",
            f"提供者={llm_provider}；模型={model or '(空白)'}；未執行模型呼叫",
            "執行一次 stock-ask 或 stock-auto-research 確認實際連線",
            extra={"configured": bool(getattr(settings, "llm_enabled", False))},
        ))
    gemini_key = str(getattr(settings, "gemini_api_key", "") or "").strip()
    gemini_model = str(getattr(settings, "gemini_model", "") or "").strip()
    if gemini_key and gemini_model:
        checks.append(SystemCheck(
            "Gemini LLM 設定",
            "ok",
            f"已設定模型 {gemini_model}（不執行 API 呼叫）",
            extra={"configured": True, "model": gemini_model},
        ))
    elif gemini_key:
        checks.append(SystemCheck(
            "Gemini LLM 設定", "warn",
            "有 API key 但模型名稱為空",
            "設定 GEMINI_MODEL，或使用預設模型",
            extra={"configured": False},
        ))
    elif llm_provider == "gemini":
        checks.append(SystemCheck(
            "Gemini LLM 設定",
            "info",
            "未設定 GEMINI_API_KEY；!ask --no-llm 與本地資料管線仍可使用",
            "需要 LLM 分析時再於 .env 設定，並另外執行 stock-gemini-test",
            extra={"configured": False, "llm_disabled": True},
        ))
    else:
        checks.append(SystemCheck(
            "Gemini LLM 設定", "info",
            "目前選用 Codex App Server；Gemini API 未啟用",
            extra={"configured": False, "selected": False},
        ))

    sheet_id = str(getattr(settings, "google_sheet_id", "") or "").strip()
    sa_value = str(getattr(settings, "google_sa_json_path", "") or "").strip()
    sa_status, sa_detail = _credential_file_state(sa_value, root)
    if not sheet_id and not sa_value:
        checks.append(SystemCheck(
            "Google Sheets 同步設定", "info",
            "未啟用雲端同步；本地 SQLite 為 SSOT",
            "若需要多機同步，再設定 GOOGLE_SHEET_ID 與 GOOGLE_SA_JSON_PATH",
            extra={"configured": False, "live_ping": False},
        ))
    elif sheet_id and sa_status == "ok":
        checks.append(SystemCheck(
            "Google Sheets 同步設定", "ok",
            f"Sheet ID 與 Service Account 已設定；{sa_detail}（未執行 live ping）",
            "需要實際同步時另執行 stock-validate --cloud 或 stock-cloud-sync",
            extra={"configured": True, "live_ping": False},
        ))
    else:
        checks.append(SystemCheck(
            "Google Sheets 同步設定", "warn",
            f"設定不完整或憑證無效：sheet_id={'有' if sheet_id else '無'}；{sa_detail}",
            "補齊兩個設定；未完成前不要執行雲端同步",
            extra={"configured": False, "live_ping": False},
        ))
    return checks


def _decode_json_object(value: str) -> Dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _evidence_refs(evidence_json: str) -> tuple[Optional[List[str]], bool]:
    """回傳 ``(refs, ok)``；``ok=False`` 表示 JSON 無法解析成陣列。"""
    try:
        decoded = json.loads(evidence_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, False
    if not isinstance(decoded, list):
        return None, False
    refs: List[str] = []
    for value in decoded:
        if isinstance(value, dict):
            value = value.get("id") or value.get("evidence_id") or ""
        text = str(value or "").strip()
        if text:
            refs.append(text)
    return refs, True


def _long_research_provenance_checks(
    root: Path,
    conn: sqlite3.Connection,
) -> List[SystemCheck]:
    """唯讀檢查 active 長期研究／報告／決策的 exact＋replayable evidence。"""
    rows = conn.execute(
        "SELECT memory_id, memory_type, evidence_json FROM memory_items "
        "WHERE memory_status='active' AND memory_layer='long'"
    ).fetchall()
    evidence_rows = {
        str(row[0]): row
        for row in conn.execute(
            "SELECT evidence_id, source_document_id, metadata_json "
            "FROM evidence_items"
        ).fetchall()
    }
    source_rows = {
        str(row[0]): row
        for row in conn.execute(
            "SELECT document_id, content_path, content_hash FROM source_documents"
        ).fetchall()
    }

    empty_ids: List[str] = []
    invalid_ids: List[str] = []
    unresolved_ids: List[str] = []
    missing_ids: List[str] = []
    non_replayable_ids: List[str] = []

    for memory_id, memory_type, evidence_json in rows:
        if str(memory_type or "") not in LONG_MEMORY_EVIDENCE_REQUIRED_TYPES:
            continue
        mid = str(memory_id)
        refs, ok = _evidence_refs(str(evidence_json or "[]"))
        if not ok:
            invalid_ids.append(mid)
            continue
        assert refs is not None
        if not refs:
            empty_ids.append(mid)
            continue
        for ref in refs:
            evidence = evidence_rows.get(ref)
            if evidence is None:
                missing_ids.append(mid)
                break
            metadata = _decode_json_object(str(evidence[2] or "{}"))
            provenance = resolve_provenance_status(
                source_document_id=str(evidence[1] or ""),
                provenance_status=str(metadata.get("provenance_status") or ""),
                metadata=metadata,
            )
            if provenance not in {"exact", "composite_exact"}:
                unresolved_ids.append(mid)
                break
            document_ids: List[str] = []
            primary = str(evidence[1] or "").strip()
            if primary:
                document_ids.append(primary)
            linked = metadata.get("source_document_ids")
            if isinstance(linked, list):
                document_ids.extend(
                    str(value).strip() for value in linked if str(value).strip()
                )
            document_ids = list(dict.fromkeys(document_ids))
            if not document_ids:
                unresolved_ids.append(mid)
                break
            replayable = True
            for document_id in document_ids:
                source = source_rows.get(document_id)
                if source is None:
                    replayable = False
                    break
                document = SimpleNamespace(
                    content_path=str(source[1] or ""),
                    content_hash=str(source[2] or ""),
                )
                if not is_source_document_replayable(root, document):
                    replayable = False
                    break
            if not replayable:
                non_replayable_ids.append(mid)
                break

    flagged = (
        empty_ids or invalid_ids or unresolved_ids or missing_ids or non_replayable_ids
    )
    if not flagged:
        return [SystemCheck(
            "長期研究記憶 provenance",
            "ok",
            "active research_summary／daily_report／decision 皆有可回放 exact evidence",
        )]

    detail_parts: List[str] = []
    if empty_ids:
        detail_parts.append(f"{len(empty_ids)} 筆缺少 evidence")
    if invalid_ids:
        detail_parts.append(f"{len(invalid_ids)} 筆 evidence_json 無法解析")
    if missing_ids:
        detail_parts.append(f"{len(missing_ids)} 筆引用不存在的 evidence")
    if unresolved_ids:
        detail_parts.append(
            f"{len(unresolved_ids)} 筆非 exact／composite_exact 或缺少 source_document_id"
        )
    if non_replayable_ids:
        detail_parts.append(f"{len(non_replayable_ids)} 筆 source_document 無法 exact 回放")
    return [SystemCheck(
        "長期研究記憶 provenance",
        "fail",
        "；".join(detail_parts),
        "保留記憶原文，只接受 exact／composite_exact 且 raw archive 可回放的 evidence；"
        "不得依 URL／標題／相似度猜補，也不可把空 evidence 當成已驗證來源",
        extra={
            "empty_evidence_memory_ids": empty_ids[:30],
            "invalid_evidence_memory_ids": invalid_ids[:30],
            "missing_evidence_memory_ids": missing_ids[:30],
            "non_exact_memory_ids": unresolved_ids[:30],
            "non_replayable_memory_ids": non_replayable_ids[:30],
        },
    )]


def _memory_checks(root: Path) -> List[SystemCheck]:
    """唯讀檢查 active memory 的 evidence 引用與容量。"""
    db_path = root / "data" / "stock.db"
    if not db_path.is_file():
        return [SystemCheck(
            "記憶 evidence 與容量",
            "info",
            "SQLite 尚未建立，未執行記憶層檢查",
        )]

    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            memory_rows = conn.execute(
                "SELECT memory_id, memory_layer, content, evidence_json "
                "FROM memory_items WHERE memory_status='active'"
            ).fetchall()
            evidence_ids = {
                str(row[0])
                for row in conn.execute("SELECT evidence_id FROM evidence_items")
            }
            linked_by_memory: Dict[str, set[str]] = {}
            for memory_id, evidence_id in conn.execute(
                "SELECT memory_id, evidence_id FROM memory_evidence"
            ).fetchall():
                linked_by_memory.setdefault(str(memory_id), set()).add(str(evidence_id))
            long_research_checks = _long_research_provenance_checks(root, conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - expose the read-only boundary
        return [SystemCheck(
            "記憶 evidence 與容量",
            "fail",
            f"唯讀記憶檢查失敗：{type(exc).__name__}: {exc}",
            "保留 DB 原狀，先執行 stock-data-audit 檢查 schema 與 SQLite 完整性",
        )]

    invalid_ids: List[str] = []
    misaligned_ids: List[str] = []
    active_counts = {"short": 0, "long": 0}
    active_chars = {"short": 0, "long": 0}
    for memory_id, layer, content, evidence_json in memory_rows:
        layer_text = str(layer or "")
        if layer_text in active_counts:
            active_counts[layer_text] += 1
            active_chars[layer_text] += len(str(content or ""))
        refs, ok = _evidence_refs(str(evidence_json or "[]"))
        if not ok:
            invalid_ids.append(str(memory_id))
            continue
        assert refs is not None
        if any(ref not in evidence_ids for ref in refs):
            invalid_ids.append(str(memory_id))
            continue
        linked = linked_by_memory.get(str(memory_id), set())
        # evidence_json 與 memory_evidence 必須對齊；不得依文字相似度猜補連結。
        if set(refs) != linked:
            misaligned_ids.append(str(memory_id))

    checks: List[SystemCheck] = []
    if invalid_ids or misaligned_ids:
        detail_parts: List[str] = []
        if invalid_ids:
            detail_parts.append(
                f"{len(invalid_ids)} 筆 active memory 有無效或無法解析的 evidence 引用"
            )
        if misaligned_ids:
            detail_parts.append(
                f"{len(misaligned_ids)} 筆 active memory 的 evidence_json 與 memory_evidence 未對齊"
            )
        checks.append(SystemCheck(
            "記憶 evidence 與容量",
            "fail",
            "；".join(detail_parts),
            "保留記憶原文，修復 exact memory_evidence 關聯；不得依文字相似度猜補",
            extra={
                "invalid_memory_ids": invalid_ids[:30],
                "misaligned_memory_ids": misaligned_ids[:30],
            },
        ))
    else:
        checks.append(SystemCheck(
            "記憶 evidence 與容量",
            "ok",
            f"active short={active_counts['short']}、long={active_counts['long']}，evidence 引用均可解析",
            extra={
                "active_short": active_counts["short"],
                "active_long": active_counts["long"],
            },
        ))

    checks.extend(long_research_checks)

    policy = MemoryPolicy()
    limits = {
        "short": (policy.max_active_short_items, policy.max_active_short_chars),
        "long": (policy.max_active_long_items, policy.max_active_long_chars),
    }
    ratios = {
        layer: {
            "item_ratio": active_counts[layer] / max_items if max_items else 1.0,
            "char_ratio": active_chars[layer] / max_chars if max_chars else 1.0,
        }
        for layer, (max_items, max_chars) in limits.items()
    }
    warning_layers = [
        layer for layer, ratio in ratios.items()
        if max(ratio["item_ratio"], ratio["char_ratio"]) >= policy.capacity_warning_ratio
    ]
    if warning_layers:
        checks.append(SystemCheck(
            "記憶容量",
            "warn",
            "容量達到 80% 警戒：" + ", ".join(warning_layers),
            "執行記憶濃縮／封存；只封存低價值項目，不硬刪原文",
            extra={
                "counts": active_counts,
                "chars": active_chars,
                "ratios": ratios,
                "warning_ratio": policy.capacity_warning_ratio,
            },
        ))
    else:
        checks.append(SystemCheck(
            "記憶容量",
            "ok",
            "active short／long 均低於 80% 容量警戒",
            extra={"counts": active_counts, "chars": active_chars, "ratios": ratios},
        ))
    return checks


def _import_checks() -> List[SystemCheck]:
    import bot

    failures: List[Dict[str, str]] = []
    module_names: List[str] = []
    for module_info in pkgutil.walk_packages(bot.__path__, bot.__name__ + "."):
        module_names.append(module_info.name)
    for name in sorted(module_names):
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - report module boundary only
            failures.append({"module": name, "error": f"{type(exc).__name__}: {exc}"})
    if failures:
        return [SystemCheck(
            "bot 模組 import sweep",
            "fail",
            f"{len(failures)}/{len(module_names)} 個模組 import 失敗",
            "先修復 import 失敗；這通常會阻斷對應 CLI 或排程工作",
            extra={"modules_checked": len(module_names), "failures": failures[:30]},
        )]
    return [SystemCheck(
        "bot 模組 import sweep", "ok",
        f"{len(module_names)} 個模組全部可 import",
        extra={"modules_checked": len(module_names)},
    )]


def run_system_preflight(
    root: Path,
    *,
    settings: Optional[Any] = None,
    check_imports: bool = False,
) -> SystemPreflightReport:
    """執行整合檢查；預設不碰網路、不呼叫 LLM、不寫入 DB。"""
    project_root = Path(root).resolve()
    if settings is None:
        settings = Settings(_env_file=str(project_root / ".env"))  # type: ignore[call-arg]
    result = SystemPreflightReport(
        fetched_at=now_tw().isoformat(timespec="seconds"),
        root=str(project_root),
    )
    try:
        audit = audit_project(project_root)
        result.checks.extend(_audit_checks(project_root, audit))
    except Exception as exc:  # noqa: BLE001 - the check must explain its boundary
        result.checks.append(SystemCheck(
            "本地資料稽核", "fail",
            f"唯讀稽核失敗：{type(exc).__name__}: {exc}",
            "保留 DB 與 raw 原狀，先單獨執行 stock-data-audit 取得完整 traceback",
        ))
    result.checks.extend(_memory_checks(project_root))
    result.checks.extend(_service_checks(settings, project_root))
    if check_imports:
        try:
            result.checks.extend(_import_checks())
        except Exception as exc:  # noqa: BLE001
            result.checks.append(SystemCheck(
                "bot 模組 import sweep", "fail",
                f"import sweep 無法完成：{type(exc).__name__}: {exc}",
            ))
    else:
        result.checks.append(SystemCheck(
            "bot 模組 import sweep", "info",
            "本次未執行；加上 --imports 才會逐一載入所有 bot 模組",
        ))

    if result.has_fail():
        result.status = "blocked"
        result.summary = "❌ 本地整合核心有阻擋項，不能宣稱所有模組可安全整合"
    elif result.has_warn():
        result.status = "partial"
        result.summary = "⚠️ 本地核心可用，但仍有需處理的品質或配置警告"
    else:
        result.status = "ready"
        result.summary = "✅ 本地整合核心可用；可選外部服務依設定狀態使用"
    return result


def report_to_dict(report: SystemPreflightReport) -> Dict[str, Any]:
    return {
        "fetched_at": report.fetched_at,
        "root": report.root,
        "status": report.status,
        "summary": report.summary,
        "checks": [asdict(item) for item in report.checks],
    }


__all__ = [
    "SystemCheck",
    "SystemPreflightReport",
    "run_system_preflight",
    "report_to_dict",
]
