# 富果（Fugle）盯盤報價備援：目標文件

## 目標
`smile_watch` 的主來源（證交所公開報價）拿不到當日報價時，改查富果日內報價，讓盯盤不中斷。2026-11-24 的 1514 從 10:12 起主來源拿不到當日報價，富果實測仍有最後成交（11:01:16、76.4，鎖漲停）。

## 範圍
- `src/bot/smile_watch.py`：`resolve_price`（主來源優先、富果備援）、`build_fugle_client`（無金鑰回 None）、狀態檔與日誌記錄報價來源。
- `src/bot/free_sources/fugle.py`：`limit_lock`、`describe_quote`。
- `src/bot/tray/status.py`、`src/bot/tray/window.py`：只加報價來源的顯示（第三階段）。
- 測試：`tests/test_fugle_fallback.py`、`tests/test_smile_watch.py`、`tests/test_tray_status.py`。

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

## 第三階段：實測與收尾

### 目標
1. 盤中用真實富果 client 跑完整盯盤迴圈（`smile_watch.main`），確認備援真的接得上：狀態檔、日誌、提醒文字都標明來源，鎖漲停時說明正確。只提醒，不碰下單。
2. 查清 2026-11-24 1514 主來源拿不到報價的原因，寫成結論。
3. 評估系統匣／儀表板要不要顯示報價來源；要做就只加顯示，不改判斷邏輯。

### 驗收標準與對應紀錄
| # | 標準 | 對應 |
|---|---|---|
| 9 | 盤中、主來源被強制回空時，`main()` 改用富果：`status-<代號>.json` 的 `price_source` 為 `fugle`、`price_note` 含「鎖漲停、最後成交價、時間」 | 實測紀錄 A |
| 10 | 同一輪的日誌有「報價來源改為 fugle …」與帶「來源 fugle（…）」的現價行 | 實測紀錄 A |
| 11 | 提醒文字（通知、`alerts.jsonl`、日誌）多一行「報價來源：fugle（備援）｜…」，其餘行與主來源時相同；通知只收集、沒有送出 | 實測紀錄 A；`test_status_file_and_alert_record_source` |
| 12 | 1514 拿不到報價的原因有富果成交明細／K 線佐證 | 實測紀錄 B |
| 13 | 系統匣讀狀態檔時帶出來源；備援時主視窗多一行來源說明、選單列標「富果備援」（鎖住時加「鎖漲停／鎖跌停」）；主來源時顯示不變；舊狀態檔沒有欄位視為主來源 | `test_holdings_show_fallback_price_source`（test_tray_status.py） |
| 14 | 相關測試與全套通過 | 測試紀錄 |

### 實測紀錄 A：盤中端到端（2026-11-24 12:07–12:08）
- 作法：暫存目錄當工作目錄與 `--out-dir`，把 `TwsePublicMarketSource.get_quotes` 換成回空 dict（模擬主來源沒有報價），Telegram 與本機通知換成只收集文字的假物件，其餘用真實 `Settings` 與真實富果 client。名單：1514（1 張@70、標準價 105，故意讓它出訊號）、2330（1 張@100、標準價 2000）。`--interval 5 --max-minutes 0.25`，共約 6 次富果請求。
- 結果：
  - `status-1514.json`：`price` 76.4、`price_source` `fugle`、`price_note`「鎖漲停、最後成交價 76.4、時間 11:50:08」、訊號 breakeven、add_buy。
  - `status-2330.json`：`price` 2230、`price_source` `fugle`、`price_note`「鎖漲停、最後成交價 2230、時間 12:07:55」。2330 當天確實鎖漲停（參考價 2030、+9.85%、委賣為空、`isLimitUpPrice` 與 `isLimitUpBid` 皆 true），標示正確。
  - 日誌：「1514 報價來源改為 fugle 鎖漲停、最後成交價 76.4、時間 11:50:08」，現價行結尾「｜來源 fugle（鎖漲停、最後成交價 76.4、時間 11:50:08）」。
  - 提醒文字（節錄）：
    ```
    微笑盯盤 1514：部位轉為淨利
    現價 76.40｜1 張、均價 70.00
    …
    報價來源：fugle（備援）｜鎖漲停、最後成交價 76.4、時間 11:50:08
    只是規則提醒，程式不會下單。
    ```
  - `alerts.jsonl` 三筆（1514 breakeven、1514 add_buy、2330 take_profit），`price_source` 都是 `fugle`。通知只進了假物件，沒有對外送出。
- 實際排程中的盯盤（主 checkout）11:35 起也已改走富果：日誌「1514 報價來源改為 fugle 鎖漲停、最後成交價 76.4、時間 11:01:16」，之後每 5 分鐘有現價行。

### 實測紀錄 B：1514 為什麼拿不到報價（結論：開盤即鎖漲停、成交稀少）
- 富果 K 線（5 分）：09:00 那根開 76.4、低 75.6、收 76.4、量 169 張；09:05 起每根 OHLC 都是 76.4，量 1–16 張，10:30–10:55、11:05–11:45 整段沒有 K 棒（沒有成交）。
- 富果成交明細：全天到 12:06 共 87 筆、242 張；09:00:16 開在 76.4（參考價 69.5 的漲停價），09:00:20–09:00:28 曾打到 75.6–76.3，之後全部成交在 76.4；最後幾筆 10:18:42、10:24:26、10:25:18、11:01:16、11:50:08，間隔動輒數十分鐘。
- 證交所 MIS 原始回應（12:06:30）：`z`（本輪成交）與 `pz` 都是 `-`、`a`（委賣）是 `-`、`b` 是 `0.0000_76.4000_…`（第一檔 0 是市價買單）。`market_source` 因此解析成 `price_basis = best_bid`、`price = 0.0`，不在 `USABLE_PRICE_BASIS` 內，盯盤就略過。
- 時間線修正：主來源不是從 10:12 才拿不到，而是 09:02 起大多數輪次都拿不到（日誌 09:02:06 起即「沒有可用的當日報價」）；只有剛好撞上成交的輪次有價（09:11、09:36 的現價行）。09:53 之後主來源再也沒有給出價格，直到 11:35 富果備援上線。10:09:58–10:12:18 的空檔是沒有日誌，不是另一個問題。
- 結論：不是主來源壞掉，是鎖漲停時委賣為空、成交稀少，主來源大部分輪次只剩單邊委買，依設計略過。富果的 `lastTrade` 不受「本輪有沒有成交」影響，備援正好補這個缺口。

### 測試紀錄（2026-11-24）
- 相關測試：`tests/test_tray_status.py tests/test_tray_app.py tests/test_fugle_fallback.py tests/test_smile_watch.py` 42 passed。
- 全套：1839 passed、3 failed。失敗的是 `test_web_search_provenance`、`test_web_search_research_slice`、`test_web_search_rss` 各一項，原因是 `MemoryError`（同時有其他平行對話在跑，機器記憶體吃緊）；這三個檔單獨重跑，改動前後都 34 passed，與本分支無關。

### 系統匣／儀表板
- 系統匣：要顯示。鎖住時價格看起來「不動」，沒有來源說明會以為盯盤卡住。只加顯示：`HoldingStatus` 多讀 `price_source`、`price_note`；主視窗多一行「報價來源：富果備援｜…」，選單列結尾加「富果備援」（鎖住時「富果備援・鎖漲停」）。
- 儀表板（`smile_dashboard`）不讀 `data/smile_watch/status-*.json`，沒有盯盤現價可標，不改。

### 留給之後（不在這個分支做）
- `market_source._first_book_price` 把市價單的 `0.0000` 當成最佳一檔價格：鎖漲停時 `best_bid` 變 0；若雙邊都有掛單而一邊第一檔是 0，`bid_ask_mid` 會算出錯誤的中間價且被當成可用。應跳過 0 取下一檔。這是共用的主來源程式，另開工作處理。

## 用量預估
盯盤每 20 秒一輪，只有主來源缺報價的標的才查，一檔一分鐘最多 3 次，低於免費上限（每分鐘 60 次，程式內限 50 次、每日 3000 次）。額度用完時 `QuotaExceeded` 被攔下，只記警告。
