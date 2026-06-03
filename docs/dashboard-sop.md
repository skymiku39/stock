# Dashboard SOP

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
