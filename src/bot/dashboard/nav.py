"""Dashboard navigation and main Streamlit app shell."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import streamlit as st

from bot.dashboard.common import (
    DASHBOARD_UI_BUILD,
    PROJECT_ROOT,
    _badge,
    _debug_log_session,
    _human_duration,
    write_dashboard_heartbeat,
)
from bot.dashboard import pages as dash_pages
from bot.dashboard.workspace import apply_theme, brand, page_updates
from bot.dashboard.tasks import task_indicator, page_tasks
from bot.dashboard.cache_helpers import cached_env_values, cached_llm_today_stats
from bot.env_io import env_path, llm_provider_ready
from bot.process_runner import get_runner, get_scheduler_runner
from bot.prompt_registry import get_registry


def _read_scheduler_status() -> dict:
    path = PROJECT_ROOT / "data" / "scheduler_status.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _render_sidebar_quick_controls() -> None:
    env_values = cached_env_values()
    bot_runner = get_runner(PROJECT_ROOT)
    scheduler_runner = get_scheduler_runner(PROJECT_ROOT)
    bot_running = bot_runner.is_running()
    scheduler_running = scheduler_runner.is_running()

    st.markdown("#### 快速啟動")
    st.markdown(
        f"{_badge('自動更新 ON' if scheduler_running else '自動更新 OFF', 'green' if scheduler_running else 'gray')} "
        f"{_badge('BOT ON' if bot_running else 'BOT OFF', 'green' if bot_running else 'gray')}",
        unsafe_allow_html=True,
    )

    include_reports = st.toggle(
        "包含當沖 LLM 報告",
        value=True,
        key="quick_auto_llm_reports",
        disabled=scheduler_running,
        help=(
            "啟動自動更新時，臨時打開 stock-intraday、stock-nextday draft/update。"
            "同一天已有報告時會跳過，避免重複呼叫 Gemini。"
        ),
    )
    if include_reports and not llm_provider_ready(env_values):
        st.caption("尚未設定可用的 LLM 提供者；當沖報告會無法產生 LLM 簡報。")

    c1, c2 = st.columns(2)
    if c1.button("啟動自動更新", disabled=scheduler_running, use_container_width=True):
        rec = scheduler_runner.start(
            run_mode="scheduler",
            extra_env=dash_pages._quick_scheduler_env(include_reports),
        )
        st.success(f"自動更新已啟動 PID {rec.pid}")
        st.rerun()
    if c2.button("停止更新", disabled=not scheduler_running, use_container_width=True):
        ok = scheduler_runner.stop()
        st.success("自動更新已停止" if ok else "自動更新停止逾時")
        st.rerun()

    scheduler_record = scheduler_runner.current()
    if scheduler_running and scheduler_record:
        elapsed = time.time() - scheduler_record.started_at
        st.caption(f"更新 PID {scheduler_record.pid} · {_human_duration(elapsed)}")
        status = _read_scheduler_status()
        # `uv run` / console-script 啟動時，wrapper PID 可能和 scheduler
        # 子行程 PID 不同；只要狀態檔是有效的就顯示真實 heartbeat。
        if status:
            state = str(status.get("state") or "unknown")
            last_job = str(status.get("last_job") or "尚未執行任務")
            last_at = str(status.get("last_job_at") or status.get("heartbeat_at") or "")
            status_pid = status.get("pid") or "?"
            st.caption(
                f"排程狀態: {state} · worker PID {status_pid} · 最近任務: {last_job} {last_at}"
            )
            jobs = status.get("jobs") or {}
            failed_jobs = [name for name, entry in jobs.items()
                           if entry.get("state") in {"failed", "timed_out", "interrupted"}]
            if failed_jobs:
                st.warning("尚未成功重跑的任務：" + "、".join(failed_jobs))
            elif (not jobs and status.get("last_success") is False) or state == "degraded":
                st.warning(f"排程最近一次任務失敗：{status.get('last_error') or '請查看日誌'}")
            if jobs:
                labels = {"running": "執行中", "succeeded": "成功", "failed": "失敗",
                          "timed_out": "逾時", "interrupted": "中斷", "dry_run": "僅演練"}
                with st.expander("各任務執行結果"):
                    for name, entry in sorted(jobs.items()):
                        job_state = str(entry.get("state") or "unknown")
                        st.caption(f"{name}：{labels.get(job_state, job_state)}")
                        st.caption(f"最近成功：{entry.get('last_success_at') or '尚無成功紀錄'}")
                        if entry.get("error"):
                            st.caption(str(entry["error"]))
        else:
            st.caption("排程正在啟動，等待第一個心跳...")

    mode_options = ["watch", "report", "trade"]
    default_mode = env_values.get("RUN_MODE", "watch")
    mode_index = mode_options.index(default_mode) if default_mode in mode_options else 0
    bot_mode = st.selectbox(
        "BOT 模式",
        mode_options,
        index=mode_index,
        key="quick_bot_mode",
        help="watch=看盤監測；report=公開資料報表；trade=依設定交易。",
    )
    simulation = str(env_values.get("SIMULATION", "true")).strip().lower() not in {"false", "0", "no"}
    real_trade_quick_start = bot_mode == "trade" and not simulation
    if real_trade_quick_start:
        st.warning("真實 trade 不允許從快速啟動進入；請到「啟動 / 監控」完成交易可行性檢查。")
    b1, b2 = st.columns(2)
    if b1.button(
        "啟動 BOT",
        disabled=bot_running or real_trade_quick_start,
        use_container_width=True,
    ):
        rec = bot_runner.start(run_mode=bot_mode)
        st.success(f"BOT 已啟動 PID {rec.pid}")
        st.rerun()
    if b2.button("停止 BOT", disabled=not bot_running, use_container_width=True):
        ok = bot_runner.stop()
        st.success("BOT 已停止" if ok else "BOT 停止逾時")
        st.rerun()

    st.write("")


PAGES = {
    "任務中心": page_tasks,
    "本次更新": page_updates,
    # 研究與分析
    "功能總覽": dash_pages.page_overview,
    "今日當沖戰情室": dash_pages.page_intraday,
    "今日當沖即時追蹤": dash_pages.page_intraday_live,
    "明日當沖關注": dash_pages.page_next_day_watch,
    "K 線看板": dash_pages.page_board,
    "盤中強弱排行": dash_pages.page_market_movers,
    "個股總覽": dash_pages.page_watchlist,
    "目前持股分析": dash_pages.page_portfolio,
    "個股深入分析": dash_pages.page_ticker_detail,
    "自動化管線": dash_pages.page_pipeline,
    # 監控與訊號
    "美股 / 跨市場": dash_pages.page_macro,
    "熱門個股期貨": dash_pages.page_hot_stock_futures,
    "跟單訊號": dash_pages.page_follow_signals,
    "主動 ETF 追蹤": dash_pages.page_etf_tracker,
    "LLM 法說分析": dash_pages.page_llm_analysis,
    # 執行與紀錄
    "啟動 / 監控": dash_pages.page_runner,
    "🛡 風控中心": dash_pages.page_risk_center,
    "交易可行性檢查": dash_pages.page_preflight,
    "交易紀錄": dash_pages.page_trades,
    "報表分析": dash_pages.page_reports,
    "模擬交易": dash_pages.page_simulation,
    # 系統與診斷
    "組態設定": dash_pages.page_config,
    "資料庫 / 雲端同步": dash_pages.page_database,
    "Prompt 管理": dash_pages.page_prompts,
    "LLM 呼叫紀錄": dash_pages.page_llm_log,
    "記憶與證據管理": dash_pages.page_memory_admin,
    "日誌檢視": dash_pages.page_logs,
    "通知測試": dash_pages.page_notifier,
    "策略與文件": dash_pages.page_docs,
}

NAV_GROUPS = {
    "🔍 研究與分析": ["功能總覽", "今日當沖戰情室", "今日當沖即時追蹤", "明日當沖關注", "K 線看板", "盤中強弱排行", "個股總覽", "目前持股分析", "個股深入分析", "自動化管線"],
    "📡 監控與訊號": [
        "美股 / 跨市場",
        "熱門個股期貨",
        "跟單訊號",
        "主動 ETF 追蹤",
        "LLM 法說分析",
    ],
    "⚡ 執行與紀錄": ["啟動 / 監控", "模擬交易", "🛡 風控中心", "交易可行性檢查", "交易紀錄", "報表分析"],
    "⚙️ 系統與診斷": ["組態設定", "資料庫 / 雲端同步", "Prompt 管理",
                  "LLM 呼叫紀錄", "記憶與證據管理", "日誌檢視", "通知測試", "策略與文件"],
}

PAGES["台股行事曆"] = dash_pages.page_market_calendar
try:
    _market_calendar_group = next(
        (
            group
            for group, page_names in NAV_GROUPS.items()
            if any(PAGES.get(page_name) is dash_pages.page_llm_analysis for page_name in page_names)
        ),
        None,
    )
    if _market_calendar_group and "台股行事曆" not in NAV_GROUPS[_market_calendar_group]:
        _group_pages = NAV_GROUPS[_market_calendar_group]
        _insert_at = next(
            (i + 1 for i, page_name in enumerate(_group_pages) if PAGES.get(page_name) is dash_pages.page_llm_analysis),
            len(_group_pages),
        )
        _group_pages.insert(_insert_at, "台股行事曆")
except Exception:
    pass


def main_app() -> None:
    # Streamlit 可從任意工作目錄啟動；dashboard 內仍有相容層會使用
    # env_path()/load_env()/Settings() 的相對路徑。先固定 cwd，避免設定、
    # LLM audit、快取或舊版頁面把資料寫到啟動者的目錄。
    if Path.cwd().resolve() != PROJECT_ROOT.resolve():
        os.chdir(PROJECT_ROOT)

    st.set_page_config(
        page_title="Stock Lab｜台股研究工作台",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    apply_theme()
    if "page" not in st.session_state or st.session_state.page not in PAGES:
        st.session_state.page = "功能總覽"

    with st.sidebar:
        brand()
        query = st.text_input("尋找功能", placeholder="搜尋頁面名稱…", label_visibility="collapsed", key="nav_search")
        common = ["功能總覽", "個股深入分析", "個股總覽", "K 線看板", "任務中心"]

        def nav_button(page):
            label = "研究工作台" if page == "功能總覽" else page
            if st.button(label, key=f"nav_{page}", use_container_width=True,
                         type="primary" if st.session_state.page == page else "secondary"):
                st.session_state.page = page
                st.rerun()

        if query.strip():
            matches = [p for p in PAGES if query.strip().lower() in (p + ("研究工作台" if p == "功能總覽" else "")).lower()]
            if not matches:
                st.caption("找不到符合的功能。")
            for page in matches:
                nav_button(page)
        else:
            st.caption("常用工作")
            for page in common:
                nav_button(page)
            labels = ["市場與研究", "持股與訊號", "執行與紀錄", "系統設定"]
            for (group, names), label in zip(NAV_GROUPS.items(), labels):
                pages = [p for p in names if p not in common]
                with st.expander(label, expanded=st.session_state.page in pages):
                    for page in pages:
                        nav_button(page)
            nav_button("本次更新")
        task_indicator()
        with st.expander("服務控制"):
            _render_sidebar_quick_controls()
        with st.expander("診斷資訊"):
            st.caption(f"專案：{PROJECT_ROOT}")
            st.caption(f"UI build: {DASHBOARD_UI_BUILD}")
            env = cached_env_values()
            st.caption("LLM 已設定" if llm_provider_ready(env) else "LLM 尚未設定")
            try:
                calls, tokens_in, tokens_out = cached_llm_today_stats(str(PROJECT_ROOT))
                st.caption(f"今日 LLM：{calls} 次 · {tokens_in:,} → {tokens_out:,} tokens")
            except Exception:
                st.caption("暫時無法讀取 LLM 統計")
        st.caption(f"{DASHBOARD_UI_BUILD} · 本次更新可從上方查看")

    pick = st.session_state.page
    write_dashboard_heartbeat(pick)
    _debug_log_session("H11", "nav.py:main_app", "dashboard page render", {"page": pick})
    if pick != "功能總覽":
        st.caption("STOCK LAB / " + pick)
    PAGES[pick]()


if __name__ == "__main__":
    main_app()
