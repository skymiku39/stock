# Dashboard SOP

## 研究工作台與背景任務

`workspace-2026.10.12` 起，首頁改為研究工作台；完整改動與支援範圍見
[2026-10-12 更新說明](dashboard-update-2026-10-12.md)。

- 長時間的研究操作先透過 `dashboard.tasks.submit_task` 建立持久任務，頁面立即返回。
- 計算與外部呼叫放在 `dashboard.task_operations`，工作執行緒不得操作 `st.*` 或 session state。
- `task_service` 保存輸入、階段、時間、結果與錯誤；同一專案一次執行一項，進行中的相同請求去重。
- 顯示結果只能讀取已完成資料；不得因資料缺漏在 render 路徑重新連外或呼叫 LLM。
- 任務中心與側欄每 2 秒輪詢；部分失敗標為「部分完成」，未知比例時不顯示虛構百分比。
- 服務停止後，執行中任務在 30 秒心跳逾時後標為「已中斷」，不自動重試；已完成 JSON 結果保留。
- 保留原有券商、同步與交易的確認機制；本次重構不新增交易執行入口。

## 更新原則

1. **先用本地資料快速呈現**
   - 預設讀取 SQLite (`data/stock.db`)、CSV 與 JSON cache。
   - 本地沒有資料時先顯示缺資料提示，不阻塞整頁等待外部 API。
   - `macro` 在快速模式只讀今日 `data/macro/macro_<date>.json`；沒有快取就略過。

2. **只刷新使用者明確勾選的資料源**
   - `即時抓籌碼`：TWSE/TPEx 籌碼資料。
   - `抓日K + 指標`：TWSE/TPEx 月 K 線補抓並寫回 DB/CSV。
   - `更新基本面`：月營收、估值、股利與季報。
   - `更新 TDCC`：集保股權分散資料。
   - `缺資料補抓`：本地沒有資料時才呼叫外部資料源。

3. **LLM 必須明確可見**
   - `自動 LLM` 預設關閉。
   - 只有使用者打開 `自動 LLM`、且 `GEMINI_API_KEY` 已設定時，才會補跑 Gemini。
   - 既有 `data/auto_llm/<ticker>.json` 與 pipeline 結果仍會被讀取，不會消耗 token。

## Loading 狀態規則

Loading 文案要說明三件事：

- 目前讀的是本地資料、雲端鏡像快取，還是外部 API。
- 如果會呼叫外部資料源，要寫出資料源名稱，例如 MOPS、TWSE、TDCC、yfinance、TAIFEX、Gemini。
- 批量流程要顯示目前處理第幾檔與總數。

避免只寫：

```text
抓取中...
Loading...
Pipeline 執行中...
```

建議寫法：

```text
[3/12] 2330: 讀取本地資料、套用刷新設定並計算評分
自 TWSE 抓近 5 日籌碼，並整理法人/融資融券摘要
研究管線執行中：抓 ETF 持股 → 抓籌碼面 → 計算 ETF 共識 → 產出每日簡報
```
