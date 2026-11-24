# 免費資料來源接入方案（2026-11-24 規劃，尚未實作）

使用者決定：七家的免費方案都要接。這份文件是每一家的使用方案，供另一個對話實作。

## 共通原則

- 帳號由使用者自己註冊，金鑰由使用者自己貼進 `.env`。程式只從 `Settings` 讀，沒有金鑰時該來源直接略過、不算失敗。
- 七家都還沒有任何程式（`src/bot` 裡搜尋不到這些名稱）。HTTP 用既有的 `requests`，不新增套件。
- 每一家都要有每日用量上限的保護（自己計數，存在 `data/` 底下），不要靠對方回錯誤才停。
- 免費方案都限個人、非商業使用；不可轉散布原始資料。
- 下面標「未確認」的項目，官方頁面這次沒讀到，實作前要用金鑰實際打一次確認。

## 建議的實作順序

1. Finnhub（補夜間新聞缺口，最直接解決使用者的需求）
2. 富果（盯盤報價備援；11-24 1514 從 10:12 起拿不到當日報價，原因未查，要先查原因）
3. FinMind（台股交叉核對）
4. FMP（美股財報）
5. Massive、Marketaux、Alpha Vantage（價值較低，最後做）

## 一、Finnhub —— 美股新聞，補夜間缺口

| 項目 | 內容 |
|---|---|
| 金鑰變數 | `FINNHUB_API_KEY` |
| 傳金鑰 | 查詢參數 `token=`，或標頭 `X-Finnhub-Token` |
| 免費額度 | 每分鐘 60 次（第三方資料，官方頁未確認）；另有全域每秒 30 次上限；超過回 HTTP 429 |
| 端點 | `GET https://finnhub.io/api/v1/company-news?symbol=NVDA&from=YYYY-MM-DD&to=YYYY-MM-DD`（只限北美公司，免費版 1 年歷史）；`GET /api/v1/news?category=general&minId=`（只有最新的，不能指定日期） |
| 回傳欄位 | `category`、`datetime`（UNIX 秒）、`headline`、`id`、`related`、`source`、`summary`、`url`（REST 端點的欄位未逐一確認） |

用法：
- 每個平日 08:00 隔夜重點產生前，對 `market_macro.DEFAULT_STOCKS` 的 14 檔查前一個平日到今天的公司新聞，再查一次 `general` 大盤新聞，一天約 15 次，08:50 再一次共約 30 次。
- 轉成和 RSS 存檔相同的項目格式，存到另一個檔（建議 `data/news/api_<日期>.json`），`feed` 寫成 `finnhub:NVDA` 這類，另加 `us_tickers`。不要混進 `rss_<日期>.json`，盤中情緒判斷（`smile_sentiment`）的算法才不會跟著變。
- `overnight_brief` 讀候選時把這個檔一起讀進來。公司新聞可以指定日期，所以電腦關機那晚隔天補得回來；大盤新聞補不回來。
- 注意：公司新聞是「提到這家公司的新聞」，量大的股票一天可能上百則，要沿用現有的歸群與去重。

## 二、富果 Fugle —— 台股即時報價備援

| 項目 | 內容 |
|---|---|
| 金鑰變數 | `FUGLE_API_KEY` |
| 傳金鑰 | 標頭 `X-API-KEY` |
| 免費方案 | 「基本用戶」，日內行情每分鐘 60 次；不含行情快照、技術指標、籌碼 |
| 端點 | `GET https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{代號}`（零股加 `?type=oddlot`） |
| 主要欄位 | `lastPrice`（含試撮）、`lastTrade.price`、`lastTrade.time`（16 位數，推算是微秒，文件沒寫單位）、`previousClose`、`referencePrice`、`openPrice`、`highPrice`、`lowPrice`、`total.tradeVolume`、`bids[]`、`asks[]`、`isTrial`、`tradingHalt.isHalted` |
| 錯誤 | 401 金鑰錯、403 方案不含、404 代號或日期無效、429 超過頻率 |
| 未確認 | 申請金鑰需不需要富果／玉山證券帳戶；當天沒有成交的股票回傳長什麼樣 |

用法：
- 只當備援：`smile_watch` 的 `_usable_price` 拿不到當日報價時（`src/bot/smile_watch.py` 約第 331、455 行）才查富果，查到就用，並在日誌與狀態檔註明報價來源。
- 要用 `lastTrade.price` 而不是 `lastPrice`（後者含試撮價），並檢查 `lastTrade.time` 是今天、`isTrial` 為 false、沒有暫停交易。
- 盯盤每 20 秒一輪、一檔一次，一分鐘 3 次，遠低於上限。
- 先查 11-24 1514 拿不到報價的原因。如果是那檔當時沒有成交，備援也拿不到，接了沒有用。
- 盯盤只提醒、不下單；這個改動不可以碰下單路徑。

## 三、FinMind —— 台股財報、月營收、籌碼的第二來源

| 項目 | 內容 |
|---|---|
| 金鑰變數 | `FINMIND_TOKEN` |
| 傳金鑰 | 標頭 `Authorization: Bearer <token>`（也可用查詢參數 `token`） |
| 免費額度 | 註冊並驗證信箱後每小時 600 次（未註冊 300 次）；超過回 HTTP 402 |
| 查剩餘額度 | `GET https://api.web.finmindtrade.com/v2/user_info`，看 `user_count` 與 `api_request_limit` |
| 端點 | `GET https://api.finmindtrade.com/api/v4/data?dataset=…&data_id=2330&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`，資料在回傳的 `data` 陣列 |
| 免費可用的資料集（要帶 `data_id`，不帶代號的全市場查詢要付費） | `TaiwanStockMonthRevenue`、`TaiwanStockFinancialStatements`、`TaiwanStockBalanceSheet`、`TaiwanStockCashFlowsStatement`、`TaiwanStockInstitutionalInvestorsBuySell`、`TaiwanStockPrice` |

欄位要注意的地方：
- 月營收：`date` 是營收月份的「下一個月 1 日」（例如 `date=2019-04-01` 是 2019 年 3 月的營收），要用 `revenue_year`、`revenue_month` 對月份。
- 三張財報是長格式（`date`、`stock_id`、`type`、`value`、`origin_name`，一個科目一列）。損益表是單季數字，現金流量表是年初累計，金額單位是元。
- `TaiwanStockPrice` 的成交量單位是股。

用法：
- 定位是「交叉核對與備援」，不取代現有來源（公開資訊觀測站、證交所）。
- 每天一次，對持股（`data/held_symbols_seen.json`）與追蹤名單查最近 3 個月的月營收、最近 4 季三張財報，和資料庫的 `monthly_revenue`、`quarterly_report`（`src/bot/stock_db.py` 第 790、848 行）比對。不一致或資料庫缺的，寫進一份核對報告，並接到資料新鮮度檢查（`bot.data_freshness`）當一項提醒。
- 用量：每檔 4 個資料集，約 280 檔的話是 1,100 多次，要分兩個小時以上跑，或只查持股加當週有新資料的標的。先從持股（個位數檔）開始。
- 授權要求標示資料來自 FinMind，報告裡要寫來源。

## 四、Financial Modeling Prep —— 美股財報

| 項目 | 內容 |
|---|---|
| 金鑰變數 | `FMP_API_KEY` |
| 免費額度 | 每天 250 次、只有收盤後資料、30 天流量 500MB（都是第三方資料） |
| 未確認 | **官方文件整站回 HTTP 403，這次完全沒讀到。** 端點路徑（推測是 `https://financialmodelingprep.com/stable/income-statement?symbol=AAPL&period=annual&limit=5&apikey=…`，另有 `balance-sheet-statement`、`cash-flow-statement`）、欄位拼法、免費版能不能查季報、涵蓋哪些代號、超過額度的回應，全部要用金鑰實際打一次確認 |
| 已知限制 | 免費版沒有新聞；第三方資料說免費與 Starter 只有年報 |

用法：
- 每週一次（例如週六），對 `market_macro.DEFAULT_STOCKS` 的 14 檔抓損益表、資產負債表、現金流量表，約 42 次。
- 存到新的目錄（建議 `data/us_fundamentals/<代號>.json`），原始回應用既有的 `SourceRegistry` 保存。
- 用途是給之後的模型摘要與個股研究當背景（營收、毛利、每股盈餘、自由現金流的趨勢），不進任何交易規則。
- 第一步先寫一個只打一檔的探測指令，確認端點與欄位再寫正式的。

## 五、Massive（原 Polygon.io）—— 美股個股新聞的第二來源

| 項目 | 內容 |
|---|---|
| 金鑰變數 | `MASSIVE_API_KEY` |
| 傳金鑰 | 查詢參數 `apiKey=`，或標頭 `Authorization: Bearer` |
| 端點 | `GET https://api.massive.com/v2/reference/news?ticker=NVDA&published_utc.gte=…&limit=1000`，用回傳的 `next_url` 翻頁 |
| 欄位 | `results[]`：`id`、`publisher.name`、`title`、`published_utc`（RFC3339）、`article_url`、`tickers[]`、`description`、`keywords`、`insights[]`（`ticker`、`sentiment`、`sentiment_reasoning`） |
| 免費方案 | Stocks Basic 含新聞、2 年歷史、每小時更新一次 |
| 未確認 | 免費方案每分鐘幾次（舊的 Polygon 是每分鐘 5 次）；`published_utc` 參數的格式；超過頻率的狀態碼 |

用法：
- 和 Finnhub 做同一件事，存進同一個 `api_<日期>.json`，`feed` 寫 `massive:<發行者>`。
- 因為頻率可能很低，不要逐檔查：一次不帶 `ticker`、只帶時間範圍查全部新聞，一天 08:00 與 08:50 各一到兩次。
- 多出來的價值是 `insights` 裡的個股情緒與理由，可以存下來給模型摘要當參考。
- 每小時才更新，所以不適合盤中使用。

## 六、Marketaux —— 持股相關新聞的情緒分數

| 項目 | 內容 |
|---|---|
| 金鑰變數 | `MARKETAUX_API_TOKEN` |
| 傳金鑰 | 查詢參數 `api_token=` |
| 免費額度 | 每天 100 次、每次最多 3 則 |
| 端點 | `GET https://api.marketaux.com/v1/news/all?symbols=TSM,NVDA&filter_entities=true&language=en&published_after=2026-11-23T21:00` |
| 欄位 | `meta`（`found`、`returned`、`limit`、`page`）；`data[]`：`uuid`、`title`、`description`、`snippet`、`url`、`published_at`、`source`、`entities[]`（`symbol`、`name`、`match_score`、`sentiment_score`） |
| 錯誤 | 402 當日用量用完、429 一分鐘內太多次、403 方案不含、401 金鑰錯 |

用法：
- 一天最多 300 則，不能當主要新聞來源。只查「和持股有關的美股」：台積電 ADR（TSM）、NVDA、AMD、MU 等少數幾檔。
- 每天 08:00 前每檔查 2 頁（6 則），取 `sentiment_score` 的平均，存成一個「個股隔夜情緒」的小表，放進隔夜重點檔。約 10～20 次。
- 分數是對方算的、方法不透明，只當參考，不進交易規則。

## 七、Alpha Vantage —— 新聞情緒對照

| 項目 | 內容 |
|---|---|
| 金鑰變數 | `ALPHAVANTAGE_API_KEY` |
| 免費額度 | 每天 25 次 |
| 端點 | `GET https://www.alphavantage.co/query?function=NEWS_SENTIMENT&topics=financial_markets&time_from=YYYYMMDDTHHMM&limit=50&apikey=…` |
| 可用主題 | `earnings`、`financial_markets`、`economy_fiscal`、`economy_monetary`、`economy_macro`、`technology`、`manufacturing` 等 15 種 |
| 欄位 | `feed[]`：`title`、`url`、`time_published`（如 `20261123T223518`）、`summary`、`source`、`overall_sentiment_score`、`overall_sentiment_label`、`ticker_sentiment[]`（`ticker`、`relevance_score`、`ticker_sentiment_score`，後兩個是字串） |
| 最大的風險 | **官方文件把這個功能標成 premium。免費金鑰很可能不能用。** 拿到金鑰後先打一次；不能用就不接這一家 |
| 未確認 | 超過額度時的回應（通常是 HTTP 200 加一個說明用的鍵，不是錯誤狀態碼，要檢查回應裡有沒有 `feed`） |

用法（前提是免費金鑰能用）：
- 每天 08:00 查三次：`financial_markets`、`technology`、`economy_monetary`，各取最新 50 則。
- 算出整體情緒分數的平均與看多／看空則數，存進隔夜重點檔，當成現有關鍵字情緒分數（`smile_sentiment`）的對照。目的是累積資料，之後檢驗哪一種分數比較準；不直接用來做決策。

## 需要使用者準備的金鑰

| 服務 | `.env` 變數名稱 |
|---|---|
| Finnhub | `FINNHUB_API_KEY` |
| 富果 | `FUGLE_API_KEY` |
| FinMind | `FINMIND_TOKEN` |
| Financial Modeling Prep | `FMP_API_KEY` |
| Massive | `MASSIVE_API_KEY` |
| Marketaux | `MARKETAUX_API_TOKEN` |
| Alpha Vantage | `ALPHAVANTAGE_API_KEY` |

變數名稱是這份規劃訂的，程式還沒有對應的設定欄位；實作時要加進 `src/bot/config.py` 與 `.env.example`。
