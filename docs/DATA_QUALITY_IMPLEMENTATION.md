# 資料品質、分類與校準實作進度

日期：2026-09-17

這幾個階段把「API 取得成功」與「資料可以安全進入分析」分開處理。目標是先
降低錯誤資料、部分回應與 LLM 自由格式對下游分析的污染，不宣稱目前已完成
投資訊號的正確性校準。

## 已完成

### 1. 外部來源品質契約

`src/bot/data_quality.py` 提供共用 `SourceFetchResult`，會保存：

- HTTP 狀態
- `ok / partial / invalid / failed`
- 取得時間與來源日期
- 回應筆數
- raw payload hash
- schema hash
- 錯誤與警告

月營收與估值 fetcher 已使用這套契約。單一市場成功時可以供當次流程使用，
但不會用部分結果覆蓋完整快取；每次非快取取得也會將來源狀態寫入本地
SQLite 的 `source_runs`。目前已使用這套契約的 adapter 會優先把
`response_to_rows()` 取得的原始 response bytes 以 `http_response` 寫入
`SourceRegistry`、`source_documents` 與 `ingestion_runs`；若 adapter 只有解析後
rows，才退回 `api_rows` raw envelope。月營收、估值與公司基本資料已接入；仍未
使用這套契約的來源需要逐一接入；籌碼 API、總經快照與 ETF 原始 HTML/PDF
則透過 `SourceRegistry.capture_document()` 走同一個 ingestion lifecycle。TWSE
MIS／TDCC／TAIFEX 即時行情也會在正式流程傳入 project root 時保存原始回應；
除權息市場日曆、DuckDuckGo／Google News 搜尋與抓回的文章原文同樣保留原始回應。
MOPS
法說行事曆、重大訊息、詳情與簡報下載，以及全球事件的官方 RSS／官方活動頁，
現在也會保存原始 HTML／JSON／XML／PDF；解析後的行事曆與事件合併快照則獨立
登錄為 `event_snapshot`，避免把推導結果冒充原始證據。

### 2. 公司名冊安全更新

TWSE / TPEx 公司名冊現在會檢查 JSON 結構與解析後筆數。若強制更新後的名冊
明顯小於既有快取，會保留舊快取，避免封鎖頁、schema 異動或單一市場成功
造成全市場名冊縮水。

### 3. 多維分類基礎

`src/bot/classification.py` 將分類拆成不同維度的基礎，而不是讓一個
`industry` 欄位承擔所有語意：

- 官方產業別
- 投資題材
- 供應鏈角色
- 市場狀態
- 之後可擴充風險標籤與使用者標籤

LLM 題材輸出現在會驗證市場狀態、熱度、股票代號、角色、重複項目與文字長度。
`known_tickers` 有值時也會移除不在本地標的名冊的代號。

### 4. LLM 輸出保守驗證

法說會分析與 logic check 現在會驗證：

- sentiment / verdict / suggestion 的允許值
- sentiment score 與 confidence 的範圍
- list / object 結構
- 欄位長度與項目數量

錯誤不會被靜默吞掉，而會放在 `validation_errors`；無法判定時採保守值。

### 5. API 時間與 fallback 修正

- MIS 行情優先使用 API 回傳的報價日期與時間。
- 估值資料沒有來源日期時保留空白，不用本機日期冒充觀測日。
- TPEx 籌碼改用欄名定位，不依賴固定欄位 index。
- USDTWD 抓不到時不再使用固定 31.5；ADR 計算會跳過並記錄不可用。
- macro 的指數、ADR、台股現貨與匯率現在各自保存 `source_date`；`asof_date`
  只代表快照日期，不再被當成所有行情的共同觀測日。ADR 來源日期不一致時，
  結果保留但加入明確 note，避免把跨市場不同收盤時點誤判成計算錯誤。
- MOPS／TWSE 重大訊息會區分「HTTP／解析失敗」與「有效回應但該 ticker 無資料」；
  `openapi_valid`、`openapi_row_count`、`openapi_error` 與 HTTP 狀態會保留在
  collector stats，來源失敗不再被當成空清單成功。
- Windows CLI 的 logger 會 best-effort 將 stdout/stderr 切換為 UTF-8；資料檔與
  SQLite 仍以 UTF-8 為正式保存格式，避免主控台亂碼被誤認為資料損壞。

### 6. SQLite 溯源與分類保存位置

新增：

- `source_runs`：每次來源取得的品質與 schema 摘要。
- `entity_classifications`：可帶 taxonomy、confidence、source、source_ref、
  有效期間與審核狀態的多維分類。

這兩張表目前不列入 Google Sheets 預設同步範圍，避免分析內部 metadata
被當成使用者資料覆蓋。

核心資料表也已補上可向後相容的欄位：

```text
source_url
source_date
retrieved_at
quality_status
source_record_id
```

舊資料會以 `unknown` 保留，不會被錯誤標成 `ok`；DAO 寫入時會自動補
`retrieved_at`。OHLCV 若違反基本高低價、負數或欄位一致性規則，資料仍會
保留供追查，但 `quality_status` 會標成 `invalid`。

LLM 自動研究結果現在另外保存 `evidence[]`，每筆包含穩定 id、來源種類、
來源參照、URL、觀測日期、摘要與品質狀態。

LLM 歷史分析與每日報告表也保存 `evidence_json`、`quality_status`、
`source_record_id`，因此報告回放不必只依賴當下仍可連線的外部頁面。

### 7. 評分資料覆蓋率 gate

`scoring.py` 現在同時保存原始動作 `raw_action` 與實際動作 `action`：

- 覆蓋率低於 50%：阻擋非觀望動作。
- 覆蓋率低於 80%：`STRONG_BUY` 降級為 `BUY`。
- 分數仍保留，方便研究資料缺口，不把缺資料偽裝成低分。

這是資料可用性保護，不是策略勝率保證；權重與門檻仍要等歷史回測後再校準。

### 8. 來源資料語意稽核

`src/bot/source_audit.py` 補上 HTTP/JSON 以外的資料檢查：

- 必要欄位與跨市場欄名 alias。
- 主鍵缺失與重複列。
- 日期格式與不合理未來日期。
- 數值可解析性與可設定的合理範圍。

`stock-validate` 的結果現在同時有 `ok`（流程是否能執行）與
`quality_status`（資料是否完整可信），並把 findings 寫進 JSON 詳情，避免
「有回傳資料」被誤讀成「資料正確」。

`stock-data-audit` 會把已由 exact-only 回填流程標記為 `legacy_unresolved` 的舊
LLM 分析列在 `known_limits`，不再與新流程真正遺失 evidence link 的缺口混在一起；
這些舊內容仍保留，但不會被當成可發布的證據鏈。

此外，`canonical_audit.py` 會以唯讀方式檢查已寫入 SQLite 的 canonical rows：
日 K／盤中 OHLCV 不變量、月營收與季報期間格式、估值／股利的必要數值範圍，
以及財務欄位是否可解析。月營收的退貨／重編負值與季報中因小分母或一次性
損益造成的超過 ±100% 比率，若能回到 exact raw source，會保留為來源事實，
不再被錯誤當成結構性 invalid。
空的獨立 table 標記為 `empty`，與 `invalid` 分離；發現 invalid 或 partial 時，
`stock-data-audit` 會建立明確 gap，不會修改或刪除原始資料。

### 8.1 全市場 fundamental raw-first 物化

`src/bot/fundamentals_research_batch.py` 與
`stock-fundamentals-refresh --materialize-all` 提供不抓網路的全市場重播入口：

- 只接受最新 exact `source_documents` 與 hash 可驗證的 raw archive。
- 月營收、估值、季報、股利各自解析並寫入 canonical，再建立 research/chunk、evidence、
  ingest run 與 source-fact claims。
- 同一個 `source_document_id` 被所有衍生資料保留；無法解析的列會計入 skipped，
  不會以 URL、標題或相似度猜來源。
- 本次實際完成：月營收 1,978、估值 1,964、季報 1,974、股利 2,141，failed=0。

### 9. 雲端逐列同步

`cloud_sync.py` 新增 `sync_row_level()` 與 `--row-level`：依各表主鍵合併本地
與 Google Sheets，保留兩端各自新增的歷史 K 線、營收或報告。相同主鍵以
`updated_at` 較新者勝；時間相同但內容不同時保留本地，並回報衝突數。

同步寫入前也會套用 `canonical_audit.py`：無效的必要欄位、日期、數值或 OHLC
列會被拒絕，不會先清空本地資料。Google Sheets 不是 raw archive；未變更的本地
列保留 provenance/evidence metadata，新增或被手動修改的列會清除不可信欄位並
標記為 `quality_status=unknown`。

舊的 table-level `MAX(updated_at)` 模式仍保留，方便先觀察結果再切換排程。
逐列模式不是 append-only fact 的最終方案；若將來需要完整修訂歷史，仍應把
外部來源原始快照放到不可變的 raw archive，再由本地資料表重算。

### 10. 評分回測與校準介面

`src/bot/scoring_calibration.py` 與 `stock-calibrate` 會把訊號日期和之後第 N
個交易日的價格配對，產生：

- 有效樣本數與無法配對的原因。
- 各 action、分數區間的平均未來報酬與正報酬率。
- 方向性 accuracy（只在非中性標籤且訊號為 BUY/SELL 時計算）。

少於預設 30 個有效樣本時標記 `insufficient`，不允許把少量回測包裝成可用
的投資結論。執行：

```text
uv run stock-calibrate --root C:\\skymiku\\stock --horizon 5 --json data/reports/calibration.json
```

### 11. 短期／長期記憶儲存

新增 `memory_items`、`memory_events`、`evidence_items`、`memory_evidence`、
`memory_proposals` 與 `src/bot/memory_store.py`：

- 每次 LLM 呼叫的完整 prompt/output 進短期記憶，失敗內容保留但不進一般可靠搜尋。
- 通過新版研究輸出驗證的研究結果，以及品質為 `ok`／`partial` 的每日報告進
  長期記憶；失敗、無效或未知品質的報告只保留在 canonical 報告表供稽核。
- 記憶保留 scope、來源、觀測時間、品質、信心與 evidence reference。
- `evidence_items`／`memory_evidence` 統一登錄與關聯新聞、官方資料、歷史原句等證據。
- `!ask` 在回答前先登錄本次 EvidencePack；LLM audit row 同步保存
  `evidence_ids` 與 trace，讓回答驗證和後續記憶可以回到同一批來源索引。
- `auto_llm` 研究完成來源組合後也會回填 `research_ticker` 的穩定分析 ID；
  audit 會分別檢查空白 evidence 與引用不存在 registry 的 evidence。
- `memory_proposals` 提供管理者或 LLM 的審核佇列；未核准前不會進入可檢索記憶。
- 到期與容量控制只做 archived，不直接刪除，並留下生命週期事件。
- `stock-memory` 提供搜尋、回填、升級、提案審核、封存與軟刪除管理指令。
- Dashboard 新增「記憶與證據管理」頁；未設定 `MEMORY_ADMIN_TOKEN` 時維持唯讀。

詳細規格見 [MEMORY_STORAGE.md](MEMORY_STORAGE.md)。

## 尚未完成

下列項目仍需要後續垂直切片處理：

1. 將 `as_of / published_at / observed_at / retrieved_at` 的語意再細分到所有
   歷史事實表；Evidence registry 已先支援這些欄位，但不同資料域仍需專用時間
   欄位與驗證規則。
2. 對 TPEx、MOPS、MIS、新聞與 ETF 每個來源補上專用 schema、日期、單位與
   跨來源 reconciliation validator；TAIFEX TX 日行情已完成 JSON／CSV、exact
   hash、research/evidence/claim 的垂直切片，但其他 TAIFEX 商品仍需獨立規則。
   目前已完成共用稽核骨架，不代表所有來源都已具備完整 domain 規則。
   MOPS／全球事件目前已完成 raw capture，但仍需補上來源專用日期、重複事件與
   跨來源衝突驗證。
3. 將所有既有 LLM 流程的結論逐步補上實際 `evidence_id`；`!ask` 新流程已在回答前登錄並把 ID 鏡像至 LLM audit，舊有歷史資料仍需可追溯回填，不能臆造證據。
4. 累積足夠的歷史訊號與價格後，才對 scoring 權重、缺失資料覆蓋率與 action
   threshold 做正式 calibration。
5. 建立交易日曆、標的 universe、單位與幣別的中央 registry。
6. 將 row-level sync 在實際多機資料上觀察後，再決定是否升為預設，並規劃
   raw archive / append-only fact 的正式儲存層。
7. 對外部署 Dashboard 時補上正式身份驗證／RBAC；目前 token 是本地管理閘門。

另外，第一版 `source_health`、`schema_migrations` 與 `stock-data-audit` 已加入：
`source_runs` 現在會更新每個來源的最後狀態與連續失敗次數，`ingestion_runs` 會保留
對應的 `source_run_id`。這些是來源治理基礎，不代表所有 fetcher 已完成 raw-first。

## 線上資料源實測

2026-09-17 以目前憑證與公開網路重新實測：

```text
49 項：49 OK、0 警告、0 失敗
```

本次另以唯讀方式驗證 Shioaji：2330 單日 1 分 K 取得 265 根、逐筆成交取得
6,833 筆；歷史 tick 已相容目前 SDK 的單日 `date` 介面，跨日請求會逐日抓取後合併，
不會因舊版 `start/end` 參數而整條路徑失效。驗證前後正式資料庫的
`source_documents / ingestion_runs / source_runs` 維持 `4 / 6 / 10`，正式
`data/raw` 檔案數維持 4。

雲端 live ping 另行檢查結果為「未設定 `GOOGLE_SHEET_ID` / `GOOGLE_SA_JSON_PATH`」；
這是目前環境設定狀態，不是本地資料收集失敗。

2026-09-10 使用 `stock-validate` 對公開資料與 ETF 持股來源進行實際 HTTP／解析
驗證，結果為：

```text
47 項：44 OK、3 警告、0 失敗
```

已納入實測的內容包括：

- TWSE／TPEx 公司名冊（共 1,984 筆）與上市／上櫃單月歷史日 K（共 42 根）。
- 月營收、PER/PBR/殖利率、股利、季報、MOPS 法說會與重大訊息。
- TWSE／TPEx 籌碼、TDCC 股權分散、鉅亨新聞、DuckDuckGo 搜尋、yfinance 總經、
  TAIFEX，以及目前設定的全部 ETF 持股來源。
- 估值資料的 `N/A` 被視為合法缺值；真正無法解析的數字仍會使品質稽核失敗。
- TWSE TWT93U 借券報表使用目前可用的 `marginTrading` 路徑，並以欄名選擇當日
  餘額，避免誤取前日餘額；舊報表路徑保留為 fallback。

目前來源驗證已在 Shioaji simulation、2330 contract／snapshot 與 TWSE MIS
read-only poll 上得到 49 OK、0 warning、0 failure；第三方 SDK 的 Pydantic
deprecation warning 只出現在測試輸出，不是本專案來源失敗。完整歷史報告仍保存於
[source_validation_20260910.md](../data/reports/source_validation_20260910.md) 與
[source_validation_20260910.json](../data/reports/source_validation_20260910.json)。

## 歷史驗證結果（2026-09-17）

```text
682 passed, 1 warning
```

這代表回歸測試通過，不代表外部 API 在每一天都完整或語意正確。下一階段應
把各資料域的專用規則、交易日曆與 raw archive 補齊，再用累積的真實訊號做校準。

目前最新基線（2026-09-20）為 `735 passed, 1 warning`；最新 49 項來源驗證為
`49 OK, 0 warning, 0 failure`。若 TAIFEX live endpoint 未來回傳 HTTP 403，來源層只會
回放 SHA-256 驗證過的 exact raw archive，並以 `quality=partial`、`cached=true`、
`stale/not live` 呈現；不可把這份資料當成即時行情，也不會因來源失敗而回傳「沒有資料」。
這條降級路徑仍會保留 `source_document → research → evidence → claim` 的可追溯鏈，
並由來源驗證報告列為 warning 而非 OK。詳見 [INTEGRATION_VERIFICATION.md](INTEGRATION_VERIFICATION.md)。

2026-09-20 10:30 的 follow-up 實測再次得到 49 項 `48 OK, 1 warning, 0 failure`，
並新增 5 個 exact raw source documents；主庫與
`data/backups/stock_20260920_103036_source-validation-followup.db` 的 integrity 與
核心研究／證據計數一致。這次新增資料也通過 raw hash、canonical semantic audit 與
research chain 稽核。

2026-09-20 10:41 的 `!ask --live --no-llm` 重跑也確認外部搜尋 query 已被壓縮並
寫入 `resource_outcomes.metadata.search_query`；因未啟用 Gemini，系統保守停止於
可追溯 EvidencePack，不產生未驗證答案。最新資料庫恢復點為
`data/backups/stock_20260920_104452_ask-query-normalization-followup.db`，主庫與
備份 integrity 均為 `ok`，核心研究／證據／記憶／ask 計數一致。

2026-09-20 11:00 的真實自動研究管線驗收為 ETF 28/28 成功、錯誤 0、警告 9，
並將 83 個無籌碼資料標的與 TAIFEX exact-cache partial 明確寫入 pipeline warnings；
不再讓「資料缺失」或「stale cache」只停留在 console log。最新恢復點為
`data/backups/stock_20260920_110407_pipeline-status-followup.db`。

2026-09-20 11:15 補上 macro evidence 品質一致化：`source_date_status` 非 aligned、
存在降級備註，或 futures 使用 partial／cache 時，raw manifest、研究切片、報告管線與
`!ask` adapter 都標示 `quality_status=partial`；只有 aligned 且無降級條件才是 `ok`。
這讓「有資料但不可直接同時比較」與「沒有資料」保持不同語意。

同日完成既有 macro exact-ID 品質回填：48 份 raw manifest 全部可重播，44 筆 reused、
4 筆建立／補齊、0 失敗；source quality 為 `ok=38`、`partial=10`。恢復點為
`data/backups/stock_20260920_112140_macro-quality-repair-final.db`，主庫與備份
`PRAGMA integrity_check=ok` 且核心 evidence／research／claim 計數一致。

另修正 preflight simulation 的未知餘額語意：`account_balance` 不受支援時保存
`balance_available=false`、`balance=null`，不以 0 參與資金或風險判讀；正式帳戶能
取得明確數值時才標示 `ok`。

最新 `stock-validate --shioaji --mis-seconds 3`（2026-09-20 11:30～11:32）為 49 項
`48 OK、1 warning、0 failure`。Shioaji 與 TWSE MIS 讀取成功；TAIFEX live 403 仍只
使用 exact raw cache，標示 partial/stale。驗證後 raw 655/655、canonical 13,324 筆
無 invalid／partial／empty，恢復點為
`data/backups/stock_20260920_113251_source-validation-final.db`。

同日 11:35 的 `stock-ask --live --no-llm` 實跑確認：沒有 LLM 時系統會保守回覆不確定，
但仍完成 QueryPlan、來源收集、exact evidence、freshness 排除與 trace 保存；不會把
MOPS 空結果當成查無此事，也不會產生未驗證結論。恢復點為
`data/backups/stock_20260920_113645_ask-cli-final.db`。

11:41～11:42 的最新自動研究也完成 raw→research→evidence 整合驗證：ETF 28/28、
research 28/28 reused／成功、焦點 187、錯誤 0、警告 6；83 個無籌碼交易日資料的
標的被明確標示，不轉成零值。恢復點為
`data/backups/stock_20260920_114323_auto-research-final.db`。

2026-09-20 11:49～11:53 又完成 MOPS 法說會行事曆的研究垂直切片。流程只接受 exact
`source_document_id` 且必須能由 raw archive 重播：16 份來源文件中 12 份建立研究／
證據／source-fact claim 鏈，4 份空白快照標為 `empty`，共 2,384 筆有效活動、8 筆
同快照重複資料去重、0 筆 invalid／failed。空白來源不會被誤解成「沒有法說會」，也
不會因缺資料而產生虛構 claim；同一來源重跑會回傳 reused。

這條切片新增專用測試並通過完整回歸 `735 passed, 1 warning`。目前正式庫稽核為
`source_documents=658`、`research_documents=14,202`、`research_chunks=14,813`、
`evidence_items=12,227`、`evidence_links=12,251`、`claims=75,322`；raw hash
`658/658`、SQLite integrity、FTS 與 orphan 檢查均通過，research errors=0。恢復點為
`data/backups/stock_20260920_115327_mops-calendar-research-final.db`。

12:00～12:01 的最新來源驗證為 49 OK、0 warning、0 failure。MOPS、新聞、DuckDuckGo、
macro、TAIFEX、ETF、Shioaji simulation 與 TWSE MIS read-only poll 均正常；驗證後
`source_documents=662`、`research_documents=14,205`、`research_chunks=14,816`、
`evidence_items=12,229`、`evidence_links=12,253`、`claims=75,335`，raw `662/662`
hash 相符、SQLite integrity=ok、FTS 一致、orphan=0。恢復點為
`data/backups/stock_20260920_120117_source-validation-49ok-final.db`。

其後已補齊搜尋結果研究層：95 份 Google News／DuckDuckGo exact raw response 中，92 份
成功建立 `news_search`／`web_search` research、evidence 與 source-excerpt claims，
3 份空回應保留為 `empty`，0 份 failed。完整回歸為 `739 passed, 1 warning`；最新
稽核為 `source_documents=664`、`research_documents=14,297`、`research_chunks=15,674`、
`evidence_items=13,803`、`evidence_links=13,827`、`claims=79,239`，raw `664/664`
hash、SQLite integrity、FTS 與 orphan 檢查均通過。恢復點為
`data/backups/stock_20260920_121722_web-search-research-final.db`。

12:18 的 production `stock-ask --live --no-llm` 再次確認搜尋研究鏈已接入正式熱路徑：
Google News 8 筆、MOPS 空結果為 partial；無 LLM 時只回覆不確定並保留 trace／證據，
不產生未驗證結論。其後 `source_documents=666`、`research_documents=14,298`、
`research_chunks=15,677`、`evidence_items=13,808`、`evidence_links=13,832`、
`claims=79,247`，raw `666/666`、SQLite integrity、FTS 與 orphan 均正常。恢復點為
`data/backups/stock_20260920_121902_web-search-ask-hotpath-final.db`。

全市場基本面物化目前也包含股利：`stock-fundamentals-refresh --materialize-all`
會從最新 exact `dividend_twse`／`dividend_tpex` raw archive 解析並依
`(symbol, year)` 聚合，將 2,141 筆寫入 `dividend_history`，再逐筆建立 research、
evidence 與 source-fact claims。相同股票年度的多筆季配／分配資料只在同一份
exact source document 內加總；若一個 key 橫跨多份來源文件，流程會略過而不猜測
代表性的 `source_document_id`。目前這條批次回填 attempted=8,057、failed=0，
canonical 八張表均為 `ok`。

canonical 稽核也會在 source document 存在後驗證來源域對齊：月營收只接受
`monthly_revenue_*`、估值只接受 `valuation_*`、股利只接受 `dividend_*`、季報只接受
`quarterly_financials_*`，行情與 Shioaji／yfinance 亦有各自允許前綴。跨域連結會以
`provenance_domain` error 阻止同步／分析使用；目前正式庫 0 筆跨域錯誤。

研究層另有唯讀 `research_audit`：確認 research document 能回到 exact raw、raw hash
可重播、日期欄位可排序、研究文件有 chunks，以及 MIME／副檔名不互相矛盾。正式庫目前
列出 1 筆歷史 MOPS HTML/PDF 型態錯誤；該 derived chain 已 quarantine，並以真正 PDF
建立新的 exact chain，新的簡報入口也已拒絕這類資料。同一來源參照的多版本 raw 會保留
並標示抓取版本、資料期間未標定或無法區分版本的警告，不會以「最新」覆蓋舊證據；
`retrieved_at` 只用來辨識 raw snapshot，不會冒充 `as_of` 或市場資料日期。

整合前置檢查可使用 `stock-system-preflight`：它以唯讀方式合併 SQLite integrity、
raw hash、FTS、provenance orphan、research gap、Gemini 與 Google Sheets 設定狀態。
缺少可選憑證只標示 `info`；error gap 才會阻擋。2026-09-20 12:35～12:38 的正式
再驗收為來源 49/49 通過、raw 673/673、SQLite integrity=ok、FTS 一致、orphan=0，
完整測試 744 passed、1 個第三方 warning。

2026-09-20 盤中 runtime 驗收另確認 TWSE 日 K 的 308／redirect loop 會被來源層安全
降級，空 OHLCV 不會再觸發 `KeyError`；Dashboard K 線 figure 也會在 render 後釋放。
next-day watch 的無 LLM fallback 也已保留規則層催化事件，避免事件計數與明細不一致。

報告證據層也已接入 intraday／next-day：新聞與 macro 只使用來源 adapter 明確提供的
`source_document_id`，並同步寫入報告 payload、`llm_daily_reports.evidence_json` 與中央
`evidence_items`；來源不可重播時標為 `unresolved`，不做來源猜測。2026-09-20 實際
next-day 執行產生 31 筆 exact evidence，且 710 項回歸測試通過。

目前正式資料庫執行 `stock-calibrate --horizon 5` 的結果為
`sample_count=0, status=insufficient`；43 筆舊 LLM row 皆為失敗呼叫，現在明確
標記為 `llm_failed` 而非誤報 `missing_ticker`；現有資料不足以支持評分權重或
action 門檻的結論。

