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
- 第一、二階段不接排程（第三階段已接，見下）；不改 `data/` 既有檔案。

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

## 第三階段：排程與用途（2026-11-24）

### 目標
1. **排程**：`stock-scheduler` 加 `fmp_fundamentals` 工作，跑 `python -m bot.free_sources.collect_fmp --root <專案根目錄>`。
   - 設定：`SCHEDULER_FMP_ENABLED`（預設 true）、`SCHEDULER_FMP_INTERVAL_MIN`（預設 360，至少 30）。
   - 排程只負責「定期叫起來」；一週只抓一輪靠收集程式的 `fetched_this_week`（同一個台灣時間 ISO 週已抓過就零請求）。不另外在排程記週次，重開排程也不會重抓。
   - 不限交易時段（美股財報與台股盤中無關）；沒有金鑰時印 skipped、回 0，不算失敗。
2. **錯誤分類**：FMP 對「方案不允許的參數值」也回 HTTP 402。2026-11-24 實測 `limit=8` 的回應是 `Premium Query Parameter: ... 'limit' must be between 0 and 5 based on your current subscription`。
   - `BaseClient` 新增 `_classify_http_error(status, body)` 掛勾（預設回 None，沿用狀態碼分類），其他家行為不變（Marketaux、FinMind 的 402 仍是 `quota_exhausted`）。
   - `FmpClient` 覆寫：402 + 參數錯字樣 → `invalid_params`；402 + 方案不含端點字樣 → `plan_restricted`；其他 402 仍是 `quota_exhausted`。
   - 收集遇到 `invalid_params` 立即停止（參數錯每檔都會失敗，繼續只會浪費額度），結果標 `stopped_reason`。
3. **用途**：趨勢摘要給**個股研究頁**當背景資料。
   - 讀取點：`ticker_view.build_snapshot` 第 10 段「美股 / 跨市場連動」之後，新欄位 `TickerSnapshot.us_fundamentals`（`snapshot_to_dict` 也輸出）。這裡已經有 `us_related`（供應鏈相關美股）與 `adr_premium`，財報趨勢放在同一處最自然。
   - 內容：該台股本身的 ADR（TSM→2330、UMC→2303、ASX→3711）排前面，再接供應鏈相關美股；只列本地有檔的代號，最多 6 檔。每檔只留最新值與年增（去掉序列），附 `currency`、`fetched_at`、`stale`（超過 14 天未更新）與「背景資料、不換算、不進交易規則」的說明。
   - 只讀本地 `data/us_fundamentals/`，不連網、不耗額度；讀取失敗只記 debug，不影響快照其他部分。
   - 為什麼先不接模型摘要（隔夜重點等）：隔夜重點的輸出欄位給顯示端讀、不可改，且模型摘要掛勾尚未接上；等模型摘要接上時可直接呼叫 `load_summary` / `summaries_for_tw` 取精簡格式。
   - 這一階段不改儀表板畫面（欄位已在快照裡，顯示另案）。

### 不做的事
- 不進任何交易規則、評分或訊號；`us_fundamentals` 不被 `scoring` 或任何下單路徑讀取。
- 不在讀取點補抓 FMP（研究頁打開不會耗額度）。

### 驗收標準
14. 預設設定下排程有 `fmp_fundamentals` 工作，模組為 `bot.free_sources.collect_fmp`、帶 `--root`、不限交易時段；`SCHEDULER_FMP_ENABLED=false` 時沒有這個工作。（`test_scheduler_fmp_job_built_and_toggle`）
15. 排程連續叫起兩次，同一週內第二次零請求；跨週才重抓。（`test_scheduled_runs_fetch_once_per_week`）
16. FMP 回 402 + `'limit' must be between 0 and 5` → `invalid_params`；402 + 方案不含端點 → `plan_restricted`；402 + 其他內容 → 仍是 `quota_exhausted`。（`test_fmp_402_param_error_classified`、`test_fmp_402_plan_restricted`、`test_fmp_402_other_is_quota`）
17. 其他家的 402 分類不受影響。（`test_other_sources_402_still_quota`，另有既有的 `test_error_kinds_and_no_key_leak`、`test_finmind_request_and_errors`）
18. 收集遇到 `invalid_params` 立即停止，後面的代號不請求，`stopped_reason == "invalid_params"`。（`test_invalid_params_stops_collection`）
19. `load_summary` 讀本地檔回精簡摘要（無序列、含 currency 與 stale）；沒檔回 None。（`test_load_summary_compact_and_stale`）
20. `summaries_for_tw("2330")` 把 TSM 排第一（relation=adr），再接供應鏈美股，只列有檔的代號。（`test_summaries_for_tw_adr_first`）
21. `build_snapshot` 輸出 `us_fundamentals`，且只讀本地、不呼叫 FMP。（`test_ticker_snapshot_includes_us_fundamentals`）

### 第三階段結果
- 驗收 14–21 的測試在 `tests/test_fmp_phase3.py`；`tests/test_scheduler.py`、`tests/test_scheduler_events.py` 中「逐一關掉預設工作」的測試補上 `scheduler_fmp_enabled=False`。
- 實測（2026-11-24，暫存目錄當 `--root` 與工作目錄，額度計數也寫在暫存目錄）：
  - 402 回應內容：以 `limit=8` 打一次 income-statement，確認字樣如上（這一次沒經過 `quota.py`）。
  - 排程同一指令 `collect_fmp --root <暫存> --symbols NVDA,TSM` 連跑兩次：第一次 12 次請求、兩檔寫檔；第二次 `skipped_week` 兩檔、零請求，額度計數維持 12。
  - `summaries_for_tw("2330", [NVDA])`：TSM（relation=adr、currency=TWD）排第一，NVDA（supply_chain、USD）其次；最新季營收年增 TSM 50.95%、NVDA 103.21%。
- 合併注意：`config.py`、`scheduler.py`、`ticker_view.py` 各是一段獨立新增區塊；兩個排程測試只多一行參數，和其他分支衝突時保留雙方即可。


## 2026-11-24 排程實跑發現

- 免費方案**不含 ASML、AVGO、MU、UMC、ASX**（402「Special Endpoint : This value set for 'symbol' is not available under your current subscription」）。14 檔裡實際能抓 9 檔：NVDA、AMD、AAPL、MSFT、GOOGL、META、AMZN、TSLA、TSM。
- 這個訊息同時含「Premium Query Parameter」，原本先比對參數錯誤，被判成 `invalid_params` 而整批停止（AVGO 之後都沒查）。已改為先判 `plan_restricted`：只跳過該檔、繼續下一檔；只有 `plan_restricted` 錯誤時結束碼為 0，排程器不會反覆重試。
- 不從名單移除這 5 檔：每週各浪費 1 次請求（共 5 次，上限 230），之後若方案或對方政策改變能自動恢復。
