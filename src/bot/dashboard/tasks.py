"""Streamlit task controls. All session state access stays on the UI thread."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from zoneinfo import ZoneInfo

import streamlit as st

from bot.dashboard.common import PROJECT_ROOT, _human_duration
from bot.dashboard.task_service import ACTIVE, get_task_service

LABELS = {"queued": "排隊中", "running": "執行中", "succeeded": "已完成",
          "partial": "部分完成", "failed": "失敗", "interrupted": "已中斷", "cancelled": "已取消",
          "timed_out": "已逾時", "termination_failed": "需人工停止工作程序"}
SLOTS = {"ticker": "detail", "watchlist": "watchlist", "research": "research",
         "pipeline": "pipeline", "intraday": "intraday", "nextday": "nextday",
         "presentation": "presentation", "macro": "macro", "movers": "movers"}
SLOTS.update({"portfolio_bundle": "portfolio_bundle", "portfolio_analysis": "portfolio_analysis", "macro_brief": "macro_brief"})
SLOTS.update({"logic_check": "logic_check", "live_review": "live_review"})


def service():
    return get_task_service(str(PROJECT_ROOT))


def timestamp(value):
    return dt.datetime.fromtimestamp(value, ZoneInfo("Asia/Taipei")).strftime("%m/%d %H:%M:%S")


def submit_task(slot, kind, title, page, params):
    try:
        task_id = service().submit(kind, title, page, params)
    except (ValueError, OSError, sqlite3.Error) as exc:
        st.error(str(exc))
        return
    st.session_state[f"task:{slot}"] = task_id
    st.toast("任務已送出，可切換頁面；進度與結果保存在任務中心。")
    st.rerun()


def find_task(slot, kind, match=None):
    match = match or {}
    task_id = st.session_state.get(f"task:{slot}")
    job = service().get(task_id) if task_id else None
    if job and job["kind"] == kind and all(job["params"].get(k) == v for k, v in match.items()):
        return job
    return service().latest(kind, match)


def task_result(slot, kind, match=None):
    job = find_task(slot, kind, match)
    if not job:
        return None
    elapsed = _human_duration((job["finished"] or time.time()) - (job["started"] or job["created"]))
    st.caption(f"{LABELS[job['state']]} · {job['title']} · {timestamp(job['created'])} · {elapsed}")
    if job["state"] in ACTIVE:
        st.info(f"{job['message']}。切換頁面或重新整理後仍會繼續，可至「任務中心」追蹤。")
        if job["progress"] is not None:
            st.progress(job["progress"])
    elif job["state"] in ("failed", "interrupted", "timed_out", "termination_failed"):
        st.error(job["error"] or job["message"])
    if job["result"]:
        for warning in job["result"].get("warnings", []):
            st.warning(warning)
        return job["result"]["data"]
    return None


@st.fragment(run_every=2)
def task_indicator():
    rows = service().recent()
    active = [r for r in rows if r["state"] in ACTIVE]
    observed = st.session_state.get("task_observed", {})
    changed = [r for r in rows if observed.get(r["id"]) in ACTIVE and r["state"] not in ACTIVE]
    st.session_state["task_observed"] = {r["id"]: r["state"] for r in rows}
    if changed:
        st.toast(f"{changed[0]['title']}：{LABELS[changed[0]['state']]}")
        st.rerun()
    with st.container(border=True):
        st.markdown(f"**背景任務 · {len(active)} 個進行中**" if active else "**背景任務 · 目前閒置**")
        if active:
            running = next((r for r in active if r["state"] == "running"), active[-1])
            st.caption(f"{running['title']} · {LABELS[running['state']]}")
            st.caption(running["message"])
        elif rows:
            st.caption(f"最近：{rows[0]['title']} · {LABELS[rows[0]['state']]}")
        else:
            st.caption("分析與搜尋送出後，都能在這裡追蹤。")
        if st.button("開啟任務中心", key="task_center_quick", use_container_width=True):
            st.session_state.page = "任務中心"
            st.rerun()


def open_task(job):
    st.session_state[f"task:{SLOTS[job['kind']]}"] = job["id"]
    if job["kind"] == "ticker":
        st.session_state["detail_ticker"] = job["params"]["tickers"][0]
        st.session_state["detail_symbol"] = job["params"]["tickers"][0]
    elif job["kind"] == "research":
        st.session_state["auto_research_tickers"] = ",".join(job["params"]["tickers"])
    elif job["kind"] == "intraday":
        st.session_state["intra_view_date"] = dt.date.fromisoformat(job["params"]["date"])
    elif job["kind"] == "nextday":
        st.session_state["nextday_target_date"] = dt.date.fromisoformat(job["params"]["date"])
        st.session_state["nextday_view_mode"] = job["params"]["mode"]
    elif job["kind"] == "movers":
        st.session_state["movers_direction"] = job["params"]["direction"]
        st.session_state["movers_exclude_etf"] = job["params"]["exclude_etf"]
        st.session_state["movers_limit"] = job["params"]["limit"]
    elif job["kind"] in ("presentation", "logic_check"):
        from bot.llm_analyzer import PresentationAnalysis
        st.session_state["llm_ticker"] = job["params"]["ticker"]
        if job["kind"] == "presentation":
            st.session_state["llm_text"] = job["params"]["text"]
            st.session_state["last_llm_analysis"] = None
        else:
            st.session_state["last_llm_analysis"] = PresentationAnalysis(**job["params"]["analysis"])
    st.session_state.page = job["page"]
    st.rerun()


def page_tasks():
    st.title("任務中心")
    st.caption("切換頁面、重新整理或關閉瀏覽器，已送出的背景任務仍會繼續。")
    st.caption("電腦與 Dashboard 服務須維持運作。服務中斷的任務會標為已中斷，不會自動重跑 LLM；已保存的結果仍可查看。")
    _task_list()


@st.fragment(run_every=2)
def _task_list():
    search, size = st.columns([3, 1])
    query = search.text_input("搜尋任務", placeholder="輸入股票代號或任務名稱")
    limit = size.selectbox("顯示筆數", [50, 100, 250, 1000])
    rows = service().recent(limit, query.strip())
    if not rows:
        st.info("沒有符合的任務。可調整搜尋條件，或從個股深入分析、LLM 自動研究開始。")
        return
    mode = st.radio("顯示任務", ["全部", "進行中", "需要處理", "已完成"], horizontal=True)
    filtered = [r for r in rows if mode == "全部"
                or (mode == "進行中" and r["state"] in ACTIVE)
                or (mode == "需要處理" and r["state"] in ("failed", "interrupted", "partial", "timed_out", "termination_failed"))
                or (mode == "已完成" and r["state"] == "succeeded")]
    if not filtered:
        st.info("這個分類目前沒有任務。")
    for row in filtered:
        with st.container(border=True):
            main, action = st.columns([4, 1])
            main.markdown(f"**{row['title']}** · {LABELS[row['state']]}")
            elapsed = (row["finished"] or time.time()) - (row["started"] or row["created"])
            main.caption(f"{timestamp(row['created'])} · {_human_duration(elapsed)} · {row['message']}")
            if row["error"]:
                main.error(row["error"])
            if row["progress"] is not None and row["state"] in ACTIVE:
                main.progress(row["progress"])
            if action.button("查看結果" if row["state"] in ("succeeded", "partial") else "回到頁面", key=f"open_{row['id']}", use_container_width=True):
                open_task(service().get(row["id"]))
            if row["state"] == "queued" and action.button("取消排隊", key=f"cancel_{row['id']}"):
                service().cancel_queued(row["id"])
                st.rerun()
            if st.button("設定、下載與重試", key=f"inspect_{row['id']}"):
                st.session_state["inspect_task"] = row["id"]
    selected = st.session_state.get("inspect_task")
    job = service().get(selected) if selected else None
    if job:
        st.subheader(f"任務詳情 · {job['title']}")
        st.json(job["params"], expanded=False)
        if job["result"]:
            st.json(job["result"], expanded=False)
            st.download_button("下載結果 JSON", json.dumps(job["result"], ensure_ascii=False, indent=2),
                               file_name=f"stock-task-{job['id'][:8]}.json", mime="application/json", key=f"dl_{job['id']}")
        if job["state"] in ("failed", "interrupted", "partial", "cancelled", "timed_out"):
            st.caption("重試會使用原設定，可能再次呼叫外部資料源或 LLM。")
            if st.button("以原設定重試", key=f"retry_{job['id']}"):
                submit_task(SLOTS[job["kind"]], job["kind"], job["title"], job["page"], job["params"])
