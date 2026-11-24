# FMP（Financial Modeling Prep）美股財報收集

## 目標
每週一次，替 `market_macro.DEFAULT_STOCKS` 的美股代號（含 TSM、UMC、ASX 三檔 ADR，共 14 檔）收集損益表、資產負債表、現金流量表，存成本地 JSON，並提供營收、毛利率、EPS、自由現金流的趨勢摘要，當作之後模型摘要與個股研究的背景資料。

## 範圍
- 模組：`src/bot/free_sources/collect_fmp.py`；指令：`python -m bot.free_sources.collect_fmp [--symbols NVDA,AMD] [--force] [--dry-run]`。
- 端點（stable）：`income-statement`、`balance-sheet-statement`、`cash-flow-statement`，各抓年報（period=annual，limit=5）與季報（period=quarter，limit=5；實測免費版 limit 上限為 5，limit=8 會被 HTTP 402 拒絕，5 季剛好夠算去年同季比較）。
- 輸出：`<root>/data/us_fundamentals/<代號>.json`，內容含 `symbol`、`fetched_at`（台灣時間 ISO）、`currency`、`statements`（income / balance / cashflow，各含 annual、quarter，依日期新到舊）。原子寫入（暫存檔 + `os.replace`）。
- 原始回應：每個「代號 × 報表 × 期別」的 API 回應用既有 `SourceRegistry.register_document` 以內容 hash 存進 `data/raw/fmp/`（來源名 `fmp`，URL 不含金鑰）。登錄失敗只記警告，不影響 JSON 寫出。
- 金鑰：`Settings().fmp_api_key`；用量走 `quota.py` 的 fmp 計數（每日上限 230）。

## 不做的事
- 不進任何交易規則、不產生買賣訊號、不送任何委託（SIMULATION=true 不變）。
- 不做匯率換算、不做跨公司的絕對金額比較。
- 不接排程（scheduler 另案決定）；不改 `data/` 既有檔案。

## 用量預估與決策
- 一檔 = 3 張報表 × (年 + 季) = 6 次；14 檔 = 84 次 / 週，遠低於每日上限 230（免費 250）。
- 決定年報與季報都抓：季報才看得到最近一季的毛利率與 EPS 轉折，年報提供長期底；多出的 42 次仍在上限內。
- 「本週是否已抓過」：檔內 `fetched_at` 與現在同屬台灣時間的同一個 ISO 週就略過，`--force` 可覆寫。重複執行不重複耗額度。
- `--force` 全部重抓一次最多 84 次，同日再跑第二輪也只有 168 次，仍在上限內；若超限，QuotaExceeded 會停止整個收集。

## 幣別
- `currency` 取自財報的 `reportedCurrency`（逐筆保留在原始列中）。
- TSM 實測為 TWD，不是 USD：金額（營收、毛利、FCF）以 TWD 計，**不換算**；摘要輸出 `currency` 欄位並標示。
- 比率類指標（毛利率、營收年增率）與幣別無關，可跨檔比較；EPS 與金額只能在同一檔、同一幣別內比較趨勢。

## 驗收標準
1. 每檔 6 次請求（3 報表 × 年/季），寫出 `us_fundamentals/<代號>.json`，內容含 fetched_at、currency、三張報表的年報與季報。（`test_collects_six_requests_and_writes_file`）
2. 寫入為原子操作，失敗不留下半截檔，也不覆蓋舊檔。（`test_atomic_write_keeps_old_file_on_failure`）
3. 沒有金鑰 → 狀態 skipped、零請求、不寫檔。（`test_no_key_skipped`）
4. 單檔錯誤（SourceError）記錄後繼續下一檔；該檔不寫入。（`test_single_symbol_error_continues`）
5. QuotaExceeded → 立即停止，後面的代號不再請求。（`test_quota_exceeded_stops`）
6. 本週已抓過就略過、不請求；`--force` 才重抓；跨週則重抓。（`test_skip_if_fetched_this_week`、`test_force_refetches`、`test_new_week_refetches`）
7. `--dry-run` 不請求、不寫檔，只列出預計動作與請求數。（`test_dry_run_makes_no_requests`）
8. `--symbols` 只處理指定代號；預設為 DEFAULT_STOCKS 全部（14 檔）。（`test_symbols_option`、`test_default_symbols_are_all_us_stocks`）
9. TSM 的 TWD 幣別被如實保留並出現在摘要。（`test_currency_twd_preserved`）
10. `summarize` 算出營收、毛利率、EPS、自由現金流的趨勢（最新值、年增 / 變化、序列），缺值不崩潰。（`test_summarize_trends`、`test_summarize_handles_missing`）
11. 原始回應存進 SourceRegistry，且內容不含金鑰。（`test_raw_responses_registered_without_key`）
12. 14 檔全跑的請求數 ≤ 每日上限。（`test_full_run_within_daily_quota`）
13. 實測：NVDA、TSM 兩檔（≤ 8 次請求）寫到暫存目錄成功。
