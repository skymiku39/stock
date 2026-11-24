# 富果（Fugle）盯盤報價備援：目標文件

## 目標
`smile_watch` 的主來源（證交所公開報價）拿不到當日報價時，改查富果日內報價，讓盯盤不中斷。2026-11-24 的 1514 從 10:12 起主來源拿不到當日報價，富果實測仍有最後成交（11:01:16、76.4，鎖漲停）。

## 範圍
- `src/bot/smile_watch.py`：`resolve_price`（主來源優先、富果備援）、`build_fugle_client`（無金鑰回 None）、狀態檔與日誌記錄報價來源。
- `src/bot/free_sources/fugle.py`：`limit_lock`、`describe_quote`。
- 測試：`tests/test_fugle_fallback.py`、`tests/test_smile_watch.py`。

## 不做的事
- 不碰任何下單路徑；盯盤只提醒。
- 主來源有當日報價時不查富果。
- 不改 base.py、quota.py、config.py、scheduler.py；不新增套件；不讀寫 `.env`。
- 不用試撮價（lastPrice）；只用 `lastTrade`。

## 漲停／跌停鎖住的處理（提醒用途）
鎖住時成交稀少，最後成交可能是幾小時前，但價格就是漲／跌停價，仍有意義。方案：
- 判定：`isLimitUpPrice` 與 `isLimitUpBid` 皆為 true ＝鎖漲停；`isLimitDownPrice` 與 `isLimitDownAsk` 皆為 true ＝鎖跌停；欄位缺漏視為沒鎖。
- 仍以最後成交價評估規則（前提是最後成交在今天），不當成無報價。
- `price_note` 寫成「鎖漲停、最後成交價 X、時間 T」，並出現在日誌、狀態檔、提醒文字。僅供提醒，不是下單依據。
- 今天完全沒有成交（lastTrade 缺或非今天）則仍視為無報價。

## 驗收標準與對應測試
1. 主來源有當日報價時不查富果 → `test_primary_quote_never_calls_fugle`
2. 主來源無報價且有富果 client 時才查，且採用富果最後成交價 → `test_fallback_used_only_when_primary_missing`
3. 沒有金鑰（空、空白）不建立 client、不查，行為與現在相同 → `test_no_key_builds_no_client`、`test_no_client_behaves_as_before`
4. 試撮（isTrial true）、暫停交易、非今天成交一律不採用 → `test_untrusted_fugle_quotes_are_rejected`
5. 鎖漲停／跌停回報「鎖…、最後成交價 X、時間 T」而非無報價 → `test_limit_lock_is_reported_not_dropped`
6. 富果出錯（額度、網路、401）不丟例外、回無報價 → `test_fugle_errors_do_not_break_the_loop`
7. 報價來源寫進狀態檔與提醒文字；主來源時提醒文字不變 → `test_status_file_and_alert_record_source`（test_smile_watch.py）
8. 實測：真實 client 查 1514、2330 各一次（共 2 次請求）跑過 `describe_quote`。

## 用量預估
盯盤每 20 秒一輪，只有主來源缺報價的標的才查，一檔一分鐘最多 3 次，低於免費上限（每分鐘 60 次，程式內限 50 次、每日 3000 次）。額度用完時 `QuotaExceeded` 被攔下，只記警告。
