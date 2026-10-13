"""Research workspace, visual system and user-facing release notes."""
from __future__ import annotations

from html import escape

import streamlit as st

from bot.dashboard.common import DASHBOARD_UI_BUILD, PROJECT_ROOT
from bot.dashboard.tasks import LABELS, service, timestamp

RELEASE_DATE = "2026-10-12"
CHANGES = [
    ("01", "更清楚的研究工作台", "首頁集中個股入口、追蹤清單與最近任務；側欄改為常用入口與收合分組。"),
    ("02", "切換頁面，任務繼續", "個股分析、自動研究、市場搜尋與報告生成改為背景執行。可查看排隊、進度、完成與失敗原因。"),
    ("03", "每次更新都有說明", "固定的「本次更新」入口說明改了什麼、在哪裡使用，以及背景執行的適用範圍。"),
]

CSS = """
<style>
:root { --stock-ink: #172a3a; --stock-muted: #627484; --stock-line: #dce5e9; --stock-accent: #0f766e; }
.stApp { background: #f6f8fa; color: var(--stock-ink); }
[data-testid="stHeader"] { background: rgba(246,248,250,.96); }
[data-testid="stMainBlockContainer"] { max-width: 1440px; padding-top: 2.1rem; padding-bottom: 4rem; }
[data-testid="stSidebar"] { background: #fff; border-right: 1px solid var(--stock-line); }
[data-testid="stSidebarUserContent"] { padding-top: 1.2rem; }
[data-testid="stSidebar"] [data-testid="stButton"] button { justify-content: flex-start; border-color: transparent; }
[data-testid="stSidebar"] [data-testid="stButton"] button[kind="secondary"] { background: transparent; }
[data-testid="stSidebar"] [data-testid="stButton"] button:hover { border-color: #b5d4d0; background: #edf6f4; }
[data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"] { background: #e4f1ee; color: #075b54; border-color: #c5e1dc; }
[data-testid="stVerticalBlockBorderWrapper"] > div { border-color: var(--stock-line) !important; border-radius: 12px; background: #fff; }
[data-testid="stMetric"] { padding: 1rem 1.2rem; border: 1px solid var(--stock-line); border-radius: 12px; background: #fff; }
[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; font-size: 1.7rem; }
[data-testid="stCaptionContainer"] { color: var(--stock-muted); }
h1 { font-size: 2.15rem !important; letter-spacing: -.035em; font-weight: 750 !important; }
h2 { font-size: 1.5rem !important; letter-spacing: -.02em; }
h3 { font-size: 1.1rem !important; }
button { border-radius: 8px !important; }
[data-testid="stTabs"] [role="tablist"] { gap: 1.2rem; border-bottom: 1px solid var(--stock-line); }
[data-testid="stTabs"] [role="tab"] { padding: .65rem .2rem; }
.stock-brand { display: flex; align-items: center; gap: .7rem; padding: .2rem 0 1.1rem; }
.stock-logo { display: grid; place-items: center; width: 36px; height: 36px; color: white; background: #0f766e; border-radius: 10px; font-weight: 800; }
.stock-brand strong { font-size: 1.08rem; letter-spacing: -.025em; }
.stock-brand small { display: block; color: #627484; font-size: .72rem; letter-spacing: .08em; }
.stock-eyebrow { color: #0f766e; letter-spacing: .15em; font-size: .7rem; font-weight: 700; margin-bottom: .65rem; }
.stock-hero { padding: 2rem 2.2rem; margin: .4rem 0 1.4rem; border: 1px solid #cbdedc; border-left: 4px solid #0f766e; border-radius: 14px; background: #fff; }
.stock-hero h1 { margin: 0; padding: 0 0 .65rem; color: #172a3a; }
.stock-hero p { max-width: 720px; color: #627484; margin: 0; line-height: 1.8; }
.stock-section { display: flex; justify-content: space-between; align-items: baseline; margin: 1.5rem 0 .6rem; }
.stock-section strong { font-size: 1.1rem; }
.stock-section span { color: #627484; font-size: .78rem; }
.stock-card-num { color: #0f766e; font-family: monospace; font-weight: 700; font-size: .78rem; }
.stock-footer { color: #627484; border-top: 1px solid #dce5e9; margin-top: 1rem; padding-top: .8rem; font-size: .75rem; }
@media (max-width: 760px) {
 [data-testid="stMainBlockContainer"] { padding-left: 1rem; padding-right: 1rem; }
 .stock-hero { padding: 1.4rem; }
 .stock-hero h1 { font-size: 1.8rem !important; }
}
</style>
"""


def apply_theme():
    st.markdown(CSS, unsafe_allow_html=True)


def brand():
    st.markdown('<div class="stock-brand"><span class="stock-logo">S</span><div><strong>Stock Lab</strong><small>台股研究工作台</small></div></div>', unsafe_allow_html=True)


def section(title, note=""):
    st.markdown(f'<div class="stock-section"><strong>{escape(title)}</strong><span>{escape(note)}</span></div>', unsafe_allow_html=True)


def navigate(page):
    st.session_state.page = page
    st.rerun()


def page_home():
    from bot import watchlist
    from bot.dashboard.cache_helpers import cached_env_values
    from bot.env_io import llm_provider_ready
    try:
        items = watchlist.load(PROJECT_ROOT).items
        list_error = ""
    except Exception as exc:
        items, list_error = [], str(exc)
    jobs = service().recent()
    env = cached_env_values()
    st.markdown('<div class="stock-hero"><div class="stock-eyebrow">RESEARCH WORKSPACE</div><h1>把注意力，留給值得研究的股票。</h1><p>從一檔股票開始，整理行情、基本面與研究證據。分析進度與結果會留下來，你可以隨時切換工作。</p></div>', unsafe_allow_html=True)
    metrics = st.columns(3)
    metrics[0].metric("追蹤清單", f"{len(items)} 檔")
    metrics[1].metric("進行中的任務", sum(j["state"] in ("queued", "running") for j in jobs))
    metrics[2].metric("研究助手", "已設定" if llm_provider_ready(env) else "尚未設定")
    if list_error:
        st.warning(f"追蹤清單暫時無法讀取：{list_error}")
    section("開始研究", "預設使用本地資料，外部更新由你決定")
    with st.container(border=True):
        with st.form("home_symbol_form"):
            left, right = st.columns([4, 1], vertical_alignment="bottom")
            symbol = left.text_input("股票代號", placeholder="例如 2330、2317、0050", max_chars=12)
            go = right.form_submit_button("開啟個股分析 →", type="primary", use_container_width=True)
        if go:
            if symbol.strip() and symbol.strip().isalnum():
                st.session_state["detail_ticker"] = symbol.strip()
                st.session_state["detail_symbol"] = symbol.strip()
                navigate("個股深入分析")
            else:
                st.warning("請輸入有效的股票代號。")
    cards = [
        ("01 / MARKET", "先看市場", "用 K 線與盤中排行，找出今天值得關注的標的。", "K 線看板", "開啟 K 線看板"),
        ("02 / RESEARCH", "深入一家公司", "整合公告、新聞與網頁素材，留下可回查的研究結果。", "LLM 法說分析", "開啟自動研究"),
        ("03 / PORTFOLIO", "回到你的持股", "查看目前持股、成本與曝險，整理下一步研究方向。", "目前持股分析", "檢視持股分析"),
    ]
    for col, (number, title, desc, page, label) in zip(st.columns(3), cards):
        with col, st.container(border=True):
            st.markdown(f'<div class="stock-card-num">{number}</div>', unsafe_allow_html=True)
            st.subheader(title)
            st.caption(desc)
            if st.button(label + " →", key=f"home_{page}", use_container_width=True):
                navigate(page)
    left, right = st.columns([3, 2])
    with left:
        section("最近任務", "自動保存 · 跨頁追蹤")
        with st.container(border=True):
            if not jobs:
                st.markdown("**還沒有研究任務**")
                st.caption("送出分析或搜尋後，進度與結果會顯示在這裡。")
            for job in jobs[:4]:
                st.markdown(f"**{job['title']}** · {LABELS[job['state']]}")
                st.caption(f"{timestamp(job['created'])} · {job['message']}")
            if st.button("查看全部任務 →", key="home_tasks"):
                navigate("任務中心")
        section("你的追蹤清單", f"{len(items)} 檔")
        with st.container(border=True):
            if items:
                for item in items[:5]:
                    if st.button(f"{item.ticker}　{item.name or '查看個股'} →", key=f"home_watch_{item.ticker}", use_container_width=True):
                        st.session_state["detail_ticker"] = item.ticker
                        st.session_state["detail_symbol"] = item.ticker
                        navigate("個股深入分析")
            else:
                st.caption("加入你關注的股票，下次可以從這裡直接開始。")
            if st.button("管理追蹤清單", key="home_watchlist"):
                navigate("個股總覽")
    with right:
        section("這次更新了什麼", RELEASE_DATE)
        with st.container(border=True):
            for number, title, description in CHANGES:
                st.markdown(f"**{number} · {title}**")
                st.caption(description)
            if st.button("閱讀本次更新 →", key="home_updates", use_container_width=True):
                navigate("本次更新")


def page_updates():
    st.title("本次更新")
    st.caption(f"{RELEASE_DATE} · {DASHBOARD_UI_BUILD}")
    st.write("這次針對三件事調整：介面太雜、切頁後進度不明、看不出更新內容。")
    for number, title, description in CHANGES:
        with st.container(border=True):
            st.subheader(f"{number} · {title}")
            st.write(description)
    st.subheader("去哪裡使用")
    st.markdown("- **研究工作台**：股票入口、追蹤清單、最近任務與更新摘要。\n- **個股深入分析**：輸入代號，展開「資料與分析選項」，再按「開始分析」。\n- **任務中心**：查看所有任務、回到結果頁、下載結果 JSON；失敗後可手動重試。\n- **側欄分組**：市場與研究、持股與訊號、執行與紀錄、系統設定。")
    st.subheader("哪些工作支援背景執行")
    st.write("個股深入分析、Watchlist 評分、LLM 自動研究（含新聞與網頁搜尋）、逐字稿分析、言行反查、持股資料整理與 LLM 投組分析、自動化研究管線、今日當沖報告、明日關注報告、即時推演、跨市場資料更新與簡報、盤中強弱排行搜尋。")
    st.info("已送出的任務在切頁、重新整理及關閉瀏覽器後會繼續；電腦與 Dashboard 服務必須維持運作。服務中斷後，進行中的工作會標為已中斷，需手動重試。已保存的結果會保留。")
    st.caption("未列出的券商連線、雲端同步與個別資料維護操作仍依原流程在當頁執行。資料缺漏、部分失敗與未驗證來源仍會如實顯示。")
