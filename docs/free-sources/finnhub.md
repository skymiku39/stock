# Finnhub 收集程式：目標與驗收標準

## 目標

每個平日隔夜重點產生前，把美股公司新聞（`market_macro.DEFAULT_STOCKS` 的美股代號）與大盤新聞抓下來，轉成與 RSS 存檔相同的項目格式，存成 `data/news/api_finnhub_<台灣日期>.json`，補上夜間 RSS 的缺口。

## 範圍

- `src/bot/free_sources/collect_finnhub.py`：`collect(root, *, now, settings=None, client=None, symbols=None) -> dict` 與 `main(argv)`。
- 對每檔查「前一個平日到今天」的 `company-news`，再查一次 `general` 新聞。
- 正規化沿用 `finnhub.normalize_news`。
- 與同日既有檔合併，以 url（無 url 則以 title）去重，原子寫入（暫存檔再 rename）。
- 用量計數存在 `root/data/api_quota/finnhub.json`。

## 不做的事

- 不接線到 scheduler、`overnight_brief`、`config.py`（由主對話做）。
- 不寫進 `rss_<日期>.json`，不影響盤中情緒判斷。
- 不下單、不碰 `SIMULATION`；不讀、不輸出金鑰。
- 不做歸群與去重以外的內容分析；不補抓大盤新聞的歷史（API 不支援）。

## 驗收標準

| # | 標準 | 驗證 |
|---|---|---|
| A1 | 沒有金鑰且沒傳 client，回傳 `{"status":"skipped"}`，不建立任何檔案、不報錯 | 測試 |
| A2 | 預設代號為 `DEFAULT_STOCKS` 中的美股代號（14 檔），每檔一次 company_news 加一次 general news | 測試 |
| A3 | 查詢區間為「前一個平日」到「今天」（週一往前到上週五；週末往前到週五） | 測試 |
| A4 | 輸出 `data/news/api_finnhub_<台灣日期>.json`，內容為 list，項目 feed 為 `finnhub:<代號>` 或 `finnhub:general`，含 `us_tickers`、`title`、`url`、`published_at` | 測試＋實測 |
| A5 | 與同日既有檔合併，以 url（無 url 以 title）去重；重跑不會重複 | 測試 |
| A6 | 同批資料內的重複項目也只留一則 | 測試 |
| A7 | 寫入為暫存檔再 rename，目錄內不殘留暫存檔；既有檔損毀（非 list）時當作空 | 測試 |
| A7b | 沒抓到任何新項目時不寫檔、不動既有檔 | 測試 |
| A8 | 單檔 `SourceError` 記錄於 errors 後繼續下一檔 | 測試 |
| A9 | `QuotaExceeded` 立即停止後續請求、摘要標示 `quota_exceeded`，已抓到的仍寫出 | 測試 |
| A10 | 摘要含每檔筆數、errors、總請求數、輸出路徑，且不含金鑰 | 測試 |
| A11 | `--dry-run` 不寫檔也不連網，只列出將查詢的代號與區間 | 測試 |
| A12 | `python -m bot.free_sources.collect_finnhub` 可執行，輸出摘要不含金鑰 | 測試＋實測 |
| A13 | 用 NVDA、TSM 兩檔實測，總請求 ≤ 4，輸出檔筆數與欄位正確 | 實測 |

## 用量預估

- 每次執行：14 檔公司新聞 + 1 次大盤 = 15 次請求；08:00 與 08:50 各一次約 30 次/天。
- 免費額度每分鐘 60 次；quota 設定為每分鐘 50、每日 1500，遠低於上限。
- 一次執行 15 次請求在同一分鐘內，低於每分鐘 50 的保護線。

## 第三階段：接線（2026-11-24）

### 目標

把 Finnhub 新聞接進美股隔夜重點，補夜間 RSS 的缺口：08:00 與 08:50 兩次隔夜重點產生前先跑 `collect_finnhub`，隔夜重點再把 `data/news/api_<來源>_<日期>.json` 併入視窗候選。讀取函式是通用的，Massive、Marketaux 產生同格式的檔時不用改讀取端。

### 設計決策

- **收集放在 `overnight_brief.main` 裡先跑，不在 `scheduler.py` 加獨立工作。** 原因：(1) 順序有保證：收集一定在產生 brief 之前，排程器在同一分鐘內的工作先後與重試無法保證這點；(2) 收集狀態可以直接寫進同一份 brief 的 `news.api_sources` 與警告，不必再讀另一份狀態檔；(3) 不動 `scheduler.py`、`config.py`，和其他分支不衝突。代價是隔夜重點工作多花約 15 次請求的時間（數秒），可接受。
- 收集器登記在 `overnight_brief.API_NEWS_COLLECTORS`（來源名 → 模組名），其他來源要接時各加一行即可，可加總合併。模組須提供 `collect(root, *, now) -> dict`。
- `--no-collect` 可在手動重跑時略過收集。
- 收集失敗（例外、`error`、`partial`、`quota_exceeded`）只記成警告，隔夜重點照常產生；`skipped`（沒金鑰）不記警告。
- RSS 缺口偵測（`detect_gaps`）與 RSS 存檔新舊警告只看 RSS；API 新聞只進候選。
- 合併時 RSS 在前，同 url 以 RSS 那則為準。
- `news.items_in_window` 維持「進入候選計算的總筆數」（RSS＋API），另加 `rss_items_in_window`、`api_items_in_window` 與 `api_sources`。
- 候選分數不變（API 新聞不額外加分，避免每則公司新聞都因有代號而被抬高）；候選另加 `us_tickers` 欄位（群內聯集）。
- **多檔代號改成合併：** 同一則新聞在 NVDA、AMD 查詢都出現時，原本只留第一個代號；改為保留第一則、`us_tickers` 取聯集（與同日既有檔合併時也一樣）。原因：只留一個代號會讓候選的 `us_tickers` 漏掉實際相關的標的，而去重鍵（url）不變，不會多出重複項目。`feed` 仍是第一個查詢的 `finnhub:<代號>`。

### 驗收標準

| # | 標準 | 驗證 |
|---|---|---|
| B1 | `read_api_news` 讀視窗涵蓋日期的 `data/news/api_<來源>_<日期>.json`（任何來源），回傳項目與各來源的檔案數、筆數；損毀檔或非 list 視為空、不報錯；`rss_*.json` 不會被讀成 API 檔 | 測試 |
| B2 | API 新聞與 RSS 合併進視窗候選；時間不在視窗內者排除；同 url 的 RSS 與 API 只算一則 | 測試 |
| B3 | `detect_gaps` 只看 RSS：有 API 新聞時，RSS 的缺口仍照樣偵測出來 | 測試 |
| B4 | brief 的 `news` 含 `rss_items_in_window`、`api_items_in_window`、`api_sources`（每個來源有 `items_in_window`、`files`、`collect`） | 測試 |
| B5 | 候選有 `us_tickers`（群內聯集） | 測試 |
| B6 | `main` 先跑收集器再產生 brief，收集摘要寫進 `api_sources[來源].collect` | 測試 |
| B7 | 收集器拋例外或回傳 `error`／`partial`／`quota_exceeded` 時，brief 照常產生、`main` 回傳 0，只多一條警告；`skipped` 不加警告 | 測試 |
| B8 | `--no-collect` 不跑收集器 | 測試 |
| B9 | 沒有金鑰時 Finnhub 收集為 `skipped`，不連網 | 測試（沿用 A1）＋實測 |
| B10 | 同一則新聞在多檔查詢都出現時，保留一則、`us_tickers` 取聯集；與既有檔合併時也一樣 | 測試 |
| B11 | brief 與 `main` 輸出、警告都不含金鑰 | 測試＋實測 |
| B12 | 暫存 root 實測：收集 → 隔夜重點，候選裡有 `finnhub:` 來源的新聞，輸出不含金鑰 | 實測 |
| B13 | 候選的 `source_count` 與分數裡的來源數：API 新聞以發布者（`source` 欄）計，RSS 仍以 feed 計；同一發布者在多檔查詢（`finnhub:NVDA`、`finnhub:AMD`）出現只算一個來源。`feeds` 仍列出所有 feed | 測試＋實測 |

B13 是實測後補的：第一次實測時排第一的候選 `source_count` 是 5，但全部都是 Finnhub、只是查詢代號不同，分數被灌高。
