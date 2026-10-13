"""Streamlit memory / evidence administration page.

The page is intentionally read-mostly.  Creating, approving, rejecting and
soft-deleting memory requires ``MEMORY_ADMIN_TOKEN`` in ``.env`` and an
explicit token entry in the current Streamlit session.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import asdict
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from bot.dashboard.common import PROJECT_ROOT, _badge
from bot.env_io import load_env
from bot.memory_store import MemoryStore
from bot.stock_db import StockDB, default_db_path


def _open_memory_db() -> StockDB:
    target = default_db_path(PROJECT_ROOT)
    cached = st.session_state.get("_memory_db_handle")
    if cached is None or cached.path != target:
        if cached is not None:
            cached.close()
        cached = StockDB.open(path=target)
        st.session_state["_memory_db_handle"] = cached
    return cached


def _admin_verified() -> bool:
    configured = (load_env().get("MEMORY_ADMIN_TOKEN") or "").strip()
    if not configured:
        st.warning(
            "目前是唯讀模式。請在『組態設定』設定 `MEMORY_ADMIN_TOKEN`，"
            "重新載入後才能核准、拒絕或軟刪除記憶。"
        )
        return False
    entered = st.text_input(
        "管理者驗證 token",
        type="password",
        key="memory_admin_token_input",
        help="Token 只用於目前 Streamlit session，不會寫入記憶內容。",
    )
    verified = bool(entered) and hmac.compare_digest(entered, configured)
    if entered and not verified:
        st.error("管理者 token 不正確；目前仍為唯讀模式。")
    elif verified:
        st.success("管理者驗證成功，允許執行記憶異動。")
    return verified


def _json_load(value: str, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _proposal_frame(rows: List[Any]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([
        {
            "proposal_id": row.proposal_id,
            "type": row.proposal_type,
            "layer": row.memory_layer,
            "memory_type": row.memory_type,
            "scope": f"{row.scope_type}:{row.scope_id}" if row.scope_id else row.scope_type,
            "subject": row.subject,
            "summary": row.summary,
            "quality": row.quality_status,
            "proposer": row.proposer,
            "created_at": row.created_at,
        }
        for row in rows
    ])


def _memory_frame(rows: List[Any]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([
        {
            "memory_id": row.memory_id,
            "layer": row.memory_layer,
            "type": row.memory_type,
            "scope": f"{row.scope_type}:{row.scope_id}" if row.scope_id else row.scope_type,
            "subject": row.subject,
            "summary": row.summary,
            "quality": row.quality_status,
            "status": row.memory_status,
            "importance": row.importance,
            "confidence": row.confidence,
            "updated_at": row.updated_at,
        }
        for row in rows
    ])


def _render_evidence(db: StockDB, memory_id: str = "") -> None:
    source = st.text_input("來源篩選", key="memory_evidence_source")
    rows = db.list_evidence(
        memory_id=memory_id or None,
        source=source.strip() or None,
        limit=200,
    )
    if not rows:
        st.info("查無證據。自動研究成功或核准帶 evidence packet 的記憶後，這裡會出現紀錄。")
        return
    st.dataframe(
        pd.DataFrame([
            {
                "evidence_id": row.evidence_id,
                "kind": row.kind,
                "source": row.source,
                "title": row.title,
                "observed_at": row.observed_at,
                "quality": row.quality_status,
                "url": row.url,
            }
            for row in rows
        ]),
        hide_index=True,
        use_container_width=True,
    )
    chosen = st.selectbox(
        "查看證據原文片段",
        [row.evidence_id for row in rows],
        key="memory_evidence_selected",
    )
    selected = next((row for row in rows if row.evidence_id == chosen), None)
    if selected:
        st.json(asdict(selected))


def page_memory_admin() -> None:
    """管理短／長記憶、證據與待審核提案。"""
    st.title("🧠 記憶與證據管理")
    st.caption(
        "短期／長期記憶分層保存；提案核准前不會進入可檢索記憶。"
        "所有刪除都是軟刪除，原文與稽核事件會保留。"
    )

    db = _open_memory_db()
    store = MemoryStore(db, actor="dashboard")
    admin = _admin_verified()
    stats = store.stats()
    metrics = st.columns(5)
    metrics[0].metric("短期 active", stats.get("short_active", 0))
    metrics[1].metric("長期 active", stats.get("long_active", 0))
    metrics[2].metric("已封存", stats.get("short_archived", 0) + stats.get("long_archived", 0))
    metrics[3].metric("證據", stats.get("evidence_items", 0))
    metrics[4].metric("待審核", stats.get("proposals_pending", 0))

    tabs = st.tabs(["待審核提案", "記憶瀏覽", "證據瀏覽"])

    with tabs[0]:
        pending = db.list_memory_proposals(status="pending", limit=200)
        if not pending:
            st.info("目前沒有待審核提案。")
        else:
            st.dataframe(_proposal_frame(pending), hide_index=True, use_container_width=True)
            selected_id = st.selectbox(
                "選擇提案",
                [row.proposal_id for row in pending],
                format_func=lambda value: next(
                    (f"{row.proposal_id} · {row.summary[:60]}" for row in pending if row.proposal_id == value),
                    value,
                ),
                key="memory_pending_proposal",
            )
            proposal = next((row for row in pending if row.proposal_id == selected_id), None)
            if proposal:
                st.markdown(f"**摘要：** {proposal.summary}")
                st.markdown(f"**範圍：** `{proposal.scope_type}:{proposal.scope_id}`")
                st.markdown("**完整內容：**")
                st.code(proposal.content, language="text")
                evidence_ids = _json_load(proposal.evidence_json, [])
                if evidence_ids:
                    st.write("**證據 ID：**", ", ".join(str(value) for value in evidence_ids))
                    evidence_rows = [db.get_evidence(str(value)) for value in evidence_ids]
                    evidence_rows = [row for row in evidence_rows if row is not None]
                    if evidence_rows:
                        st.dataframe(
                            pd.DataFrame([asdict(row) for row in evidence_rows]),
                            hide_index=True,
                            use_container_width=True,
                        )
                note = st.text_area(
                    "審核備註",
                    key=f"memory_review_note_{selected_id}",
                    placeholder="例如：已對照官方公告；或說明拒絕原因",
                )
                c1, c2 = st.columns(2)
                if c1.button("✅ 核准並寫入記憶", disabled=not admin, type="primary", use_container_width=True):
                    item = store.approve_proposal(
                        selected_id, reviewer="dashboard", review_note=note,
                    )
                    if item is None:
                        st.error("提案核准失敗，可能已被其他管理者處理。")
                    else:
                        st.success(f"已核准並寫入 `{item.memory_id}`")
                        st.rerun()
                if c2.button("⛔ 拒絕提案", disabled=not admin, use_container_width=True):
                    if store.reject_proposal(selected_id, reviewer="dashboard", review_note=note):
                        st.success("提案已拒絕並留下稽核事件。")
                        st.rerun()

    with tabs[1]:
        c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
        layer = c1.selectbox("層", ["全部", "short", "long"], key="memory_view_layer")
        status = c2.selectbox("狀態", ["active", "archived", "superseded", "deleted", "全部"], key="memory_view_status")
        scope_type = c3.text_input("scope type", key="memory_view_scope_type")
        query = c4.text_input("搜尋內容", key="memory_view_query")
        if query.strip():
            rows = store.search(
                query.strip(),
                layers=("short", "long") if layer == "全部" else (layer,),
                scope_type=scope_type.strip() or None,
                include_low_quality=True,
                limit=200,
            )
            if status != "全部":
                rows = [row for row in rows if row.memory_status == status]
        else:
            rows = db.list_memory_items(
                memory_layer=None if layer == "全部" else layer,
                memory_status=None if status == "全部" else status,
                scope_type=scope_type.strip() or None,
                limit=200,
            )
        if not rows:
            st.info("查無記憶。")
        else:
            st.dataframe(_memory_frame(rows), hide_index=True, use_container_width=True)
            selected_memory = st.selectbox(
                "查看完整記憶",
                [row.memory_id for row in rows],
                key="memory_view_selected",
            )
            item = next((row for row in rows if row.memory_id == selected_memory), None)
            if item:
                st.code(item.content, language="text")
                linked = db.list_memory_evidence(item.memory_id)
                if linked:
                    st.caption(f"已關聯 {len(linked)} 筆證據")
                    st.dataframe(pd.DataFrame([asdict(row) for row in linked]), hide_index=True, use_container_width=True)
                if item.memory_status not in {"deleted", "archived"}:
                    if st.button("軟刪除此記憶", disabled=not admin, key=f"memory_delete_{item.memory_id}"):
                        if store.delete(item.memory_id):
                            st.success("記憶已軟刪除，原文仍保留。")
                            st.rerun()

    with tabs[2]:
        _render_evidence(db)


__all__ = ["page_memory_admin"]
