"""Stock Bot Dashboard -- 一覽所有功能、編組態、跑模式、看報表的 Streamlit 介面。

啟動方式 (任一即可):
    uv run stock-dashboard
    uv run streamlit run src/bot/dashboard.py
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# 確保被 `streamlit run src/bot/dashboard.py` 啟動時也能 import bot.*
_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from bot.env_io import (  # noqa: E402
    ENV_FIELDS,
    EnvField,
    env_path,
    grouped_fields,
    load_env,
    save_env,
    validate,
)
from bot.process_runner import get_runner, tail_file  # noqa: E402
from bot.active_etf import (  # noqa: E402
    ActiveEtf,
    DEFAULT_ACTIVE_ETFS,
    Holding,
    HoldingsSnapshot,
    list_active_etfs_path,
    list_holdings_dates,
    load_active_etfs,
    load_holdings,
    save_active_etfs,
    save_holdings,
)
from bot.etf_consensus import (  # noqa: E402
    build_consensus,
    consensus_additions,
    consensus_new_builds,
    diff_snapshots,
)
from bot.prompt_registry import get_registry  # noqa: E402
from bot.llm_log import get_call_logger  # noqa: E402
from bot.scoring import (  # noqa: E402
    STRATEGY_RULES,
    TIMEFRAMES,
    TIMEFRAME_LABELS,
    WEIGHTS,
    compute_scorecard,
    scorecard_to_row,
)
from bot.ticker_view import build_snapshot, snapshot_to_dict  # noqa: E402
from bot import watchlist as wl  # noqa: E402
from bot.stock_db import (  # noqa: E402
    ALL_TABLES,
    SYNCABLE_TABLES,
    StockDB,
    StockInfo,
    WatchlistRow,
    default_db_path,
    get_db,
    reset_db_singleton,
)
from bot.cloud_sync import (  # noqa: E402
    CloudConfig,
    CloudSyncDependencyError,
    GoogleSheetSync,
    TableSyncResult,
    load_config_from_env,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ======================================================================
# 共用工具
# ======================================================================


def _project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def _safe_read_csv(path: Path) -> Optional[pd.DataFrame]:
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        try:
            return pd.read_csv(path, encoding="utf-8")
        except Exception as e:
            st.error(f"無法讀取 {path.name}: {e}")
            return None


def _list_files(folder: Path, pattern: str) -> List[Path]:
    if not folder.exists():
        return []
    return sorted(folder.glob(pattern), reverse=True)


def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} 分"
    hours = minutes / 60
    return f"{hours:.1f} 時"


def _badge(text: str, color: str = "gray") -> str:
    palette = {
        "green": "#1d9c5b",
        "red": "#d64545",
        "yellow": "#d49a17",
        "blue": "#2563eb",
        "gray": "#6b7280",
    }
    bg = palette.get(color, palette["gray"])
    return (
        f"<span style='background:{bg};color:#fff;padding:2px 10px;"
        f"border-radius:10px;font-size:12px;'>{text}</span>"
    )


# ======================================================================
# 頁面: 功能總覽
# ======================================================================


FEATURE_GRID = [
    ("三種執行模式", "trade 自動交易 / watch 看盤 / report 公開資料分析"),
    ("Shioaji 原生整合", "Decorator 回呼、Queue 解耦、避免阻塞"),
    ("盤前收盤價載入", "透過 snapshots / TWSE 取得精確前日收盤"),
    ("多檔資金追蹤", "即時追蹤已用資金，避免超額下單"),
    ("移動停利", "追蹤持倉最高價，自高點回撤 N% 觸發出場"),
    ("部位管理", "即時持倉均價、數量，成交回報自動更新"),
    ("委託單追蹤", "防止重複下單，輪詢狀態自動清理"),
    ("斷線重連", "指數退避重試，自動恢復行情訂閱"),
    ("收盤全出場", "EXIT_TIME 到後自動市價清倉"),
    ("交易紀錄匯出", "trade 模式收盤後匯出 trades_*.csv"),
    ("訊號與報表匯出", "watch/report 模式匯出 signals/report CSV"),
    ("Telegram 通知", "買賣/停損停利/收盤摘要推播"),
    ("模擬模式", "SIMULATION=true 走 Shioaji 模擬環境"),
    ("主動 ETF 追蹤", "28+ 檔主動式 ETF 持股快照、權重、共識計算"),
    ("ETF 共識跟單", "跨多檔 ETF 偵測新建倉/共識加碼，自動納入監控池"),
    ("MOPS 法說會爬蟲", "抓取法人說明會行事曆與個股重大訊息"),
    ("Gemini LLM 分析", "免費 API 解析法說會、提取情緒與成長驅動因子"),
    ("言行反查", "用籌碼面驗證管理階層是否言行一致 (出貨/吸籌)"),
    ("Prompt 版本化管理", "所有 LLM prompt 集中於 prompts/*.yaml，可在 UI 直接編輯"),
    ("LLM 呼叫全紀錄", "每次 LLM 呼叫的 input/output/延遲/tokens 寫入 JSONL 供審計"),
    ("ETF 持股自動抓取", "依 URL 配置 HTTP 下載 + Gemini 自動抽取持股 JSON"),
    ("籌碼面自動拉取", "TWSE OpenAPI 自動抓三大法人/借券/融資/鉅額交易"),
    ("一鍵研究管線", "ETF×籌碼×法說×LLM×每日簡報全部串連執行"),
    ("評分量表系統", "六個 factor × 四時間框架 (當沖/短/中/長) 加權，自動產出建議"),
    ("個股總覽 Watchlist", "表格式列出多檔股票，依時間框分數排序篩選"),
    ("個股深入分析", "單檔股票六分頁：分析、原資料、分析數據、購買策略、現況、歷史"),
]

MODES = [
    {
        "name": "trade",
        "title": "trade — 自動交易",
        "color": "green",
        "desc": "完整 Shioaji 登入 + CA，策略觸發時真正送單 (SIMULATION=true 走模擬環境)。",
        "needs": ["API Key", "Secret Key", "電子憑證 (實單)"],
        "outputs": ["data/trades_YYYY-MM-DD.csv"],
    },
    {
        "name": "watch",
        "title": "watch — 看盤模式",
        "color": "blue",
        "desc": "Shioaji 即時行情 + 不啟用 CA 不下單，記錄 would-buy / would-sell 訊號。",
        "needs": ["API Key", "Secret Key"],
        "outputs": ["data/reports/signals_*.csv", "data/reports/report_*.csv"],
    },
    {
        "name": "report",
        "title": "report — 報表分析",
        "color": "yellow",
        "desc": "不登入券商，輪詢 TWSE 公開延遲報價 (≥ 20 分鐘) 做策略分析。",
        "needs": ["(不需要 API Key)"],
        "outputs": ["data/reports/signals_*.csv", "data/reports/report_*.csv"],
    },
]


def page_overview() -> None:
    st.title("Stock Bot 功能總覽")
    st.caption("台股當沖自動交易機器人 · Shioaji + TWSE 公開資料")

    env_values = load_env()
    runner = get_runner(PROJECT_ROOT)
    current = runner.current()
    running = runner.is_running()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("執行模式", env_values.get("RUN_MODE", "trade"))
    col2.metric("行情來源", env_values.get("MARKET_SOURCE", "") or "(自動)")
    col3.metric(
        "監控股票",
        len([s for s in env_values.get("SYMBOLS", "").split(",") if s.strip()]) or 0,
    )
    sim = env_values.get("SIMULATION", "true").lower() == "true"
    col4.markdown(
        f"**模擬模式**<br>{_badge('SIMULATION', 'yellow' if sim else 'red')} "
        f"{_badge('ON', 'green') if sim else _badge('OFF', 'red')}",
        unsafe_allow_html=True,
    )

    st.markdown("---")

    s1, s2, s3 = st.columns([1, 1, 2])
    with s1:
        if running:
            st.markdown(_badge("● Bot 執行中", "green"), unsafe_allow_html=True)
        else:
            st.markdown(_badge("○ Bot 未執行", "gray"), unsafe_allow_html=True)
    with s2:
        if current:
            elapsed = (current.ended_at or time.time()) - current.started_at
            st.caption(
                f"最近一次: {current.run_mode} (PID {current.pid}, {_human_duration(elapsed)})"
            )
        else:
            st.caption("尚未從儀表板啟動過")
    with s3:
        st.caption(f"專案位置: `{PROJECT_ROOT}`")

    st.markdown("### 三種執行模式")
    cols = st.columns(3)
    for col, m in zip(cols, MODES):
        with col:
            st.markdown(f"#### {m['title']}")
            st.markdown(_badge(m["name"].upper(), m["color"]), unsafe_allow_html=True)
            st.write(m["desc"])
            st.caption("需要：" + " / ".join(m["needs"]))
            st.caption("輸出：")
            for o in m["outputs"]:
                st.code(o, language="text")

    st.markdown("### 核心功能")
    grid_cols = st.columns(3)
    for i, (title, desc) in enumerate(FEATURE_GRID):
        with grid_cols[i % 3]:
            st.markdown(f"**{title}**")
            st.caption(desc)

    st.markdown("### 快速入口")
    st.caption("整體工作流程：(1) 自動化管線一鍵抓料 → (2) 個股總覽看 watchlist 評分 → (3) 點個股深入分析")

    q1, q2, q3, q4 = st.columns(4)
    if q1.button("📊 K 線看板", use_container_width=True, type="primary"):
        st.session_state.page = "K 線看板"
        st.rerun()
    if q2.button("🔬 個股深入分析", use_container_width=True):
        st.session_state.page = "個股深入分析"
        st.rerun()
    if q3.button("⚙ 自動化管線", use_container_width=True):
        st.session_state.page = "自動化管線"
        st.rerun()
    if q4.button("🔧 組態設定", use_container_width=True):
        st.session_state.page = "組態設定"
        st.rerun()

    q5, q6, q7, q8 = st.columns(4)
    if q5.button("跟單訊號", use_container_width=True):
        st.session_state.page = "跟單訊號"
        st.rerun()
    if q6.button("主動 ETF 追蹤", use_container_width=True):
        st.session_state.page = "主動 ETF 追蹤"
        st.rerun()
    if q7.button("LLM 法說分析", use_container_width=True):
        st.session_state.page = "LLM 法說分析"
        st.rerun()
    if q8.button("啟動 / 監控", use_container_width=True):
        st.session_state.page = "啟動 / 監控"
        st.rerun()


# ======================================================================
# 頁面: 組態設定
# ======================================================================


def _render_field(f: EnvField, value: str) -> str:
    """根據欄位型別渲染對應的 widget，回傳新值的字串。"""
    key = f"env_{f.key}"
    help_text = f.help or None

    if f.kind == "select":
        opts = f.options
        idx = opts.index(value) if value in opts else 0
        new = st.selectbox(f.label, opts, index=idx, key=key, help=help_text)
        return new

    if f.kind == "bool":
        cur = value.lower() in ("true", "1", "yes")
        new = st.toggle(f.label, value=cur, key=key, help=help_text)
        return "true" if new else "false"

    if f.kind == "time":
        try:
            hh, mm = value.split(":") if value else ("09", "30")
            t_init = dt.time(int(hh), int(mm))
        except Exception:
            t_init = dt.time(9, 30)
        new_t = st.time_input(f.label, value=t_init, key=key, help=help_text)
        return new_t.strftime("%H:%M")

    if f.kind == "password":
        return st.text_input(
            f.label, value=value, type="password", key=key, help=help_text,
        )

    if f.kind == "int":
        try:
            v_int = int(value) if value else int(f.default or 0)
        except ValueError:
            v_int = int(f.default or 0)
        new_i = st.number_input(
            f.label, value=v_int, step=1, key=key, help=help_text,
        )
        return str(int(new_i))

    if f.kind == "float":
        try:
            v_f = float(value) if value else float(f.default or 0)
        except ValueError:
            v_f = float(f.default or 0)
        new_f = st.number_input(
            f.label, value=v_f, step=0.1, format="%.2f", key=key, help=help_text,
        )
        return f"{new_f}"

    if f.kind == "symbols":
        new = st.text_input(
            f.label, value=value, key=key,
            help=(help_text or "") + " (例: 2330,0050,2881)",
            placeholder="2330,0050",
        )
        cleaned = ",".join(s.strip() for s in new.split(",") if s.strip())
        return cleaned

    return st.text_input(f.label, value=value, key=key, help=help_text)


def page_config() -> None:
    st.title("組態設定")
    st.caption(f".env 路徑: `{env_path()}`")

    env_values = load_env()

    if not env_values:
        st.warning("尚未找到 .env，將使用各欄位預設值。儲存後會自動建立。")

    groups = grouped_fields()
    new_values: Dict[str, str] = dict(env_values)

    for section, fields_in_section in groups.items():
        with st.expander(section, expanded=section in ("執行模式", "策略", "風控")):
            cols = st.columns(2)
            for i, f in enumerate(fields_in_section):
                with cols[i % 2]:
                    cur = env_values.get(f.key, str(f.default))
                    new_values[f.key] = _render_field(f, cur)

    st.markdown("---")
    c1, c2, c3 = st.columns([1, 1, 2])
    save_clicked = c1.button("儲存到 .env", type="primary", use_container_width=True)
    reload_clicked = c2.button("放棄變更 / 重新載入", use_container_width=True)

    if reload_clicked:
        st.rerun()

    if save_clicked:
        errors = validate(new_values)
        if errors:
            st.error("發現以下錯誤，請修正後再儲存：")
            for e in errors:
                st.write(f"- {e}")
        else:
            p = save_env(new_values)
            st.success(f"已儲存到 {p}（同時備份為 .env.bak）")
            st.toast("組態已更新", icon="✅")

    with st.expander("檢視目前 .env 原文 (已遮罩敏感欄位)"):
        from bot.env_io import masked
        st.code(
            "\n".join(f"{k}={v}" for k, v in masked(load_env()).items()) or "(空)",
            language="ini",
        )


# ======================================================================
# 頁面: 啟動 / 監控
# ======================================================================


def page_runner() -> None:
    st.title("啟動 / 監控")
    runner = get_runner(PROJECT_ROOT)

    env_values = load_env()
    default_mode = env_values.get("RUN_MODE", "trade")

    # 啟動前提醒：建議先跑 preflight
    if env_values.get("RUN_MODE") == "trade" and env_values.get("SIMULATION", "true").lower() in ("false", "0"):
        with st.container(border=True):
            cols = st.columns([5, 1])
            with cols[0]:
                st.warning(
                    "⚠️ 即將以 **trade + 真實環境** 模式啟動。建議先到「交易可行性檢查」"
                    "驗證 API Token 權限、CA 憑證、簽署狀態。"
                )
            with cols[1]:
                if st.button("🩺 去檢查", use_container_width=True):
                    st.session_state.page = "交易可行性檢查"
                    st.rerun()

    col1, col2 = st.columns([1, 2])
    with col1:
        st.subheader("啟動模式")
        mode = st.radio(
            "選擇本次執行模式",
            ["trade", "watch", "report"],
            index=["trade", "watch", "report"].index(default_mode)
            if default_mode in ("trade", "watch", "report") else 0,
            help="會覆蓋 .env 的 RUN_MODE (僅本次執行)",
        )
        symbols_override = st.text_input(
            "本次監控股票 (留空使用 .env)",
            value="",
            placeholder="2330,0050",
        )
        running = runner.is_running()
        start_clicked = st.button(
            "啟動 Bot", type="primary", disabled=running, use_container_width=True,
        )
        stop_clicked = st.button(
            "停止 Bot", disabled=not running, use_container_width=True,
        )

    with col2:
        st.subheader("執行狀態")
        cur = runner.current()
        if running and cur:
            elapsed = time.time() - cur.started_at
            st.markdown(
                f"{_badge('執行中', 'green')} &nbsp; PID {cur.pid} &nbsp; "
                f"模式 `{cur.run_mode}` &nbsp; 持續 {_human_duration(elapsed)}",
                unsafe_allow_html=True,
            )
            st.caption(f"Log: `{cur.log_path}`")
        elif cur:
            duration = (cur.ended_at or cur.started_at) - cur.started_at
            rc_badge = _badge(
                f"exit={cur.return_code}",
                "green" if cur.return_code == 0 else "red",
            )
            st.markdown(
                f"{_badge('已結束', 'gray')} &nbsp; {rc_badge} &nbsp; "
                f"模式 `{cur.run_mode}` &nbsp; 總長 {_human_duration(duration)}",
                unsafe_allow_html=True,
            )
            st.caption(f"Log: `{cur.log_path}`")
        else:
            st.info("尚未啟動過任何 Bot。設定好 .env 後按「啟動 Bot」即可。")

    if start_clicked:
        extra: Dict[str, str] = {}
        if symbols_override.strip():
            extra["SYMBOLS"] = ",".join(
                s.strip() for s in symbols_override.split(",") if s.strip()
            )
        record = runner.start(run_mode=mode, extra_env=extra)
        st.success(f"已啟動 (PID {record.pid})，模式: {record.run_mode}")
        time.sleep(0.5)
        st.rerun()

    if stop_clicked:
        ok = runner.stop()
        if ok:
            st.success("已送出停止訊號")
        else:
            st.warning("強制終止 (timeout)")
        time.sleep(0.5)
        st.rerun()

    st.markdown("---")
    st.subheader("即時 Log (最後 200 行)")
    cur = runner.current()
    if cur:
        auto = st.checkbox("自動刷新 (每 3 秒)", value=running)
        st.code(tail_file(cur.log_path, lines=200) or "(尚無輸出)", language="log")
        if auto and running:
            time.sleep(3)
            st.rerun()
    else:
        st.caption("尚未產生任何執行 log。")


# ======================================================================
# 頁面: 風控中心
# ======================================================================


def page_risk_center() -> None:
    st.title("風控中心 (Risk Center)")
    st.caption("看一眼今天的資金/部位/虧損狀態，必要時拉下「緊急 Kill Switch」立刻關掉所有新進場。")

    from bot.config import Settings as S  # noqa: E402
    from bot.risk_guard import KILL_SWITCH_FILENAME, RiskGuard  # noqa: E402

    settings = S()
    guard = RiskGuard(settings=settings, project_root=PROJECT_ROOT)
    snap = guard.snapshot()

    # ============ Kill Switch =============
    with st.container(border=True):
        cols = st.columns([3, 1, 1])
        with cols[0]:
            if snap["kill_switch"]:
                st.error(
                    "🔴 **Kill Switch 已啟動** — 所有新進場單會被拒絕。"
                    f" 觸發檔案: `data/{KILL_SWITCH_FILENAME}`"
                )
            else:
                st.success("🟢 Kill Switch 未啟動 — 正常運作中")
        with cols[1]:
            if not snap["kill_switch"]:
                if st.button("🔴 緊急拉閘", type="primary", use_container_width=True):
                    guard.engage_kill_switch("dashboard_manual")
                    st.rerun()
        with cols[2]:
            if snap["kill_switch"]:
                if st.button("🟢 解除拉閘", use_container_width=True):
                    guard.release_kill_switch()
                    st.rerun()

    # ============ 即時概況 =============
    st.markdown("### 📊 今日即時概況")
    fund_used = float(snap["fund_used"])
    fund_cap = float(snap["fund_cap"])
    fund_remaining = float(snap["fund_remaining"])
    fund_pct = (fund_used / fund_cap * 100) if fund_cap > 0 else 0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "已用資金", f"{fund_used:,.0f}",
        f"{fund_pct:.1f}% of {fund_cap:,.0f}",
        delta_color="off",
    )
    m2.metric(
        "剩餘可用", f"{fund_remaining:,.0f}",
        f"{100-fund_pct:.1f}%",
        delta_color="off",
    )
    m3.metric(
        "在倉檔數", f"{snap['open_positions']}",
        f"上限 {snap['max_open_positions'] or '不限'}",
        delta_color="off",
    )
    m4.metric(
        "今日進場次數", f"{snap['today_orders']}",
        f"上限 {snap['daily_max_orders'] or '不限'}",
        delta_color="off",
    )

    # 資金使用進度條
    st.progress(min(fund_pct / 100, 1.0), text=f"資金使用 {fund_pct:.1f}%")

    # ============ 損益 =============
    realized = float(snap["realized_pnl_twd"])
    unrealized = float(snap["unrealized_pnl_twd"])
    loss_cap = float(snap["daily_loss_cap"])
    pnl_color = "green" if realized >= 0 else "red"

    st.markdown("### 💰 損益狀態")
    p1, p2, p3 = st.columns(3)
    p1.metric("已實現損益", f"{realized:+,.0f}", delta=None)
    p2.metric("未實現損益", f"{unrealized:+,.0f}", delta=None)
    if loss_cap > 0:
        # 距離熔斷剩多少
        gap = loss_cap + realized  # realized 是負的時候 gap 才會變小
        if gap > 0:
            p3.metric("距熔斷剩餘空間", f"{gap:,.0f}", f"門檻 -{loss_cap:,.0f}")
        else:
            p3.metric("熔斷狀態", "🚨 已觸發", f"虧損 {realized:,.0f}")
    else:
        p3.metric("熔斷設定", "未啟用", "DAILY_MAX_LOSS_* 都是 0")

    # ============ 當前風控設定 =============
    with st.expander("🛡 當前風控設定 (12 道閘門)", expanded=False):
        rules_data = [
            ["A. 總資金上限", f"{settings.max_fund:,} TWD", "MAX_FUND"],
            ["A. 單筆委託上限", f"{settings.per_order_max_cost_twd:,} TWD" if settings.per_order_max_cost_twd > 0 else "(不限)", "PER_ORDER_MAX_COST_TWD"],
            ["B. 單檔最大張數", f"{settings.max_lot_per_symbol}", "MAX_LOT_PER_SYMBOL"],
            ["B. 同時最多在倉", f"{settings.max_open_positions} 檔" if settings.max_open_positions > 0 else "(不限)", "MAX_OPEN_POSITIONS"],
            ["B. 黑名單", ", ".join(snap["blacklist"]) or "(無)", "BLACKLIST_SYMBOLS"],
            ["C. 每日虧損 (元)", f"-{settings.daily_max_loss_twd:,} TWD" if settings.daily_max_loss_twd > 0 else "(不限)", "DAILY_MAX_LOSS_TWD"],
            ["C. 每日虧損 (%)", f"-{settings.daily_max_loss_pct}% × max_fund" if settings.daily_max_loss_pct > 0 else "(不限)", "DAILY_MAX_LOSS_PCT"],
            ["D. 當日進場次數", f"≤ {settings.daily_max_orders}" if settings.daily_max_orders > 0 else "(不限)", "DAILY_MAX_ORDERS"],
            ["D. 單檔進場次數", f"≤ {settings.per_symbol_daily_max_orders}" if settings.per_symbol_daily_max_orders > 0 else "(不限)", "PER_SYMBOL_DAILY_MAX_ORDERS"],
            ["D. 平倉後冷卻", f"{settings.reentry_cooldown_seconds}s" if settings.reentry_cooldown_seconds > 0 else "(立即可再進)", "REENTRY_COOLDOWN_SECONDS"],
            ["E. 進場最大漲幅", f"≤ {settings.max_pct_chg_on_entry}%" if settings.max_pct_chg_on_entry > 0 else "(不限)", "MAX_PCT_CHG_ON_ENTRY"],
            ["E. 價格區間", f"{settings.min_price:.0f}~{settings.max_price:.0f}" if (settings.min_price or settings.max_price) else "(不限)", "MIN_PRICE / MAX_PRICE"],
            ["F. 停損/停利", f"{settings.stop_loss_pct}% / +{settings.take_profit_pct}% / 移動 {settings.trailing_stop_pct}%", "STOP_LOSS_PCT / TAKE_PROFIT_PCT / TRAILING_STOP_PCT"],
        ]
        df_rules = pd.DataFrame(rules_data, columns=["閘門", "當前值", "環境變數"])
        st.dataframe(df_rules, hide_index=True, use_container_width=True)
        st.caption("💡 要調整這些值，去「組態設定」頁找對應的 ENV，存檔即可。重啟 bot 才會生效。")

    # ============ 各檔今日下單統計 =============
    sym_orders = snap.get("today_orders_per_symbol", {})
    if sym_orders:
        st.markdown("### 📋 各檔今日進場次數")
        df_sym = pd.DataFrame(
            [(s, n) for s, n in sym_orders.items()],
            columns=["代號", "次數"],
        ).sort_values("次數", ascending=False)
        st.dataframe(df_sym, hide_index=True, use_container_width=True)

    # ============ 被擋下來的進場 =============
    blocked = snap.get("blocked_recent", [])
    st.markdown(f"### ⛔ 最近被擋下的進場 ({len(blocked)} / 最多保留 20 筆)")
    if blocked:
        df_blocked = pd.DataFrame(blocked)
        st.dataframe(df_blocked, hide_index=True, use_container_width=True)
    else:
        st.info("今日尚無進場被風控擋下。")

    # ============ 手動 CLI 提示 =============
    with st.expander("🛠 也可以用 CLI 控制 Kill Switch", expanded=False):
        st.code(
            "# 立刻拉閘 (Bot 下次嘗試進場時會立刻被拒)\n"
            f"echo manual > {PROJECT_ROOT / 'data' / KILL_SWITCH_FILENAME}\n\n"
            "# 解除拉閘\n"
            f"del {PROJECT_ROOT / 'data' / KILL_SWITCH_FILENAME}\n",
            language="powershell",
        )
        st.caption(
            "Kill Switch 只擋「新進場」，不擋「平倉」"
            "（停損/停利/收盤強平永遠會執行，因為平倉本身就是降低風險）。"
        )


# ======================================================================
# 頁面: 交易可行性檢查
# ======================================================================


_PREFLIGHT_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}
_PREFLIGHT_COLOR = {"ok": "green", "warn": "orange", "fail": "red", "info": "gray"}
_PREFLIGHT_SECTIONS = [
    ("section_env", "1. 環境變數"),
    ("section_ca", "2. 電子憑證 (CA)"),
    ("section_login", "3. Shioaji 連線測試"),
    ("section_account", "4. 帳戶權限"),
    ("section_risk", "5. 風控設定"),
    ("section_time", "6. 時間窗口"),
]


def page_preflight() -> None:
    st.title("交易可行性檢查 (Preflight)")
    st.caption(
        "在啟動 Bot 之前，先確認：API 金鑰 / 電子憑證 / 帳戶權限 / 模式設定 / "
        "停損停利 / 是否在盤中，全部到位才能真實下單。"
    )

    with st.expander("這份檢查在驗證什麼？", expanded=False):
        st.markdown(
            """
            **本系統只能買賣台股**（透過永豐 Shioaji API）。美股相關資料只用於分析，無法下單。

            真實下單的硬性條件 (全部要 ✅ 才能下單)：

            1. `API_KEY` / `SECRET_KEY` 已設定 — 在永豐 e leader 申請 Shioaji
            2. **API 金鑰具備「下單」權限** (不只是 Data) — 在 e leader 勾選後重新產生
            3. 線上協議已簽署 (stock_account.signed = True)
            4. 電子憑證 `.pfx` 檔存在、密碼正確、未過期
            5. 設定 `RUN_MODE=trade` (watch / report 不會下單)
            6. 設定 `SIMULATION=false` (true 為模擬環境)
            7. 現在處於台股盤中 09:00–13:30，且為交易日
            8. 風控設定 (MAX_FUND / MAX_LOT_PER_SYMBOL) 合理

            **目前不支援**：盤後零股交易 (15:00 盤、13:30 之後)、興櫃股票、海外證券。
            """
        )

    col_btn, col_opt = st.columns([1, 2])
    with col_btn:
        run_btn = st.button(
            "🩺 立刻檢查",
            type="primary",
            use_container_width=True,
        )
    with col_opt:
        do_login = st.checkbox(
            "包含實際 Shioaji 連線測試 (約需 5-10 秒)",
            value=True,
            help="關掉的話只檢查 .env 設定，不會打永豐 API",
        )

    if not run_btn and "preflight_report" not in st.session_state:
        st.info("按上方「🩺 立刻檢查」開始體檢。")
        return

    if run_btn:
        from bot.config import Settings as S  # noqa: E402
        from bot.preflight import report_to_dict, run_preflight  # noqa: E402

        with st.spinner("檢查中… (連線 Shioaji 約需數秒)"):
            try:
                report = run_preflight(S(), do_real_login=do_login)
                st.session_state["preflight_report"] = report_to_dict(report)
            except Exception as e:
                st.error(f"檢查時發生未預期錯誤: {e}")
                return

    data = st.session_state.get("preflight_report")
    if not data:
        return

    # ===== 結論 =====
    if data["can_trade_now"]:
        st.success(f"🟢 **可以下真實單** ｜ {data['summary']}")
    elif data["can_simulate"]:
        st.warning(f"🟡 **暫不可下真實單** ｜ {data['summary']}")
    else:
        st.error(f"🔴 **設定有阻擋** ｜ {data['summary']}")
    st.caption(f"檢查時間: {data['fetched_at']}")

    # ===== 摘要計數 =====
    sections = data["sections"]
    all_items = [c for items in sections.values() for c in items]
    ok = sum(1 for c in all_items if c["status"] == "ok")
    warn = sum(1 for c in all_items if c["status"] == "warn")
    fail = sum(1 for c in all_items if c["status"] == "fail")
    info = sum(1 for c in all_items if c["status"] == "info")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("✅ 通過", ok)
    m2.metric("⚠️ 警告", warn)
    m3.metric("❌ 阻擋", fail)
    m4.metric("ℹ️ 資訊", info)

    # ===== 阻擋清單 (有 fail 時最上方提醒) =====
    if fail > 0:
        st.markdown("##### ❌ 必須先解決")
        for c in all_items:
            if c["status"] != "fail":
                continue
            with st.container(border=True):
                st.markdown(f"**{c['name']}**")
                st.write(c["detail"])
                if c.get("suggestion"):
                    st.info(f"💡 {c['suggestion']}")

    # ===== 分區明細 =====
    st.markdown("---")
    st.subheader("分區明細")
    section_keys_dash = [
        ("env", "1. 環境變數"),
        ("ca", "2. 電子憑證 (CA)"),
        ("login", "3. Shioaji 連線測試"),
        ("account", "4. 帳戶權限"),
        ("risk", "5. 風控設定"),
        ("time", "6. 時間窗口"),
    ]
    for key, title in section_keys_dash:
        items = sections.get(key, [])
        if not items:
            continue
        with st.expander(f"{title} ({len(items)} 項)", expanded=(fail > 0 and key in ("env", "login", "account"))):
            for c in items:
                icon = _PREFLIGHT_ICON.get(c["status"], " ")
                cols = st.columns([3, 5])
                with cols[0]:
                    st.markdown(f"{icon} **{c['name']}**")
                with cols[1]:
                    if c["detail"]:
                        st.write(c["detail"])
                    if c.get("suggestion"):
                        st.caption(f"💡 {c['suggestion']}")

    # ===== 原始 JSON =====
    with st.expander("檢視原始 JSON", expanded=False):
        st.json(data)


# ======================================================================
# 頁面: 報表分析
# ======================================================================


def _render_csv_chart(df: pd.DataFrame, ts_col: str, value_col: str, group_col: Optional[str] = None) -> None:
    try:
        import altair as alt
        chart_df = df.copy()
        chart_df[ts_col] = pd.to_datetime(chart_df[ts_col], errors="coerce")
        if group_col and group_col in chart_df.columns:
            base = alt.Chart(chart_df).mark_line(point=True).encode(
                x=alt.X(f"{ts_col}:T", title="時間"),
                y=alt.Y(f"{value_col}:Q", title=value_col),
                color=alt.Color(f"{group_col}:N", title=group_col),
                tooltip=list(chart_df.columns),
            )
        else:
            base = alt.Chart(chart_df).mark_line(point=True).encode(
                x=alt.X(f"{ts_col}:T", title="時間"),
                y=alt.Y(f"{value_col}:Q", title=value_col),
                tooltip=list(chart_df.columns),
            )
        st.altair_chart(base, use_container_width=True)
    except Exception as e:
        st.caption(f"繪圖失敗: {e}")


def page_reports() -> None:
    st.title("報表分析")
    report_dir = _project_path("data", "reports")
    st.caption(f"報表目錄: `{report_dir}`")

    tab_report, tab_signal = st.tabs(["分析報表 report_*.csv", "訊號明細 signals_*.csv"])

    with tab_report:
        files = _list_files(report_dir, "report_*.csv")
        if not files:
            st.info("尚無分析報表。請先在 watch / report 模式跑過一次。")
        else:
            chosen = st.selectbox("選擇報表", [f.name for f in files], key="report_pick")
            f = report_dir / chosen
            df = _safe_read_csv(f)
            if df is not None:
                st.dataframe(df, use_container_width=True)
                c1, c2 = st.columns(2)
                with c1:
                    if "pnl_pct" in df.columns and "symbol" in df.columns:
                        st.bar_chart(df.set_index("symbol")["pnl_pct"])
                with c2:
                    if {"buy_signals", "sell_signals", "symbol"}.issubset(df.columns):
                        st.bar_chart(
                            df.set_index("symbol")[["buy_signals", "sell_signals"]]
                        )
                with f.open("rb") as fp:
                    st.download_button(
                        "下載 CSV", data=fp.read(), file_name=chosen, mime="text/csv",
                    )

    with tab_signal:
        files = _list_files(report_dir, "signals_*.csv")
        if not files:
            st.info("尚無訊號明細。")
        else:
            chosen = st.selectbox("選擇訊號檔", [f.name for f in files], key="signal_pick")
            f = report_dir / chosen
            df = _safe_read_csv(f)
            if df is not None and not df.empty:
                col1, col2, col3 = st.columns(3)
                with col1:
                    syms = sorted(df["symbol"].astype(str).unique().tolist()) if "symbol" in df.columns else []
                    pick_sym = st.multiselect("商品篩選", syms, default=syms)
                with col2:
                    actions = sorted(df["action"].unique().tolist()) if "action" in df.columns else []
                    pick_act = st.multiselect("動作", actions, default=actions)
                with col3:
                    reasons = sorted(df["reason"].unique().tolist()) if "reason" in df.columns else []
                    pick_rs = st.multiselect("原因", reasons, default=reasons)

                view = df
                if "symbol" in view.columns and pick_sym:
                    view = view[view["symbol"].astype(str).isin(pick_sym)]
                if "action" in view.columns and pick_act:
                    view = view[view["action"].isin(pick_act)]
                if "reason" in view.columns and pick_rs:
                    view = view[view["reason"].isin(pick_rs)]

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("總訊號", len(view))
                if "action" in view.columns:
                    m2.metric("would-buy", int((view["action"] == "would-buy").sum()))
                    m3.metric("would-sell", int((view["action"] == "would-sell").sum()))
                if "pnl_pct" in view.columns and len(view):
                    m4.metric("平均虛擬損益(%)", f"{view['pnl_pct'].mean():.2f}")

                st.dataframe(view, use_container_width=True)

                if {"ts", "price", "symbol"}.issubset(view.columns):
                    st.markdown("#### 訊號價格走勢")
                    _render_csv_chart(view, "ts", "price", "symbol")

                with f.open("rb") as fp:
                    st.download_button(
                        "下載 CSV", data=fp.read(), file_name=chosen, mime="text/csv",
                    )


# ======================================================================
# 頁面: 交易紀錄
# ======================================================================


def page_trades() -> None:
    st.title("交易紀錄")
    data_dir = _project_path("data")
    st.caption(f"交易檔目錄: `{data_dir}`")
    files = _list_files(data_dir, "trades_*.csv")
    if not files:
        st.info("尚無交易紀錄。需在 trade 模式跑過一次才會匯出。")
        return

    chosen = st.selectbox("選擇交易紀錄", [f.name for f in files])
    f = data_dir / chosen
    df = _safe_read_csv(f)
    if df is None or df.empty:
        st.warning("檔案為空。")
        return

    if "amount" not in df.columns and {"price", "quantity"}.issubset(df.columns):
        df["amount"] = df["price"] * df["quantity"] * 1000

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("成交筆數", len(df))
    if "action" in df.columns:
        buys = df[df["action"] == "Buy"]
        sells = df[df["action"] == "Sell"]
        m2.metric("買進金額", f"{buys['amount'].sum():,.0f}" if "amount" in buys.columns else "-")
        m3.metric("賣出金額", f"{sells['amount'].sum():,.0f}" if "amount" in sells.columns else "-")
        if "amount" in df.columns:
            net = float(sells["amount"].sum() - buys["amount"].sum())
            m4.metric("淨額", f"{net:,.0f}")

    st.dataframe(df, use_container_width=True)

    if {"datetime", "amount", "action"}.issubset(df.columns):
        st.markdown("#### 成交金額走勢")
        df_plot = df.copy()
        df_plot["datetime"] = pd.to_datetime(df_plot["datetime"], errors="coerce")
        st.line_chart(df_plot.set_index("datetime")["amount"])

    with f.open("rb") as fp:
        st.download_button("下載 CSV", data=fp.read(), file_name=chosen, mime="text/csv")


# ======================================================================
# 頁面: 日誌檢視
# ======================================================================


def page_logs() -> None:
    st.title("日誌檢視")
    log_dir = _project_path("log")
    st.caption(f"日誌目錄: `{log_dir}`")

    if not log_dir.exists():
        st.info("尚無 log 目錄，跑過任何模式後就會出現。")
        return

    files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        st.info("尚無 log 檔。")
        return

    name_to_path = {f.name: f for f in files}
    chosen = st.selectbox("選擇 log 檔", list(name_to_path.keys()))
    f = name_to_path[chosen]
    n = st.slider("顯示最後 N 行", 50, 2000, 300, step=50)
    auto = st.checkbox("自動刷新 (每 5 秒)")

    st.caption(f"檔案: `{f}` · 大小 {f.stat().st_size:,} bytes")
    st.code(tail_file(f, lines=n) or "(空)", language="log")

    if auto:
        time.sleep(5)
        st.rerun()


# ======================================================================
# 頁面: 通知測試
# ======================================================================


def page_notifier() -> None:
    st.title("Telegram 通知測試")

    env_values = load_env()
    token = env_values.get("TELEGRAM_BOT_TOKEN", "")
    chat = env_values.get("TELEGRAM_CHAT_ID", "")

    c1, c2 = st.columns(2)
    c1.markdown(
        f"Bot Token: {_badge('已設定', 'green') if token else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )
    c2.markdown(
        f"Chat ID: {_badge('已設定', 'green') if chat else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )

    if not (token and chat):
        st.warning("請先在「組態設定」頁填入 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID。")

    st.markdown("---")
    msg = st.text_area(
        "測試訊息",
        value="🤖 <b>Stock Bot Dashboard</b>\n這是一則來自儀表板的測試訊息。",
        height=120,
    )
    if st.button("發送測試", disabled=not (token and chat), type="primary"):
        from bot.notifier import TelegramNotifier

        notifier = TelegramNotifier(bot_token=token, chat_id=chat)
        notifier.send(msg)
        st.success("已送出 (Telegram 為背景非同步發送，請至手機確認)")


# ======================================================================
# 頁面: 策略與文件
# ======================================================================


def page_docs() -> None:
    st.title("策略與文件")

    src_files = [
        ("README.md", _project_path("README.md")),
        ("docs/setup.md", _project_path("docs", "setup.md")),
        ("docs/architecture.md", _project_path("docs", "architecture.md")),
        ("docs/operations.md", _project_path("docs", "operations.md")),
        ("docs/profit-plan.md", _project_path("docs", "profit-plan.md")),
        ("docs/trading-rules.md", _project_path("docs", "trading-rules.md")),
    ]
    src_files = [(n, p) for n, p in src_files if p.exists()]

    tab_doc, tab_strategy, tab_modules = st.tabs(["文件", "策略原始碼", "模組總覽"])

    with tab_doc:
        if not src_files:
            st.info("沒有找到文件。")
        else:
            pick = st.selectbox("選擇文件", [n for n, _ in src_files])
            p = dict(src_files)[pick]
            st.markdown(p.read_text(encoding="utf-8"))

    with tab_strategy:
        sp = _project_path("src", "bot", "strategy.py")
        if sp.exists():
            st.caption(f"`{sp.relative_to(PROJECT_ROOT)}`")
            st.code(sp.read_text(encoding="utf-8"), language="python")
        else:
            st.warning("找不到 strategy.py")

    with tab_modules:
        modules = [
            ("main.py", "程式進入點 (多模式分流 + 策略切換)"),
            ("config.py", "組態管理 (pydantic-settings + .env)"),
            ("broker.py", "Shioaji 連線管理 (登入/行情/下單/斷線重連)"),
            ("strategy.py", "策略引擎 (BaseStrategy + MyStrategy)"),
            ("strategy_etf_follow.py", "主動 ETF 共識跟單策略"),
            ("models.py", "資料模型 (PositionInfo, MarketTick, SignalEvent)"),
            ("market_source.py", "TWSE 公開延遲行情來源"),
            ("signal_recorder.py", "訊號記錄與報表匯出 (watch/report)"),
            ("recorder.py", "交易紀錄收集與 CSV 匯出 (trade)"),
            ("notifier.py", "Telegram 推播通知"),
            ("utils.py", "工具函數 (Logger, 時間)"),
            ("active_etf.py", "主動式 ETF 清單與持股資料模型"),
            ("etf_consensus.py", "共識持股 / 加碼 / 抬轎候選計算"),
            ("mops_scraper.py", "MOPS 法說會 / 重大訊息爬蟲"),
            ("llm_analyzer.py", "Gemini LLM 法說會語意解析 + 邏輯反查"),
            ("prompt_registry.py", "Prompt YAML 載入 / 渲染 / 儲存"),
            ("llm_log.py", "LLM 呼叫 JSONL 紀錄"),
            ("etf_holdings_fetcher.py", "ETF 持股自動抓取 (HTTP + LLM 抽取)"),
            ("chips_fetcher.py", "TWSE 籌碼面自動拉取"),
            ("data_pipeline.py", "自動化研究管線編排器"),
            ("auto_research.py", "stock-auto-research CLI 進入點"),
            ("env_io.py", "儀表板的 .env 讀寫工具"),
            ("process_runner.py", "儀表板的 bot 子行程管理"),
            ("dashboard.py", "本儀表板"),
        ]
        st.dataframe(
            pd.DataFrame(modules, columns=["檔案", "說明"]),
            hide_index=True,
            use_container_width=True,
        )


# ======================================================================
# 頁面: 主動 ETF 追蹤
# ======================================================================


def page_etf_tracker() -> None:
    st.title("主動式 ETF 追蹤")
    st.caption("依 2025-2026 公開資訊整理的主動式 ETF 清單與持股快照")

    etfs = load_active_etfs(PROJECT_ROOT)
    st.markdown(f"### 共 **{len(etfs)}** 檔主動式 ETF")

    df_meta = pd.DataFrame([
        {
            "代號": e.symbol, "名稱": e.name, "投信": e.issuer,
            "地區": e.region, "配息": e.freq,
            "持股 URL": e.holdings_url or "—",
        } for e in etfs
    ])

    holdings_meta = []
    for e in etfs:
        dates = list_holdings_dates(e.symbol, PROJECT_ROOT)
        df_meta_count = len(dates)
        holdings_meta.append({
            "代號": e.symbol,
            "快照數": df_meta_count,
            "最新快照": dates[0].isoformat() if dates else "—",
        })
    meta_df = pd.merge(
        df_meta, pd.DataFrame(holdings_meta), on="代號", how="left",
    )

    m1, m2, m3 = st.columns(3)
    m1.metric("已建立快照的 ETF", int((meta_df["快照數"] > 0).sum()))
    m2.metric("總快照數", int(meta_df["快照數"].fillna(0).sum()))
    m3.metric("最新更新日", meta_df["最新快照"].max() or "—")

    st.dataframe(meta_df, hide_index=True, use_container_width=True)

    st.markdown("---")
    st.markdown("### ETF 持股快照管理")
    col_sel, col_dt = st.columns([1, 1])
    sym_options = [f"{e.symbol} {e.name}" for e in etfs]
    sel = col_sel.selectbox("選擇 ETF", sym_options, key="etf_pick")
    sym = sel.split(" ")[0]
    dates = list_holdings_dates(sym, PROJECT_ROOT)
    if dates:
        date_pick = col_dt.selectbox(
            "選擇快照日期",
            [d.isoformat() for d in dates],
            key="etf_date_pick",
        )
        snap = load_holdings(sym, dt.date.fromisoformat(date_pick), PROJECT_ROOT)
    else:
        col_dt.info("尚未匯入此 ETF 的持股快照")
        snap = None

    if snap and snap.holdings:
        st.markdown(f"#### {sym} {date_pick} 持股 ({len(snap.holdings)} 檔)")
        holding_df = pd.DataFrame([
            {"代號": h.ticker, "名稱": h.name, "權重(%)": h.weight_pct,
             "張數": h.shares, "市值": h.value}
            for h in snap.holdings
        ])
        st.dataframe(holding_df, hide_index=True, use_container_width=True)
        if "權重(%)" in holding_df.columns and len(holding_df):
            top10 = holding_df.nlargest(10, "權重(%)")
            st.markdown("#### 前 10 大持股權重")
            st.bar_chart(top10.set_index("代號")["權重(%)"])
    else:
        st.info("此 ETF 尚無對應日期的持股檔。可在下方匯入 / 編輯。")

    with st.expander("自動從投信網頁抓持股 (使用 Gemini 抽取)", expanded=False):
        st.caption(
            "於下方填入每一檔 ETF 的官方持股頁 URL (或留空關閉)，"
            "按「立即抓取所有」會：HTTP 下載 → 轉純文字 → 呼叫 Gemini `extract_etf_holdings` prompt → "
            "存為當日 CSV。"
        )
        edited_rows = []
        for e in etfs:
            cols = st.columns([1, 2, 4])
            cols[0].write(f"**{e.symbol}**")
            cols[1].write(e.name)
            url = cols[2].text_input(
                "URL", value=e.holdings_url, key=f"url_{e.symbol}",
                label_visibility="collapsed",
                placeholder="https://投信網站/該 ETF 持股頁",
            )
            edited_rows.append((e, url))

        cu, cf = st.columns(2)
        if cu.button("儲存 URL 設定", key="save_urls"):
            new_list = []
            for e, url in edited_rows:
                new_list.append(ActiveEtf(
                    symbol=e.symbol, name=e.name, issuer=e.issuer,
                    region=e.region, freq=e.freq, holdings_url=url.strip(),
                ))
            p = save_active_etfs(new_list, PROJECT_ROOT)
            st.success(f"已儲存到 {p}")
            st.rerun()

        if cf.button("立即抓取所有 (有 URL 的)", type="primary", key="fetch_all"):
            env_values = load_env()
            api_key = env_values.get("GEMINI_API_KEY", "")
            model = env_values.get("GEMINI_MODEL", "gemini-2.5-flash")
            if not api_key:
                st.error("請先在「組態設定」填入 GEMINI_API_KEY")
            else:
                from bot.etf_holdings_fetcher import fetch_all_active_etfs
                from bot.llm_analyzer import GeminiClient

                client = GeminiClient(api_key=api_key, model=model)
                with st.spinner("抓取中..."):
                    results = fetch_all_active_etfs(
                        client, root=PROJECT_ROOT,
                    )
                rows = [{
                    "代號": r.etf.symbol,
                    "名稱": r.etf.name,
                    "成功": r.success,
                    "持股數": r.holdings_count,
                    "錯誤": r.error,
                    "存檔": str(r.saved_path) if r.saved_path else "",
                } for r in results]
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    with st.expander("匯入 / 編輯持股 CSV", expanded=False):
        st.caption(
            "CSV 欄位: `ticker,name,weight_pct,shares,value`，"
            "存於 `data/etf_holdings/<symbol>/<YYYY-MM-DD>.csv`"
        )
        new_date = st.date_input("快照日期", value=dt.date.today())
        uploaded = st.file_uploader("上傳 CSV", type=["csv"], key="etf_upload")
        if uploaded is not None and st.button("儲存到快照", key="etf_save_csv"):
            try:
                df_in = pd.read_csv(uploaded, encoding="utf-8-sig")
            except Exception:
                uploaded.seek(0)
                df_in = pd.read_csv(uploaded, encoding="utf-8")
            holdings: List[Holding] = []
            for _, row in df_in.iterrows():
                holdings.append(Holding(
                    ticker=str(row.get("ticker", "")).strip(),
                    name=str(row.get("name", "")),
                    weight_pct=float(row.get("weight_pct", 0) or 0),
                    shares=float(row.get("shares", 0) or 0),
                    value=float(row.get("value", 0) or 0),
                ))
            snap_new = HoldingsSnapshot(symbol=sym, date=new_date, holdings=holdings)
            p = save_holdings(snap_new, PROJECT_ROOT)
            st.success(f"已儲存 {len(holdings)} 筆到 `{p}`")

        st.markdown("##### 手動快速建立 (粘貼 CSV 文字)")
        sample = "ticker,name,weight_pct,shares,value\n2330,台積電,22.1,500,5000000\n"
        text = st.text_area(
            "貼上 CSV 內容", value="", height=150, placeholder=sample, key="etf_paste",
        )
        if text and st.button("從文字儲存", key="etf_save_text"):
            import io
            df_in = pd.read_csv(io.StringIO(text))
            holdings = [
                Holding(
                    ticker=str(r.get("ticker", "")).strip(),
                    name=str(r.get("name", "")),
                    weight_pct=float(r.get("weight_pct", 0) or 0),
                    shares=float(r.get("shares", 0) or 0),
                    value=float(r.get("value", 0) or 0),
                )
                for _, r in df_in.iterrows()
            ]
            snap_new = HoldingsSnapshot(symbol=sym, date=new_date, holdings=holdings)
            p = save_holdings(snap_new, PROJECT_ROOT)
            st.success(f"已儲存 {len(holdings)} 筆到 `{p}`")


# ======================================================================
# 頁面: 跟單訊號
# ======================================================================


def _load_all_latest_snapshots() -> Dict[str, "HoldingsSnapshot"]:
    etfs = load_active_etfs(PROJECT_ROOT)
    out: Dict[str, HoldingsSnapshot] = {}
    for e in etfs:
        dates = list_holdings_dates(e.symbol, PROJECT_ROOT)
        if not dates:
            continue
        snap = load_holdings(e.symbol, dates[0], PROJECT_ROOT)
        if snap:
            out[e.symbol] = snap
    return out


def _load_prev_snapshots() -> Dict[str, "HoldingsSnapshot"]:
    etfs = load_active_etfs(PROJECT_ROOT)
    out: Dict[str, HoldingsSnapshot] = {}
    for e in etfs:
        dates = list_holdings_dates(e.symbol, PROJECT_ROOT)
        if len(dates) < 2:
            continue
        snap = load_holdings(e.symbol, dates[1], PROJECT_ROOT)
        if snap:
            out[e.symbol] = snap
    return out


def page_follow_signals() -> None:
    st.title("ETF 共識跟單訊號")
    st.caption("跨多檔主動式 ETF 自動偵測共識持股、加碼、新建倉")

    etfs = load_active_etfs(PROJECT_ROOT)
    etf_meta = {e.symbol: e for e in etfs}
    latest = _load_all_latest_snapshots()
    prev = _load_prev_snapshots()

    if not latest:
        st.warning(
            "尚未有任何 ETF 持股快照。請先到「主動 ETF 追蹤」頁匯入至少一檔 ETF 的持股 CSV。"
        )
        return

    col1, col2, col3 = st.columns(3)
    min_consensus = col1.number_input("共識持股 ETF 數門檻", 1, 10, 2)
    min_new = col2.number_input("新建倉 ETF 數門檻", 1, 10, 2)
    min_add = col3.number_input("共識加碼 ETF 數門檻", 1, 10, 3)

    tab_consensus, tab_new, tab_add, tab_changes = st.tabs([
        "共識持股 (橫向)", "共識新建倉", "共識加碼", "持股變動明細",
    ])

    with tab_consensus:
        consensus = build_consensus(latest, etf_meta, min_etf_count=int(min_consensus))
        st.metric("符合門檻的個股數", len(consensus))
        if consensus:
            rows = []
            for c in consensus[:200]:
                rows.append({
                    "個股": c.ticker,
                    "名稱": c.name,
                    "ETF 數": c.etf_count,
                    "合計權重(%)": round(c.total_weight, 2),
                    "平均權重(%)": round(c.avg_weight, 2),
                    "持有 ETF": ", ".join(w.etf_symbol for w in c.held_by),
                })
            df = pd.DataFrame(rows)
            st.dataframe(df, hide_index=True, use_container_width=True)
            top20 = df.head(20)
            st.markdown("#### Top 20 共識持股 (ETF 數)")
            st.bar_chart(top20.set_index("個股")["ETF 數"])
        else:
            st.info("沒有達到門檻的共識持股。")

    with tab_new:
        if not prev:
            st.info("需要至少兩個快照日期才能比較新建倉。")
        else:
            from bot.etf_consensus import diff_snapshots
            changes = []
            for sym, snap_after in latest.items():
                snap_before = prev.get(sym)
                if snap_before:
                    changes.extend(diff_snapshots(snap_before, snap_after))
            news = consensus_new_builds(changes, min_etfs=int(min_new))
            st.metric("共識新建倉訊號", len(news))
            if news:
                rows = [{
                    "個股": s.ticker, "名稱": s.name,
                    "同步建倉 ETF 數": s.etf_count,
                    "合計權重": round(s.total_weight_delta, 2),
                    "建倉 ETF": ", ".join(s.related_etfs),
                    "說明": s.note,
                } for s in news]
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            else:
                st.info("沒有達到門檻的新建倉訊號。")

    with tab_add:
        if not prev:
            st.info("需要至少兩個快照日期才能比較加碼。")
        else:
            from bot.etf_consensus import diff_snapshots
            changes = []
            for sym, snap_after in latest.items():
                snap_before = prev.get(sym)
                if snap_before:
                    changes.extend(diff_snapshots(snap_before, snap_after))
            adds = consensus_additions(changes, min_etfs=int(min_add))
            st.metric("共識加碼訊號", len(adds))
            if adds:
                rows = [{
                    "個股": s.ticker, "名稱": s.name,
                    "同步加碼 ETF 數": s.etf_count,
                    "權重增幅(%)": round(s.total_weight_delta, 2),
                    "加碼 ETF": ", ".join(s.related_etfs),
                } for s in adds]
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            else:
                st.info("沒有達到門檻的加碼訊號。")

    with tab_changes:
        if not prev:
            st.info("需要至少兩個快照日期才能比較持股變動。")
        else:
            from bot.etf_consensus import diff_snapshots
            sel_etf = st.selectbox(
                "選擇 ETF",
                [f"{s} {etf_meta[s].name}" for s in sorted(latest.keys() & prev.keys())],
                key="changes_etf",
            )
            sym = sel_etf.split(" ")[0]
            changes = diff_snapshots(prev[sym], latest[sym])
            if changes:
                rows = [{
                    "個股": c.ticker, "名稱": c.name,
                    "變動類型": c.change_type,
                    "前權重": round(c.weight_pct_before, 2),
                    "新權重": round(c.weight_pct_after, 2),
                    "Δ權重": round(c.weight_delta, 2),
                    "Δ張數": round(c.shares_delta, 0),
                } for c in changes]
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            else:
                st.info("無持股變動。")


# ======================================================================
# 頁面: LLM 法說會分析
# ======================================================================


def page_llm_analysis() -> None:
    st.title("LLM 法說會分析 / 邏輯反查")

    env_values = load_env()
    api_key = env_values.get("GEMINI_API_KEY", "")
    model = env_values.get("GEMINI_MODEL", "gemini-2.5-flash")

    c1, c2 = st.columns(2)
    c1.markdown(
        f"Gemini API Key: {_badge('已設定', 'green') if api_key else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )
    c2.markdown(f"模型: `{model}`")

    if not api_key:
        st.warning(
            "請先到「組態設定」頁填入 `GEMINI_API_KEY`。"
            "可在 https://aistudio.google.com 免費取得。"
        )

    tab_paste, tab_mops, tab_logic = st.tabs([
        "貼文字分析", "MOPS 行事曆 / 重大訊息", "言行反查",
    ])

    with tab_paste:
        ticker = st.text_input("股票代號", value="2330", key="llm_ticker")
        text = st.text_area(
            "貼上法說會逐字稿 / 簡報文字",
            height=300,
            placeholder="例：本季營收 850 億美元，年增 35%，下季毛利率指引 56-58% ...",
            key="llm_text",
        )
        run = st.button("用 Gemini 分析", type="primary", disabled=not (api_key and text))
        if run:
            from bot.llm_analyzer import GeminiClient, analyze_presentation
            client = GeminiClient(api_key=api_key, model=model)
            with st.spinner("Gemini 解析中..."):
                analysis = analyze_presentation(text, ticker=ticker, client=client)

            st.session_state["last_llm_analysis"] = analysis

            c1, c2, c3 = st.columns(3)
            c1.metric("情緒", analysis.sentiment)
            c2.metric("情緒分數", f"{analysis.sentiment_score:+.2f}")
            c3.metric("置信度", f"{analysis.confidence:.0%}")

            st.markdown("### 摘要")
            st.write(analysis.summary or "(無)")

            cc1, cc2 = st.columns(2)
            with cc1:
                st.markdown("**成長驅動因子**")
                for d in analysis.growth_drivers:
                    st.write(f"- {d}")
            with cc2:
                st.markdown("**風險**")
                for r in analysis.risks:
                    st.write(f"- {r}")

            st.markdown("**關鍵指標**")
            st.json(analysis.key_metrics)

            st.markdown(
                f"**Capex 信號:** {analysis.capex_signal} "
                f"&nbsp;|&nbsp; **毛利率展望:** {analysis.margin_outlook}"
            )

            with st.expander("Gemini 原始回應"):
                st.code(analysis.raw_response or "(無)", language="json")

    with tab_mops:
        st.caption("從公開資訊觀測站抓法說會行事曆與重大訊息 (測試用)")
        sub_tab_conf, sub_tab_mat = st.tabs(["法說會行事曆", "個股重大訊息"])

        with sub_tab_conf:
            today = dt.date.today()
            roc_year = today.year - 1911
            ry = st.number_input("民國年", 100, roc_year + 1, roc_year)
            rm = st.number_input("月份", 1, 12, today.month)
            if st.button("抓取行事曆", key="mops_conf_fetch"):
                from bot.mops_scraper import fetch_conference_schedule
                with st.spinner("MOPS 抓取中..."):
                    entries = fetch_conference_schedule(int(ry), int(rm))
                if entries:
                    df = pd.DataFrame([
                        {"日期": e.date.isoformat(), "時間": e.time,
                         "代號": e.ticker, "公司": e.company, "備註": e.note}
                        for e in entries
                    ])
                    st.dataframe(df, hide_index=True, use_container_width=True)
                else:
                    st.info("查無資料，或 MOPS 端點異動。")

        with sub_tab_mat:
            mt = st.text_input("股票代號", value="2330", key="mops_mat_ticker")
            if st.button("抓取重大訊息", key="mops_mat_fetch"):
                from bot.mops_scraper import fetch_material_info
                with st.spinner("MOPS 抓取中..."):
                    materials = fetch_material_info(mt)
                if materials:
                    df = pd.DataFrame([
                        {"日期": m.date.isoformat(), "時間": m.time,
                         "代號": m.ticker, "主旨": m.subject}
                        for m in materials
                    ])
                    st.dataframe(df, hide_index=True, use_container_width=True)
                else:
                    st.info("查無資料。")

    with tab_logic:
        st.caption("用『法說會語意』+『籌碼面』交叉檢驗管理階層是否言行一致")
        last_analysis = st.session_state.get("last_llm_analysis")
        if last_analysis is None:
            st.info("請先在『貼文字分析』頁完成一場法說會分析。")
        else:
            st.write(
                f"基於最近一次分析: **{last_analysis.ticker}** "
                f"(sentiment={last_analysis.sentiment} {last_analysis.sentiment_score:+.2f})"
            )

            auto_fetch = st.button(
                f"自動抓 {last_analysis.ticker} 籌碼面 (近 5 日 TWSE)",
                key="auto_chips",
            )
            if auto_fetch:
                from bot.chips_fetcher import build_chip_summary
                with st.spinner("自 TWSE 抓籌碼面..."):
                    s = build_chip_summary(
                        last_analysis.ticker, days=5, root=PROJECT_ROOT,
                    )
                st.session_state["auto_chips_summary"] = s
                st.success(
                    f"完成。外資 {s.foreign_net:+.0f} 張、投信 {s.investment_trust_net:+.0f}、"
                    f"借券變動 {s.short_borrow_change_pct:+.1f}%"
                )

            auto_s = st.session_state.get("auto_chips_summary")
            cc1, cc2 = st.columns(2)
            with cc1:
                foreign = st.number_input(
                    "外資近期淨買超 (張)",
                    value=float(auto_s.foreign_net) if auto_s else 0.0,
                    step=100.0,
                )
                trust = st.number_input(
                    "投信淨買超 (張)",
                    value=float(auto_s.investment_trust_net) if auto_s else 0.0,
                    step=100.0,
                )
                dealer = st.number_input(
                    "自營商淨買超 (張)",
                    value=float(auto_s.dealer_net) if auto_s else 0.0,
                    step=100.0,
                )
            with cc2:
                margin = st.number_input(
                    "融資餘額變動 (%)",
                    value=float(auto_s.margin_buy_change_pct) if auto_s else 0.0,
                    step=1.0,
                )
                short_b = st.number_input(
                    "借券賣出餘額變動 (%)",
                    value=float(auto_s.short_borrow_change_pct) if auto_s else 0.0,
                    step=1.0,
                )
                block = st.number_input(
                    "鉅額交易淨額 (張)",
                    value=float(auto_s.block_trade_net) if auto_s else 0.0,
                    step=100.0,
                )
            note = st.text_input("備註", "")
            if st.button("執行反查", type="primary"):
                from bot.llm_analyzer import ChipsContext, GeminiClient, logic_check
                chips = ChipsContext(
                    foreign_net=foreign,
                    investment_trust_net=trust,
                    dealer_net=dealer,
                    margin_buy_change_pct=margin,
                    short_borrow_change_pct=short_b,
                    block_trade_net=block,
                    notes=note,
                )
                client = GeminiClient(api_key=api_key, model=model) if api_key else None
                with st.spinner("反查中..."):
                    result = logic_check(last_analysis, chips, client)
                verdict_color = {
                    "consistent": "green",
                    "suspicious_distribution": "red",
                    "suspicious_accumulation": "yellow",
                    "inconclusive": "gray",
                }.get(result.verdict, "gray")
                st.markdown(
                    f"### 判定: {_badge(result.verdict, verdict_color)}",
                    unsafe_allow_html=True,
                )
                cc1, cc2 = st.columns(2)
                cc1.metric("置信度", f"{result.confidence:.0%}")
                cc2.metric("建議", result.suggestion.upper())
                st.write(result.reasoning)


# ======================================================================
# 頁面: 自動化管線
# ======================================================================


def page_pipeline() -> None:
    st.title("自動化研究管線")
    st.caption(
        "一鍵完成：ETF 持股自動抓取 → 共識計算 → 籌碼面拉取 → 法說 LLM 解析 → "
        "言行反查 → 每日簡報"
    )

    env_values = load_env()
    api_key = env_values.get("GEMINI_API_KEY", "")
    model = env_values.get("GEMINI_MODEL", "gemini-2.5-flash")

    c1, c2, c3 = st.columns(3)
    c1.markdown(
        f"Gemini API Key: {_badge('已設定', 'green') if api_key else _badge('未設定', 'red')}",
        unsafe_allow_html=True,
    )
    c2.markdown(f"模型: `{model}`")
    etfs_with_url = [e for e in load_active_etfs(PROJECT_ROOT) if e.holdings_url]
    c3.metric("有 URL 的 ETF", f"{len(etfs_with_url)}")

    st.markdown("### 管線設定")
    a, b, c, d = st.columns(4)
    fetch_etf = a.toggle(
        "抓 ETF 持股",
        value=env_values.get("PIPELINE_FETCH_ETF", "true").lower() == "true",
    )
    fetch_chips = b.toggle(
        "抓籌碼面",
        value=env_values.get("PIPELINE_FETCH_CHIPS", "true").lower() == "true",
    )
    run_llm = c.toggle("LLM 法說分析", value=bool(api_key))
    gen_brief = d.toggle(
        "產出每日簡報",
        value=env_values.get("PIPELINE_GENERATE_BRIEF", "true").lower() == "true",
    )

    e1, e2, e3 = st.columns(3)
    days = e1.number_input(
        "籌碼面回顧天數",
        2, 20,
        int(env_values.get("PIPELINE_CHIP_LOOKBACK_DAYS", "5") or 5),
    )
    min_cons = e2.number_input(
        "共識焦點門檻 (ETF 數)",
        1, 10,
        int(env_values.get("PIPELINE_MIN_CONSENSUS", "2") or 2),
    )
    extra_focus = e3.text_input(
        "額外焦點 (逗號分隔)", value="", placeholder="2330,2382",
    )

    st.markdown("##### 附加法說會輸入 (可選)")
    pres_ticker = st.text_input(
        "ticker", value="", placeholder="2330", key="pipe_pres_ticker",
    )
    pres_text = st.text_area(
        "法說會逐字稿 / 簡報內容",
        height=180,
        placeholder="可貼一份重要法說會的內容；留空則跳過此步驟",
        key="pipe_pres_text",
    )

    run_now = st.button("立即執行管線", type="primary", use_container_width=True)
    if run_now:
        from bot.data_pipeline import (
            PipelineConfig,
            PresentationInput,
            run_full_pipeline,
        )

        focus = [s.strip() for s in extra_focus.split(",") if s.strip()]
        presentations: List[PresentationInput] = []
        if pres_ticker.strip() and pres_text.strip():
            presentations.append(PresentationInput(
                ticker=pres_ticker.strip(),
                text=pres_text,
                source="dashboard",
                label="manual_paste",
            ))

        config = PipelineConfig(
            project_root=PROJECT_ROOT,
            gemini_api_key=api_key,
            gemini_model=model,
            focus_tickers=focus,
            chip_lookback_days=int(days),
            fetch_etf_holdings=bool(fetch_etf),
            fetch_chips=bool(fetch_chips),
            run_llm_analysis=bool(run_llm) and bool(api_key),
            generate_brief=bool(gen_brief) and bool(api_key),
            min_consensus_for_focus=int(min_cons),
            presentation_inputs=presentations,
        )

        with st.spinner("Pipeline 執行中，請稍候 (依勾選步驟與資料量約 1-5 分鐘)..."):
            run = run_full_pipeline(config)
        st.success(
            f"完成! run_id={run.run_id} 耗時 {run.duration_sec:.1f}s "
            f"焦點 {len(run.focus_tickers)} 檔，錯誤 {len(run.errors)} 筆"
        )
        st.session_state["last_pipeline_run_id"] = run.run_id

    st.markdown("---")
    st.markdown("### 歷史執行紀錄")
    from bot.data_pipeline import list_pipeline_runs, load_pipeline_run
    history = list_pipeline_runs(PROJECT_ROOT)
    if not history:
        st.info("尚無執行紀錄。設定好參數後按「立即執行管線」即可。")
        return

    df = pd.DataFrame(history)
    if "run_id" in df.columns:
        st.dataframe(df, hide_index=True, use_container_width=True)
    run_ids = [h["run_id"] for h in history]
    default_id = st.session_state.get("last_pipeline_run_id", run_ids[0])
    idx = run_ids.index(default_id) if default_id in run_ids else 0
    pick = st.selectbox("檢視 run", run_ids, index=idx, key="pipe_pick")

    run = load_pipeline_run(PROJECT_ROOT, pick)
    if run is None:
        st.warning("找不到 run 內容")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("焦點個股", len(run.focus_tickers))
    m2.metric("法說會分析", len(run.presentation_analyses))
    m3.metric("ETF 抓取成功", run.etf_fetch_summary.get("success", 0))
    m4.metric("錯誤", len(run.errors))

    t_brief, t_etf, t_cons, t_pres, t_logic, t_chip, t_err = st.tabs([
        "每日簡報", "ETF 抓取", "共識/訊號", "法說分析", "言行反查",
        "籌碼面", "錯誤",
    ])

    with t_brief:
        if run.daily_brief_md:
            st.caption(
                f"prompt: {run.daily_brief_prompt_id} v{run.daily_brief_prompt_version}"
            )
            st.markdown(run.daily_brief_md)
            st.download_button(
                "下載 daily_brief.md",
                data=run.daily_brief_md.encode("utf-8"),
                file_name=f"daily_brief_{pick}.md",
            )
        else:
            st.info("此次未產出簡報 (可能 LLM 未啟用或產生失敗)")

    with t_etf:
        details = run.etf_fetch_summary.get("details", [])
        if details:
            st.dataframe(pd.DataFrame(details), hide_index=True, use_container_width=True)
        else:
            st.info("(略過或無資料)")

    with t_cons:
        st.markdown("#### 共識持股 Top 20")
        if run.consensus_top:
            st.dataframe(pd.DataFrame(run.consensus_top[:20]), hide_index=True, use_container_width=True)
        st.markdown("#### 共識新建倉")
        if run.new_build_signals:
            st.dataframe(pd.DataFrame(run.new_build_signals), hide_index=True, use_container_width=True)
        st.markdown("#### 共識加碼")
        if run.add_signals:
            st.dataframe(pd.DataFrame(run.add_signals), hide_index=True, use_container_width=True)

    with t_pres:
        if run.presentation_analyses:
            for a in run.presentation_analyses:
                with st.expander(f"{a.get('ticker','-')} — {a.get('label','')}"):
                    st.write(f"sentiment: **{a.get('sentiment')}** ({a.get('sentiment_score'):+.2f}), confidence: {a.get('confidence'):.0%}")
                    st.write(a.get("summary", ""))
                    st.json({k: a[k] for k in ("growth_drivers","risks","key_metrics","capex_signal","margin_outlook") if k in a})
        else:
            st.info("(無法說會分析)")

    with t_logic:
        if run.logic_checks:
            st.dataframe(pd.DataFrame(run.logic_checks), hide_index=True, use_container_width=True)
        else:
            st.info("(無)")

    with t_chip:
        if run.chip_summaries:
            cols = ["ticker", "days", "foreign_net", "investment_trust_net",
                    "dealer_net", "margin_buy_change_pct", "short_borrow_change_pct",
                    "block_trade_net"]
            rows = [{k: c.get(k) for k in cols} for c in run.chip_summaries]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.info("(略)")

    with t_err:
        if run.errors:
            for e in run.errors:
                st.error(e)
        else:
            st.success("無錯誤")


# ======================================================================
# 頁面: Prompt 管理
# ======================================================================


def page_prompts() -> None:
    st.title("Prompt 管理")
    st.caption(
        "所有 LLM 呼叫使用的 Prompt 都集中在 `prompts/*.yaml` 中。"
        "於此修改後立即生效，並會同步寫入 Git 控管的檔案。"
    )

    reg = get_registry(PROJECT_ROOT / "prompts")
    ids = reg.list_ids()

    c1, c2 = st.columns([3, 1])
    c1.metric("Prompt 數", len(ids))
    if c2.button("重新載入"):
        reg.reload()
        st.toast("已重新載入 Prompt", icon="✅")
        st.rerun()

    if not ids:
        st.warning(
            f"prompts 目錄找不到任何 YAML：{PROJECT_ROOT / 'prompts'}"
        )
        return

    # 概覽表
    rows = []
    for pid in ids:
        p = reg.get(pid)
        if p is None:
            continue
        rows.append({
            "id": p.id,
            "version": p.version,
            "model": p.model_hint,
            "max_tokens": p.max_output_tokens,
            "輸入欄位": ", ".join(i.name for i in p.inputs),
            "說明": p.description,
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.markdown("---")
    pid = st.selectbox("選擇 Prompt", ids, key="prompt_pick")
    p = reg.get(pid)
    if p is None:
        return

    st.markdown(f"### {p.id} (v{p.version})")
    st.caption(p.description)

    yaml_text = ""
    if p.path and p.path.exists():
        yaml_text = p.path.read_text(encoding="utf-8")
    else:
        yaml_text = p.to_yaml_text()

    edited = st.text_area(
        "YAML 原文 (修改後按下方儲存即可生效)",
        value=yaml_text,
        height=500,
        key=f"prompt_yaml_{pid}",
    )

    csave, ctest = st.columns(2)
    if csave.button("儲存", type="primary", key=f"save_{pid}"):
        try:
            path = reg.save_yaml(pid, edited)
            st.success(f"已儲存到 {path}，並已重新載入")
        except Exception as e:
            st.error(f"儲存失敗: {e}")

    if ctest.button("Render 預覽", key=f"render_{pid}"):
        try:
            sample_vars = {}
            for inp in p.inputs:
                sample_vars[inp.name] = f"<{inp.name}>"
            preview = p.render(**sample_vars)
            st.code(preview, language="text")
        except Exception as e:
            st.error(f"render 失敗: {e}")


# ======================================================================
# 頁面: LLM 呼叫紀錄
# ======================================================================


def page_llm_log() -> None:
    st.title("LLM 呼叫紀錄")
    st.caption(
        "所有透過 `gemini_call()` 與 `analyze_presentation()` / `logic_check()` 等 "
        "API 發出的 LLM 請求都會落地於 `log/llm_calls/*.jsonl`。"
    )

    log = get_call_logger()
    dates = log.list_dates()
    if not dates:
        st.info("尚無 LLM 呼叫紀錄。執行管線或法說分析後即會出現。")
        return

    c1, c2, c3 = st.columns([1, 1, 2])
    date_pick = c1.selectbox("日期", [d.isoformat() for d in dates], key="llm_date")
    limit = c2.number_input("最多顯示筆數", 10, 5000, 200, step=10)
    flt_id = c3.text_input("過濾 prompt_id (留空全部)", value="", key="llm_filter")

    records = log.read(dt.date.fromisoformat(date_pick), limit=int(limit))
    if flt_id:
        records = [r for r in records if flt_id in r.prompt_id]

    if not records:
        st.info("此日無紀錄")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("總筆數", len(records))
    m2.metric("成功", sum(1 for r in records if r.success))
    m3.metric("失敗", sum(1 for r in records if not r.success))
    avg_latency = sum(r.latency_ms for r in records) / max(1, len(records))
    m4.metric("平均延遲", f"{avg_latency:.0f} ms")

    rows = []
    for r in records:
        rows.append({
            "時間": r.ts,
            "prompt_id": r.prompt_id,
            "版本": r.prompt_version,
            "模型": r.model,
            "延遲(ms)": r.latency_ms,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "成功": r.success,
            "error": r.error[:80] if r.error else "",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.markdown("---")
    st.markdown("### 檢視單筆")
    idx_options = [
        f"{i}: {r.ts} {r.prompt_id} v{r.prompt_version}"
        for i, r in enumerate(records)
    ]
    pick = st.selectbox("選擇一筆", idx_options, key="llm_call_pick")
    idx = int(pick.split(":")[0])
    r = records[idx]

    tabs = st.tabs(["Output", "Input (rendered prompt)", "Metadata"])
    with tabs[0]:
        st.code(r.output or "(空)", language="json" if r.prompt_id != "(rule_based)" else "text")
    with tabs[1]:
        st.code(r.input, language="text")
    with tabs[2]:
        st.json({
            "ts": r.ts,
            "prompt_id": r.prompt_id,
            "prompt_version": r.prompt_version,
            "model": r.model,
            "latency_ms": r.latency_ms,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "success": r.success,
            "error": r.error,
            "metadata": r.metadata,
        })


# ======================================================================
# 頁面: 個股總覽 (Watchlist)
# ======================================================================


def _action_badge(action: str) -> str:
    palette = {
        "STRONG_BUY": ("強烈買進", "green"),
        "BUY": ("買進", "green"),
        "HOLD": ("觀望", "gray"),
        "REDUCE": ("減碼", "orange"),
        "SELL": ("賣出", "red"),
    }
    label, color = palette.get(action, (action, "gray"))
    return _badge(label, color)


def _build_scorecards(
    tickers: List[str],
    *,
    name_map: Optional[Dict[str, str]] = None,
    refresh_chips: bool = False,
    refresh_fundamentals: bool = False,
    refresh_technicals: bool = False,
    refresh_distribution: bool = False,
) -> List:
    """對一群 ticker 組 snapshot 後跑 scoring。"""
    out = []
    nm = name_map or {}
    from bot.fundamentals_fetcher import snapshot_to_dict as _fund_dict
    from bot.technicals import snapshot_to_dict as _tech_dict
    for t in tickers:
        snap = build_snapshot(
            t, PROJECT_ROOT,
            refresh_chips=refresh_chips,
            refresh_fundamentals=refresh_fundamentals,
            refresh_technicals=refresh_technicals,
            refresh_distribution=refresh_distribution,
            name_hint=nm.get(t, ""),
        )
        card = compute_scorecard(
            ticker=snap.ticker,
            name=snap.name,
            price=snap.price,
            pct_change=snap.pct_change,
            volume=snap.volume,
            llm_analysis=snap.llm_analysis,
            logic_result=snap.logic_check,
            consensus=snap.consensus,
            new_build_signal=snap.new_build_signal,
            add_signal=snap.add_signal,
            chip_summary=snap.chip_summary,
            fundamental=_fund_dict(snap.fundamentals) if snap.fundamentals else None,
            technical_snapshot=_tech_dict(snap.technicals) if snap.technicals else None,
            distribution_label=snap.distribution_label,
            distribution_detail=snap.distribution_detail,
            distribution_score=snap.distribution_score,
            has_distribution=bool(snap.distribution_trend and snap.distribution_trend.weeks),
            fetched_at=snap.fetched_at,
        )
        out.append((snap, card))
    return out


def page_watchlist() -> None:
    st.title("個股總覽 (Watchlist)")
    st.caption(
        "把你關注的股票放這裡。每一檔會用四個時間框架 (當沖/短/中/長) "
        "對 LLM 法說、籌碼、ETF 共識、技術面、風險做加權評分。"
    )

    wlist = wl.load(PROJECT_ROOT)
    existing = {it.ticker for it in wlist.items}

    # ------ 工具列：增 / 刪 / 來源同步 ------
    with st.expander("管理清單 (增刪 / 來源同步)", expanded=not wlist.items):
        c1, c2, c3 = st.columns([2, 2, 1])
        new_ticker = c1.text_input("代號", value="", placeholder="2330", key="wl_new_t")
        new_name = c2.text_input("名稱 (可空)", value="", key="wl_new_n")
        new_tag = c3.text_input("標籤", value="", placeholder="AI", key="wl_new_tag")
        if c3.button("加入", use_container_width=True, key="wl_add"):
            if new_ticker.strip():
                wl.add(
                    new_ticker.strip(), name=new_name.strip(),
                    tags=[new_tag.strip()] if new_tag.strip() else [],
                    root=PROJECT_ROOT,
                )
                st.toast(f"已加入 {new_ticker}", icon="✅")
                st.rerun()

        st.markdown("##### 一鍵從現有資料合併")
        s1, s2 = st.columns(2)
        if s1.button("加入所有 ETF 共識焦點 (≥2 檔 ETF)", use_container_width=True):
            wl.merge_etf_focus(min_consensus=2, root=PROJECT_ROOT)
            st.toast("ETF 共識焦點已合併", icon="✅")
            st.rerun()
        if s2.button("加入最近一次 Pipeline 焦點", use_container_width=True):
            wl.merge_pipeline_focus(root=PROJECT_ROOT)
            st.toast("Pipeline 焦點已合併", icon="✅")
            st.rerun()

        if wlist.items:
            st.markdown("##### 清單編輯 / 移除")
            del_t = st.selectbox(
                "選擇要移除的 ticker",
                [f"{i.ticker} {i.name}" for i in wlist.items],
                key="wl_del",
            )
            if st.button("移除", key="wl_del_btn"):
                wl.remove(del_t.split()[0], root=PROJECT_ROOT)
                st.rerun()

    # ------ 評分量表規則 ------
    with st.expander("評分規則速覽 (四時間框架 × 8 個 factor)", expanded=False):
        rows = []
        factor_labels = {
            "fundamental": "基本面",
            "technical": "技術面",
            "chips": "三大法人",
            "distribution": "大戶結構",
            "etf_consensus": "ETF 共識",
            "llm_sentiment": "法說語意",
            "logic": "言行一致性",
            "risk": "風險警示",
        }
        for tf, weights in WEIGHTS.items():
            row = {"時間框架": TIMEFRAME_LABELS[tf]}
            for fk, label in factor_labels.items():
                row[label] = f"{weights.get(fk, 0):.0%}" if fk in weights else "—"
            rows.append(row)
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        st.markdown("**建議行動門檻** — `≥78 強烈買進 / ≥62 買進 / ≥45 觀望 / ≥30 減碼 / <30 賣出`")
        strat_rows = []
        for tf, r in STRATEGY_RULES.items():
            strat_rows.append({
                "時間框架": TIMEFRAME_LABELS[tf],
                "停損": f"{r['stop_pct']:+.1f}%",
                "停利": f"{r['target_pct']:+.1f}%",
                "盈虧比": f"{abs(r['target_pct']/r['stop_pct']):.2f}",
                "持有期": r["horizon"],
                "部位": r["size_hint"],
                "進場邏輯": r["entry_logic"],
            })
        st.dataframe(pd.DataFrame(strat_rows), hide_index=True, use_container_width=True)

    if not wlist.items:
        st.info("Watchlist 是空的。請從上方加入 ticker，或一鍵合併 ETF 共識焦點。")
        return

    # ------ 評分 (含資料抓取選項) ------
    cs1, cs2, cs3, cs4, cs5 = st.columns([1, 1, 1, 1, 1])
    refresh_chips = cs1.toggle(
        "現抓 5 日籌碼", value=False, key="wl_refresh",
        help="勾選會對每檔逐一呼叫 TWSE OpenAPI (約每檔 1-3 秒)。",
    )
    refresh_tech = cs2.toggle("抓日K + 指標", value=False, key="wl_refresh_t")
    refresh_fund = cs3.toggle("更新基本面", value=False, key="wl_refresh_f")
    refresh_dist = cs4.toggle("更新 TDCC", value=False, key="wl_refresh_d")
    if cs5.button("計算評分", type="primary", use_container_width=True, key="wl_calc"):
        st.session_state.pop("watchlist_cards", None)

    cards = st.session_state.get("watchlist_cards")
    if cards is None:
        with st.spinner("組裝個股資料 + 計算評分..."):
            cards = _build_scorecards(
                [i.ticker for i in wlist.items],
                name_map={i.ticker: i.name for i in wlist.items},
                refresh_chips=refresh_chips,
                refresh_technicals=refresh_tech,
                refresh_fundamentals=refresh_fund,
                refresh_distribution=refresh_dist,
            )
            st.session_state["watchlist_cards"] = cards

    # ------ 主表 ------
    rows = []
    for snap, card in cards:
        row = scorecard_to_row(card)
        row["持倉"] = f"{snap.position_qty:.0f} 張" if snap.position_qty else "—"
        row["最近分析"] = snap.pipeline_run_id or "—"
        row["共識ETF"] = (snap.consensus or {}).get("etf_count", 0)
        rows.append(row)
    df = pd.DataFrame(rows)

    # 篩選 / 排序
    cf1, cf2, cf3 = st.columns(3)
    tf_pick = cf1.selectbox(
        "排序依據",
        ["當沖分", "短期分", "中期分", "長期分"],
        index=2,
    )
    only_buy = cf2.toggle("只顯示『買進 / 強烈買進』", value=False)
    name_filter = cf3.text_input("搜尋 (代號/名稱)", value="")

    if tf_pick in df.columns:
        df = df.sort_values(tf_pick, ascending=False, na_position="last")
    if only_buy:
        df = df[df["建議"].isin(["STRONG_BUY", "BUY"])]
    if name_filter.strip():
        kw = name_filter.strip()
        df = df[df["代號"].str.contains(kw) | df["名稱"].fillna("").str.contains(kw)]

    st.dataframe(
        df, hide_index=True, use_container_width=True,
        column_config={
            "當沖分": st.column_config.ProgressColumn(
                "當沖分", min_value=0, max_value=100, format="%.1f",
            ),
            "短期分": st.column_config.ProgressColumn(
                "短期分", min_value=0, max_value=100, format="%.1f",
            ),
            "中期分": st.column_config.ProgressColumn(
                "中期分", min_value=0, max_value=100, format="%.1f",
            ),
            "長期分": st.column_config.ProgressColumn(
                "長期分", min_value=0, max_value=100, format="%.1f",
            ),
        },
    )

    # 下載 + 跳轉到深入分析
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    cd1, cd2 = st.columns([2, 3])
    cd1.download_button(
        "下載 CSV", csv_bytes,
        file_name=f"watchlist_scores_{dt.date.today().isoformat()}.csv",
    )
    picks = [r["代號"] for r in rows]
    target = cd2.selectbox("跳到個股深入分析", picks, key="wl_jump")
    if cd2.button("查看", use_container_width=True):
        st.session_state["detail_ticker"] = target
        st.session_state["page"] = "個股深入分析"
        st.rerun()


# ======================================================================
# 頁面: 個股深入分析
# ======================================================================


def _tf_card(tf_score) -> None:
    """渲染單一時間框架的評分卡。"""
    color = {"green": "#16a34a", "gray": "#6b7280", "orange": "#ea580c", "red": "#dc2626"}[
        tf_score.color
    ]
    st.markdown(
        f"""<div style="border:1px solid {color}33; background:{color}0d;
            border-radius:8px; padding:10px 14px; margin-bottom:6px;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <div style="font-weight:600; font-size:16px;">{tf_score.label}</div>
                <div style="font-size:22px; font-weight:700; color:{color}">
                    {tf_score.total:.1f}
                </div>
            </div>
            <div style="font-size:13px; color:{color}; font-weight:600;">
                {tf_score.action_label} · 資料覆蓋 {tf_score.confidence:.0%}
            </div>
            </div>""",
        unsafe_allow_html=True,
    )

    # factor breakdown
    for f in tf_score.factors:
        bar = "█" * int(f.score / 5) + "░" * (20 - int(f.score / 5))
        avail = "" if f.available else " (缺資料)"
        st.markdown(
            f"<div style='font-family:monospace; font-size:11.5px; "
            f"color:#374151; line-height:1.4;'>"
            f"{f.label} (w={f.weight:.0%}{avail}): "
            f"<span style='color:{color}'>{bar}</span> {f.score:.0f}"
            f"</div>",
            unsafe_allow_html=True,
        )
        if f.detail:
            st.caption(f"　└ {f.detail}")

    if tf_score.strategy and tf_score.strategy.get("applicable"):
        s = tf_score.strategy
        st.markdown(
            f"**策略** | 進 `{s['entry']}` · 停損 `{s['stop']}` ({s['stop_pct']:+.1f}%) "
            f"· 停利 `{s['target']}` ({s['target_pct']:+.1f}%) · 盈虧比 `{s['rrr']:.2f}` · "
            f"部位 `{s['size_hint']}`"
        )
        st.caption(f"持有期: {s['horizon']} ｜ {s['entry_logic']}")
    elif tf_score.strategy:
        st.caption(f"策略: {tf_score.strategy.get('note', '評分不足，不建議進場')}")

    for n in tf_score.notes:
        st.caption(n)


def page_ticker_detail() -> None:
    st.title("個股深入分析")

    wlist = wl.load(PROJECT_ROOT)
    options: List[str] = sorted({i.ticker for i in wlist.items})

    # 來自 watchlist 跳轉
    default_t = st.session_state.get("detail_ticker", "")
    free_t = st.text_input(
        "Ticker", value=default_t,
        placeholder="輸入代號 (例如 2330)，或從 watchlist 選一檔",
    )
    if options:
        from_wl = st.selectbox("或從 Watchlist 選", [""] + options, key="detail_pick_wl")
        if from_wl and from_wl != default_t:
            free_t = from_wl
            st.session_state["detail_ticker"] = from_wl

    cgo, ccall, crefresh, ctech, cfund, cdist, cadd = st.columns(
        [1.0, 1.1, 0.9, 0.9, 0.9, 0.8, 1.0],
    )
    if ccall.button(
        "一鍵抓全部即時資料",
        use_container_width=True,
        key="detail_fetch_all",
        help="同時啟用下面四個 toggle，把籌碼/日K/基本面/TDCC 全部重抓",
    ):
        for k in ("detail_rc", "detail_rt", "detail_rf", "detail_rd"):
            st.session_state[k] = True
        st.session_state.pop("detail_cache", None)
        st.session_state.pop("detail_cache_key", None)
    refresh_chips = crefresh.toggle("即時抓籌碼", value=False, key="detail_rc")
    refresh_technicals = ctech.toggle("抓日K + 指標", value=False, key="detail_rt")
    refresh_fundamentals = cfund.toggle("更新基本面", value=False, key="detail_rf")
    refresh_distribution = cdist.toggle("更新 TDCC", value=False, key="detail_rd")
    if cgo.button("分析", type="primary", use_container_width=True, key="detail_go"):
        st.session_state["detail_ticker"] = free_t.strip()
        st.session_state.pop("detail_cache", None)
        st.session_state.pop("detail_cache_key", None)

    if cadd.button("加入 Watchlist", use_container_width=True, key="detail_add"):
        if free_t.strip():
            wl.add(free_t.strip(), root=PROJECT_ROOT)
            st.toast(f"已加入 {free_t} 到 Watchlist", icon="✅")

    ticker = st.session_state.get("detail_ticker", free_t.strip())
    if not ticker:
        st.info("請輸入或選擇一檔股票")
        return

    cache_key = f"detail_{ticker}_{refresh_chips}_{refresh_technicals}_{refresh_fundamentals}_{refresh_distribution}"
    if (
        st.session_state.get("detail_cache_key") != cache_key
        or "detail_cache" not in st.session_state
    ):
        with st.spinner(f"組裝 {ticker} 的完整資料..."):
            from bot.fundamentals_fetcher import snapshot_to_dict as _fund_dict
            from bot.technicals import snapshot_to_dict as _tech_dict
            snap = build_snapshot(
                ticker, PROJECT_ROOT,
                refresh_chips=refresh_chips,
                refresh_fundamentals=refresh_fundamentals,
                refresh_technicals=refresh_technicals,
                refresh_distribution=refresh_distribution,
            )
            card = compute_scorecard(
                ticker=snap.ticker, name=snap.name,
                price=snap.price, pct_change=snap.pct_change, volume=snap.volume,
                llm_analysis=snap.llm_analysis,
                logic_result=snap.logic_check,
                consensus=snap.consensus,
                new_build_signal=snap.new_build_signal,
                add_signal=snap.add_signal,
                chip_summary=snap.chip_summary,
                fundamental=_fund_dict(snap.fundamentals) if snap.fundamentals else None,
                technical_snapshot=_tech_dict(snap.technicals) if snap.technicals else None,
                distribution_label=snap.distribution_label,
                distribution_detail=snap.distribution_detail,
                distribution_score=snap.distribution_score,
                has_distribution=bool(snap.distribution_trend and snap.distribution_trend.weeks),
                fetched_at=snap.fetched_at,
            )
            st.session_state["detail_cache"] = (snap, card)
            st.session_state["detail_cache_key"] = cache_key

    snap, card = st.session_state["detail_cache"]

    # ---- 資料完整度提示 ----
    missing: List[str] = []
    if snap.price <= 0 and not (snap.technicals and snap.technicals.has_data):
        missing.append("**現價／日K** (請勾選 `抓日K + 指標`)")
    if not snap.chip_summary:
        missing.append("**籌碼面** (請勾選 `即時抓籌碼`)")
    if not snap.fundamentals or not snap.fundamentals.revenues:
        missing.append("**基本面** (請勾選 `更新基本面`)")
    if not snap.distribution_trend or not snap.distribution_trend.weeks:
        missing.append("**大戶結構 TDCC** (請勾選 `更新 TDCC`)")
    if snap.consensus is None and not snap.held_by_etfs:
        missing.append(
            "**主動 ETF 共識** (請先到 `主動 ETF 追蹤` 抓 ETF 持股，"
            "或在 `自動化管線` 一鍵跑完整流程)"
        )
    if not snap.llm_analysis:
        missing.append("**LLM 法說分析** (請在 `LLM 法說分析` 頁貼逐字稿執行)")
    if missing:
        st.warning(
            "目前快取中缺少以下資料，點上方 **一鍵抓全部即時資料** 或勾選對應 toggle 後按 **分析**：\n\n- "
            + "\n- ".join(missing)
        )

    # ---- 頂部 KPI ----
    st.markdown(f"## {snap.ticker} {snap.name}")
    st.caption(
        f"快照時間 {snap.fetched_at} · 來源 pipeline_run "
        f"{snap.pipeline_run_id or '—'}"
    )

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("現價", f"{snap.price:.2f}" if snap.price else "—")
    k2.metric("漲跌%", f"{snap.pct_change:+.2f}%" if snap.pct_change else "—")
    if snap.chip_summary:
        k3.metric("外資累計 (張)", f"{snap.chip_summary.get('foreign_net', 0):+.0f}")
        k4.metric("投信累計", f"{snap.chip_summary.get('investment_trust_net', 0):+.0f}")
        k5.metric(
            "借券變動",
            f"{snap.chip_summary.get('short_borrow_change_pct', 0):+.1f}%",
        )
    else:
        k3.metric("外資累計", "—")
        k4.metric("投信累計", "—")
        k5.metric("借券變動", "—")
    k6.metric(
        "ETF 共識",
        f"{(snap.consensus or {}).get('etf_count', 0)} 檔",
    )

    if snap.summary_notes if False else card.summary_notes:
        for n in card.summary_notes:
            st.warning(n)

    # ---- 四張時間框評分卡 ----
    st.markdown("### 四時間框架評分")
    tcols = st.columns(4)
    for i, tf in enumerate(TIMEFRAMES):
        with tcols[i]:
            _tf_card(card.timeframes[tf])

    # ---- 完整分頁 (3D 視角 + 操作) ----
    tabs = st.tabs([
        "📝 分析", "💰 基本面", "📈 技術面",
        "🏦 籌碼面", "🎁 股利政策", "🗓️ 季報 Q1-Q4",
        "🌐 美股連動",
        "📂 原始資料", "🎯 購買策略", "💼 目前狀況", "📜 歷史狀況",
    ])

    with tabs[0]:
        st.markdown("#### LLM 法說會解讀")
        if snap.llm_analysis:
            a = snap.llm_analysis
            st.markdown(
                f"**情緒**: `{a.get('sentiment','-')}` ({a.get('sentiment_score', 0):+.2f}) "
                f" · **信心**: {float(a.get('confidence', 0)):.0%}"
                f" · prompt: `{a.get('prompt_id','')}` v{a.get('prompt_version','')}"
            )
            st.write(a.get("summary", ""))
            cA, cB = st.columns(2)
            with cA:
                st.markdown("**成長驅動**")
                for d in a.get("growth_drivers", []) or []:
                    st.markdown(f"- {d}")
            with cB:
                st.markdown("**風險點**")
                for d in a.get("risks", []) or []:
                    st.markdown(f"- {d}")
            st.markdown(
                f"**Capex 信號**: {a.get('capex_signal','-')} ｜ "
                f"**毛利率展望**: {a.get('margin_outlook','-')}"
            )
            if a.get("key_metrics"):
                st.json(a["key_metrics"])
        else:
            st.info("尚無 LLM 法說分析。請在「LLM 法說分析」頁或「自動化管線」中執行。")

        st.markdown("#### 言行反查")
        if snap.logic_check:
            lc = snap.logic_check
            st.markdown(
                f"**verdict**: `{lc.get('verdict')}` ｜ "
                f"**建議**: `{lc.get('suggestion')}` ｜ "
                f"信心 {float(lc.get('confidence', 0)):.0%}"
            )
            st.write(lc.get("reasoning", ""))
        else:
            st.info("尚未做言行反查 (需要 LLM 法說 + 籌碼資料)")

    with tabs[6]:
        st.markdown("#### 美股連動 (供應鏈夥伴 + ADR 溢價)")
        macro = snap.macro_snapshot or {}
        related = snap.us_related or []
        adr = snap.adr_premium

        if not macro:
            st.warning("尚未抓 macro 資料。請執行 `stock-macro-update` 或在「美股 / 跨市場」頁按重新抓取。")
        else:
            indices = macro.get("indices", {})
            cs = st.columns(5)
            for i, sym in enumerate(["^GSPC", "^IXIC", "^SOX", "^TWII", "^VIX"]):
                q = indices.get(sym)
                if q:
                    cs[i].metric(
                        q.get("name", sym),
                        f"{q.get('price', 0):.2f}",
                        f"{q.get('pct_change', 0):+.2f}%",
                    )
            st.caption(f"asof: {macro.get('asof_date', '-')} ｜ USDTWD={macro.get('usdtwd', 0):.3f}")

        st.markdown("##### 對應美股客戶 / 供應鏈夥伴")
        if related:
            stocks = macro.get("stocks", {}) if macro else {}
            rows = []
            for r in related:
                us = r.get("us_ticker", "")
                q = stocks.get(us) or {}
                rows.append({
                    "美股代號": us,
                    "美股名稱": r.get("us_name", us),
                    "類別": r.get("us_category", ""),
                    "供應鏈角色": r.get("role", ""),
                    "權重": r.get("weight", 0),
                    "當夜表現": (
                        f"{q.get('pct_change', 0):+.2f}%" if q else "—"
                    ),
                    "收盤價(USD)": q.get("price", "—") if q else "—",
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            avg_pct = (
                sum(float((macro.get("stocks", {}).get(r["美股代號"]) or {}).get("pct_change", 0))
                    * float(r["權重"]) for r in rows if r["當夜表現"] != "—")
                / max(1.0, sum(float(r["權重"]) for r in rows if r["當夜表現"] != "—"))
            )
            st.metric("加權平均連動 %", f"{avg_pct:+.2f}%")
        else:
            st.info(
                f"`{snap.ticker}` 在 supply_chain.json 找不到對應美股客戶。\n"
                "可以到「美股 / 跨市場」頁編輯對照表。"
            )

        if adr:
            st.markdown("##### 本股有 ADR — 溢價分析")
            cols = st.columns(4)
            cols[0].metric(f"{adr.get('adr_symbol')} (USD)", f"{adr.get('adr_price_usd', 0):.2f}")
            cols[1].metric("公允台股價", f"{adr.get('fair_tw_price', 0):.2f}")
            cols[2].metric("實際台股價", f"{adr.get('tw_price_twd', 0):.2f}")
            prem = adr.get("premium_pct", 0)
            cols[3].metric(
                "溢價/折價", f"{prem:+.2f}%",
                delta_color="normal" if abs(prem) < 1 else ("inverse" if prem < 0 else "off"),
            )
            if prem > 1.5:
                st.success("ADR 溢價 — 隔日台股早盤偏多訊號")
            elif prem < -1.5:
                st.error("ADR 折價 — 隔日台股早盤偏空訊號")
            else:
                st.info("ADR 與母股接近平價，無顯著訊號")

    with tabs[7]:
        st.markdown("#### 原始參考資料 / 來源檔")
        st.caption("此股票出現過的所有原始下載檔；點檔名可在系統檔案總管定位")
        if snap.source_files:
            df_src = pd.DataFrame(snap.source_files)
            st.dataframe(df_src, hide_index=True, use_container_width=True)
        else:
            st.info("尚無關聯原始檔。")

        st.markdown("#### 持有此股的主動式 ETF")
        if snap.held_by_etfs:
            st.dataframe(
                pd.DataFrame(snap.held_by_etfs),
                hide_index=True, use_container_width=True,
            )
        else:
            st.info("目前無主動 ETF 持有 (或尚未抓取 ETF 持股)")

        if snap.pipeline_run_dir:
            st.markdown(f"**Pipeline run 目錄**: `{snap.pipeline_run_dir}`")

    with tabs[3]:
        st.markdown("#### 三大法人 / 借券 / 融資 (近 N 日累計)")
        if snap.chip_summary:
            base = {k: v for k, v in snap.chip_summary.items() if k != "rows"}
            cols = st.columns(4)
            cols[0].metric("外資 (張)", f"{base.get('foreign_net', 0):+,.0f}")
            cols[1].metric("投信 (張)", f"{base.get('investment_trust_net', 0):+,.0f}")
            cols[2].metric("自營商 (張)", f"{base.get('dealer_net', 0):+,.0f}")
            cols[3].metric(
                "鉅額交易 (張)", f"{base.get('block_trade_net', 0):+,.0f}",
            )
            with st.expander("詳細數據 (JSON)", expanded=False):
                st.json(base)

            rows = snap.chip_summary.get("rows") or []
            if rows:
                # ---- 法人累計買賣超 + 股價疊加圖 ----
                df_c = pd.DataFrame(rows)
                if {"date", "foreign_net", "investment_trust_net"}.issubset(df_c.columns):
                    df_c = df_c.sort_values("date")
                    df_c["外資累計"] = df_c["foreign_net"].cumsum()
                    df_c["投信累計"] = df_c["investment_trust_net"].cumsum()
                    if "dealer_net" in df_c.columns:
                        df_c["自營累計"] = df_c["dealer_net"].cumsum()

                    st.markdown("**📈 法人累計買賣超走勢 (近 N 日)**")
                    try:
                        import altair as alt
                        keep_cols = ["date", "外資累計", "投信累計"]
                        if "自營累計" in df_c.columns:
                            keep_cols.append("自營累計")
                        long = df_c[keep_cols].melt(
                            id_vars=["date"], var_name="法人", value_name="累計張數",
                        )
                        chart = alt.Chart(long).mark_line(
                            point=alt.OverlayMarkDef(size=60),
                        ).encode(
                            x=alt.X("date:T", title="日期"),
                            y=alt.Y(
                                "累計張數:Q", title="累計買賣超 (張)",
                                scale=alt.Scale(zero=True),
                            ),
                            color=alt.Color(
                                "法人:N",
                                scale=alt.Scale(
                                    domain=["外資累計", "投信累計", "自營累計"],
                                    range=["#1f77b4", "#ff7f0e", "#2ca02c"],
                                ),
                            ),
                            tooltip=["date", "法人", "累計張數"],
                        ).properties(height=260)
                        zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
                            color="gray", strokeDash=[4, 4],
                        ).encode(y="y:Q")
                        st.altair_chart(zero + chart, use_container_width=True)
                    except Exception:
                        st.line_chart(df_c.set_index("date")[
                            [c for c in ["外資累計", "投信累計", "自營累計"] if c in df_c.columns]
                        ])

                st.markdown("**逐日明細**")
                st.dataframe(df_c, hide_index=True, use_container_width=True)
        else:
            st.info("尚無籌碼資料 — 點上方『即時抓籌碼』可線上抓取")

        st.markdown("#### 大戶 vs 散戶 (集保戶股權分散)")
        if snap.distribution_trend and snap.distribution_trend.weeks:
            dist = snap.distribution
            tcols = st.columns(4)
            if dist:
                tcols[0].metric("大戶持股 %", f"{dist.large_holder_pct:.2f}%")
                tcols[1].metric("超大戶 %", f"{dist.whale_holder_pct:.2f}%")
                tcols[2].metric("散戶 %", f"{dist.retail_holder_pct:.2f}%")
                tcols[3].metric("大戶結構分數", f"{snap.distribution_score:.1f}")
            if snap.distribution_label:
                color_map = {
                    "accumulation": "✅", "distribution": "⚠️",
                    "neutral": "•", "no_data": "•",
                }
                st.info(f"{color_map.get(snap.distribution_label, '•')} {snap.distribution_detail}")
            df_dist = pd.DataFrame([
                {
                    "週別": w.week_date,
                    "大戶 %": w.large_holder_pct,
                    "超大戶 %": w.whale_holder_pct,
                    "散戶 %": w.retail_holder_pct,
                } for w in snap.distribution_trend.weeks
            ])
            try:
                import altair as alt  # noqa: F401
                long = df_dist.melt(id_vars=["週別"], var_name="類別", value_name="比例")
                chart = alt.Chart(long).mark_line(point=True).encode(
                    x=alt.X("週別:T", title="日期"),
                    y=alt.Y("比例:Q", title="持股比例 %"),
                    color=alt.Color("類別:N"),
                ).properties(height=260)
                st.altair_chart(chart, use_container_width=True)
            except Exception:
                st.dataframe(df_dist, hide_index=True, use_container_width=True)
        else:
            st.info("尚無 TDCC 集保戶資料 — 點上方『更新 TDCC』可抓取最新一期")

        st.markdown("#### ETF 共識與訊號")
        if snap.consensus:
            st.json(snap.consensus)
        if snap.new_build_signal:
            st.success(f"共識新建倉訊號: {snap.new_build_signal}")
        if snap.add_signal:
            st.success(f"共識加碼訊號: {snap.add_signal}")

    with tabs[1]:
        _render_fundamentals_tab(snap)

    with tabs[2]:
        _render_technicals_tab(snap)

    with tabs[4]:
        _render_dividends_tab(snap)

    with tabs[5]:
        _render_quarterly_tab(snap)

    with tabs[8]:
        st.markdown("#### 四時間框架的具體進場/停損/停利")
        rows = []
        for tf in TIMEFRAMES:
            tfs = card.timeframes[tf]
            s = tfs.strategy
            rows.append({
                "時間框架": tfs.label,
                "建議": tfs.action_label,
                "分數": f"{tfs.total:.1f}",
                "進場": s.get("entry", "—"),
                "停損": s.get("stop", "—"),
                "停利": s.get("target", "—"),
                "盈虧比": s.get("rrr", "—"),
                "部位": s.get("size_hint", "—"),
                "持有": s.get("horizon", "—"),
                "可進場": "是" if s.get("applicable") else "否",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        st.markdown("#### 進場邏輯說明")
        for tf in TIMEFRAMES:
            tfs = card.timeframes[tf]
            applicable = tfs.strategy.get("applicable")
            tag = "✓" if applicable else "—"
            st.markdown(
                f"- **{tag} {tfs.label}** ({tfs.action_label}, {tfs.total:.1f} 分): "
                f"{tfs.strategy.get('entry_logic', tfs.strategy.get('note','-'))}"
            )

    with tabs[9]:
        st.markdown("#### 持倉狀況")
        if snap.position_qty > 0:
            c1, c2, c3 = st.columns(3)
            c1.metric("持倉張數", f"{snap.position_qty:.0f}")
            c2.metric("平均成本", f"{snap.position_avg_cost:.2f}")
            if snap.unrealized_pl is not None:
                c3.metric("未實現損益", f"{snap.unrealized_pl:+,.0f}")
        else:
            st.info("目前無持倉 (依 data/trades_*.csv FIFO 估算)")

        st.markdown("#### 此股的交易紀錄")
        if snap.trades:
            df = pd.DataFrame([dataclasses_asdict(t) for t in snap.trades])
            st.dataframe(df, hide_index=True, use_container_width=True)
        else:
            st.info("無交易紀錄")

    with tabs[10]:
        st.markdown("#### 歷次 Pipeline 對此股的分析")
        if snap.history:
            df = pd.DataFrame([{
                "run_id": h.run_id,
                "時間": h.started_at,
                "LLM 情緒": h.llm_sentiment or "—",
                "情緒分": f"{h.llm_score:+.2f}" if h.llm_score is not None else "—",
                "言行反查": h.logic_verdict or "—",
                "外資累計": f"{h.foreign_net:+.0f}" if h.foreign_net is not None else "—",
                "備註": h.note,
            } for h in snap.history])
            st.dataframe(df, hide_index=True, use_container_width=True)
        else:
            st.info("尚無歷史紀錄。後續每次跑 pipeline 都會累積。")


def dataclasses_asdict(obj):
    """避免引入太多 import，包一層。"""
    import dataclasses as dc
    return dc.asdict(obj)


# ======================================================================
# 個股 360 度分析 — 基本面 / 技術面 / 配股配息 / 季報 tabs
# ======================================================================


def _render_fundamentals_tab(snap) -> None:
    """基本面：估值量表 + 月營收走勢 + 三率走勢。"""
    f = snap.fundamentals
    if not f or not f.has_data:
        st.info(
            "尚無基本面資料。請在上方勾選『更新基本面』讓系統呼叫 TWSE OpenAPI，"
            "或將季度 EPS/三率 JSON 放到 `data/fundamentals_manual/<ticker>.json`。"
        )
        return

    cols = st.columns(5)
    val = f.valuation
    cols[0].metric("本益比 PER", f"{val.pe_ratio:.2f}" if val and val.pe_ratio else "—")
    cols[1].metric("股價淨值比 PBR", f"{val.pb_ratio:.2f}" if val and val.pb_ratio else "—")
    cols[2].metric(
        "現金殖利率", f"{val.dividend_yield:.2f}%" if val and val.dividend_yield else "—",
    )
    streak = f.revenue_yoy_streak()
    cols[3].metric("月營收 YoY 連續", f"{streak} 個月" if streak else "—")
    last_rev = f.latest_revenue()
    cols[4].metric(
        "最近月營收 YoY",
        f"{last_rev.yoy:+.1f}%" if last_rev and last_rev.yoy else "—",
    )

    st.markdown("#### 每月營收走勢 (YoY / MoM)")
    if f.revenues:
        df_rev = pd.DataFrame([
            {
                "年月": f"{r.year}/{r.month:02d}",
                "營收 (千元)": r.revenue,
                "YoY %": r.yoy,
                "MoM %": r.mom,
                "累計 YoY %": r.cum_yoy,
            } for r in sorted(f.revenues, key=lambda x: (x.year, x.month))
        ])
        try:
            import altair as alt  # noqa: F401
            base = alt.Chart(df_rev).encode(x=alt.X("年月:N", title="年月"))
            bar = base.mark_bar(opacity=0.4, color="#4c78a8").encode(
                y=alt.Y("營收 (千元):Q", title="營收"),
                tooltip=["年月", "營收 (千元)", "YoY %", "MoM %"],
            )
            yoy_line = base.mark_line(color="#e45756", point=True).encode(
                y=alt.Y("YoY %:Q", title="YoY %"),
            )
            chart = alt.layer(bar, yoy_line).resolve_scale(y="independent").properties(height=320)
            st.altair_chart(chart, use_container_width=True)
        except Exception:
            pass
        st.dataframe(df_rev, hide_index=True, use_container_width=True)
    else:
        st.info("無月營收歷史 — 重新更新基本面或手動補資料。")

    st.markdown("#### 三率走勢 (毛利率 / 營業利益率 / 淨利率)")
    if f.quarterlies:
        df_q = pd.DataFrame([
            {
                "年季": f"{q.year}Q{q.quarter}",
                "毛利率 %": q.gross_margin,
                "營業利益率 %": q.operating_margin,
                "淨利率 %": q.net_margin,
                "EPS": q.eps,
            } for q in sorted(f.quarterlies, key=lambda x: (x.year, x.quarter))
        ])
        try:
            import altair as alt  # noqa: F401
            melted = df_q.melt(
                id_vars=["年季"],
                value_vars=["毛利率 %", "營業利益率 %", "淨利率 %"],
                var_name="指標", value_name="%",
            )
            chart = alt.Chart(melted).mark_line(point=True).encode(
                x=alt.X("年季:N", title="年季"),
                y=alt.Y("%:Q", title="比率 (%)"),
                color="指標:N",
            ).properties(height=300)
            st.altair_chart(chart, use_container_width=True)
        except Exception:
            pass
        st.dataframe(df_q, hide_index=True, use_container_width=True)
    else:
        st.info(
            "無季度三率資料。建議手動建立 "
            "`data/fundamentals_manual/<ticker>.json` 並填入 quarterlies "
            "（欄位：year / quarter / eps / gross_margin / operating_margin / net_margin / roe）。"
        )

    if val:
        st.caption(
            f"估值資料來源：TWSE OpenAPI BWIBBU_ALL · 殖利率所屬年度 = `{val.dividend_year or '-'}`"
        )


def _render_technicals_tab(snap) -> None:
    """技術面：K 線 + MA / MACD / RSI 圖。"""
    t = snap.technicals
    if not t or not t.has_data:
        st.info(
            "尚無日 K 資料。請在上方勾選『抓日K + 指標』，系統會打 TWSE STOCK_DAY "
            "抓近 6 個月日 K 線 (含成交量) 並計算技術指標。"
        )
        return

    cols = st.columns(6)
    cols[0].metric("收盤", f"{t.last_close:.2f}" if t.last_close else "—")
    cols[1].metric("1 日 %", f"{t.pct_change_1d:+.2f}%")
    cols[2].metric("5 日 %", f"{t.pct_change_5d:+.2f}%")
    cols[3].metric("20 日 %", f"{t.pct_change_20d:+.2f}%")
    cols[4].metric("60 日 %", f"{t.pct_change_60d:+.2f}%")
    cols[5].metric("技術面分", f"{t.technical_score:.1f}")

    sigcols = st.columns(2)
    sigcols[0].markdown("**訊號摘要**")
    bullish = [s for s in t.signals if s.get("bullish") is True]
    bearish = [s for s in t.signals if s.get("bullish") is False]
    neutral = [s for s in t.signals if s.get("bullish") is None]
    for s in bullish:
        sigcols[0].success(f"✅ {s['label']}：{s['detail']}")
    for s in bearish:
        sigcols[0].error(f"⚠️ {s['label']}：{s['detail']}")
    for s in neutral:
        sigcols[0].info(f"• {s['label']}：{s['detail']}")

    with sigcols[1]:
        st.markdown("**指標數值**")
        st.dataframe(
            pd.DataFrame([
                {"指標": "RSI(14)", "值": t.rsi14},
                {"指標": "K", "值": t.k},
                {"指標": "D", "值": t.d},
                {"指標": "MACD", "值": t.macd},
                {"指標": "Signal", "值": t.macd_signal},
                {"指標": "MACD Hist", "值": t.macd_hist},
                {"指標": "MA5", "值": t.ma5},
                {"指標": "MA20", "值": t.ma20},
                {"指標": "MA60", "值": t.ma60},
                {"指標": "MA120", "值": t.ma120},
            ]),
            hide_index=True, use_container_width=True,
        )

    # K 線圖
    try:
        from bot.technicals import build_technical_snapshot
        _, df = build_technical_snapshot(
            snap.ticker, root=PROJECT_ROOT, refresh=False,
        )
    except Exception:
        df = None

    if df is not None and not df.empty:
        try:
            import altair as alt  # noqa: F401
            df_plot = df.tail(120).copy()
            df_plot["date"] = pd.to_datetime(df_plot["date"])
            df_plot["color"] = (df_plot["close"] >= df_plot["open"]).map({
                True: "red", False: "green",
            })
            base = alt.Chart(df_plot).encode(x=alt.X("date:T", title="日期"))
            rule = base.mark_rule().encode(
                y="low:Q", y2="high:Q", color=alt.Color("color:N", scale=None),
            )
            bar = base.mark_bar(size=4).encode(
                y="open:Q", y2="close:Q", color=alt.Color("color:N", scale=None),
            )
            ma_layers = []
            for ma_col, color in [
                ("ma5", "#ffa500"), ("ma20", "#1f77b4"),
                ("ma60", "#9467bd"), ("ma120", "#8c564b"),
            ]:
                if ma_col in df_plot.columns:
                    ma_layers.append(
                        base.mark_line(color=color).encode(y=f"{ma_col}:Q"),
                    )
            price_chart = alt.layer(rule, bar, *ma_layers).properties(
                height=320, title="日 K + 均線 (近 120 日)",
            )

            vol_chart = alt.Chart(df_plot).mark_bar(opacity=0.6).encode(
                x=alt.X("date:T", title=""),
                y=alt.Y("volume:Q", title="成交張數"),
                color=alt.Color("color:N", scale=None),
            ).properties(height=120, title="成交量")

            macd_chart = alt.Chart(df_plot).transform_fold(
                ["macd", "macd_signal"], as_=["指標", "值"],
            ).mark_line().encode(
                x="date:T", y="值:Q", color="指標:N",
            ).properties(height=140, title="MACD")
            macd_hist = alt.Chart(df_plot).mark_bar(opacity=0.5).encode(
                x="date:T", y="macd_hist:Q",
                color=alt.condition(
                    "datum.macd_hist >= 0",
                    alt.value("#d62728"), alt.value("#2ca02c"),
                ),
            ).properties(height=140)
            macd_layer = alt.layer(macd_hist, macd_chart)

            rsi_chart = alt.Chart(df_plot).mark_line(color="#e377c2").encode(
                x="date:T", y=alt.Y("rsi14:Q", title="RSI14"),
            ).properties(height=140, title="RSI(14)")

            st.altair_chart(price_chart, use_container_width=True)
            st.altair_chart(vol_chart, use_container_width=True)
            st.altair_chart(macd_layer, use_container_width=True)
            st.altair_chart(rsi_chart, use_container_width=True)
        except Exception as e:
            st.warning(f"K 線繪圖失敗: {e}")
            st.dataframe(df.tail(60), hide_index=True, use_container_width=True)
    else:
        st.info("無 K 線 DataFrame；請先勾選『抓日K + 指標』後再分析一次。")


def _render_dividends_tab(snap) -> None:
    """股利政策：歷年配息穩定度 + 盈餘分配率。"""
    f = snap.fundamentals
    if not f or not f.dividends:
        st.info(
            "尚無股利紀錄。請勾選『更新基本面』讓系統呼叫 TWSE OpenAPI；"
            "舊年度需要逐年累積。"
        )
        return
    df_d = pd.DataFrame([
        {
            "年度": d.year,
            "現金股利": d.cash_dividend,
            "股票股利": d.stock_dividend,
            "合計": round(d.cash_dividend + d.stock_dividend, 2),
            "除息日": d.ex_dividend_date,
            "除權日": d.ex_right_date,
            "填息日": d.fill_date,
            "填息天數": d.fill_days,
            "盈餘分配率 %": d.payout_ratio,
        } for d in sorted(f.dividends, key=lambda x: x.year)
    ])

    cols = st.columns(3)
    cols[0].metric(
        "平均盈餘分配率 (近 5 年)",
        f"{f.avg_payout_ratio() or 0:.1f}%" if f.avg_payout_ratio() else "—",
    )
    last_cash = df_d["現金股利"].iloc[-1] if not df_d.empty else 0
    cols[1].metric("最新一年現金股利", f"{last_cash:.2f}")
    if not df_d.empty:
        cols[2].metric(
            "近 5 年配息穩定度 (std/mean)",
            _stability(df_d["現金股利"].tail(5).tolist()),
        )

    try:
        import altair as alt  # noqa: F401
        long = df_d.melt(
            id_vars=["年度"],
            value_vars=["現金股利", "股票股利"],
            var_name="類別", value_name="元",
        )
        chart = alt.Chart(long).mark_bar().encode(
            x=alt.X("年度:O", title="年度"),
            y=alt.Y("元:Q", title="每股股利"),
            color="類別:N",
        ).properties(height=280, title="歷年股利")
        st.altair_chart(chart, use_container_width=True)
    except Exception:
        pass
    st.dataframe(df_d, hide_index=True, use_container_width=True)

    st.caption(
        "盈餘分配率 = 現金 + 股票股利 / EPS；需要 quarterlies 完整才會計算。"
    )


def _stability(values: List[float]) -> str:
    arr = [v for v in values if v is not None]
    if len(arr) < 2:
        return "—"
    mean = sum(arr) / len(arr)
    if mean == 0:
        return "—"
    var = sum((v - mean) ** 2 for v in arr) / len(arr)
    std = var ** 0.5
    cv = std / mean
    if cv < 0.1:
        tag = "✅ 穩定"
    elif cv < 0.3:
        tag = "🟡 中等"
    else:
        tag = "🔴 波動大"
    return f"{cv:.2%} {tag}"


def _render_quarterly_tab(snap) -> None:
    """季報 Q1～Q4 框架 + 滾動 EPS。"""
    qv = snap.quarterly_view or {}
    focus = qv.get("current_focus") or {}
    rolling = qv.get("rolling_eps") or []
    qrev = qv.get("quarterly_revenue") or []

    if focus:
        st.markdown(f"### 目前市場焦點：{focus.get('year','-')} {focus.get('deadline_label','')}")
        st.info(focus.get("summary", ""))
        cA, cB = st.columns(2)
        with cA:
            st.markdown("**關鍵問題清單**")
            for q in focus.get("key_questions", []):
                st.markdown(f"- {q}")
        with cB:
            st.markdown("**重點觀察**")
            for w in focus.get("what_to_watch", []):
                st.markdown(f"- {w}")
        st.caption(
            f"公佈期限：{focus.get('deadline_string','-')}；"
            f"焦點季度 = Q{focus.get('quarter','-')}"
        )

    st.markdown("#### 滾動式累計 EPS (Q1/H1/9M/FY 進度)")
    if rolling:
        df_eps = pd.DataFrame(rolling)
        df_eps_disp = df_eps.rename(columns={
            "label": "節點", "year": "年度", "eps_sum": "累計 EPS",
            "prev_year_eps_sum": "去年同期", "diff_pct": "與去年同期 %", "note": "備註",
        })
        st.dataframe(
            df_eps_disp[["年度", "節點", "累計 EPS", "去年同期", "與去年同期 %", "備註"]],
            hide_index=True, use_container_width=True,
        )
        try:
            import altair as alt  # noqa: F401
            df_eps["key"] = df_eps["year"].astype(str) + " " + df_eps["label"]
            long = df_eps.melt(
                id_vars=["key"], value_vars=["eps_sum", "prev_year_eps_sum"],
                var_name="序列", value_name="EPS",
            )
            chart = alt.Chart(long).mark_bar().encode(
                x=alt.X("key:N", title="年度 / 節點", sort=None),
                y=alt.Y("EPS:Q"),
                color="序列:N",
                xOffset="序列:N",
            ).properties(height=260, title="滾動 EPS vs 去年同期")
            st.altair_chart(chart, use_container_width=True)
        except Exception:
            pass
    else:
        st.info(
            "尚無季度 EPS 資料。請手動建立 `data/fundamentals_manual/<ticker>.json`：\n\n"
            "```json\n"
            "{\n  \"ticker\": \"2330\",\n  \"quarterlies\": [\n"
            "    {\"year\": 2025, \"quarter\": 3, \"eps\": 14.71, "
            "\"gross_margin\": 59.1, \"operating_margin\": 48.5, "
            "\"net_margin\": 41.0, \"roe\": 8.2}\n"
            "  ]\n}\n```"
        )

    st.markdown("#### 季度營收聚合 (Q1=1-3 / Q2=4-6 / Q3=7-9 / Q4=10-12)")
    if qrev:
        df_qr = pd.DataFrame([
            {
                "年季": f"{r['year']}Q{r['quarter']}",
                "月份": ",".join(map(str, r.get("months", []))),
                "季營收": r.get("revenue", 0),
                "去年同期": r.get("revenue_last_year", 0),
                "YoY %": r.get("yoy", 0),
            } for r in qrev
        ])
        st.dataframe(df_qr, hide_index=True, use_container_width=True)
        try:
            import altair as alt  # noqa: F401
            chart = alt.Chart(df_qr).mark_bar(color="#4c78a8").encode(
                x=alt.X("年季:N", sort=None),
                y=alt.Y("季營收:Q"),
                tooltip=["年季", "季營收", "YoY %"],
            ).properties(height=240, title="季度營收")
            st.altair_chart(chart, use_container_width=True)
        except Exception:
            pass
    else:
        st.info("無月營收歷史，無法聚合季度營收。")


# ======================================================================
# 頁面: K 線看板 (Stock Board) -- 多檔股票一覽 + mini K 線
# ======================================================================


def _board_rows_summary(symbols: List[str], db: StockDB) -> pd.DataFrame:
    """組多檔股票的「最新報價 / 漲跌 / 5日 20日 % / 量比」快報。"""
    rows: List[Dict[str, object]] = []
    for sym in symbols:
        bars = db.get_price_history(sym, limit=60, ascending=True)
        if not bars:
            rows.append({
                "symbol": sym,
                "last_date": "—",
                "close": None,
                "chg_1d_%": None,
                "chg_5d_%": None,
                "chg_20d_%": None,
                "volume": None,
                "vol_ratio": None,
                "high_60d": None,
                "low_60d": None,
                "rows": 0,
            })
            continue
        last = bars[-1]
        prev = bars[-2] if len(bars) >= 2 else last
        d5 = bars[-6] if len(bars) >= 6 else bars[0]
        d20 = bars[-21] if len(bars) >= 21 else bars[0]

        def _pct(a: float, b: float) -> Optional[float]:
            if not b:
                return None
            return round(100.0 * (a - b) / b, 2)

        vol_recent = [b.volume for b in bars[-20:] if b.volume > 0]
        vol_ma = sum(vol_recent) / len(vol_recent) if vol_recent else 0.0
        rows.append({
            "symbol": sym,
            "last_date": last.date,
            "close": round(last.close, 2),
            "chg_1d_%": _pct(last.close, prev.close),
            "chg_5d_%": _pct(last.close, d5.close),
            "chg_20d_%": _pct(last.close, d20.close),
            "volume": round(last.volume, 0),
            "vol_ratio": round(last.volume / vol_ma, 2) if vol_ma else None,
            "high_60d": round(max(b.high for b in bars), 2),
            "low_60d": round(min(b.low for b in bars if b.low > 0), 2)
                       if any(b.low > 0 for b in bars) else None,
            "rows": len(bars),
        })
    return pd.DataFrame(rows)


def _spark_chart(bars, *, height: int = 90):
    """單一個股的迷你 K 線縮圖 (用蠟燭 + 收盤線)。"""
    import altair as alt

    df = pd.DataFrame([{
        "date": b.date, "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "volume": b.volume,
    } for b in bars])
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    df["color"] = (df["close"] >= df["open"]).map({True: "#d64545", False: "#1d9c5b"})

    base = alt.Chart(df).encode(
        x=alt.X("date:T", axis=None),
    )
    rule = base.mark_rule().encode(
        y=alt.Y("low:Q", axis=None, scale=alt.Scale(zero=False)),
        y2="high:Q",
        color=alt.Color("color:N", scale=None, legend=None),
    )
    bar = base.mark_bar(size=3).encode(
        y="open:Q", y2="close:Q",
        color=alt.Color("color:N", scale=None, legend=None),
    )
    return alt.layer(rule, bar).properties(height=height)


def _full_kline_chart(bars, *, ma_periods=(5, 20, 60), title: str = ""):
    """完整 K + MA + 成交量子圖。"""
    import altair as alt

    df = pd.DataFrame([{
        "date": b.date, "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "volume": b.volume,
    } for b in bars])
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    for p in ma_periods:
        df[f"ma{p}"] = df["close"].rolling(p, min_periods=1).mean()
    df["color"] = (df["close"] >= df["open"]).map({True: "#d64545", False: "#1d9c5b"})

    base = alt.Chart(df).encode(x=alt.X("date:T", title=""))
    rule = base.mark_rule().encode(
        y=alt.Y("low:Q", scale=alt.Scale(zero=False), title="價"),
        y2="high:Q",
        color=alt.Color("color:N", scale=None, legend=None),
    )
    bar = base.mark_bar(size=4).encode(
        y="open:Q", y2="close:Q",
        color=alt.Color("color:N", scale=None, legend=None),
    )
    ma_colors = {"ma5": "#ffa500", "ma20": "#1f77b4", "ma60": "#9467bd"}
    ma_layers = []
    for p in ma_periods:
        col = f"ma{p}"
        if col in df.columns:
            ma_layers.append(
                base.mark_line(color=ma_colors.get(col, "#888")).encode(
                    y=f"{col}:Q",
                ),
            )
    price = alt.layer(rule, bar, *ma_layers).properties(
        height=260, title=title or "",
    )
    vol = alt.Chart(df).mark_bar(opacity=0.5).encode(
        x=alt.X("date:T", title="日期"),
        y=alt.Y("volume:Q", title="成交張數"),
        color=alt.Color("color:N", scale=None, legend=None),
    ).properties(height=90)
    return price, vol


def page_board() -> None:
    """K 線看板：多檔股票一覽，每檔一張 mini K 線縮圖，可展開看完整 K 線。"""
    st.title("📊 K 線看板 (Stock Board)")
    st.caption(
        "一目了然多檔股票的最新走勢。資料來源為本地 SQLite `price_history` "
        "(由「個股深入分析 → 抓日 K + 指標」或下方『一鍵更新所有』寫入)，"
        "可透過「資料庫 / 雲端同步」推送到 Google Sheets。"
    )

    db = _open_db_for_page()

    # ---- 來源選擇 ----
    src_col1, src_col2, src_col3, src_col4 = st.columns([2, 1, 1, 1])
    source = src_col1.radio(
        "監控池來源",
        ["Watchlist", "DB 已有資料的全部 symbol", "自訂"],
        horizontal=True, key="board_source",
    )

    if source == "Watchlist":
        wlist = wl.load(PROJECT_ROOT)
        symbols = [i.ticker for i in wlist.items]
    elif source == "DB 已有資料的全部 symbol":
        symbols = db.list_price_symbols()
    else:
        custom = src_col1.text_input(
            "自訂代號 (逗號分隔)", value="2330,2454,2317,0050",
            key="board_custom",
        )
        symbols = [s.strip() for s in custom.split(",") if s.strip()]

    period = src_col2.selectbox(
        "時間範圍",
        ["近 30 日", "近 60 日", "近 120 日", "近 250 日"],
        index=1, key="board_period",
    )
    period_map = {"近 30 日": 30, "近 60 日": 60, "近 120 日": 120, "近 250 日": 250}
    n_days = period_map[period]

    cols_per_row = src_col3.selectbox(
        "每列張數", [2, 3, 4], index=1, key="board_cols",
    )

    refresh_clicked = src_col4.button(
        "🔄 一鍵更新所有 K 線",
        type="primary",
        use_container_width=True,
        help="對清單中每檔呼叫 TWSE STOCK_DAY 取近 6 個月 K 線，寫回 SQLite。",
    )

    if not symbols:
        st.info("尚未選定股票。可到「個股總覽」加入 watchlist，或選『自訂』直接輸入代號。")
        return

    # ---- 重新抓 K 線 ----
    if refresh_clicked:
        from bot.technicals import fetch_recent_kline
        progress = st.progress(0, text="抓取 K 線中...")
        ok, fail = 0, 0
        for i, sym in enumerate(symbols, start=1):
            try:
                fetch_recent_kline(
                    sym, months=6, root=PROJECT_ROOT, save_to_db=True,
                )
                ok += 1
            except Exception as e:
                fail += 1
                st.warning(f"{sym} 抓取失敗：{e}")
            progress.progress(i / len(symbols), text=f"({i}/{len(symbols)}) {sym}")
        progress.empty()
        st.success(f"完成：成功 {ok} 檔 / 失敗 {fail} 檔，已寫入 price_history")
        st.rerun()

    # ---- 摘要表 (排序 / 過濾) ----
    summary = _board_rows_summary(symbols, db)
    if summary.empty:
        st.warning("symbols 為空。")
        return

    sort_col1, sort_col2, sort_col3 = st.columns([2, 2, 2])
    sort_by = sort_col1.selectbox(
        "排序依據",
        ["chg_1d_%", "chg_5d_%", "chg_20d_%", "vol_ratio", "close", "symbol"],
        index=0, key="board_sort",
    )
    ascending = sort_col2.toggle("升冪", value=False, key="board_asc")
    only_up = sort_col3.toggle("只顯示上漲", value=False, key="board_up")

    df_view = summary.copy()
    if only_up and "chg_1d_%" in df_view.columns:
        df_view = df_view[df_view["chg_1d_%"].fillna(0) > 0]
    df_view = df_view.sort_values(
        sort_by, ascending=ascending, na_position="last",
    ).reset_index(drop=True)

    # ---- 摘要表頭 ----
    st.markdown("### 摘要表")
    st.dataframe(
        df_view,
        hide_index=True,
        use_container_width=True,
        column_config={
            "chg_1d_%": st.column_config.NumberColumn("1 日 %", format="%+.2f"),
            "chg_5d_%": st.column_config.NumberColumn("5 日 %", format="%+.2f"),
            "chg_20d_%": st.column_config.NumberColumn("20 日 %", format="%+.2f"),
            "vol_ratio": st.column_config.NumberColumn("量比", format="%.2f"),
            "close": st.column_config.NumberColumn("收盤", format="%.2f"),
            "volume": st.column_config.NumberColumn("成交張", format="%.0f"),
            "high_60d": st.column_config.NumberColumn("近 60 高", format="%.2f"),
            "low_60d": st.column_config.NumberColumn("近 60 低", format="%.2f"),
        },
    )

    csv_buf = df_view.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "下載摘要 CSV",
        data=csv_buf,
        file_name=f"board_{dt.date.today().isoformat()}.csv",
        mime="text/csv",
    )

    # ---- mini K 線 grid ----
    st.markdown(f"### K 線縮圖 (近 {n_days} 日)")
    sorted_syms = df_view["symbol"].tolist()
    cols = st.columns(cols_per_row)
    for i, sym in enumerate(sorted_syms):
        bars = db.get_price_history(sym, limit=n_days, ascending=True)
        with cols[i % cols_per_row]:
            row = df_view.loc[df_view["symbol"] == sym].iloc[0]
            chg = row.get("chg_1d_%")
            chg_str = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "—"
            color = "green" if (isinstance(chg, (int, float)) and chg > 0) else (
                "red" if isinstance(chg, (int, float)) and chg < 0 else "gray"
            )
            st.markdown(
                f"**`{sym}`** · 收 {row['close'] if row['close'] is not None else '—'} "
                f"· {_badge(chg_str, color)}",
                unsafe_allow_html=True,
            )
            if bars:
                chart = _spark_chart(bars, height=110)
                if chart is not None:
                    st.altair_chart(chart, use_container_width=True)
            else:
                st.caption("無 K 線資料 - 點上方『一鍵更新』")

    # ---- 展開單檔完整 K 線 ----
    st.markdown("---")
    st.markdown("### 展開查看完整 K 線 (含 MA + 成交量)")
    pick = st.selectbox(
        "選一檔深入看", options=sorted_syms, index=0 if sorted_syms else None,
        key="board_detail_pick",
    )
    if pick:
        bars = db.get_price_history(pick, limit=n_days, ascending=True)
        if not bars:
            st.info(f"{pick} 尚無 K 線資料。請點上方『一鍵更新所有』。")
        else:
            result = _full_kline_chart(bars, title=f"{pick} 近 {n_days} 日 K 線")
            if result is not None:
                price_chart, vol_chart = result
                st.altair_chart(price_chart, use_container_width=True)
                st.altair_chart(vol_chart, use_container_width=True)
            with st.expander(f"原始資料 ({len(bars)} 根 K 棒)"):
                st.dataframe(
                    pd.DataFrame([{
                        "date": b.date, "open": b.open, "high": b.high,
                        "low": b.low, "close": b.close, "volume": b.volume,
                    } for b in bars]).sort_values("date", ascending=False),
                    hide_index=True, use_container_width=True,
                )

    # ---- Google Sheets 同步快捷 ----
    st.markdown("---")
    st.markdown("### 把歷史 K 線同步到 Google Sheets")
    st.caption(
        "把 `price_history` 整張表推到雲端 worksheet (覆蓋整張)，"
        "另一台電腦或手機可 Pull 回本地。完整設定請見「資料庫 / 雲端同步」頁。"
    )
    cfg = load_config_from_env()
    sc1, sc2, sc3 = st.columns(3)
    sc1.markdown(
        f"雲端狀態: {_badge('啟用', 'green') if cfg.enabled else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )
    if cfg.enabled:
        if sc2.button("⬆ Push price_history 到 Sheets",
                      use_container_width=True, key="board_push"):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                with st.spinner("Pushing price_history..."):
                    r = sync.push("price_history")
                st.markdown(_format_sync_result(r))
            except Exception as e:
                st.error(f"Push 失敗: {e}")
        if sc3.button("⬇ Pull price_history (覆蓋本地)",
                      use_container_width=True, key="board_pull"):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                with st.spinner("Pulling price_history..."):
                    r = sync.pull("price_history")
                st.markdown(_format_sync_result(r))
                st.rerun()
            except Exception as e:
                st.error(f"Pull 失敗: {e}")
    else:
        st.info("尚未設定 Google Sheets 雲端同步。請到「資料庫 / 雲端同步」頁完成設定。")


# ======================================================================
# 頁面: 資料庫 / 雲端同步
# ======================================================================


def _resolved_db_path() -> Path:
    env = load_env()
    p = env.get("STOCK_DB_PATH", "").strip()
    if p:
        return Path(p).expanduser()
    return default_db_path(PROJECT_ROOT)


def _open_db_for_page() -> StockDB:
    """開啟由 .env 指定的 DB；session 內快取一個 handle。"""
    target = _resolved_db_path()
    cached = st.session_state.get("_db_handle")
    if cached is None or cached.path != target:
        reset_db_singleton()
        cached = StockDB.open(path=target)
        st.session_state["_db_handle"] = cached
    return cached


def _format_sync_result(r: "TableSyncResult") -> str:
    if r.error:
        return f"❌ `{r.table}`: {r.error}"
    if r.skipped:
        return f"⚠️ `{r.table}`: 跳過"
    return f"✅ `{r.table}`: {r.direction} {r.rows} 筆 {('· ' + r.note) if r.note else ''}"


def page_database() -> None:
    st.title("資料庫 / 雲端同步")
    st.caption(
        "本地 SQLite 集中管理冷/溫資料；可選擇透過 Google Sheets 在多台電腦間共用。"
    )

    db = _open_db_for_page()

    # ---- DB 狀態 ----
    st.markdown("### 本地資料庫狀態")
    c1, c2, c3 = st.columns([2, 1, 1])
    c1.code(str(db.path), language="text")
    try:
        size = db.path.stat().st_size if db.path.exists() else 0
        c2.metric("檔案大小", f"{size/1024:,.1f} KB")
    except Exception:
        c2.metric("檔案大小", "—")
    c3.metric("Tables", len(ALL_TABLES))

    path_lower = str(db.path).lower().replace("\\", "/")
    drive_hint = "/my drive/" in path_lower or "googledrive" in path_lower
    if drive_hint:
        st.info(
            "📁 DB 看起來放在 Google Drive 同步資料夾。**單機共用沒問題，但兩台電腦不要同時開啟，**"
            "避免 SQLite WAL 衝突。多機並行請改用下方「Google Sheets 雲端同步」。"
        )

    counts = db.table_counts()
    summary_rows = []
    for t in ALL_TABLES:
        meta = db.get_sync_meta(t) if t in SYNCABLE_TABLES else None
        summary_rows.append({
            "table": t,
            "rows": counts.get(t, 0),
            "last_push": (meta.last_push_at if meta else "—") or "—",
            "last_pull": (meta.last_pull_at if meta else "—") or "—",
            "last_error": (meta.last_error if meta else "") or "",
        })
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    st.markdown("---")

    # ---- 雲端同步 ----
    st.markdown("### Google Sheets 雲端同步")
    cfg = load_config_from_env()
    cc1, cc2, cc3 = st.columns([2, 2, 1])
    cc1.markdown(
        f"Sheet ID: {_badge('已設定', 'green') if cfg.sheet_id else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )
    cc2.markdown(
        f"Service Account: {_badge('已設定', 'green') if cfg.service_account_json else _badge('未設定', 'gray')}",
        unsafe_allow_html=True,
    )
    cc3.markdown(
        f"狀態: {_badge('啟用', 'green') if cfg.enabled else _badge('停用', 'red')}",
        unsafe_allow_html=True,
    )

    with st.expander("🛠 雲端同步設定步驟 (第一次使用必看)", expanded=not cfg.enabled):
        st.markdown(
            """
            1. 到 [Google Cloud Console](https://console.cloud.google.com) 建立 / 選一個 Project
            2. 啟用 **Google Sheets API** 與 **Google Drive API**
            3. 建立 **Service Account**，下載金鑰 JSON 檔
            4. 開一張 Google Sheet (空白即可)，把 SA 的 email 加為「**編輯者**」
            5. 在「組態設定」頁填入：
               - `GOOGLE_SHEET_ID` = Sheet URL 中 `/d/` 後的字串
               - `GOOGLE_SA_JSON_PATH` = 剛下載的 JSON 檔絕對路徑
            6. 安裝雲端套件：`uv sync --extra cloud`
            7. (選用) 在 Sheet 上掛 **Google Form**：手機就能新增股票 → Forms 寫進 Sheet → 用「Pull」拉回本地

            完整文件: `docs/cloud_sync_setup.md`
            """
        )

    if not cfg.enabled:
        st.warning("雲端同步未啟用 — 上方欄位填齊後才能 push/pull。")
    else:
        cs1, cs2, cs3, cs4 = st.columns(4)
        if cs1.button("🔌 測試連線", use_container_width=True):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                title = sync.ping()
                st.success(f"連線成功 — Sheet 標題：「{title}」")
            except CloudSyncDependencyError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"連線失敗: {e}")

        if cs2.button("⬇ Pull 全部", help="把雲端覆蓋到本地", use_container_width=True):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                with st.spinner("Pulling from Google Sheets..."):
                    results = sync.pull_all()
                for r in results:
                    st.markdown(_format_sync_result(r))
            except Exception as e:
                st.error(f"Pull 失敗: {e}")

        if cs3.button("⬆ Push 全部", help="把本地覆蓋到雲端",
                      type="primary", use_container_width=True):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                with st.spinner("Pushing to Google Sheets..."):
                    results = sync.push_all()
                for r in results:
                    st.markdown(_format_sync_result(r))
            except Exception as e:
                st.error(f"Push 失敗: {e}")

        if cs4.button("🔄 智能同步", help="依 updated_at 自動判斷方向",
                      use_container_width=True):
            try:
                sync = GoogleSheetSync(cfg, db=db)
                with st.spinner("Syncing..."):
                    results = sync.sync_all()
                for r in results:
                    st.markdown(_format_sync_result(r))
            except Exception as e:
                st.error(f"Sync 失敗: {e}")

        st.markdown("**單表同步** (細部控制)")
        tcol1, tcol2, tcol3, tcol4 = st.columns([2, 1, 1, 1])
        picked = tcol1.selectbox("選擇 table", list(SYNCABLE_TABLES))
        if tcol2.button("⬇ Pull", key="single_pull", use_container_width=True):
            sync = GoogleSheetSync(cfg, db=db)
            r = sync.pull(picked)
            st.markdown(_format_sync_result(r))
        if tcol3.button("⬆ Push", key="single_push", use_container_width=True):
            sync = GoogleSheetSync(cfg, db=db)
            r = sync.push(picked)
            st.markdown(_format_sync_result(r))
        if tcol4.button("🔄 Sync", key="single_sync", use_container_width=True):
            sync = GoogleSheetSync(cfg, db=db)
            r = sync.sync(picked)
            st.markdown(_format_sync_result(r))

    st.markdown("---")

    # ---- 瀏覽 / 編輯資料 ----
    st.markdown("### 瀏覽 / 編輯資料")

    tabs = st.tabs([
        "stock_info", "watchlist", "etf_meta",
        "monthly_revenue", "quarterly_report", "price_history", "匯入 CSV",
    ])

    # --- stock_info ---
    with tabs[0]:
        st.caption("冷資料 — 公司基本面 (公司名 / 產業 / 上市日 / 股本)")
        infos = db.list_stock_info()
        if infos:
            df = pd.DataFrame([dataclasses_asdict(i) for i in infos])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("尚無資料。可至「個股深入分析」抓取，或於「匯入 CSV」上傳。")

        with st.expander("➕ 手動新增 / 編輯一筆"):
            wc = st.columns(3)
            sym = wc[0].text_input("symbol", key="si_sym")
            name = wc[1].text_input("name", key="si_name")
            ind = wc[2].text_input("industry", key="si_ind")
            wc2 = st.columns(3)
            mkt = wc2[0].selectbox("market", ["TWSE", "TPEX", "EMG"], key="si_mkt")
            ld = wc2[1].text_input("listed_date (YYYY-MM-DD)", key="si_ld")
            cap = wc2[2].text_input("capital (億)", key="si_cap")
            note = st.text_input("note", key="si_note")
            if st.button("儲存", key="si_save"):
                if not sym.strip():
                    st.error("symbol 必填")
                else:
                    try:
                        cap_v = float(cap) if cap.strip() else 0.0
                    except ValueError:
                        cap_v = 0.0
                    db.upsert_stock_info(StockInfo(
                        symbol=sym.strip(), name=name.strip(), industry=ind.strip(),
                        market=mkt, listed_date=ld.strip(), capital=cap_v,
                        note=note.strip(),
                    ))
                    st.success(f"{sym} 已寫入 DB")
                    st.rerun()

    # --- watchlist ---
    with tabs[1]:
        st.caption("使用者監控清單 (與雲端同步可在多機共用)")
        watch_rows = db.list_watchlist()
        if watch_rows:
            df = pd.DataFrame([dataclasses_asdict(w) for w in watch_rows])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("Watchlist 是空的。")

        with st.expander("➕ 新增 / 編輯"):
            wcol = st.columns(3)
            wsym = wcol[0].text_input("symbol", key="wl_sym")
            wname = wcol[1].text_input("name", key="wl_name")
            wtags = wcol[2].text_input("tags (逗號分隔)", key="wl_tags")
            wnote = st.text_input("note", key="wl_note")
            ba1, ba2 = st.columns(2)
            if ba1.button("儲存", key="wl_save"):
                if not wsym.strip():
                    st.error("symbol 必填")
                else:
                    db.upsert_watch(WatchlistRow(
                        symbol=wsym.strip(), name=wname.strip(),
                        tags=wtags.strip(), note=wnote.strip(),
                    ))
                    st.success(f"{wsym} 已加入 watchlist")
                    st.rerun()
            del_sym = ba2.text_input("欲刪除 symbol", key="wl_del")
            if st.button("刪除", key="wl_del_btn") and del_sym.strip():
                db.remove_watch(del_sym.strip())
                st.success(f"{del_sym} 已刪除")
                st.rerun()

    # --- etf_meta ---
    with tabs[2]:
        st.caption("主動式 ETF 基本資料 (與 active_etfs.json 互通)")
        metas = db.list_etf_meta()
        if metas:
            df = pd.DataFrame([dataclasses_asdict(m) for m in metas])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("尚無資料。")

    # --- monthly_revenue ---
    with tabs[3]:
        st.caption("每月營收 (溫資料 — 每月 10 號後更新)")
        sym_q = st.text_input("查詢 symbol", key="mr_q", placeholder="留空則列出全部")
        if sym_q.strip():
            mrs = db.get_monthly_revenue(sym_q.strip())
            rows_view = [dataclasses_asdict(m) for m in mrs]
        else:
            cur = db.conn.execute(
                "SELECT * FROM monthly_revenue ORDER BY year_month DESC, symbol LIMIT 500"
            )
            rows_view = [dict(r) for r in cur.fetchall()]
        if rows_view:
            st.dataframe(pd.DataFrame(rows_view),
                         use_container_width=True, hide_index=True)
        else:
            st.info("查無資料。")

    # --- quarterly_report ---
    with tabs[4]:
        st.caption("季報 EPS / 三率 (溫資料)")
        cur = db.conn.execute(
            "SELECT * FROM quarterly_report ORDER BY period DESC, symbol LIMIT 500"
        )
        qs = [dict(r) for r in cur.fetchall()]
        if qs:
            st.dataframe(pd.DataFrame(qs), use_container_width=True, hide_index=True)
        else:
            st.info("尚無資料。")

    # --- price_history ---
    with tabs[5]:
        st.caption(
            "歷史 K 線 OHLCV (溫資料)。由「個股深入分析」抓 K 線或「K 線看板 → 一鍵更新」自動寫入。"
        )
        summary_ph = db.price_history_summary()
        if summary_ph:
            st.markdown("##### 各 symbol K 線筆數 / 最新日期")
            st.dataframe(
                pd.DataFrame(summary_ph),
                hide_index=True, use_container_width=True,
            )
            phc1, phc2 = st.columns([2, 3])
            ph_sym = phc1.text_input(
                "查詢 symbol 的歷史 K 線", value="", placeholder="2330",
                key="ph_q",
            )
            ph_n = phc2.slider("顯示最近 N 根", 30, 500, 120, step=10, key="ph_n")
            if ph_sym.strip():
                bars = db.get_price_history(
                    ph_sym.strip(), limit=int(ph_n), ascending=False,
                )
                if bars:
                    st.dataframe(
                        pd.DataFrame([dataclasses_asdict(b) for b in bars]),
                        hide_index=True, use_container_width=True,
                    )
                else:
                    st.info(f"DB 中查無 {ph_sym} 的 K 線；可到「K 線看板」一鍵更新。")
        else:
            st.info(
                "尚無 K 線資料。請到「📊 K 線看板」或「個股深入分析」抓取，"
                "或從 CSV / Google Sheets 匯入。"
            )

    # --- 匯入 CSV ---
    with tabs[6]:
        st.caption("把 CSV 內容寫進指定 table (欄位名稱要對齊 schema)")
        target = st.selectbox("目標 table", list(SYNCABLE_TABLES), key="imp_target")
        mode = st.radio(
            "寫入模式",
            ["upsert (合併)", "replace (覆蓋整張表)"],
            horizontal=True, key="imp_mode",
        )
        f = st.file_uploader("選 CSV", type=["csv"], key="imp_csv")
        if f is not None:
            try:
                df = pd.read_csv(f, encoding="utf-8-sig", dtype=str).fillna("")
            except Exception:
                f.seek(0)
                df = pd.read_csv(f, encoding="utf-8", dtype=str).fillna("")
            st.write("**預覽 (前 10 筆)**")
            st.dataframe(df.head(10), use_container_width=True)
            if st.button("執行匯入", type="primary", key="imp_run"):
                from bot.cloud_sync import _coerce_row
                cols = db._table_columns(target)
                rows_csv = df.to_dict(orient="records")
                typed = [_coerce_row(target, cols, r) for r in rows_csv]
                if mode.startswith("replace"):
                    n = db.replace_table_rows(target, typed)
                else:
                    n = db.upsert_rows(target, typed)
                st.success(f"已寫入 {n} 筆到 {target}")
                st.rerun()


# ======================================================================
# 主程式
# ======================================================================


# ======================================================================
# 頁面: 今日當沖戰情室
# ======================================================================


def page_intraday() -> None:
    st.title("⚡ 今日當沖戰情室")
    st.caption(
        "盤前 8:30 流程: 新聞 (鉅亨網) → LLM 萃取題材 → 合併候選股 → 算當沖分 → LLM 戰情簡報。"
        " 一鍵跑完整流程: `uv run stock-intraday`。"
    )

    from bot.intraday_pipeline import load_latest_intraday, run_intraday

    cs = st.columns([2, 1, 1, 1])
    if cs[1].button("載入最新", use_container_width=True, key="intra_load"):
        st.session_state["intra_report"] = load_latest_intraday(PROJECT_ROOT)
    if cs[2].button("重抓新聞 + 重跑", type="primary", use_container_width=True, key="intra_refresh"):
        env_values = load_env()
        from bot.config import Settings as S
        # 確保 GEMINI_API_KEY 從 .env 載到 process env
        import os
        for k, v in env_values.items():
            if v and not os.environ.get(k):
                os.environ[k] = v
        s = S()
        with st.spinner("LLM 跑當沖管線中 (預估 30-60 秒)..."):
            report = run_intraday(
                project_root=PROJECT_ROOT,
                settings=s,
                force_refresh_news=True,
            )
        from bot.intraday_pipeline import _report_to_json
        st.session_state["intra_report"] = _report_to_json(report)
    if cs[3].button("用快取重跑", use_container_width=True, key="intra_cached_run"):
        env_values = load_env()
        from bot.config import Settings as S
        import os
        for k, v in env_values.items():
            if v and not os.environ.get(k):
                os.environ[k] = v
        s = S()
        with st.spinner("跑當沖管線..."):
            report = run_intraday(project_root=PROJECT_ROOT, settings=s)
        from bot.intraday_pipeline import _report_to_json
        st.session_state["intra_report"] = _report_to_json(report)

    if "intra_report" not in st.session_state:
        st.session_state["intra_report"] = load_latest_intraday(PROJECT_ROOT)

    data = st.session_state.get("intra_report")
    if not data:
        st.warning("尚無當沖報告。請點「重抓新聞 + 重跑」或在終端機執行 `uv run stock-intraday`。")
        return

    # ---- 頂部 KPI ----
    cs[0].caption(
        f"asof: {data.get('asof', '-')} ｜ "
        f"市場氛圍: **{data.get('market_tone', '-').upper()}** ｜ "
        f"題材 {len(data.get('themes', []))} 個 ｜ 候選 {len(data.get('rankings', []))} 檔"
    )
    if data.get("errors"):
        st.error("管線錯誤: " + " / ".join(data["errors"][:3]))

    overall = data.get("overall_brief", "")
    if overall:
        st.info(overall)

    # ---- LLM 戰情簡報 ----
    if data.get("brief_md"):
        with st.expander("🤖 LLM 戰情簡報", expanded=True):
            st.markdown(data["brief_md"])
            st.caption(
                f"prompt: {data.get('brief_prompt_id','')} v{data.get('brief_prompt_version','')}"
            )

    # ---- 今日熱門題材 ----
    st.markdown("### 🔥 今日熱門題材")
    themes = data.get("themes") or []
    if not themes:
        st.info("尚未萃取題材")
    else:
        cols = st.columns(min(3, len(themes)))
        for i, t in enumerate(themes):
            col = cols[i % len(cols)]
            heat = int(t.get("heat", 0))
            with col:
                st.markdown(
                    f"#### {t.get('theme', '')}  "
                    f"{'🔥' * heat}{'·' * (5-heat)}"
                )
                st.caption(
                    f"{t.get('category','')} ｜ {t.get('horizon','')} ｜ 熱度 {heat}/5"
                )
                for d in (t.get("drivers") or [])[:2]:
                    st.markdown(f"- {d}")
                cands = t.get("candidate_tickers") or []
                if cands:
                    chips = " ".join([
                        f"`{c.get('ticker','')}` {c.get('name','')[:6]}"
                        for c in cands[:6]
                    ])
                    st.markdown(f"**標的:** {chips}")
                for r in (t.get("risks") or [])[:1]:
                    st.caption(f"⚠ {r}")

    # ---- 候選股排序 ----
    st.markdown("### 📋 當沖候選排行 (按 day_trade 分)")
    rankings = data.get("rankings") or []
    if rankings:
        rows = []
        for r in rankings:
            prem = r.get("adr_premium_pct")
            rows.append({
                "代號": r["ticker"],
                "名稱": r.get("name", ""),
                "題材": r.get("theme", ""),
                "當沖分": r.get("day_trade_score", 0),
                "動作": r.get("action", "HOLD"),
                "美股連動": r.get("us_market_score", 50),
                "ADR 溢價%": prem if prem is not None else float("nan"),
                "籌碼": r.get("chip_summary_text", ""),
                "來源": ",".join(r.get("sources") or []),
            })
        df = pd.DataFrame(rows)
        st.dataframe(
            df, hide_index=True, use_container_width=True,
            column_config={
                "當沖分": st.column_config.ProgressColumn(
                    "當沖分", min_value=0, max_value=100, format="%.1f",
                ),
                "美股連動": st.column_config.ProgressColumn(
                    "美股連動", min_value=0, max_value=100, format="%.0f",
                ),
                "ADR 溢價%": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )

        # 快速跳轉到深入分析
        st.markdown("##### 跳轉到深入分析")
        pick = st.selectbox(
            "選一檔看 360 度視角",
            ["(none)"] + [f"{r['ticker']} {r.get('name','')}" for r in rankings[:10]],
            key="intra_pick",
        )
        if pick != "(none)":
            tk = pick.split()[0]
            if st.button(f"分析 {pick}", type="primary"):
                st.session_state["detail_ticker"] = tk
                st.session_state.pop("detail_cache", None)
                st.session_state.pop("detail_cache_key", None)
                st.session_state["page"] = "個股深入分析"
                st.rerun()


# ======================================================================
# 頁面: 美股 / 跨市場
# ======================================================================


def page_macro() -> None:
    st.title("美股 / 跨市場連動")
    st.caption(
        "追蹤美股大盤 (S&P/NASDAQ/SOX/VIX)、加權指數、重要科技股與 ADR 溢價。"
        "免費 API (yfinance)，每日自動快取於 `data/macro/`。"
    )

    from bot.market_macro import (
        fetch_macro_snapshot,
        load_supply_chain,
        macro_to_dict,
        save_supply_chain,
    )

    cs = st.columns([2, 1, 1])
    if cs[1].button("使用快取", use_container_width=True, key="macro_cache"):
        st.session_state["macro_force"] = False
        st.session_state.pop("macro_data", None)
    if cs[2].button("重新抓取", type="primary", use_container_width=True, key="macro_refresh"):
        st.session_state["macro_force"] = True
        st.session_state.pop("macro_data", None)

    if "macro_data" not in st.session_state:
        with st.spinner("抓取美股 + 指數 + ADR..."):
            snap = fetch_macro_snapshot(
                root=PROJECT_ROOT,
                force_refresh=st.session_state.get("macro_force", False),
            )
            st.session_state["macro_data"] = macro_to_dict(snap)
    data = st.session_state["macro_data"]

    cs[0].caption(
        f"asof: {data.get('asof_date', '-')} ｜ "
        f"USDTWD={data.get('usdtwd', 0):.3f} ｜ "
        f"{'快取' if data.get('cached') else '即時抓取'}"
    )

    # ---- 指數 ----
    st.markdown("### 主要指數")
    indices = data.get("indices", {})
    ic = st.columns(6)
    for i, sym in enumerate(["^GSPC", "^IXIC", "^DJI", "^SOX", "^VIX", "^TWII"]):
        q = indices.get(sym)
        if q:
            ic[i].metric(
                q.get("name", sym),
                f"{q.get('price', 0):.2f}",
                f"{q.get('pct_change', 0):+.2f}%",
            )

    # ---- 美股科技龍頭 ----
    st.markdown("### 重要美股 / ADR")
    stocks = data.get("stocks", {})
    rows = []
    for sym, info in stocks.items():
        rows.append({
            "代號": sym,
            "名稱": info.get("name", sym),
            "類別": info.get("role", ""),
            "收盤 (USD)": round(info.get("price", 0), 2),
            "漲跌 %": round(info.get("pct_change", 0), 2),
            "母股": info.get("parent_tw_ticker", ""),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("漲跌 %", ascending=False)
    st.dataframe(
        df, hide_index=True, use_container_width=True,
        column_config={
            "漲跌 %": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )

    # ---- ADR 溢價 ----
    st.markdown("### ADR 溢價 / 折價")
    st.caption("公允台股價 = ADR USD × USDTWD ÷ ADR 比例；溢價>0 表示隔日台股早盤偏多")
    prem = data.get("adr_premiums", [])
    if prem:
        st.dataframe(
            pd.DataFrame(prem), hide_index=True, use_container_width=True,
            column_config={
                "premium_pct": st.column_config.NumberColumn("溢價 %", format="%.2f%%"),
                "fair_tw_price": st.column_config.NumberColumn("公允價", format="%.2f"),
                "tw_price_twd": st.column_config.NumberColumn("實際價", format="%.2f"),
            },
        )
    else:
        st.info("尚未抓到 ADR 溢價 (請點重新抓取)")

    # ---- LLM 跨市場簡報 ----
    st.markdown("---")
    st.markdown("### LLM 跨市場簡報 (美股 → 台股早盤影響)")

    env_values = load_env()
    api_key = env_values.get("GEMINI_API_KEY", "")
    if not api_key:
        st.info("尚未設定 GEMINI_API_KEY，無法產出 LLM 簡報")
    else:
        if st.button("呼叫 Gemini 產出簡報", type="primary", key="macro_brief"):
            from bot.llm_analyzer import GeminiClient, gemini_call

            client = GeminiClient(
                api_key=api_key,
                model=env_values.get("GEMINI_MODEL", "gemini-2.5-flash"),
            )
            with st.spinner("LLM 撰寫中..."):
                raw, info = gemini_call(
                    "us_market_brief",
                    client=client,
                    metadata={"task": "us_market_brief", "source": "dashboard"},
                    asof_date=data.get("asof_date", ""),
                    indices_json=json.dumps(data.get("indices", {}), ensure_ascii=False, indent=2),
                    stocks_json=json.dumps(data.get("stocks", {}), ensure_ascii=False, indent=2),
                    adr_premiums_json=json.dumps(data.get("adr_premiums", []), ensure_ascii=False, indent=2),
                    supply_chain_json=json.dumps(
                        load_supply_chain(PROJECT_ROOT).get("us_stocks", {}),
                        ensure_ascii=False, indent=2,
                    ),
                )
            if raw:
                st.session_state["us_brief"] = (raw, info)

        brief = st.session_state.get("us_brief")
        if brief:
            md, info = brief
            st.caption(f"prompt: {info.get('prompt_id', '')} v{info.get('prompt_version','')} "
                       f"｜ 延遲 {info.get('latency_ms', 0)} ms")
            st.markdown(md)

    # ---- 供應鏈對照表編輯 ----
    st.markdown("---")
    st.markdown("### 美股 → 台股供應鏈對照表")
    sc = load_supply_chain(PROJECT_ROOT)
    us_stocks = sc.get("us_stocks", {})
    st.caption(f"目前載入 {len(us_stocks)} 檔美股對照。檔案位置: `data/supply_chain.json`")

    with st.expander("檢視 / 編輯對照表", expanded=False):
        sel = st.selectbox("選擇美股", sorted(us_stocks.keys()), key="sc_sel")
        if sel:
            info = us_stocks.get(sel, {})
            st.markdown(f"**{sel} {info.get('name', '')}** ({info.get('category','')})")
            tw_rows = info.get("tw_supply_chain", []) or []
            st.dataframe(pd.DataFrame(tw_rows), hide_index=True, use_container_width=True)

        edited = st.text_area(
            "supply_chain.json 原文編輯",
            value=json.dumps(sc, ensure_ascii=False, indent=2),
            height=300, key="sc_edit",
        )
        if st.button("儲存", key="sc_save"):
            try:
                new_sc = json.loads(edited)
                save_supply_chain(new_sc, PROJECT_ROOT)
                st.success("已儲存")
                st.rerun()
            except json.JSONDecodeError as e:
                st.error(f"JSON 格式錯誤: {e}")


PAGES = {
    # 研究與分析
    "功能總覽": page_overview,
    "今日當沖戰情室": page_intraday,
    "K 線看板": page_board,
    "個股總覽": page_watchlist,
    "個股深入分析": page_ticker_detail,
    "自動化管線": page_pipeline,
    # 監控與訊號
    "美股 / 跨市場": page_macro,
    "跟單訊號": page_follow_signals,
    "主動 ETF 追蹤": page_etf_tracker,
    "LLM 法說分析": page_llm_analysis,
    # 執行與紀錄
    "啟動 / 監控": page_runner,
    "🛡 風控中心": page_risk_center,
    "交易可行性檢查": page_preflight,
    "交易紀錄": page_trades,
    "報表分析": page_reports,
    # 系統與診斷
    "組態設定": page_config,
    "資料庫 / 雲端同步": page_database,
    "Prompt 管理": page_prompts,
    "LLM 呼叫紀錄": page_llm_log,
    "日誌檢視": page_logs,
    "通知測試": page_notifier,
    "策略與文件": page_docs,
}

NAV_GROUPS = {
    "🔍 研究與分析": ["功能總覽", "今日當沖戰情室", "K 線看板", "個股總覽", "個股深入分析", "自動化管線"],
    "📡 監控與訊號": ["美股 / 跨市場", "跟單訊號", "主動 ETF 追蹤", "LLM 法說分析"],
    "⚡ 執行與紀錄": ["啟動 / 監控", "🛡 風控中心", "交易可行性檢查", "交易紀錄", "報表分析"],
    "⚙️ 系統與診斷": ["組態設定", "資料庫 / 雲端同步", "Prompt 管理",
                  "LLM 呼叫紀錄", "日誌檢視", "通知測試", "策略與文件"],
}


def main_app() -> None:
    st.set_page_config(
        page_title="Stock Bot Dashboard",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.sidebar.title("Stock Bot")
    st.sidebar.caption("台股當沖機器人 儀表板")

    if "page" not in st.session_state:
        st.session_state.page = "功能總覽"

    page_keys = list(PAGES.keys())
    if st.session_state.page not in page_keys:
        st.session_state.page = page_keys[0]

    # 分組導覽：每個區塊用 markdown 標題 + 一組 radio
    for group_name, pages_in_group in NAV_GROUPS.items():
        st.sidebar.markdown(f"#### {group_name}")
        for p in pages_in_group:
            is_current = st.session_state.page == p
            label = f"▸ {p}" if is_current else f"　{p}"
            if st.sidebar.button(
                label,
                key=f"nav_{p}",
                use_container_width=True,
                type="primary" if is_current else "secondary",
            ):
                st.session_state.page = p
                st.rerun()
        st.sidebar.write("")  # 區塊之間的空隙

    pick = st.session_state.page

    st.sidebar.markdown("---")
    runner = get_runner(PROJECT_ROOT)
    if runner.is_running():
        st.sidebar.markdown(_badge("Bot 執行中", "green"), unsafe_allow_html=True)
    else:
        st.sidebar.markdown(_badge("Bot 待機", "gray"), unsafe_allow_html=True)

    try:
        env_values_for_sb = load_env()
        if env_values_for_sb.get("GEMINI_API_KEY"):
            st.sidebar.markdown(_badge("LLM 已啟用", "green"), unsafe_allow_html=True)
        else:
            st.sidebar.markdown(_badge("LLM 未啟用", "red"), unsafe_allow_html=True)
        try:
            reg = get_registry(PROJECT_ROOT / "prompts")
            st.sidebar.caption(f"Prompt 載入: {len(reg.list_ids())} 個")
        except Exception:
            pass
    except Exception:
        pass

    st.sidebar.caption(f".env: `{env_path()}`")
    st.sidebar.caption(f"專案: `{PROJECT_ROOT}`")

    PAGES[pick]()


def run() -> None:
    """從 console script 啟動 (`uv run stock-dashboard`)。

    透過呼叫 `streamlit run <此檔案>` 來啟動，這樣使用者不必記 streamlit 指令。
    """
    script = Path(__file__).resolve()
    cmd = [sys.executable, "-m", "streamlit", "run", str(script)]
    extra = sys.argv[1:]
    if extra:
        cmd.append("--")
        cmd.extend(extra)
    try:
        sys.exit(subprocess.call(cmd))
    except KeyboardInterrupt:
        sys.exit(0)


# Streamlit 用 `streamlit run dashboard.py` 啟動時，script 的 __name__ 為 "__main__"。
# 透過 console script (`stock-dashboard`) 進來時走 run() → 再 spawn streamlit。
if __name__ == "__main__":
    main_app()
