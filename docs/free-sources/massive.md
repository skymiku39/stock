# Massive（原 Polygon.io）美股新聞收集

第二階段。客戶端（`src/bot/free_sources/massive.py`）已完成；本階段新增收集器 `collect_massive.py`。

## 目標

每天一到兩次，用「不帶 ticker、只帶時間範圍」的方式抓美股新聞，寫成 `data/news/api_massive_<台灣日期>.json`，供隔夜簡報當 Finnhub 以外的第二來源，並保留 `insights` 的個股情緒與理由。

## 範圍

- 時間範圍：前一個平日的美東 16:00（收盤）到現在，以 `published_utc.gte` 傳 UTC（`YYYY-MM-DDTHH:MM:SSZ`）。
- 最多翻 N 頁（預設 3，`--max-pages`），每頁 `limit=100`，用 `next_url` 翻頁。
- 正規化沿用 `massive.normalize_news`：`feed` 為 `massive:<發行者>`，保留 `insights`。
- 輸出為 JSON list；與同日既有檔合併，以 url 去重，原子寫入。
- CLI：`python -m bot.free_sources.collect_massive [--root DIR] [--max-pages N] [--dry-run]`。

## 不做的事

- 不逐檔查詢、不使用 `ticker` 參數。
- 不排進 scheduler（接線留待之後決定）；不改 overnight_brief 的讀取。
- 不處理美國假日（假日只會多抓一些重複項，靠去重吸收）。
- 不轉散布原始資料；不在盤中使用（來源每小時才更新）。
- 不對真實 API 實測（金鑰目前無效）。

## 驗收標準（對應測試，皆在 `tests/test_collect_massive.py`）

1. 沒有金鑰 -> `status=skipped`，不送請求、不寫檔。`test_no_key_is_skipped`
2. 請求不帶 `ticker`，帶 `published_utc.gte` 與 `limit`；金鑰走 Bearer 標頭。`test_request_has_time_range_and_no_ticker`
3. 起算時間為前一個平日美東收盤（週一、週末、盤前、盤後各情況，含夏令/冬令）。`test_window_start_*`
4. 最多翻 `max_pages` 頁；沒有 `next_url` 就提早停。`test_paging_respects_max_pages`、`test_paging_stops_without_next_url`
5. 項目 `feed` 為 `massive:<publisher>`，`insights` 保留。`test_items_keep_feed_and_insights`
6. 輸出 `data/news/api_massive_<台灣日期>.json`，內容為 list。`test_writes_list_to_taiwan_dated_file`
7. 與同日既有檔合併、以 url 去重（既有項目優先），重跑不會膨脹。`test_merge_dedups_by_url`
8. 原子寫入：目錄內不留暫存檔。`test_atomic_write_leaves_no_temp`
9. 401 -> 回報 `status=error`、`error=auth`，不 raise、不寫檔。`test_auth_error_reported_not_raised`
10. QuotaExceeded 或 429 -> 停止並回報，已取得的頁面仍寫入。`test_quota_exceeded_stops`、`test_429_stops_and_keeps_collected_pages`
11. `--dry-run`：不送請求、不寫檔，只回報時間窗與參數。`test_dry_run_no_request_no_write`
12. 摘要與錯誤訊息不含金鑰。`test_summary_never_contains_key`
13. `main(argv)` 可用，exit code：ok/skipped 為 0，其餘為 2。`test_main_exit_codes`

## 用量預估

- 每次執行最多 3 次請求；一天 08:00、08:50 各一次 -> 約 6 次/日，遠低於 quota 預設 200/日。
- 每分鐘上限預設 4 次（免費方案頻率未確認）；3 頁一次跑完不會超過。

## 待金鑰修好後要實測確認

- 金鑰有效（目前 401 Unknown API Key）。
- 免費方案實際每分鐘上限，以及超限時的狀態碼（是否為 429）。
- `published_utc.gte` 接受的格式（目前送 `YYYY-MM-DDTHH:MM:SSZ`）。
- `limit` 免費方案的上限（目前 100；文件寫最大 1000）。
- 不帶 ticker 時 3 頁 x 100 則是否足以涵蓋時間窗；排序是否為新到舊。
- `next_url` 是否需要再帶金鑰（目前只用 Bearer 標頭）。
- 免費方案是否回傳 `insights`。
- 每小時更新的延遲對 08:00 / 08:50 的實際影響。
