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
