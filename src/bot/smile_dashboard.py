"""Standalone UI: uv run python -m streamlit run src/bot/smile_dashboard.py."""
from collections import Counter
from datetime import date
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from bot.smile_dashboard_service import BASELINE, REPORTS, RunRequest, run_comparison, save_run
from bot.smile_snapshot import load_snapshot

ROOT = Path(os.environ.get("STOCK_SMILE_ROOT", Path(__file__).resolve().parents[2])).resolve()

st.set_page_config(page_title="微笑曲線回測",layout="wide")
st.title("微笑曲線回測")
st.caption("收盤訊號 · 次日開盤 · 多檔共用本金")

try:
    manifest, histories = load_snapshot(ROOT, ROOT / BASELINE)
except (OSError,ValueError,KeyError,TypeError) as exc:
    st.error(f"資料快照無法驗證，已停止回測：{exc}")
    st.stop()

with st.sidebar:
    st.header("回測設定")
    symbols = st.multiselect("股票",options=sorted(histories),default=sorted(histories),key="symbols")
    if symbols:
        first = max(histories[s][0].date for s in symbols)
        last = min(histories[s][-1].date for s in symbols)
    else:
        first,last = "2026-06-01","2026-09-18"
    st.caption(f"所選股票共同資料區間：{first} ～ {last}")
    with st.form("parameters"):
        capital = st.number_input("共用本金（元）",min_value=1000,value=500000,step=10000,key="capital")
        start = st.date_input("開始日期",value=date.fromisoformat(first),key=f"start_{first}_{last}")
        end = st.date_input("結束日期",value=date.fromisoformat(last),key=f"end_{first}_{last}")
        slip = st.number_input("每邊滑價（%）",min_value=0.0,max_value=10.0,value=.1,step=.05,format="%.3f",key="slip")
        volume = st.number_input("成交量參與率（%）",min_value=.0001,max_value=100.0,value=.1,step=.01,format="%.4f",key="volume",
                                 help="每檔開盤買賣合計上限＝前一交易日成交股數 × 此比例。")
        compare = st.checkbox("同時比較理想成交與壓力情境",value=True,key="compare")
        with st.expander("壓力情境參數"):
            stress_slip = st.number_input("壓力滑價（%）",min_value=0.0,max_value=10.0,value=.3,step=.1,format="%.3f",key="stress_slip")
            stress_volume = st.number_input("壓力成交量參與率（%）",min_value=.0001,max_value=100.0,value=.01,step=.01,format="%.4f",key="stress_volume")
        submitted = st.form_submit_button("執行回測",type="primary",width="stretch")
    st.caption("固定策略：跌 1/3/5/8% 分批買，基本一張，單檔最多兩張。手續費折扣 0.28、最低 1 元、賣出稅率 0.3%。")
    st.divider()
    st.subheader("已保存的回測")
    files = sorted((ROOT / REPORTS).glob("*.json"),reverse=True)[:100]
    if files:
        chosen = st.selectbox("選擇紀錄",files,format_func=lambda p:p.stem)
        if st.button("載入紀錄"):
            try:
                saved = json.loads(chosen.read_text(encoding="utf-8"))
                if saved.get("schema_version") != 1 or "目前設定" not in saved.get("results",{}):
                    raise ValueError("不支援的結果格式")
                st.session_state["result"] = saved
                st.session_state["result_file"] = str(chosen)
            except (OSError,ValueError,KeyError) as exc:
                st.error(f"無法載入：{exc}")
    else:
        st.caption("完成第一次回測後，紀錄會保存在此處。")

if submitted:
    try:
        request = RunRequest(tuple(symbols),start.isoformat(),end.isoformat(),int(capital),
                             slip,volume,compare,stress_slip,stress_volume)
        with st.spinner("驗證快照並執行回測…"):
            result = run_comparison(ROOT,request)
            path = save_run(ROOT,result)
        st.session_state["result"] = result
        st.session_state["result_file"] = str(path)
        st.success("回測完成，結果與參數已保存。")
    except (OSError,ValueError,KeyError,TypeError) as exc:
        st.error(f"本次未完成：{exc}。若下方有結果，仍是上一次的紀錄。")

payload = st.session_state.get("result")
if not payload:
    st.info("在左側調整本金與成交條件，按「執行回測」。結果包含未平倉損益與每日權益，不只看已實現獲利。")
else:
    req = payload["request"]
    st.subheader("回測結果")
    st.caption(f"以下結果使用：{', '.join(req['symbols'])}｜{req['start']} ～ {req['end']}｜"
               f"本金 {req['capital']:,} 元｜滑價 {req['slippage_pct']}%｜參與率 {req['participation_pct']}%｜"
               f"執行時間 {payload['created_at']}。調整左側參數後需重新執行才會更新。")
    current = payload["results"]["目前設定"]
    metrics = st.columns(4)
    metrics[0].metric("總損益（含未實現）",f"{current['net_pnl_twd']:+,.0f} 元")
    metrics[1].metric("本金報酬率",f"{current['return_pct']:+.2f}%")
    metrics[2].metric("最大日終回撤",f"{current['max_drawdown_pct']:.2f}%")
    metrics[3].metric("最大金額回撤",f"{current['max_drawdown_twd']:,.0f} 元")
    st.caption(f"已實現 {current['realized_pnl_twd']:+,.0f} 元 · 未實現 {current['unrealized_pnl_twd']:+,.0f} 元 · "
               f"已平倉 {current['trade_count']} 筆 · 期末現金 {current['cash']:,.0f} 元")
    rows = []
    chart_rows = []
    for name,r in payload["results"].items():
        model = r["execution"]
        rows.append({"情境":name,"每邊滑價（%）":model["slippage_bps"]/100,
                     "成交量參與率":f"{model['participation']*100:g}%" if model["volume_guard"] else "不限制",
                     "總損益（元）":round(r["net_pnl_twd"],2),
                     "已實現（元）":round(r["realized_pnl_twd"],2),"未實現（元）":round(r["unrealized_pnl_twd"],2),
                     "報酬率（%）":round(r["return_pct"],2),"最大回撤（%）":round(r["max_drawdown_pct"],2),
                     "平倉筆數":r["trade_count"]})
        chart_rows.extend({"日期":p["date"],"情境":name,"權益（元）":p["equity"],"回撤（%）":p["drawdown_pct"]} for p in r["equity_curve"])
    st.subheader("情境比較")
    st.dataframe(pd.DataFrame(rows),hide_index=True,width="stretch")
    st.caption("各情境均使用相同本金與股票。理想成交不含滑價、成交量與漲跌停限制；壓力情境不是收益下界，成交改變可能導致不同交易路徑。")
    if chart_rows:
        frame = pd.DataFrame(chart_rows)
        frame["日期"] = pd.to_datetime(frame["日期"])
        left,right = st.columns(2)
        with left:
            st.subheader("每日權益")
            st.line_chart(frame.pivot(index="日期",columns="情境",values="權益（元）"),x_label="日期",y_label="權益（元）")
        with right:
            st.subheader("日終回撤")
            st.line_chart(frame.pivot(index="日期",columns="情境",values="回撤（%）"),x_label="日期",y_label="回撤（%）")
    with st.expander("成交與未成交明細"):
        scenario = st.selectbox("查看情境",list(payload["results"]))
        detail = payload["results"][scenario]
        reasons = {"cash_or_position_limit":"資金或持倉上限","volume_limit":"成交量上限",
                   "volume_limit_whole_lot_sell":"成交量不足以賣出整筆部位","unprofitable_open":"扣除成本後不獲利",
                   "limit_up_buy_blocked":"漲停買進封鎖","limit_down_sell_blocked":"跌停賣出封鎖",
                   "outside_proxy_band_requires_reference":"超出代理漲跌停範圍",
                   "slippage_outside_price_band":"滑價後超出價格範圍","invalid_open":"開盤價無效",
                   "missing_limit_reference":"缺少價格限制基準"}
        cancelled = Counter(reasons.get(o.get("reason"),o.get("reason","未知原因")) for o in detail["orders"] if o["status"] == "cancelled")
        if cancelled:
            st.dataframe(pd.DataFrame([{"取消原因":k,"次數":v} for k,v in cancelled.items()]),hide_index=True)
        orders = [{"股票":o["symbol"],"訊號日":o["signal_date"],"成交嘗試日":o["execution_date"],
                   "方向":"買進" if o["side"]=="buy" else "賣出",
                   "狀態":"成交" if o["status"]=="filled" else "取消",
                   "成交張數":o["filled_quantity"],"模擬價格":o.get("execution_price"),
                   "取消原因":reasons.get(o.get("reason"),o.get("reason",""))} for o in detail["orders"]]
        if orders:
            st.dataframe(pd.DataFrame(orders),hide_index=True,width="stretch")
        trades = [{"股票":t["symbol"],"買進日":t["entry_date"],"賣出日":t["exit_date"],
                   "張數":t["quantity"],"買價":t["entry_price"],"賣價":t["exit_price"],
                   "淨損益（元）":t["pnl_twd"]} for s in detail["results"] for t in s["round_trips"]]
        if trades:
            st.write("已平倉交易")
            st.dataframe(pd.DataFrame(trades),hide_index=True,width="stretch")
        if not orders:
            st.caption("這個區間沒有產生委託。")
    st.download_button("下載完整結果 JSON",json.dumps(payload,ensure_ascii=False,indent=2),
                       file_name="微笑曲線回測.json",mime="application/json")
    with st.expander("資料版本與保存位置"):
        st.write("資料 SHA-256",payload["input_sha256"])
        st.write("資料覆蓋",payload["coverage"])
        st.code(st.session_state.get("result_file",""),language=None)

st.divider()
st.caption("歷史日 K 近似，非實盤收益。前日成交量與前日收盤只是開盤流動性、漲跌停基準的代理；未包含委託簿、股利及完整公司行動。只賣淨利為正的批次，仍可能有長期未平倉虧損。")
