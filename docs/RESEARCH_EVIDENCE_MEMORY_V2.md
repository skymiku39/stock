# Stock LLM／Evidence／Memory v2 實作說明

本文件是 `Stock LLM／Evidence／Memory 完整化計畫` 的落地說明。系統維持
SQLite 為 SSOT、FTS5 為可重建索引、raw archive 為不可變來源；沒有加入下單
路徑，也沒有把 Graphiti、Mem0 或 GraphRAG 當成核心依賴。

## 實際資料流

```text
QueryPlan
  -> local canonical / short-long memory / historical evidence
  -> conditional ResourceAdapter (news/web/MOPS)
  -> EvidencePack
  -> LLM JSON claims
  -> evidence / numeric / date / conflict verification
  -> verified llm_runs + claims + optional memory mirror
```

`source_document_id` 只接受呼叫端已知的 exact ID。系統不會依 URL、標題或
文字相似度猜原始文件；找不到來源時保留空值並由 audit/backfill 顯示缺口。
`SourceRegistry.materialize_research_document()` 可從已知 raw manifest 建立
`research_documents` 與可重建的 `research_chunks`，embedding 欄位目前只保留
契約，沒有引入向量資料庫。

### MOPS 官方垂直切片（方案 B／B.1）

目前已對 **單一官方來源** `mops_material_detail` 打通：

```text
raw archive (data/raw/mops_material_detail/<sha256>.html)
  -> source_documents
  -> research_documents + research_chunks
  -> evidence_items (provenance_status=exact, source_document_id 必填)
  -> llm_runs (task=mops_material_detail_ingest，status=source_ingested)
  -> claims (source_excerpt；換行／Tab／多餘空白正規化後可核對才 verified)
```

入口：

- `bot.mops_scraper.ingest_material_detail(...)`（失敗時 `research_status=failed` + `research_error`）
- `bot.mops_research.materialize_mops_detail_slice(...)`（`status`=`ok`／`reused`／`failed`；重跑冪等）

驗收加固（方案 B.1）：

- ingest run 使用 `source_ingested`，與 `!ask` answer run 的 `verified` 分開
- claim 核對容忍換行、Tab、多餘空白（不做語意推論）
- `MopsAdapter` 優先重用 research evidence，避免對同一 `source_document_id` 再造 news 重複證據

`source_document_id` 只接受已知 exact ID；不會依 URL／標題猜測。

### MOPS 法說會簡報垂直切片

`mops_scraper.download_presentation()` 現在在下載或讀取既有檔案後，會用檔案
SHA-256 回查 `mops_presentation`／`mops_calendar_pdf` 的 exact manifest；找不到
manifest 時只回傳 `research_status=skipped`，不猜來源。找到後流程為：

```text
raw PDF/PPTX bytes
  -> source_documents
  -> pypdf/text extraction
  -> research_documents + research_chunks (document_type=mops_presentation)
  -> evidence_items (provenance_status=exact)
  -> llm_runs (task=mops_presentation_ingest，status=source_ingested)
  -> source_excerpt claim（可重播摘錄才 verified）
```

入口：`bot.mops_presentation_research.materialize_mops_presentation_slice(...)`。
抽取結果保存頁數、抽取方法、ticker 與簡報日期；此切片不把簡報文字自動
延伸成市場結論。目前已有一筆實際 MOPS 簡報 production replay；稽核發現其中一個
連結雖以 `.pdf` 目的名保存，實際回傳 HTML。該既有 derived chain 已 quarantine，並
由真正的 27 頁 MOPS PDF 建立新的 exact research chain；新的 `mops_presentation`
入口也會拒絕這類 HTML。來源層會區分「抓取版本」與「資料有效期間」：`retrieved_at`
可保存不同 raw snapshot 的 exact 版本，但不能取代 `as_of`，也不會把抓取時間冒充
市場資料日期。無法以任何可解析時間區分的多版本才列為未版本化警告；同期間內容不同
則保留為衝突候選，不覆蓋歷史 hash。PDF／
簡報的來源專用日期、單位與衝突驗證仍需更多不同格式的實際資料。

### 月營收垂直切片（方案 C）

目前已對 **單一基本面切片** TWSE／TPEx OpenAPI 月營收打通（canonical table
`monthly_revenue`）。個股 refresh 取得 raw response 後會沿用 exact
`source_document_id` 寫入 canonical／research／evidence；不會只停留在
`data/fundamentals/*.json` 快取。籌碼目前先完成多來源 evidence links，ETF holdings 已接入獨立的
canonical snapshot → research → evidence → claim 切片。

```text
raw archive (data/raw/monthly_revenue/<sha256>.json)
  -> source_documents（exact source_document_id）
  -> monthly_revenue（canonical SQLite；單位=新台幣千元）
  -> research_documents + research_chunks（canonical JSON + field_defs）
  -> evidence_items（kind=monthly_revenue，provenance_status=exact）
  -> llm_runs（task=monthly_revenue_ingest，status=source_ingested）
  -> claims（source_fact：營收／YoY／MoM／年月／單位；可核對才 verified）
  -> MonthlyRevenueAdapter（resource=fundamental）→ !ask
```

#### 資料契約

| 欄位 | 語意 |
|---|---|
| `symbol` | 股票代號 |
| `year_month` / `as_of` | 營收所屬年月（ISO `YYYY-MM`），不是抓取日 |
| `revenue` | 當月營收；單位 `TWD_thousand`（新台幣千元） |
| `yoy_pct` / `mom_pct` | 年增／月增率（%） |
| `retrieved_at` | 本機抓取時間；嚴格 freshness 優先看此欄 |
| `published_at` / `source_date` | 來源資料日（若 raw 有提供） |
| `source_record_id` | 必須等於 exact `source_document_id`；否則 ask 標 `unresolved` |

#### 失敗／空結果狀態

| status | 意義 |
|---|---|
| `ok` | 新建切片成功 |
| `reused` | 同 source／內容重跑冪等命中 |
| `failed` | 來源缺失、hash 不符、品質不可用等 |
| `empty` | 來源成功但 raw 無該 ticker 列（**不是**查無此事） |
| `stale_skipped` | 既有 `retrieved_at` 較新，拒絕用較舊資料覆寫 |

入口：

- `bot.fundamentals_fetcher.fetch_monthly_revenue(...)`（正式個股 refresh 入口；
  raw manifest 成功時自動重播 slice）
- `stock-fundamentals-refresh`（透過 `build_fundamental_snapshot` 使用上述入口）
- `bot.monthly_revenue_research.ingest_monthly_revenue_from_source_document(...)`
- `bot.monthly_revenue_research.materialize_monthly_revenue_slice(...)`
- `bot.resource_adapters.MonthlyRevenueAdapter`（僅當 QueryPlan 含 `fundamental`）

限制：不自動寫入長期記憶；不產生看多／看空 claim；不修改交易／下單模組。
既有 `monthly_revenue` 列若無證明得出的 `source_document_id`，維持
`provenance_status=unresolved`，不補猜。

### 日 K provenance 垂直切片

TWSE／TPEx 日 K 的正式抓取入口也會保存月份級 immutable raw response，並把
exact `source_document_id`、來源 URL、資料月份、抓取時間與品質狀態寫入
`price_history`。因此 ticker snapshot／`!ask` 讀取最新 K 線時，可以回到原始
API response；不會依日期、URL 或相似內容反查或猜測來源。

```text
TWSE／TPEx stock day API response
  -> data/raw/<source>/<sha256>.json
  -> source_documents
  -> price_history（source_record_id=exact source_document_id）
  -> local ticker／technical evidence（composite_exact + source_document_ids）→ !ask
```

若走 yfinance 備援，資料會標示 `source=yfinance`、`quality_status=partial`，且
不填造 `source_document_id`；這類資料只能作為有限補充，不能宣稱為
TWSE／TPEx exact 官方證據。舊有未帶 provenance 的 K 線不會被日期或 URL 回填猜測；
若要補齊，必須重新抓取並驗證原始回應。

### 季報 EPS／三率垂直切片

季報抓取原本只保留 `quarterly_financials_all.json`；目前正式 refresh 會將每個
產業 endpoint 的原始回應保存，再把個股解累計後的季報寫入
`quarterly_report`，並建立同一個 exact source document 對應的 research、evidence
與 source-fact claims。手動匯入或舊快取沒有 exact ID 時仍可供相容 API 使用，
但不會被升格為已驗證的市場證據。

```text
TWSE／TPEx 季報 OpenAPI
  -> source_documents（每個 endpoint 的 raw response）
  -> quarterly_report（解累計後的單季 EPS／三率）
  -> research_documents + research_chunks
  -> evidence_items(kind=quarterly_report)
  -> source-fact claims → !ask
```

目前季報垂直切片只產生可由 canonical JSON 直接核對的欄位事實；不把
解累計結果延伸成獲利預測或投資結論。

### 每日估值垂直切片

TWSE `BWIBBU_ALL` 與 TPEx valuation response 會先保存 raw，再將個股估值快照
寫入 `valuation_daily`。目前接入的事實欄位為資料日、PER、PBR、殖利率與股利年度；
每筆 canonical row、research document、evidence 與 claims 都保留 exact
`source_document_id`。錯誤或異常極端殖利率仍會保留 raw，但不進入可用估值 row。

### 年度股利垂直切片

TWSE／TPEx 股利分派資料現在會由 raw response 進入
`dividend_history`，再建立年度股利的 research document、evidence 與可核對的
現金／股票股利 claims。若同一年度彙總跨越多個無法合併成單一 exact 文件，
系統會保留資料但清除 `source_document_id`，避免替彙總結果猜一個來源。

### MOPS 法說會行事曆垂直切片

法說會行事曆現在會把已保存的月份快照以 exact `source_document_id` 接入研究層。
每筆活動只產生可由同一份 raw JSON 核對的 `source_fact` claim；空白快照標為
`empty`，不轉成「沒有法說會」，也不寫入虛構事件。活動以日期、時間、ticker、公司
組合去重；同一份 raw snapshot 重跑會回傳 `reused`，不會重複建立 evidence。

```text
MOPS conference calendar response
  -> immutable raw JSON / source_documents(kind=event_snapshot)
  -> research_documents/chunks(document_type=mops_conference_calendar)
  -> evidence(kind=mops_conference_calendar, provenance=exact)
  -> llm_runs(status=source_ingested)
  -> claims(source_fact, verified only when event is in raw)
```

入口為 `bot.conference_calendar._save_month` 與
`bot.mops_conference_research.materialize_mops_conference_calendar_slice(...)`。
切片不呼叫 LLM、不產生市場方向結論、不修改交易／下單流程；原始 MOPS HTML
來源與月份 JSON 快照會各自保留，不以相似度合併或猜測來源。

### 新聞日快取垂直切片

鉅亨日快取現在會以 immutable raw JSON 建立一份 `news_cache` research document，
每則新聞建立 exact `news` evidence 與可由標題在 raw archive 核對的
`source_excerpt` claim。新聞快取與 raw archive 保持相同內容；載入時以 exact
`source_ref=news:<date>` 回查 manifest，不把 provenance ID 回寫進 raw payload。

```text
raw archive (data/raw/cnyes/<sha256>.json)
  -> source_documents(kind=news_cache)
  -> research_documents/chunks(document_type=news_cache)
  -> evidence(kind=news, source_document_id=exact)
  -> llm_runs(status=source_ingested)
-> claims(source_excerpt, verified only when title is in raw)
```

### Google News／DuckDuckGo 搜尋回應垂直切片

搜尋 adapter 保存的 Google News RSS 與 DuckDuckGo HTML response 現在也會接入
research layer。這裡的 claim 僅代表「搜尋回應中確實出現某標題／摘要」，不是該文章
全文，也不代表搜尋引擎摘要本身就是官方事實；標題無法在 exact raw archive 核對時，
claim 維持 `unverified`。

```text
Google News RSS / DuckDuckGo HTML
  -> source_documents(kind=search_response, exact hash)
  -> research_documents/chunks(document_type=news_search / web_search)
  -> evidence(kind=news / web_search, provenance=exact)
  -> llm_runs(status=source_ingested)
  -> claims(source_excerpt, verified only when title is in raw)
```

入口為 `bot.web_search_research.materialize_web_search_slice(...)`；既有 raw 可由
`replay_web_search_document(...)` exact-only 重播。沒有依 URL、標題或相似度猜測
`source_document_id`，也不會因搜尋結果缺少摘要而產生市場方向結論。

### Macro snapshot 垂直切片

跨市場 macro 快照以同一份 raw JSON 建立 `macro_snapshot` research document，
並只產生可由 JSON 欄位直接核對的日期、來源日期狀態與資料筆數 claims；不把
ADR 溢價或跨市場數值延伸成預測或投資結論。`!ask` 使用與切片相同的摘要格式，
以避免同一份 raw snapshot 產生重複 evidence。

TAIFEX `DailyMarketReportFut` 現行回應可能是 `application/octet-stream` 的 UTF-8
CSV，而非 JSON；adapter 會依實際格式解析 TX 近月、結算價與未平倉量，並以
`source_ref=taifex:<as_of>:<contract_month>` 保存 exact raw document。解析失敗不會
把空結果當成「沒有期貨資料」，而會保留來源失敗狀態供驗證與後續重試。

### TAIFEX TX 研究垂直切片

TAIFEX raw capture 現在已接上獨立的 research/evidence/claim slice：

```text
TAIFEX DailyMarketReportFut (JSON／UTF-8 CSV)
  -> source_documents（exact hash）
  -> research_documents/chunks (document_type=taifex_futures)
  -> evidence_items(kind=taifex_futures, provenance_status=exact)
  -> llm_runs(status=source_ingested)
  -> claims(source_fact：合約／月份／價格／未沖銷量／日期)
```

入口是 `bot.taifex_research.materialize_taifex_slice(...)`；正式的
`fetch_tx_futures_basis(root=...)` 在保存 raw 後會自動呼叫它。研究切片會先驗證
raw archive SHA-256，且同一份 source／canonical 內容重跑會回傳 `reused`。
期貨正逆價差、現貨與狀態屬於使用外部現貨輸入的衍生計算，會保留在 canonical
JSON 的 `calculated` 區塊，但不會被冒充成 TAIFEX 原始 claim；只有能從 raw
逐欄核對的來源事實才標記 `verified`。

### 自動研究與網頁原文 provenance

`auto_llm` 組合 MOPS 詳情時會沿用 `ingest_material_detail` 產生的 exact
research evidence；新聞列若已有新聞日快取的 exact evidence，也會重用同一個
evidence ID。網頁原文 `PageContent` 會保留 `web_page` raw document ID，讓自動
研究保存的網頁 evidence 可以回到原始 HTML；缺少 raw manifest 時仍維持
`unresolved`，不依 URL 補造來源。

### 快取型來源的 provenance 傳遞

新聞與 macro 的既有快取不把 manifest ID 寫入 replay payload；載入時以 exact
`source_ref` 回查並在記憶體／EvidencePack 帶出 CNYES 或 macro snapshot 的
document ID。`ask_adapters` 只有在缺少 exact ID 時才標示 `cache_only`，不會把
已保存來源無故降級，也不會依 URL／標題自行補造 ID。籌碼摘要由多個 endpoint／交易日
組成，因此使用 `provenance_status=composite_exact`，並在 metadata 與
`evidence_links` 保存全部 exact source document IDs；週末唯讀查詢會回退到
最近已保存交易日並保留 `as_of`。ETF 共識在 holdings raw 存在時也採
composite exact（每個參與的 ETF 快照各自連到 source document）；尚未取得
holdings raw 時仍是 cache-only，不提供臆測結果。ETF 持股切片會保留抽取方法、
raw source ID 與 claim 支持狀態；只有持股識別與權重都能在 raw archive 核對時
才標成 `verified`，LLM／解析結果無法核對時維持 `unverified`。

目前已用 `ALI006` MoneyDJ 基金頁完成不依賴 LLM 的 production replay，並以
etfinfo.tw Nuxt payload 完成全清單 ETF 的 deterministic replay。六檔原先使用
MoneyDJ top-N 的 ETF，現在已取得可重播的完整持股陣列：`00998A` 50、`00402A` 49、
`00404A` 53、`00405A` 47、`00406A` 62、`00407A` 50 檔股票曝險；每檔都建立
exact source document、research document、evidence 與 claims。
`00404A`／`00406A` 的選擇權／衍生品列仍保留於 raw source，但明確排除於股票持股
模型，不把負權重誤當成股票持倉；未知類型的 null 或負權重仍拒絕解析。全清單
production batch 為 28/28 成功、0 warning；`source_incomplete_top_n` 保護邏輯仍
保留，防止未來來源退化時由 LLM 假補完整 holdings snapshot。

#### 尚未處理的其他來源

- 法說會 PDF 的來源專用日期／單位／衝突驗證
- Graphiti／Mem0／GraphRAG
- 大規模歷史回填

## v2 schema

新增的 local-only tables：

- `research_documents`、`research_chunks`
- `evidence_links`
- `llm_runs`、`llm_run_evidence`、`llm_outputs`
- `claims`、`claim_evidence`
- `ask_runs`（同一 trace 的後續補抓上下文）

既有 `evidence_items` 增加 `source_document_id`。舊欄位與既有 DAO 保持相容；
`schema_migrations` 目前會記錄 v1 至 v5。舊版 `schema_version()` 仍回報 v1
compatibility baseline，新的實際版本使用 `latest_schema_version()` 或直接查看
`schema_migrations`，避免舊 dashboard 失效。

## `!ask` 即時來源政策

預設仍是本地優先且不打外網。只有 `enable_live_sources=True` 或設定
    `STOCK_ASK_LIVE=1`，而且 QueryPlan 要求 `news`／`mops`／`web_search`／
    `professional_knowledge`／`ticker_snapshot`
且不是歷史 `unbounded` 查詢時，才會啟用 bounded adapters。第一批已接入：

- `WebSearchAdapter`：DuckDuckGo / Google News 的現有 raw-first fetcher
- `ProfessionalKnowledgeAdapter`：只接受 allowlist 官方／監管／學術網域；頁面原文
  優先，搜尋摘要只能標記為 `partial`，一般搜尋結果不會冒充專業證據
- `MopsAdapter`：既有 MOPS/TWSE 重大訊息與 detail fetcher
- `LiveTickerAdapter`：TWSE MIS 公開延遲報價；raw response 先進 source registry，
  quote date/time 仍由 freshness／temporal policy 驗證，不把延遲報價當成即時保證
- `MonthlyRevenueAdapter`：本地 `monthly_revenue` 垂直切片（exact 優先；無
  source_document_id 則 unresolved）
- `taifex`：市場級台指期資源；直接重用 `taifex_futures` exact evidence，不需要
  ticker，也不把 macro cache 的衍生價差當成 TAIFEX 原始證據
- canonical snapshot、technical、ETF、macro 仍由現有 local/cache adapters
  提供；technical／ticker snapshot／fundamental 在資料對齊且 raw 存在時使用
  `composite_exact`，macro adapter 已連到 exact raw snapshot，chips／ETF adapter
  已支援 composite exact raw provenance；股利已納入 `dividend_history` research slice，
  並可由 `stock-fundamentals-refresh --materialize-all` 以 exact raw archive 做全市場
  canonical／research／evidence／claim 回填。
- memory、historical evidence 由 SQLite evidence/memory DAO 提供

即時來源使用約 8 秒總預算；逾時、錯誤、空結果都以 warning/status 表示，
不會轉成「沒有這件事」。`AskResult.pending_resources` 與 `ask_runs` 保存未
完成資源；可呼叫 `AskEngine.follow_up(trace_id)` 或：

```powershell
uv run stock-ask --follow-up TRACE_ID --live --root C:\skymiku\stock
```

後續補抓沿用原 QueryPlan 與 `trace_id`，不建立另一筆無關對話。

## `!ask` 狀態語意

回答結果的 `status` 與 EvidencePack 的 `collection_status` 必須一致可追蹤，
timeout／failed **不得**被表示成「沒有這件事」：

| status | 意義 |
|---|---|
| `no_evidence` | 來源已成功完成，但沒有找到可用資料 |
| `resource_failed` | 資料來源明確失敗 |
| `timeout` | 資料來源逾時 |
| `live_disabled` | 即時來源未啟用（需 `STOCK_ASK_LIVE=1` 或 `--live`） |
| `partial` | 任一來源明確只取得部分內容，或部分來源成功、部分失敗／逾時／未啟用 |
| `uncertain` | 最終無法產生可驗證回答（例如 LLM 未啟用） |
| `answered` | 通過驗證的可發布回答 |
| `verification_failed` | 有證據但回答驗證失敗 |

`pending_resources`、`warnings`、`ask_runs.status`、EvidencePack 與 CLI JSON
共用同一套狀態。`follow_up(trace_id)` 沿用原 QueryPlan／trace，並保留
`resource_outcomes` 與證據鏈。

## 品質與溯源分離

每筆 evidence 同時保存：

- `quality_status`：內容是否通過品質檢查（`ok`／`partial`／…）
- `provenance_status`：`exact`／`composite_exact`／`cache_only`／`unresolved`／`legacy_unlinked`
- `source_document_id`：只有確定存在的 exact ID 才填入，永不依 URL／標題臆造

一般 cache-only 資料可以 `quality_status=ok`，並標
`provenance_status=cache_only`，回答提示中會標明「非官方 raw document」。
例外是 canonical row 已存在、但 `source_record_id` 缺失或 raw 不可回放的
月營收：`MonthlyRevenueAdapter` 必須清除不可證明的來源 ID，將 packet 的
`provenance_status` 保持為 `unresolved`，並以 `metadata.cache_only=true` 表示
它只能作為本地快取內容；兩者都不得升格為 `exact`。

自動研究升級長期記憶時，會再檢查 evidence 是否已登錄中央 registry、
是否為 `exact`／`composite_exact`，以及所有 source document ID 是否存在。
不合格 evidence 不會跟著研究摘要進入長期記憶；若完全沒有合格 evidence，
只保留研究 history／cache 並跳過長期升級。

## 對話記憶隔離

`conversation_turn` 可作後續對話上下文，但：

- 不可直接作為股價／營收／估值／新聞等市場事實證據
- 即使 `quality_status` 為 `partial`／`ok` 也不可升格為 market fact
- 只有使用者明確詢問歷史對話時，才會以 `kind=conversation` 進入 EvidencePack
- verifier 會拒絕把對話記憶引用為 fact claim

## Claims 驗證

回答只有在 EvidencePack 中存在可用 evidence ID、claims 逐項引用，且回答正文中
帶單位／百分比／小數的數值與完整日期也能在 cited evidence 原文核對，且沒有未標示的來源衝突時才可發布。預測必須明確使用「可能／
預測／估計」等標記。驗證最多修復一次；仍失敗就保守回答「我不確定」，不升級
長期研究記憶。

## 記憶生命週期

`content` 永遠保留；`summary` 只服務檢索。短期／長期都可按項目數與字數
限制，超限只標記 `archived`，原文與 `memory_events` 保留。長期預設上限為
10,000 筆或 8,000,000 字，`MemoryStore.capacity_report()` 在任一用量達 80%
時回報警戒。自動寫入的完全相同內容／scope 會去重；合併摘要透過
`consolidate()` 保留子記憶 ID 與 evidence ID。

## 舊資料回填

```powershell
# 只盤點，不寫入
uv run stock-provenance-backfill --root C:\skymiku\stock

# 確認報告後才寫入 research tables
uv run stock-provenance-backfill --root C:\skymiku\stock --apply
```

回填只使用 JSONL 明確列出的 `trace_id`、`run_id`、`evidence_ids`。43 筆沒有
可證明 evidence 的舊分析會保留原狀並標示 unresolved；不以 URL／標題臆造
source document。`stock-data-audit --json` 同時顯示 legacy compatibility
版本、實際 migration 版本與 research layer 的 evidence/claim 缺口。
## 每日報告回放的多來源 provenance

`intraday` 與 `next_day_watch` 報告通常同時使用新聞、macro 與其他來源，因此不能
用單一 `source_record_id` 代表整份報告。報告保存前會重新通過
`prepare_report_payload_for_storage`：所有 evidence packet 只接受 collector 已明確
提供、且 raw archive hash 可回放的 `source_document_id`；多筆 exact 來源標記為
`composite_exact`，無法證明的舊 packet 降為 `unresolved`。完整來源以報告
`evidence_json`、中央 evidence 與 evidence links 回放，不依 URL／標題猜補。
