# Marketaux：持股相關美股的隔夜情緒

## 目標

每天早上（台灣時間 08:00 前）取得少數持股相關美股在「前一個美股收盤後」的新聞情緒，整理成「個股隔夜情緒」小表，供隔夜重點檔引用。

## 範圍

- 預設追蹤 TSM、NVDA、AMD、MU，可用 `--symbols` 覆寫。
- 逐檔查 `published_after`（前一個平日美東 16:00 收盤，轉成 UTC）之後的新聞，每檔最多 2 頁（每頁最多 3 則）。
- 只取每則新聞 `entities` 中 `symbol` 等於該檔的 `sentiment_score`，算平均分數、則數、正/負/中性則數。
- 輸出兩個檔（皆在 `data/news/`，日期為台灣日期）：
  - `marketaux_sentiment_<日期>.json`：情緒小表（原子寫入）。
  - `api_marketaux_<日期>.json`：正規化新聞，與既有內容合併、以 url 去重。
- 入口：`python -m bot.free_sources.collect_marketaux [--symbols TSM,NVDA] [--dry-run]`。
- 與隔夜重點檔的串接由主對話另行處理，本階段不改 `overnight_brief.py`。

## 不做的事

- 分數是 Marketaux 自己算的、方法不透明，只當參考，不進任何交易規則。
- 不當主要新聞來源（一天最多約 300 則）。
- 沒有金鑰時不報錯，直接略過（skipped）。
- 不自動重試 402／429，不超出自己的每日用量上限（`quota.py` 設 90 次）。
- 沒有資料的代號標示「無資料」，不以 0 分代替。

## 驗收標準（對應 `tests/test_collect_marketaux.py`）

1. 沒有金鑰 → `status=skipped`，不寫檔。 `test_no_key_skipped`
2. `--dry-run` 不連網、不寫檔，列出代號與計畫請求數。 `test_dry_run_no_request_no_write`
3. `published_after` 為前一個平日美東收盤（冬令 21:00Z、夏令 20:00Z；週一往前跳到週五）。 `test_published_after_winter_summer_weekend`
4. 每檔最多 2 頁；第一頁已是全部（`found` 不超過已取）就不查第 2 頁。 `test_max_two_pages_per_symbol`、`test_stops_when_no_next_page`
5. 只取 `symbol` 等於該檔的分數，其他 entity 的分數不計入。 `test_only_own_symbol_scores`
6. 平均分數、則數、正/負則數計算正確，跨頁以 uuid 去重。 `test_sentiment_table_values`
7. 沒有資料的代號為 `no_data`、`avg` 為 None（不是 0）。 `test_no_data_symbol_is_marked_not_zero`
8. 情緒檔寫到正確路徑與格式，含日期、published_after、備註。 `test_sentiment_file_written`
9. 新聞檔以 url 去重合併，重跑不重複。 `test_news_merge_dedup_on_rerun`
10. 額度用完（QuotaExceeded 或 HTTP 402）→ 停止、`status=quota_exceeded`，後面的代號不再送請求。 `test_quota_exceeded_stops`、`test_http_402_stops_and_redacts_key`
11. 429 → 停止並回報 `rate_limited`。 `test_http_429_stops`
12. 單檔其他錯誤不影響其他代號（partial），出錯代號不覆蓋既有檔中的舊資料。 `test_partial_error_keeps_other_symbols_and_previous`
13. 錯誤訊息不含金鑰。 `test_http_402_stops_and_redacts_key`
14. `main(argv)` 可解析 `--symbols` 與 `--dry-run`。 `test_main_parses_symbols_and_dry_run`

## 用量預估

每檔 2 頁 × 4 檔 = 最多 8 次／天（上限 100，程式自設 90）。追蹤到 10 檔也只有 20 次。每頁 3 則，所以每檔每天最多 6 則，樣本很小，平均分數波動大，解讀要保守。

## 拿到金鑰後要實測確認的項目

1. `published_after` 格式：目前送 `YYYY-MM-DDTHH:MM:SS`（UTC、無字尾）。確認伺服器是否按 UTC 解讀，或要改帶 `Z`。
2. `page` 參數是否如預期（第 2 頁回不同的 3 則）、`meta.found` 是否可靠。
3. 免費方案實際每頁上限是否為 3 則，`limit` 要不要明確帶。
4. `filter_entities=true` 時 `entities` 是否只含查詢的代號，以及 TSM 這類 ADR 是否有 `sentiment_score`。
5. 沒有新聞時回傳是 `data: []` 還是錯誤碼。
6. 402、429 實際的回應內文（確認 `SourceError.kind` 分類正確）。
7. 一次查多檔（`symbols=TSM,NVDA`）與逐檔查的額度消耗差異（目前逐檔，確保每檔都有分數）。
8. 分數範圍（預期 -1 ~ 1）與正負判斷（目前以 >0 為正、<0 為負、=0 為中性）。

## 第三階段：實測與接線

### 目標

1. 拿到金鑰後逐項實測上面 8 項，依結果修正 `collect_marketaux.py`／`marketaux.py`，結果記在下方「實測紀錄」。每天只有 100 次，實測預算 30 次以內。
2. 收集在 08:00 隔夜重點之前自動執行，有開關。
3. 隔夜重點（`data/overnight_brief/<日期>.json`）多一個 `us_sentiment` 欄位，儀表板「美股隔夜重點」頁顯示小表；分數只當參考，不進交易規則。

### 接線方式與原因

- 用既有的背景排程器（`stock-scheduler`），新增每日一次的工作 `marketaux_sentiment`，時段 07:40–07:59（`SCHEDULER_MARKETAUX_TIME`／`SCHEDULER_MARKETAUX_END_TIME`），開關 `SCHEDULER_MARKETAUX_ENABLED`（預設開）。
  原因：隔夜重點本來就由這個排程器在 08:00 跑，放在同一個排程器裡，先後順序有保證，不必另外註冊 Windows 排程工作。
- 沒有 `MARKETAUX_API_TOKEN` 就不建立這個工作（不會每天跑一個註定 skipped 的程序，也不改變既有排程測試的預期工作清單）。
- 排程呼叫帶 `--scheduled`：額度用完（402／自設上限）或被限流（429）時回傳 0。原因：排程器對失敗的每日工作會在時段內自動重試，重試會再花額度，違反「不自動重試 402／429」。手動執行不帶此旗標時仍回 1。
- 隔夜重點只讀「當天」的 `marketaux_sentiment_<台灣日期>.json`；沒檔為 `status=missing`、`rows=[]`；代號為 `no_data` 或 `error` 時 `avg=None`、`display="無資料"`，不以 0 分代替。08:50 的開盤前更新會重讀一次，07:40 失敗而之後手動補跑也會被帶到。

### 驗收標準

| # | 標準 | 對應 |
|---|------|------|
| A1 | `published_after` 格式被伺服器按 UTC 解讀（回傳新聞的 `published_at` 都晚於該時間） | 實測紀錄 1 |
| A2 | `page=2` 回不同的新聞，`meta.found` 與實際一致；否則修正分頁停止條件 | 實測紀錄 2 |
| A3 | 免費方案每頁筆數確認，`PAGE_SIZE` 與實際一致 | 實測紀錄 3 |
| A4 | TSM 的 entities 有 `symbol=TSM` 與數值 `sentiment_score` | 實測紀錄 4 |
| A5 | 沒有新聞時程式得到 `no_data`，不是 error | 實測紀錄 5；`test_no_data_symbol_is_marked_not_zero` |
| A6 | 402／429 內文被分類為 `quota_exhausted`／`rate_limited`，錯誤訊息不含金鑰 | 實測紀錄 6；`test_http_402_stops_and_redacts_key`、`test_http_429_stops` |
| A7 | 多檔一次查與逐檔查的取捨有結論 | 實測紀錄 7 |
| A8 | 分數範圍在 -1～1，正負判斷規則合理 | 實測紀錄 8 |
| B1 | 有金鑰且開關開時排程有 `marketaux_sentiment`，時段在 08:00 之前；沒金鑰或關掉就沒有 | `test_scheduler_job_needs_token`、`test_scheduler_job_runs_before_overnight_brief`、`test_scheduler_time_settings_parse_from_strings` |
| B2 | 排程呼叫遇額度／限流回 0（不被重試），一般錯誤仍回 1 | `test_scheduled_flag_does_not_fail_on_limits` |
| B3 | 隔夜重點有 `us_sentiment`；`no_data`／`error` 顯示「無資料」、`avg=None`；沒檔為 `missing` | `tests/test_marketaux_wiring.py` 的 `test_us_sentiment_*`、`test_build_brief_*` |
| B4 | 儀表板頁顯示情緒小表與「僅供參考、不進交易規則」說明 | 實測紀錄 9（Streamlit AppTest 冒煙） |
| B5 | 實際跑一次收集（暫存 `--root`）寫出兩個檔，再產生隔夜重點能讀到 | 實測紀錄 10 |

### 實測紀錄

- 9（2026-11-24）：以 Streamlit `AppTest` 跑 `page_overnight_brief`，無例外；表格 TSM 顯示「+0.25（3 則：正 2／負 1）」、MU 顯示「無資料」，下方附說明。
- 1～8、10：等使用者取得金鑰後補。
