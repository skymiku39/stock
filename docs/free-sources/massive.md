# Massive（原 Polygon.io）美股新聞收集

第二階段。客戶端（`src/bot/free_sources/massive.py`）已完成；本階段新增收集器 `collect_massive.py`。

## 目標

每天一到兩次，用「不帶 ticker、只帶時間範圍」的方式抓美股新聞，寫成 `data/news/api_massive_<台灣日期>.json`，供隔夜簡報當 Finnhub 以外的第二來源，並保留 `insights` 的個股情緒與理由。

## 範圍

- 時間範圍：前一個平日的美東 16:00（收盤）到現在，以 `published_utc.gte` 傳 UTC（`YYYY-MM-DDTHH:MM:SSZ`）。
- 最多翻 N 頁（預設 3，`--max-pages`），每頁 `limit=1000`（第三階段由 100 改為免費方案上限），用 `next_url` 翻頁。
- 正規化沿用 `massive.normalize_news`：`feed` 為 `massive:<發行者>`，保留 `insights`。
- 輸出為 JSON list；與同日既有檔合併，以 url 去重，原子寫入。
- CLI：`python -m bot.free_sources.collect_massive [--root DIR] [--max-pages N] [--dry-run]`。

## 不做的事

- 不逐檔查詢、不使用 `ticker` 參數。
- （第二階段）不排進 scheduler；第三階段已接線，見下。不改 overnight_brief 的讀取。
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

- 每次執行最多 3 次請求；一天 07:50、08:40 各一次 -> 最多 6 次/日，遠低於 quota 預設 200/日。
- 每分鐘上限預設 4 次（實測免費方案為 5 次，留 1 次餘裕）；3 頁一次跑完不會超過。

## 待金鑰修好後要實測確認（已於第三階段完成，結果見下）

- 金鑰有效（目前 401 Unknown API Key）。
- 免費方案實際每分鐘上限，以及超限時的狀態碼（是否為 429）。
- `published_utc.gte` 接受的格式（目前送 `YYYY-MM-DDTHH:MM:SSZ`）。
- `limit` 免費方案的上限（目前 100；文件寫最大 1000）。
- 不帶 ticker 時 3 頁 x 100 則是否足以涵蓋時間窗；排序是否為新到舊。
- `next_url` 是否需要再帶金鑰（目前只用 Bearer 標頭）。
- 免費方案是否回傳 `insights`。
- 每小時更新的延遲對 08:00 / 08:50 的實際影響。

## 第三階段：實測與接線

### 目標

1. 用有效金鑰逐項實測上面 8 項，請求之間間隔 13 秒（頻率測試除外），依結果修正程式與 quota 預設值。
2. 排進 scheduler：在 08:00 與 08:50 隔夜重點之前各跑一次收集，有開關、沒有金鑰就不排。
3. 不改 `overnight_brief.py`（讀取 `api_<來源>_<日期>.json` 的通用函式由 Finnhub 分支負責）。

### 驗收標準

| # | 標準 | 對應 |
|---|------|------|
| A1 | 8 項待實測逐項有紀錄（下表） | 實測紀錄 |
| A2 | `limit` 用免費方案上限 1000（週末窗口 100×3 頁不夠） | `test_page_limit_is_free_plan_max`、實測 |
| A3 | quota 每分鐘預設不超過實測上限 | `test_quota_defaults_match_free_plan`、實測 |
| A4 | 沒金鑰或 `SCHEDULER_MASSIVE_ENABLED=false` 就不排 | `test_scheduler_jobs_need_key_and_switch` |
| A5 | 兩個每日一次的工作 `massive_news`（07:50–07:59）、`massive_news_update`（08:40–08:49），各自在對應隔夜重點開始前結束 | `test_scheduler_jobs_run_before_each_overnight_brief` |
| A6 | 時間設定可用 `"HH:MM"` 字串覆寫 | `test_scheduler_time_settings_parse_from_strings` |
| A7 | 排程用 `--scheduled`：額度用完或限流回 0（不讓排程器重試多花額度），其他錯誤照常回 2 | `test_scheduled_flag_does_not_fail_on_limits` |
| A8 | 用正式收集器對真實 API 跑一次寫檔、重跑不膨脹 | 實測（暫存 root） |

測試都在 `tests/test_massive_wiring.py`；第二階段的 `tests/test_collect_massive.py` 照常通過。

### 實測紀錄（2026-11-24 12:13–12:20 台北，免費方案）

| 項目 | 結果 | 處置 |
|------|------|------|
| 金鑰 | 換成後台 Default 那把後 probe 回 200 | — |
| 每分鐘上限 | 清空一分鐘後連打：第 1–5 次 200，第 6 次 **429**（訊息 "exceeded the maximum requests per minute"）；回應沒有 rate-limit 相關標頭 | quota `per_minute` 維持 4；429 已對應 `rate_limited` → `status=quota` |
| `published_utc.gte` 格式 | `2026-11-23T21:00:00Z`、`2026-11-23`、`...00.000Z`、`+00:00` 皆 200；`yesterday` 回 400 "invalid format" | 維持送 `YYYY-MM-DDTHH:MM:SSZ` |
| `limit` 上限 | 1000 可用；1001 回 400（validation 'max'） | `PAGE_LIMIT` 100 → 1000 |
| 涵蓋與排序 | 週一收盤（21:00Z）到 04:13Z 共 97 則，一頁就完、沒有 `next_url`；預設排序新到舊 | 平日一頁足夠；週末窗口約 2.5 倍時間，1000×3 頁有餘裕；萬一截斷，丟的是最舊的 |
| `next_url` | 只帶 `cursor` 參數、主機 `api.massive.com`、不含 `apiKey`；只用 Bearer 標頭翻頁 200，與第一頁無重複 | 維持現行（只送 Bearer、拒絕外部主機） |
| `insights` | 免費方案有回傳：97/97 則都有，含 ticker、sentiment、sentiment_reasoning | 維持保留 |
| 更新延遲 | 04:13Z 查詢時最新一則為 03:35Z（約 38 分鐘） | 07:50 那次約可涵蓋到前一小時；08:40 再補一次 |
| 正式收集器 | 暫存 root 跑 `collect_massive`：1 頁、97 則寫入 `api_massive_2026-11-24.json`；隔 13 秒重跑 `new_items=0`、總數不變 | 符合 A8 |

### 決策

- **排程時間 07:50 / 08:40**：scheduler 的工作是平行執行緒、沒有相依順序，所以用時間窗讓收集在隔夜重點（08:00、08:50）開始前結束。07:50 與 Marketaux 的 07:40–07:59 重疊無妨（不同服務、各自額度）。
- **`--scheduled` 回 0**：沿用 Marketaux 的作法。限流時排程器 5 分鐘後重試沒有意義（每日一次工作），每日額度用完時重試更是浪費。
- **預設開啟**：沒有金鑰就不排，所以開關預設 `True` 不會對沒設定的環境產生請求。
