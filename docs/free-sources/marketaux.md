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
