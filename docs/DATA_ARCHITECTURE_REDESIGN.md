# Stock 資料架構重設計規劃

> 狀態：設計基線；Phase 1 已部分落地，尚未取代所有既有來源流程
>
> 盤點日期：2026-09-09
>
> 目標：以本專案已使用且可維護的技術為基礎，重新整理行情、基本面、研究素材、LLM 分析、使用者資料與交易稽核的資料邊界。

## 1. 結論先行

本專案不應先把所有資料搬進 Graphiti、GraphRAG 或新的資料平台。第一版應採用：

1. **SQLite 作為本地結構化資料的唯一真實來源（SSOT）**。
2. **原始下載檔採不可變保存**，以檔案雜湊與 manifest 追蹤來源、版本與抓取時間。
3. **SQLite FTS5 作為第一層文字檢索**；向量索引只作為補充，不取代精確 SQL／FTS。
4. **LLM 只做規劃、抽取、摘要與解釋**；不得把沒有證據的輸出直接升格成市場事實。
5. **所有分析都保存 evidence links**，讓回答可以回到原始資料、快照、報告或新聞。
6. **CSV／JSON 保留為交換格式與輸出格式，不再同時扮演多個資料源頭**。
7. **使用者筆記、投資 thesis、手動判斷與 LLM 摘要分開保存**，不可混成一個「記憶」欄位。

這樣可以保留現在的本地優先、離線可分析、Google Sheets 可選同步、Prompt 版本控管與事件稽核，同時補上時間、來源、證據、修訂與可回放能力。

## 2. 目前盤點

### 已有且值得保留的技術／觀念

| 現有能力 | 重設計中的定位 |
|---|---|
| `stock_db.py` 的 SQLite DAO | 結構化資料的本地 SSOT 與 Cache-Aside 入口 |
| `events/` Event Bus + Protocol | 擷取、計算、通知、稽核的解耦邊界 |
| `prompts/*.yaml` + Prompt Registry | LLM 契約、版本與可回放基礎 |
| `llm_log.py` JSONL | 完整呼叫稽核；另以 DB 保存可查詢摘要 |
| `llm_analysis_history` | 分析結果索引，但需增加證據與執行批次關聯 |
| `llm_daily_reports` | 報告快照與 dashboard 回放 |
| `data-sources.md` 的 link/http/parse 三層驗證 | ingestion quality gate |
| `auto_llm` 的 source sections | 研究素材與模型結論分離的雛形 |
| `pipeline_runs/<timestamp>` | 可重現的研究批次與輸出 manifest |
| `stock-db-backup` | schema migration 前的保險與可回復機制 |
| `watch`／`report`／`trade` 分離 | 分析、觀察、執行的安全邊界 |
| RiskGuard、preflight、kill switch | 不讓 LLM 分析直接越過交易安全層 |

### 目前最需要修正的資料問題

1. `stock_info`、`price_history`、`monthly_revenue`、`quarterly_report` 等表以 `updated_at` 為主，缺少 `as_of`、`published_at`、`source_id`、品質狀態與修訂版本。
2. 新聞、重大訊息、法說簡報、籌碼與行事曆主要留在 JSON／CSV／下載目錄，查詢入口不一致，難以組合成同一份證據包。
3. `raw_output` 可以保存模型輸出，但目前未強制連到「本次輸入使用了哪些資料」以及「輸出中的結論由哪些證據支持」。
4. `watchlist.tags` 為逗號字串；使用者筆記、研究假設、交易決策與系統摘要的生命週期不同，不適合共用同一種欄位。
5. 成交／訊號／風控主要有 CSV、JSON 與 JSONL 等多種落點，查詢歷史決策時需要跨格式拼接。
6. `sync_meta` 原本只記錄 table 層級同步狀態；目前已補 `sync_conflicts` 保存
   row-level 雙端內容與決策，但仍未把所有資料表升級成完整 revision graph。
7. 目前盤點的 `data/stock.db` 僅有少量 LLM 歷史資料，其餘核心表為空；因此現在適合先定義 schema 與寫入契約，再開始累積正式資料，不必先做高風險大搬遷。

## 3. 目標資料分層

不要以「檔案格式」分類，而要以資料的權威性、可重算性與生命週期分類。

```text
外部來源／使用者輸入
        │
        ▼
┌────────────────────┐
│ Raw immutable layer │  原始 JSON/CSV/HTML/PDF，內容雜湊，不覆寫
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ Canonical facts    │  SQLite 正規化行情、基本面、事件、持股
└─────────┬──────────┘
          ├──────────────► FTS5／向量索引（可重建）
          ▼
┌────────────────────┐
│ Derived analytics   │  指標、評分、訊號、回測、共識、摘要
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ Evidence pack       │  某次回答／報告實際採用的資料快照與來源
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ LLM output          │  結構化結論、自然語言報告、信心與限制
└────────────────────┘

使用者筆記、投資 thesis、交易決策與系統稽核是旁路的可編輯／可追蹤資料，
不可直接覆寫 canonical facts。
```

### 3.1 Raw immutable layer

所有外部下載或手動匯入先寫入原始層，檔名不再是唯一識別，需建立 `source_documents` manifest：

| 欄位 | 說明 |
|---|---|
| `document_id` | 內容或來源事件的穩定 ID |
| `source` | `twse`、`tpex`、`mops`、`yfinance`、`cnyes`、`manual` 等 |
| `source_uri` | 原始 URL 或匯入來源 |
| `retrieved_at` | 本機實際抓取時間 |
| `published_at` | 來源公布時間，未知可為空 |
| `as_of` | 資料代表的市場／財務時間 |
| `sha256` | 原始內容雜湊，防止無聲覆寫 |
| `mime_type`、`byte_size` | 原始檔描述 |
| `local_path` | 本機相對路徑 |
| `parse_status`、`quality_status` | 抓取與解析結果 |
| `ingestion_run_id` | 所屬擷取批次 |

原始檔可放在 `data/raw/<source>/<yyyy>/<mm>/<document_id>.<ext>`。既有 `mops_downloads`、`mops_cache`、各種 JSON／CSV cache 不必立即搬移，但新流程要先建立 manifest，再逐步轉為此規則。

目前已先落地最小可用版本：`src/bot/source_registry.py` 以
`data/raw/<source>/<sha256>.<ext>` 保存內容，以 `source_documents` 保存 manifest，
並以 `ingestion_runs` 記錄一次擷取批次。現在 ingestion 也會以同一個 run 關聯
`source_runs`，並更新本地 `source_health` 摘要；新聞 fetcher 已接入這條流程。
`response_to_rows()` 取得的 API response bytes 也會優先保存；目前月營收、估值與
公司基本資料已接入。**TWSE／TPEx 日 K（`STOCK_DAY` 等）在呼叫端提供 `root` 時，
會經 `_capture_kline_response()` 寫入 immutable raw**；`root=None` 時維持舊有純抓取、
不寫 raw。**raw capture 與 research／canonical materialization 是不同步驟**：capture
只保存原始回應與 manifest；物化／研究切片才把 exact raw 重播成結構化列與 evidence。

籌碼 API、TWSE MIS／TDCC／TAIFEX 即時回應、總經 snapshot、ETF HTML/PDF、MOPS 行事曆／
重大訊息／詳情／簡報，以及全球事件的官方 RSS／官方活動頁，部分已能
`SourceRegistry.capture_document()` 保存原始內容；法說行事曆與全球事件的合併
快照另以 `event_snapshot` 保存，除權息市場日曆、DuckDuckGo／Google News 搜尋
與抓回的文章原文也會保存原始回應。這些表是 local-only，不列入 Google Sheets
同步。yfinance 日 K fallback（包含 history queue 與 watch-data-fetch）與 Shioaji 分 K／Tick 歷史則保存明確標記
`cache_only` 的 normalized replay envelope；這可重播與驗證 hash，但不宣稱是
上游 HTTP raw。

**仍明確未完成（incomplete backlog）**：籌碼與全球事件雖可能已有 raw capture，
其完整 research materialization／域專用驗證與歷史回填仍分階段；其他尚未接入或
僅部分接入的 fetcher／backlog 項目，不得宣稱「全部外部來源已 raw-first」。
只讀盤點請用 `uv run stock-data-audit --json`（不修改 DB／raw）；
全市場基本面重播用 `stock-fundamentals-refresh --materialize-all`（只重播既有
exact raw → canonical／research，**不宣稱修復歷史 orphan 或猜測 provenance**）。

### 3.2 Canonical facts

這一層是可以用 SQL 明確回答的資料，必須具備來源與時間，不由 LLM 自由改寫：

- 商品與公司主檔：股票、ETF、基金、產業、別名、上市狀態。
- 市場觀測：日 K、分 K、tick、成交量、指數、VIX、期現貨價差。
- 基本面：月營收、季報、EPS、三率、股利、估值。
- 市場事件：法說、重大訊息、除權息、財報公布、全球事件。
- 持股與供應鏈：ETF／基金持股快照、公司關係與來源。

每個事實至少要能回答：

```text
這是什麼？對哪個標的？代表哪個時間？何時公布？來自哪裡？
目前是否有效？是否被後續版本修訂？資料品質如何？
```

### 3.3 Derived analytics

技術指標、評分、微笑曲線、候選清單、回測、題材共識與摘要都屬於可重算資料。它們不應覆寫原始事實，至少要保存：

- `analysis_run_id`
- `algorithm_version`
- `input_snapshot` 或輸入資料版本
- `created_at`
- `valid_until`（若適用）
- `result_json`
- `status`／`error`

同一檔股票同一天重新計算時，應產生新的 run 或 revision，而不是只留下最後一版。

### 3.4 User／decision layer

將目前容易混在一起的資料拆成：

- `watchlists`、`watchlist_items`：使用者想追蹤什麼。
- `user_notes`：人工筆記，可編輯但有歷史版本。
- `investment_theses`：為何看好／看壞、支持證據、失效條件、有效期間。
- `trade_decisions`：某次決策採用的資訊、人工或系統建議、最後決定。
- `risk_overrides`：任何手動放寬或限制，必須有操作者、理由、期限。

LLM 可以協助整理這些內容，但不能不經確認直接把模型推測寫成 thesis 或交易事實。

## 4. 建議的 SQLite 目標 schema

這是目標邊界，不要求一次全部實作。第一階段先新增 manifest、ingestion、evidence 與 revision 表，既有表用相容欄位逐步演進。

### 4.1 來源與批次

```sql
source_documents(
  document_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_uri TEXT,
  retrieved_at TEXT NOT NULL,
  published_at TEXT,
  as_of TEXT,
  sha256 TEXT NOT NULL,
  mime_type TEXT,
  local_path TEXT NOT NULL,
  parse_status TEXT NOT NULL,
  quality_status TEXT NOT NULL,
  ingestion_run_id TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

ingestion_runs(
  run_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  request_json TEXT NOT NULL DEFAULT '{}',
  summary_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT ''
);

source_health(
  source TEXT PRIMARY KEY,
  checked_at TEXT NOT NULL,
  link_ok INTEGER NOT NULL,
  http_ok INTEGER NOT NULL,
  parse_ok INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT ''
);
```

### 4.2 結構化資料的共通 provenance

現有表不需要立刻拆成 EAV。保留適合 SQL 的欄位，但各表加入下列共通欄位：

```text
source_document_id
ingestion_run_id
as_of
published_at（若資料有公布時間）
observed_at（行情觀測時間）
revision_no
quality_status
is_current
```

對 `price_history`、`intraday_bars` 等高頻表，`observed_at` 與 `source` 優先；對季報、營收、事件，`as_of` 與 `published_at` 優先。不要用一個 `updated_at` 同時代表這些不同概念。

### 4.3 研究文件與檢索

```sql
research_documents(
  document_id TEXT PRIMARY KEY,
  document_type TEXT NOT NULL, -- presentation/news/announcement/research/brief
  symbol TEXT,
  title TEXT NOT NULL DEFAULT '',
  published_at TEXT,
  source_document_id TEXT NOT NULL,
  canonical_text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active'
);

research_chunks(
  chunk_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  token_count INTEGER NOT NULL DEFAULT 0,
  embedding_model TEXT,
  embedding_json TEXT,
  FOREIGN KEY(document_id) REFERENCES research_documents(document_id)
);

evidence_links(
  evidence_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  source_document_id TEXT,
  table_name TEXT,
  row_key TEXT,
  quote_or_summary TEXT NOT NULL DEFAULT '',
  as_of TEXT,
  created_at TEXT NOT NULL
);
```

FTS5 與向量索引都是可刪除、可重建的 derived index，不把索引當原始資料。第一版只做 SQLite FTS5；只有在實際評估顯示同義語意檢索不足時，才加入本地向量模型。

### 4.4 LLM 執行與輸出

保留 `llm_analysis_history` 與 `llm_daily_reports` 的相容 API，但補上：

```sql
llm_runs(
  run_id TEXT PRIMARY KEY,
  task_type TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_id TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  input_hash TEXT,
  output_hash TEXT,
  error TEXT NOT NULL DEFAULT ''
);

llm_run_evidence(
  run_id TEXT NOT NULL,
  evidence_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  role TEXT NOT NULL DEFAULT 'supporting',
  PRIMARY KEY(run_id, evidence_id)
);

llm_outputs(
  output_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  output_format TEXT NOT NULL,
  structured_json TEXT,
  markdown TEXT,
  confidence REAL,
  limitation TEXT NOT NULL DEFAULT '',
  is_publishable INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
```

`log/llm_calls/*.jsonl` 仍保存完整 input/output 供除錯；DB 只保存查詢、dashboard 與 evidence graph 需要的索引與結構化結果。

### 4.5 事件、訊號與交易

將 CSV／JSONL 的重要結果以 append-only 事件索引進 SQLite：

```sql
domain_events(
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  symbol TEXT,
  source TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  correlation_id TEXT,
  created_at TEXT NOT NULL
);

signals(
  signal_id TEXT PRIMARY KEY,
  analysis_run_id TEXT,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  price REAL,
  score REAL,
  llm_gate TEXT,
  as_of TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'observed'
);

risk_events(
  risk_event_id TEXT PRIMARY KEY,
  event_id TEXT,
  symbol TEXT,
  rule_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

真正的訂單、成交與持倉仍由交易層負責；研究層只能產生 signal，不能直接改寫交易結果。

## 5. QueryPlan 與資料取得流程

`!ask` 或 dashboard 的自然語言問題不應直接把所有資料塞進 prompt。建議採以下流程：

```text
使用者問題
  ↓
規則式正規化：ticker、日期、時間點、問題類型
  ↓
QueryPlan（LLM 可選，JSON schema 固定）
  ├─ needs：market / fundamentals / events / documents / portfolio / history
  ├─ as_of：回答允許使用的時間點
  ├─ search_queries：SQL／FTS／web 各自的查詢
  ├─ evidence_requirements：需要數值、原文、比較或引用
  └─ answer_mode：direct / historical / compare / uncertain
  ↓
確定性資料查詢
  ├─ SQL 精確篩選時間與標的
  ├─ FTS5 找事件／報告／筆記
  ├─ 向量檢索（第二階段才開）
  └─ web 只在 current_external_fact 明確需要時使用
  ↓
Evidence Pack：資料、時間、來源、品質、限制
  ↓
LLM 結構化回答
  ↓
確定性驗證：數字、日期、ticker、引用、JSON schema
  ↓
回答／正式報告；失敗則「我不確定」並保留錯誤稽核
```

### QueryPlan 資源對照

| 問題類型 | 優先資源 | 不應直接使用 |
|---|---|---|
| 現價、漲跌、成交量 | 最新 market observation | 舊 LLM 摘要 |
| 某日／某段期間績效 | `price_history`／`intraday_bars` | 沒有 `as_of` 的 cache |
| 營收、EPS、三率 | fundamentals + published time | 新聞摘要代替財報 |
| 為何上漲、風險是什麼 | events、news、research chunks | 未附來源的 LLM 推測 |
| ETF／基金持股 | holdings snapshot + effective date | 最新下載檔名推斷日期 |
| 我的觀察清單／投資想法 | watchlist、notes、theses | `stock_info.note` 混合所有用途 |
| 過去 bot 說過什麼 | llm_runs + evidence links | 只搜尋最後一份報告 |
| 是否可交易 | RiskGuard／preflight／live state | LLM 自行批准 |

LLM 規劃器失敗時，規則式保底必須仍然產生合法 QueryPlan；不可用「快速分類沒有 other」作為是否呼叫 LLM 的唯一閘門。未知問題應保留多個候選資源或進入低風險廣搜，而不是靜默漏查。

## 6. 記憶、摘要與修訂策略

股票資料的「記憶」不應套用聊天機器人的單一 Mem0 記憶桶。建議採四種記憶：

1. **權威事實**：來自官方或明確資料源，可被修訂，但每次修訂保留版本。
2. **研究摘要**：模型根據 evidence pack 產生，可重算，不能取代原文。
3. **使用者知識**：人工筆記、偏好、投資 thesis、失效條件，需可手動修改與稽核。
4. **互動／決策歷史**：某次問題、採用的資料與最後決策，永久保存或依容量政策濃縮。

### 保存規則

- 原始資料不以摘要取代；容量不足時先壓縮 derived index、報告快取與重建資料。
- 摘要必須帶 `source_document_id`、`as_of` 與 `analysis_run_id`。
- 新資料與舊資料衝突時，不覆寫舊結論，新增 revision 並標記 `supersedes`。
- LLM 失敗、fallback、無證據的答案不寫入長期知識。
- 只有具備最小證據數、來源品質與 JSON schema 通過的輸出，才可標記 `is_publishable=1`。
- 使用者要求刪除或更正時，建立 append-only audit event；不直接抹掉不可恢復的交易稽核。

### 摘要層級

```text
原始文件／行情
  → 每份文件摘要（document summary）
  → 每檔股票／每日期摘要（ticker-day summary）
  → 每週／每月主題摘要（theme summary）
  → 使用者指定的 thesis／決策摘要
```

每一層都要有輸入範圍與時間窗，不允許「摘要的摘要」無限遞迴而失去證據。

## 7. LLM 角色重新分工

不建議目前導入多代理人互相審判或讓 LLM 自行修改人格／資料 schema。這個專案更適合先把角色固定：

| 角色 | 責任 | 可否寫 canonical facts |
|---|---|:---:|
| Normalizer | ticker、日期、單位、來源欄位正規化 | 否 |
| Query Planner | 選資源、時間範圍、查詢詞與證據要求 | 否 |
| Extractor | 從法說、公告、新聞抽結構化候選事實 | 否，先進 proposal |
| Summarizer | 依 evidence pack 產生摘要 | 否 |
| Verifier | 檢查數字、日期、引用與 schema | 否 |
| Answerer | 以繁中產生回答或報告 | 否 |
| Human/Admin | 核准更正、使用者 thesis、資料升格 | 是，經 audit |

其中 Extractor 的結果進 `fact_proposals` 或 `memory_proposals`，經規則驗證與人工／管理者核准後，才寫入 user／derived 層。這沿用既有專案「LLM 產出 JSON + 呼叫完整記錄」的成熟方向。

## 8. 是否需要 Graphiti、Mem0、GraphRAG

### 第一階段：不引入

- `Mem0` 類工具若要使用，只放使用者偏好與互動摘要，不放價格、EPS、持股等權威事實。
- 供應鏈、ETF 持股、公司—產業關係先用關聯表與 `evidence_links` 表達；這些關係有明確欄位與有效期間，不需要先引入圖資料庫。
- GraphRAG 的第一步可由 SQLite relation tables + FTS5 + evidence pack 完成。

### 何時才升級圖檢索

只有在下列查詢成為主要瓶頸時才評估 Graphiti／Neo4j 等：

- 使用者經常查詢多跳供應鏈關係，且關係版本與時間有效性難以用 SQL 維護。
- 需要跨公司、ETF、基金、法說事件、新聞與主題自動探索多跳路徑。
- 評測證明 SQL／FTS／向量混合檢索在關係型問題的 recall 明顯不足。

升級時，圖資料庫也應是 derived projection；SQLite 與 raw manifest 仍是可回復的主資料，不把圖資料庫變成唯一依賴。

## 9. 遷移順序

### Phase 0：凍結與盤點

- 以 `stock-db-backup` 建立基線備份。
- 建立 `data/manifest.json` 或 `source_documents` 的初始清單。
- 列出所有 JSON／CSV／PDF／HTML／JSONL 的 owner、來源、用途、保留期。
- 定義 ticker、日期、時區、單位、市場別與 null 語意。
- 可先執行 `uv run stock-data-audit --root C:\\skymiku\\stock --json`，盤點目前
  DB、raw、pipeline 與 LLM log 的 provenance 缺口（**只讀**；不修復 orphan、
  不猜測 provenance）。整合前也可跑 `uv run stock-system-preflight --root C:\\skymiku\\stock --json`。
- 暫不修改既有資料檔，只建立報告與驗證腳本。
- 若需重播既有基本面 raw：`stock-fundamentals-refresh --materialize-all` 只做
  exact-archive materialization，**不宣稱歷史 orphan 修復**。

### Phase 1：來源與 provenance（進行中）

- 已新增 `ingestion_runs`、`source_documents` 與 `SourceRegistry` raw archive 介面；
  `source_runs` 關聯、`source_health` 最新狀態與 `stock-data-audit` 已完成第一版，
  各 fetcher 的全面接入仍待完成。
- SQLite 已記錄 schema baseline (`schema_migrations`)，後續結構變更應追加版本化
  migration；舊版欄位仍保留相容補欄位路徑。
- 所有 fetcher 先寫 raw，再 parse，再 upsert canonical。
- 對現有 `stock_info`、`price_history`、`monthly_revenue`、`quarterly_report` 增加相容 provenance 欄位。
- 明確區分 `as_of`、`published_at`、`retrieved_at`、`updated_at`。

### Phase 2：研究素材與檢索

- 將 MOPS、新聞、法說與 pipeline 輸出註冊為 research documents。
- 建立 `research_chunks` + FTS5；索引失效時可以重建。
- 實作 `EvidencePack` 資料模型與查詢 API。
- 先改 dashboard 的「個股深入分析」與 `research_ticker` 使用 EvidencePack。

### Phase 3：QueryPlan 垂直切片

先做三種問題：

1. 「2330 最近一個月漲跌如何？」→ SQL K 線。
2. 「2330 最近有哪些可能影響股價的事件？」→ 事件／新聞 FTS。
3. 「你為什麼把它列入觀察？」→ analysis run + evidence + user thesis。

要求 QueryPlan 失敗仍能規則式降級，且所有回答可列出證據時間與來源。

### Phase 4：記憶與管理

- `user_notes`、`investment_theses`、`fact_proposals`、`audit_events`。
- 提供 CLI／dashboard 的 propose、show、approve、reject、supersede、forget。
- LLM 不可直接寫入 canonical facts。

### Phase 5：同步與清理

- Google Sheets 只同步適合人工編輯的表：watchlist、notes、theses、少量 canonical reference。
- 不同步高頻 intraday、raw PDF、完整 LLM prompt 與敏感交易資料到 Sheets。
- 已加入 `sync_conflicts` row-level conflict log；後續才是完整 revision graph 與
  來源優先級治理。
- 最後才處理舊 CSV／JSON 的退場與相容讀取移除。

## 10. 評測與完成條件

沿用 LongMemEval 的精神，但改成股票資料問題集：

| 類別 | 例子 | 指標 |
|---|---|---|
| 時間定位 | 截至某日的價格／營收 | as-of accuracy |
| 歷史回顧 | 某次法說曾說什麼 | evidence recall |
| 知識更新 | 後續公告修正前次數字 | stale fact rate |
| 多源整合 | 財報 + 新聞 + 籌碼 | citation coverage |
| 來源衝突 | 官方公告與新聞不一致 | authority selection |
| 不確定性 | 沒有足夠資料時 | abstention precision |
| QueryPlan | 是否查對資源 | plan accuracy |
| 運行品質 | cache、延遲、失敗 | p95 latency／failure rate |

最低 DoD：

- 每個回答都能回溯到至少一個 evidence，否則明確標示不確定。
- 任何 LLM 失敗或 fallback 不會污染正式摘要／知識層。
- 同一份原始資料可重跑出相同的 canonical revision 與分析 run。
- 查詢不依賴 dashboard 先載入過某個 JSON cache。
- migration 前後既有 `stock_db`、pipeline、dashboard 測試全部通過。
- 任何交易動作仍需通過既有 RiskGuard、preflight 與 kill switch。

## 11. 第一個實作切片建議

下一步最適合做一個小而完整的垂直切片，而不是一次重寫全部資料：

1. 新增 `source_documents`、`ingestion_runs`、`evidence_links` 的 SQLite migration。
2. 寫一個只讀 `stock-data-audit`，掃描現有 `data/`、`log/`、`pipeline_runs/`，產生 provenance 缺口報告（已落地；仍為只讀盤點，不自動修 orphan）。
3. 讓 `research_ticker` 將資料來源註冊到 manifest，輸出 `EvidencePack`。
4. 讓 QueryPlan 只支援「價格歷史／公司基本面／事件研究」三類資源。
5. 將 `llm_analysis_history` 的每筆結果連到 `llm_run_id` 與 evidence。
6. 用 20～50 個人工問題建立第一版評測集，再決定是否需要向量或圖資料庫。
7. （進行中）籌碼／全球事件／其餘 backlog 的 research materialization 與域專用驗證；勿把 raw capture 誤當成整條鏈已完成。

完成這個切片後，才有足夠證據判斷後續要不要引入更複雜的記憶或圖檢索系統。
