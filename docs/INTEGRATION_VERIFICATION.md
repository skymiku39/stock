# Stock Bot 整合驗證紀錄

日期：2026-09-20（歷史整合主軸）；現況對齊補充：2026-09-22  
範圍：`C:\skymiku\stock` 目前本地工作區  
原則：本地優先、來源可追溯、分析不臆造、下單路徑不由研究／問答流程呼叫。

## 現況摘要（2026-09-22）

本文件主體是 **2026-09-20～21 的時間線驗收紀錄**（保留不刪）。下列為文件維護日
對「現況契約」的對齊，**不重跑 live 驗收、不發明計數**：

| 主題 | 現況契約 |
|------|----------|
| 預設執行模式 | `RUN_MODE=watch` |
| ETF／基金清單 | 27 檔主動 `*A` + ALI006 ＝ 28（程式內建；可被 JSON 覆寫） |
| Dashboard | `nav.py` 權威頁面清單；`dashboard.py` 為 shim |
| 未設定 Google 憑證時的 `stock-cloud-sync` | **exit code 2**（與 `src/bot/cloud_sync_cli.py` 一致；下文若出現 exit code 1 的敘述視為過期／筆誤） |
| 只讀稽核入口 | `stock-data-audit`、`stock-system-preflight`；能力報告可選 `--audit-json` |
| `stock-fundamentals-refresh --materialize-all` | 重播既有 exact raw；**不宣稱**歷史 orphan 修復 |
| 下方「目前／最新」數字、4／43 gap、orphan=0 等 | **當次歷史快照**；現況以重跑 audit／preflight 為準 |

## 已通過的整合驗證

> 自本節起，除另標「現況」外，標題中的「目前／最新」均指**該時間戳當次**結果。

### 測試與程式載入

- `uv run --no-sync pytest -m "not integration and not slow"`
  - **735 passed, 1 warning**（目前測試套件沒有宣告 integration/slow marker，
    因此這個命令實際涵蓋目前收集到的全部測試。）
  - 唯一 warning 是第三方 Shioaji SDK 的 Pydantic deprecation，不是本專案測試失敗。
- 另以隔離暫存 SQLite 逐一驗證 10 張可同步表的 push、pull 與 row-level merge，全部成功。
- `GoogleSheetSync` 的公開 API 也會拒絕 `evidence_items`、記憶與其他 local-only
  table；不只依賴 CLI 的 table whitelist，避免直接呼叫 API 時繞過 SSOT 邊界。
- `StockDB` 對同一個 SQLite 路徑加入程序內寫入鎖；`!ask` 並行來源 adapter
  同時建立短生命週期 `SourceRegistry` 時，不會因 schema 初始化／raw manifest
  寫入競爭而把 `database is locked` 當成來源失敗；新連線的 WAL 初始化也
  使用同一把鎖。此情境有 24 路並行 raw capture 回歸測試。
- 另補上跨程序 schema sidecar lock 與 30 秒 busy timeout；三個獨立
  `stock-ask --no-llm --json` 同時啟動的實際 smoke 已通過，沒有再出現
  `database is locked`，並新增跨程序 `StockDB.open()` 回歸測試。
- Google Sheets push 已改為先寫入新 payload、成功後才清除多餘舊列；模擬網路更新
  失敗時舊 worksheet 內容保留，避免 `clear → update` 中斷造成遠端空表。
- `uv run --no-sync python -m compileall -q src` 通過。
- `stock-data-audit` 現在也執行 SQLite `PRAGMA integrity_check`；相容欄位
  `schema_version=1` 與實際 `latest_schema_version=5` 分開顯示，避免把舊版
  dashboard 相容欄位誤讀成目前資料庫版本。
- `stock-validate` 未指定 `--root` 時使用的暫存資料根會在檢查完成後自動清理；
  指定正式 root 時不再建立無用暫存目錄，避免排程／CI 長期累積暫存資料。
- 所有已註冊 CLI 的 `--help` smoke test 通過。
- `src/bot` 目前全部 150 個 Python 模組逐一 import smoke test 通過，沒有 import failure；
  Streamlit 無 runtime 時只有預期的 cache backend warning。
- 30 個已安裝 `stock-*` CLI 的 `--help` smoke test 全部通過；`stock-bot`
  也不會因未知／說明參數而誤啟動長駐流程。
- 記憶管理提案的 `update`／`consolidate`／evidence 保留流程已加入回歸測試。
- 隔離 SQLite 實跑 `stock-memory propose → approve`，成功建立 active long memory。
- 問答／分析路徑沒有 `place_order`、`send_order` 或 broker 呼叫。
- QueryPlan 已補上「重大訊息／重大公告／新聞稿／公開資訊觀測站／法人說明會」
  等常見用語；實際查詢會同時評估新聞與 MOPS，而不是只走基本面。
- EvidencePack quota 已改為先保留每個要求資源的代表證據，再補充其他證據；
  本地快取不會因先填滿上限而把即時新聞／MOPS 靜默丟掉。
- Answer verifier 現在除了逐項核對 claims，也會核對回答正文中的百分比、帶單位數值、
  小數與完整日期是否出現在 cited evidence；ticker／年份識別碼不會被誤當成數值。
- `!ask --live` 的 ticker snapshot 現在使用 `LiveTickerAdapter`；同一資源的 live
  evidence 優先於舊本地快取，並對並行 raw manifest 寫入的短暫 SQLite lock 做有限重試。
- `professional_knowledge` 現在使用 `ProfessionalKnowledgeAdapter`；只接受 allowlist
  官方／監管／學術網域，頁面原文優先，搜尋摘要會標 `partial`，一般搜尋結果不會
  冒充專業證據。實際網路 smoke 取得 2 筆 SEC 來源證據，均保留 raw search manifest。
- `!ask` 的 MOPS adapter 現在把 live budget 傳遞到列表／詳情 HTTP timeout，
  並在達到 deadline 時停止後續詳情抓取；不讓已返回的問答在背景無界地寫入
  raw／research。此行為已有 budget propagation 回歸測試。
- yfinance 日 K fallback（history queue 與 watch-data-fetch）與 Shioaji 分 K／Tick 歷史現在會保存 normalized raw
  封套、source record ID 與 cloud mirror；兩者明確標成 `partial`／`cache_only`，
  不冒充 TWSE／TPEx 或 SDK 底層 exact HTTP raw。
- 00929 歷史日 K 的既有未連結資料已透過現有 `stock-watch-data-fetch` 入口重新保存；
  其中 116 筆明確為 `source=yfinance`、`quality=partial`、可回放但非官方 exact。
  `ask_adapters` 現在拒絕 yfinance row 參與 `composite_exact`，並有回歸測試防止再次升級。
- `!ask` 的公司資料、日 K、估值、股利與季報 adapter 現在會再次確認
  `source_document_id` 確實存在於 manifest，且 manifest 指向的 raw 檔存在、位於
  專案根目錄內、SHA-256 與 manifest 相符；canonical row 遺留的不存在、遺失或被
  改寫的 raw artifact 會標成 `unresolved`，不會只因字串前綴而宣稱 exact。
- `watch-data-fetch` 呼叫公司基本資料更新時，現在明確傳入同一個 `--root`；
  不會因工作目錄不同而把公司資料寫入另一份 SQLite。此隔離已加入回歸測試。
- `stock-cloud-sync`、`stock-ask` 與 `stock-validate` 現在支援明確 `--root`；
  `.env`、SQLite、同步設定與驗證用資料不會因排程／終端機的 CWD 改變而分離。
- Gemini prompt registry 也按專案根目錄隔離；`gemini_call(root=...)` 與
  `auto_llm` 不會因目前工作目錄或另一個專案先載入 prompt 而使用錯誤模板，
  並有 root-only prompt 回歸測試。
- `stock-bot` 入口現在也支援 `--root` 與安全的 `--help`；啟動時會將相對
  路徑統一到指定專案根目錄，避免事件、報表與策略相容輸出散落到呼叫端 CWD。
- `stock-calibrate` 也已補上 `--root`；SQLite 輸入與相對 JSON 報告會依指定
  專案根目錄解析，校準 CLI 不再是外部 CWD 整合的例外。
- `stock-memory` 與 `stock-sync-conflicts` 的相對 `--db` 路徑也會以 `--root`
  為基準；不同排程啟動目錄不會再意外開啟另一份 SQLite。
- `stock-db-backup` 現在支援 `--root`；來源 DB 與相對備份目錄會以專案根目錄解析，
  已從 `C:\\Windows\\Temp` 實際執行並確認沒有備份到錯誤 CWD。
- `stock-validate` 的相對 `--json`／`--md` 報告路徑也會以 `--root` 為基準；
  已加入 root-isolation 回歸測試，並從 `C:\\Windows\\Temp` 實際執行確認報告只寫入
  `C:\\skymiku\\stock\\data\\validation`，外部 CWD 沒有殘留報告。
- 預設 logger 也加入 root scope；`stock-preflight`／`stock-validate` 從外部 CWD
  執行時，log 會寫入 `C:\\skymiku\\stock\\log`，不再在呼叫端建立 `log` 目錄。
  已以 `C:\\Windows\\Temp` 實測，外部既有 log 的時間與大小沒有新增變化。
- `stock-preflight` 的 CLI 與 Dashboard 現在把明確 `project_root` 傳入核心檢查；
  監控池合併不會因外部 CWD 而讀錯另一份資料。
- 策略物件本身也接受 `project_root`；Configurable／ETF 策略會把明確 root
  傳給風控、LLM 閘門、部位恢復、成交／訊號報表與監控池，直接由 API 或排程
  建立策略時也不會把持久化資料寫到呼叫端 CWD；新增 root propagation 回歸測試。
- `AskEngine`、記憶管理 CLI、公司資料更新、回測 API／CLI 與 Dashboard 的
  root 切換現在遵守 SQLite handle ownership；自有連線在正常、例外與 root
  變更路徑都會關閉，避免長駐排程與 Windows WAL 資源累積。
- `AskEngine` ownership 回歸測試確認：自建 DB 由 engine 關閉，外部注入 DB
  仍由呼叫端管理；CLI 會在問答／follow-up 正常與例外路徑收尾。
- auto-research、intraday、next-day 的高頻 SQLite 讀寫 helper 現在會在
  fallback、提前 return 與例外路徑明確關閉自己建立的連線；傳入呼叫端持有的
  `StockDB` 則不會誤關，避免長駐排程／dashboard 累積連線與 WAL 資源。
- watch-data、dashboard 看板快取與事件鏈 handler 也已補上自有 SQLite handle
  的釋放，避免量化資料補齊或畫面重跑時持續累積連線。
- Shioaji intraday history writer 也採相同 ownership 契約：傳入的 DB 由呼叫端
  管理，未傳入時由 writer 自動關閉，保留 CLI／回測／排程的共用 API。
- history-fetch queue 的批次處理也改為相同的 DB ownership wrapper，避免慢速
  排程每批執行後遺留連線。
- `stock-dashboard` 支援 `--root`，並在進入 Streamlit 應用時固定到指定專案根目錄；
  未指定時使用安裝專案根目錄。模擬設定、`.env`、LLM audit 與相容頁面不會因從
  其他目錄啟動而讀寫錯誤位置。
- 技術分析的 TWSE 日 K adapter 已改用目前可直接取得的官方
  `exchangeReport/STOCK_DAY` JSON endpoint；對來源自我 308／redirect loop 會以
  `invalid` source result 可追蹤地降級，不再讓 `TooManyRedirects` 污染整批盤中分析。
  `_coerce_df()` 對空或缺欄位資料會回傳穩定的空 OHLCV frame，不猜造數值，也不再
  以 `KeyError: close` 造成噪音 traceback；此情境保留為不可排序候選。
- Dashboard Matplotlib K 線圖在 `st.pyplot()` 後明確 `close(fig)`，避免長時間切頁
  累積 pyplot figure；本專案自己的 figure warning 已消失。
- next-day watch 在沒有 Gemini、LLM timeout 或 radar 沒有有效輸出時，會把已由
  calendar／規則層取得的催化事件標準化保留在 `event_focus`，不再出現
  `catalyst_count > 0` 但事件明細為空的報告；強勢文字對缺少籌碼欄位也會安全降級。
- `default_db_path(root)` 會讀取指定根目錄的 `.env` 中 `STOCK_DB_PATH`；
  相對 DB 路徑會以該根目錄解析，並有專用回歸測試。
- `company_info` 與 `market_meta` 的程序內快取現在按 `project_root` 隔離；
  同一個長駐程序不會把另一份專案的公司／市場名冊帶入目前資料庫。
- 市場代號清單的 TWSE／TPEx 回應現在也會進入 raw archive 與 source manifest，
  解析失敗和空回應不再只留下 JSON 快取而無法稽核。
- Google service-account 相對路徑會以 `--root` 為基準解析，不會因啟動目錄改變而找錯憑證檔。
- Google Sheets pull／row-level sync／table-level sync 會先通過 canonical semantic
  audit；無效 OHLC／期間／必要欄位會拒絕寫入，試算表不可信的 provenance/evidence
  不會覆蓋本地 raw metadata。
- `stock-cloud-sync` CLI 現在會在成功、失敗與同步例外後關閉自己建立的 SQLite
  連線；不會因排程反覆執行而持續持有 Windows WAL 資源。
- auto-research、macro、fundamentals refresh、calendar、history、intraday、
  nextday 與 scheduler 等主要 pipeline CLI 也已支援 `--root`，並以同一根目錄
  載入設定與寫入輸出。
- intraday backtest、smile backtest／audit／screen 與 Gemini smoke 也已支援
  `--root`；回測輸出、分析設定與模型設定不再依賴啟動 CWD。
- 事件匯流排與 chain handler 也沿用相同 `project_root`，JSONL 事件與衍生處理
  不會寫回啟動目錄；同一程序切換不同 root 時也會建立隔離的 bus 與 JSONL audit。
- `stock-data-audit` 現在也檢查 canonical tables 的 `source_record_id`；**當時**
  `stock_info`、日 K、基本面、盤中資料、記憶與 LLM 表的 manifest orphan 全部為 0。
  （只讀稽核；不宣稱已永久修復所有歷史 orphan。現況請重跑 audit。）
- `stock-data-audit` 另加入只讀 canonical semantic audit：檢查日 K／盤中 OHLCV
  invariant、月營收／季報期間格式、估值／股利／財務數值範圍；空的獨立資料表
  標記為 `empty`，不誤報成資料錯誤；並驗證 `source_date`、`retrieved_at`、
  `updated_at` 的時間格式，支援日／月／季度來源期間。
- 主動式 ETF 的本地設定現在會由 `sync_etf_meta_to_db()` 冪等物化到
  `etf_meta`；設定列使用 `quality_status=unknown` 與 `config://` 來源，
  不會被當作外部證據；已有 exact 外部列則保留不覆蓋。
- C 槽完整 `run_full_pipeline()` 以關閉外部抓取／LLM 的整合模式實跑：
  `run_id=20260920_025305`，metadata 28 筆 unchanged、錯誤 0、警告 0，
  pipeline 正常收尾。

- 2026-09-20 08:52 完成全市場 fundamental raw-first materialization：
  - 只讀取既有、可用 SHA-256 重播的最新 `source_documents`，沒有在物化階段
    重新抓網路，也沒有依 URL／標題猜造來源 ID。
  - 月營收：1,978 rows，1,978 attempted，failed=0。
  - 估值：1,964 rows，1,964 attempted，failed=0；1 筆來源列依 parser 規則跳過。
  - 季報：1,974 rows，1,974 attempted，failed=0；4 筆來源列依 parser 規則跳過。
  - 三個 canonical domain 全部以 exact `source_document_id` 連回 raw archive；
    research／chunk／evidence／claim／llm ingest chain 均建立且可重播。

### 真實資料擷取與保存

- `stock-watch-data-fetch` 以 `2330`、短日期範圍實跑成功：
  - 日 K：897 筆。
  - 基本面快照：1 筆成功、0 失敗。
  - 錯誤：0。
- `stock-validate --root C:\\skymiku\\stock --shioaji --mis-seconds 5` 實際來源驗證：
  - 49 項檢查。
  - 49 OK、0 警告、0 失敗。
  - Shioaji simulation 登入、2330 contract、snapshot 與 TWSE MIS read-only poll 均通過；取得 2 筆 tick。
- 2026-09-20 05:43 再次執行同一個 49 項來源驗證，結果為 49 OK、0 警告、0 失敗；
  可重播的報告檔為 `data/validation/source_validation_20260920_final2.json` 與同名 `.md`。
- 2026-09-20 06:41 以外部 CWD 重跑含 Shioaji simulation／MIS read-only poll 的 49 項驗證，
  結果仍為 **49 OK、0 警告、0 失敗**；TAIFEX 回應已支援實際 UTF-8 CSV 格式，並保存
  exact `taifex_daily_fut` source document，且自動建立 `taifex_futures`
  research/evidence/claim slice，報告檔為
  `data/validation/source_validation_20260920_taifex_slice.json` 與同名 `.md`。
- 2026-09-20 07:38 從 `C:\\Windows\\Temp` 再次執行完整 root-isolated 驗證，結果為
  **49 OK、0 警告、0 失敗**；最新報告為
  `data/validation/source_validation_20260920_final.json` 與同名 `.md`。
- 2026-09-20 07:49 執行 `stock-auto-research --focus 2330 --no-llm --no-brief
  --no-calendar --no-auto-research`：28/28 ETF 持股成功、籌碼與 macro 完成，焦點
  187 檔，錯誤 0、警告 0；輸出 run 為 `data/pipeline_runs/20260920_074919`。
- `stock-ask --root C:\\skymiku\\stock --live --no-llm` 實際端到端重跑通過：
  QueryPlan、新聞／MOPS／TWSE MIS 蒐集、EvidencePack 與保守 fallback 均完成；
  修補後沒有再出現 `database is locked`，週末延遲行情仍正確標為 stale，未冒充最新資料。
- `stock-data-audit`（2026-09-20 07:51 最新）：
  - 本輪外部 CWD root-isolation、TAIFEX provenance／research slice、TAIFEX `!ask` exact resource、ETF research pipeline、00929 yfinance provenance 修補與最新完整來源驗證後 `source_runs` 為 1,124；重播入口取得月營收 1,978 筆、估值 1,965 筆，
    兩者均沿用 SourceRegistry 的 exact raw 文件，不以快取或 URL 猜造來源。
  - raw files：344。
  - `source_documents`：344。
  - `evidence_items`：190。
  - `research_documents`：150。
  - `research_chunks`：361。
  - `evidence_links`：198。
  - `llm_runs`：193。
  - `claims`：3,335。
  - canonical semantic audit：3,301 rows；0 invalid、0 partial、0 empty table；
    `etf_meta` 已有 28 筆本地設定 metadata（`quality_status=unknown`，不作為外部證據）。
  - `source_health`：73/73 有健康紀錄，其中 72 為 `ok`、1 為已明確標示的 `partial`，沒有 `failed`。
  - running ingestion：0。
  - provenance gaps：目前仍有 4 筆 2026-09-16 舊 `source_runs` 沒有可證明的
    `source_document_id`；`stock-data-audit --json` 會逐筆輸出
    `reason_code=source_document_link_missing`、原始 row count、是否有 raw hash／URL、
    raw hash 是否能 exact match 現有 archive，以及 exact-only 的修復建議；本次 4 筆
    均沒有 hash candidate。沒有未註冊 raw 檔，且 raw archive hash audit 已納入 CLI。
  - `schema_version=1` 是舊 CLI 相容基線；實際 migration registry 的
    `latest_schema_version=5`，兩者分開保存是既有相容設計，不是 migration 未套用。
  - `stock-data-audit` 現在會直接驗證 raw archive：344/344 個登錄文件存在且 SHA-256 相符，
    並執行 SQLite `PRAGMA integrity_check=ok`。
  - 344/344 個 `source_documents` 的 raw archive 都存在，SHA-256 全部相符；沒有 missing 或 mismatch。
  - relational integrity：memory/evidence、claim/evidence、LLM run/evidence、
    research/source、research chunk/document 均為 0 孤立；memory／research FTS
    索引筆數分別為 106/106 與 361/361。
  - row-level cloud sync conflict log：目前 `total=0`、`open=0`；若兩端同一
    primary key 與時間相同但內容不同，local／remote 兩份 row 會保留於本地
    `sync_conflicts` 供人工裁決，不會靜默覆蓋；可用 `stock-sync-conflicts`
    CLI 查詢與標記 resolved。
  - `SourceRegistry` raw archive 的 cloud mirror／缺檔還原已以隔離 cache 實跑，
    研究切片也已在另一份工作區以 exact manifest 重播，避免只有 resolver
    單元測試通過但實際研究鏈未接上的假成功。
  - cloud mirror 暫時失敗時，本地 canonical cache／raw 寫入仍可完成，並有回歸測試。

- `stock-intraday --root C:\\skymiku\\stock --limit 3`（2026-09-20 07:58）實際
  runtime replay：10.8 秒完成、候選 3、錯誤 0；部分代號的 TWSE 308 被標記為
  不可用並排除排序，沒有 `TooManyRedirects` 或 `KeyError`。
- `stock-validate --root C:\\skymiku\\stock --shioaji --mis-seconds 5`（2026-09-20
  08:04）再次完成 **49 OK、0 warning、0 failure**；報告為
  `data/validation/source_validation_20260920_intraday_fix.json` 與同名 `.md`。
- `stock-data-audit`（2026-09-20 08:04 最新）確認：
  - `source_runs=1,281`、`source_documents=416`、`ingestion_runs=1,277`、
    `evidence_items=192`、`memory_items=107`。
  - `research_documents=153`、`research_chunks=364`、`evidence_links=201`、
    `llm_runs=196`、`claims=3,348`。
  - raw archive **416/416** 存在且 hash 相符；未註冊 raw=0；SQLite integrity=ok；
    canonical 4,598 rows，invalid/partial/empty=0。
  - source health 74 筆（73 `ok`、1 `partial`），running ingestion=0，sync conflict
    `total/open=0`；memory FTS 107/107、research FTS 364/364。
  - 既有 provenance gap 仍為 1（4 筆 2026-09-16 舊 source run），沒有新增缺口。

- next-day／watch／calendar 入口整合驗收後，`stock-data-audit`（2026-09-20 08:12）
  最新確認：
  - `source_runs=1,469`、`source_documents=549`、`ingestion_runs=1,465`、
    `evidence_items=192`、`memory_items=108`。
  - `research_documents=153`、`research_chunks=364`、`evidence_links=201`、
    `llm_runs=196`、`claims=3,348`。
  - raw archive **549/549** hash 相符、未註冊 raw=0；canonical 5,215 rows，
    invalid/partial/empty=0；SQLite integrity=ok。
  - source health 78 筆（77 `ok`、1 `partial`），running ingestion=0，sync conflict
    `total/open=0`；memory／research FTS 均一致；provenance gap 仍為 1。

- 報告證據層整合驗收（2026-09-20 08:20～08:23）：
  - intraday 與 next-day report 都會把新聞／macro 的明確 `source_document_id`
    轉成統一 Evidence packet，並同時保存於報告 JSON、`llm_daily_reports.evidence_json`
    與中央 `evidence_items`。
  - next-day 實際結果為 8 個催化事件、4 組 `event_focus`、3 筆 ranking、31 筆證據，
    其中 30 筆新聞與 1 筆 macro，31 筆皆為 `provenance_status=exact`，錯誤 0。
  - 31 筆 evidence 對應 2 個可重播的 raw source document；不存在的來源 ID 會被降級為
    `unresolved`，不會依 URL、標題或文字猜補來源。中央 evidence 共 193 筆。
  - `710 passed` 後重新執行 compile/import：147 個非 `__init__.py` Python 模組全部成功。
    `stock-data-audit` 最新為 source runs 1,544、source documents 576、raw 576/576 hash
    相符、canonical 5,272 rows、invalid/partial/empty=0、orphan=0、sync conflict=0；
    SQLite integrity=ok，memory FTS 108/108、research FTS 364/364，一般 provenance gap
    仍為 1（4 筆歷史 source run）。

- 最終來源驗證與備份（2026-09-20 08:26～08:27）：
  - `stock-validate --root C:\\skymiku\\stock --shioaji --mis-seconds 5` 完成
    **49 OK、0 warning、0 failure**；Shioaji 為 simulation/watch read-only，未呼叫下單。
  - 驗證後最新 `stock-data-audit` 為 `source_runs=1,574`、`source_documents=581`、
    `evidence_items=195`、`memory_items=114`、`llm_runs=200`、`claims=3,366`；raw **581/581** hash 相符，
    canonical 5,272 rows，SQLite integrity=ok，memory／research FTS 一致，orphan=0、
    sync open conflict=0，既有 gap 仍為 1（4 筆歷史 source run）。

- auto-research 受限實際整合（2026-09-20 08:38～08:39）：
  - `stock-auto-research --root C:\\skymiku\\stock --focus 2330 --no-etf
    --no-chips --no-llm --no-brief --no-calendar --no-auto-research` 完成，run
    `20260920_083856` 在 manifest 標示 `status=ok`、錯誤 0、警告 0。
  - 執行後 audit 為 `source_runs=1,576`、`source_documents=582`、
    `research_documents=158`、`research_chunks=369`、`evidence_links=204`、
    `llm_runs=201`、`claims=3,371`；raw **582/582** hash 相符，canonical 5,272
    rows，SQLite integrity=ok，FTS 一致、orphan=0、sync open conflict=0。

- MOPS 法說會實際 replay：
  - 行事曆更新 596 筆，找到既有簡報連結的場次可供下載。
  - 一筆實際簡報連結雖以 `.pdf` 目的名保存，回傳內容是 HTML；內容型態 fallback 已正確抽取 22,563 字。
  - 該檔案完成 `raw → source_document → research_document/chunks → exact evidence → claim`，未猜測來源。

- ETF holdings 實際 replay：
  - `ALI006` MoneyDJ 基金頁以確定性 parser 解析 10 檔持股。
  - 六檔原先使用 MoneyDJ top-N 的 ETF 已改走 etfinfo.tw Nuxt 完整 payload：
    `00998A` 50、`00402A` 49、`00404A` 53、`00405A` 47、`00406A` 62、`00407A` 50 檔。
  - `00404A`／`00406A` 的選擇權／衍生品列保留在 raw source，明確排除於股票持股模型，
    不把負權重誤當成股票持倉；未知類型仍拒絕解析。
  - 全部 ETF holdings 均完成 raw archive、exact source document、canonical CSV、research、evidence 與 claims；不需要 Gemini。

- Shioaji intraday 歷史資料實際 replay：
  - simulation 登入成功，2330、2026-09-18、1 分 K 取得並寫入 266 筆。
  - `intraday_bars` quality=`ok`，時間範圍 `09:01:00`～`13:30:00`，266 筆共用 1 個 exact raw source document。
  - raw archive、source manifest、canonical row 與 SQLite integrity 均通過；未呼叫下單。

- `!ask` 實際 no-LLM／live budget replay：
  - 查詢「台積電最近的重大訊息與最新盤中資料」時，MOPS 回傳 0 筆、Google News 回傳 8 筆，
    TWSE MIS live ticker adapter 也會保留 quote date/time；系統保留來源狀態與缺資料原因，
    未把「沒有回傳」誤判成「查無此事」。
  - 連續 3 次並行 live replay 都取得 exact `source_document_id`；週末報價日期為
    2026-09-18，因此由 `freshness=24h` 正確排除，沒有把延遲行情冒充最新行情。
  - 因目前未設定 Gemini key，驗證器正確進入 `partial` 與保守回答；本輪 live replay
    的 QueryPlan、新聞／MOPS／MIS 蒐集與 evidence 登錄均完成，但必要來源不足時：
    「我不確定，目前沒有足夠的可驗證資料。」；未寫入未驗證的長期記憶。
- `stock-cloud-sync --push --tables stock_info` 在未設定 Google 憑證時實際以 exit code 2
  拒絕執行（與現行 `cloud_sync_cli.py` 契約一致；若舊筆記曾寫 exit code 1，以程式為準），
  且本次主庫 bytes／SHA-256 前後均為 `172,634,112` /
  `f65badab1051c12fc7b409b0957ba40550db4ede4cd3f1c2e1e9f77e145c2373`，未修改本地
  SQLite；不把 cloud sync 未設定偽裝成成功。
- `stock-gemini-test --root C:\\skymiku\\stock` 實際以 exit code 1 回報
  `GEMINI_API_KEY is not set`；因此 `!ask` 在目前環境只能驗證證據蒐集與保守棄答，
  尚不能宣稱已完成 Gemini 生成回答的 live 驗證。

- 全 ETF pipeline 實際整合：
  - `stock-auto-research --root C:\\skymiku\\stock --no-chips --no-llm --no-brief --no-calendar --no-auto-research`
    實跑 28 檔，28 檔成功、0 失敗、0 警告，run id=`20260920_052605`。
  - pipeline、`run.json`、中央 `data/pipeline_runs/manifest.jsonl` 與完成事件一致標示 `status=ok`；
    `source_incomplete_top_n` 保護邏輯仍保留，防止未來來源退化時假補完整持股。

- 2026-09-20 06:24 從 `C:\\Windows\\Temp` 重跑 ETF／研究管線：28 檔成功、0 失敗、0 警告，
  187 檔焦點個股，run id=`20260920_062415`，輸出與事件均標示 `status=ok`；外部 CWD 沒有新增 log。

### Evidence、研究層與記憶

- 既有 MOPS、月營收、季報、估值、股利、新聞、macro 與技術資料仍走
  `raw → canonical/research → evidence → claim` 的可追溯鏈。
  - 4 筆舊 `source_runs` 因沒有可證明的 raw manifest／document ID，保留為歷史 gap，沒有猜測補鏈；
    `stock-data-audit --json` 現在會列出每筆 `unresolved_source_runs` 及其
    `reason_code`／`remediation`，以便後續重新抓取或人工追查；不會依 URL、標題或
    相似度猜補 document ID。
- 43 筆舊 LLM 分析保留為 `legacy_unresolved`，不冒充已驗證證據。
  `stock-provenance-backfill` exact-only dry-run 找不到任何可由 JSONL 明確證明的新鏈結；
  因此沒有執行猜測式回填。
- `stock-memory backfill-db` 實際執行結果為 `long_analyses=0`、
  `long_analysis_skipped=43`、`skip_reasons={llm_failed: 43}`；因此長期記憶為 0
  是失敗 LLM 呼叫被安全排除，不是回填流程靜默遺失。
- `stock-calibrate --root C:\\skymiku\\stock --minimum-samples 1` 目前為
  `sample_count=0`、`status=insufficient`；43 筆舊失敗 LLM 呼叫明確歸類為
  `llm_failed`，不進入評分校準。
  - 短期記憶：目前 106 筆 active，仍未達 80% 容量警戒（上限 2,000,000 字）。
- 長期記憶：0 筆；沒有通過品質與 evidence gate 的內容不會被硬升級。
- auto-research 長期升級已加入 exact/composite source-document gate；混合低可信
  evidence 時只保存合格 evidence 並標 `partial`，完全沒有合格 evidence 時跳過升級。
- 記憶查詢預設排除 `unknown`／`failed`；管理者回放時可使用
  `stock-memory search ... --include-low-quality`，因此「保存」與「可作為可信回答上下文」是分開的。

- 全市場 materialization 後的最新 `stock-data-audit --json`：
  - `source_runs=1,576`、`source_documents=582`、`ingestion_runs=1,572`。
  - `evidence_items=6,110`、`research_documents=6,074`、`research_chunks=6,285`、
    `evidence_links=6,119`、`llm_runs=6,117`、`claims=32,961`。
  - canonical rows **11,185**：monthly_revenue=1,978、valuation_daily=1,964、
    quarterly_report=1,974；8 張 canonical table 均為 `ok`，invalid/partial/empty=0。
  - raw archive **582/582** 存在且 SHA-256 相符；canonical 三個 fundamental domain
    exact source link orphan=0；SQLite integrity=ok；memory FTS 114/114、research
    FTS 6,285/6,285；sync conflict total/open=0。
  - 唯一 warning 仍是 4 筆 2026-09-16 舊 source run 無可證明 manifest；另有 43 筆
    舊 LLM 分析維持 `legacy_unresolved`，兩者均未被猜測式回填。

- 最新完整來源驗證與 TAIFEX 降級路徑（2026-09-20 09:08）：
  - `stock-validate --root C:\\skymiku\\stock --shioaji --mis-seconds 5` 共 49 項，
    **48 OK、1 warning、0 failure**。Shioaji simulation／contract／snapshot 與 TWSE
    MIS read-only poll 均通過，沒有呼叫下單。
  - TAIFEX live endpoint 在目前網路環境回傳 HTTP 403；系統沒有把快取冒充即時資料，
    而是驗證 SHA-256 後回放 `2026-09-18` 的 exact raw archive，標示
    `quality=partial`、`cached=true`、`stale/not live`，並保留同一條
    source document → research → evidence → claim 鏈。
  - 最新 `stock-data-audit --json`：`source_runs=1,660`、`source_documents=594`、
    `ingestion_runs=1,656`、`evidence_items=6,117`、`research_documents=6,083`、
    `research_chunks=6,294`、`evidence_links=6,126`、`llm_runs=6,126`、
    `claims=33,000`；canonical rows **11,185**，invalid/partial/empty=0。
  - raw archive **594/594** 存在且 SHA-256 相符；SQLite integrity=ok、research／memory
    FTS 一致、orphan=0、sync open conflict=0。剩餘只有 4 筆舊 source run 無法 exact
    回溯，以及 43 筆 `legacy_unresolved` 舊 LLM 分析；兩者均未猜測補鏈。
- 全市場基本面 raw-first 回填已補齊股利（2026-09-20 09:20）：
  - `stock-fundamentals-refresh --materialize-all` 僅重播既有 exact raw archive，月營收
    1,978、估值 1,964、季報 1,974、股利 2,141 筆成功，總 attempted=8,057、failed=0。
    估值 1 筆與季報 4 筆為來源規則下的合法略過，不是失敗。
  - 股利來源原本已有 2,189 筆 raw rows／2,003 檔股票，但 canonical 只有 2 筆；新增
    全市場聚合後 canonical `dividend_history=2,141`，每筆仍通過 exact source document
    hash、research、evidence、claim 鏈，重複的同股票同年度分配會在同一份來源文件內聚合。
  - 回填後 audit：`evidence_items=12,194`、`research_documents=14,138`、
    `research_chunks=14,349`、`evidence_links=12,203`、`llm_runs=14,181`、
    `claims=71,146`；canonical rows **13,324**，8 張 canonical table 全部 `ok`，
    invalid/partial/empty=0，raw 594/594、orphan=0、FTS 一致。
- 基本面批次冪等 smoke（2026-09-20 09:25）：
  - 以 `--materialize-limit 1` 重跑四個資料域，結果為 0 failed；月營收命中
    `reused=1`，其他域沒有新增 canonical／research／evidence／claim。
  - 重跑前後計數保持 `evidence=12,194`、`research=14,138`、`chunks=14,349`、
    `links=12,203`、`llm_runs=14,181`、`claims=71,146`、`dividend_history=2,141`。
- 股利問答整合 smoke（2026-09-20 09:28～09:29）：
  - 「台積電近年股利與現金股利是多少？」現在會命中
    `resource_needs=[memory, fundamental, ticker_snapshot]`，`planner_required=false`，
    並取得 2026、2025 兩筆 `dividend_history` exact evidence；「近年／歷年」會保留
    最多五個年度，不再只取最新一筆。
  - 在未設定 Gemini 時，結果仍依政策為 `uncertain`，但 collection=`ok`、missing
    resources=0；這證明資料蒐集與 provenance 已完成，未把 LLM 缺少誤判為來源缺失。
  - 問答 smoke 後最新 audit 相關計數為 `evidence_items=12,197`、`evidence_links=12,206`、
    `memory_items=117`、`ask_runs=80`；SQLite integrity=ok、memory／research FTS 一致。
- 跨資源問答 smoke（2026-09-20 09:31）：
  - 營收／估值／股利／季報均進入 `fundamental`；技術、籌碼、ETF、macro 均各自
    命中對應 adapter，所有案例沒有 exception。
  - 「最新」查詢在目前非交易日資料超過 freshness window 時，會列出缺少或 stale
    的 ticker snapshot；新聞未啟用 live 時回報 `partial/live_disabled`。系統沒有用
    舊日 K 假裝盤中行情，也沒有把來源失敗轉成「查無此事」。
  - smoke 後 audit：gap=1（仍為 4 筆歷史 source run）、raw 594/594、SQLite integrity=ok、
    orphan=0、sync conflict open=0、8 張 canonical table 全部 `ok`；最新計數為
    `evidence_items=12,198`、`evidence_links=12,222`、`memory_items=126`、
    `ask_runs=89`，memory／research FTS 均一致。
- QueryPlan 搜尋正規化 follow-up（2026-09-20 10:39）：
  - 複合問題不再把完整格式指令送入 Google News；實際 query 會保留 ticker／公司名、
    主題詞、新聞意圖 OR 群組與 `when:7d`。
  - 真實 `stock-ask --live --no-llm` 重跑取得 2026-09-18 的新聞 evidence；
    MOPS 當次無 2330 結果仍標 `partial`，未被誤判為查無此事。
  - 實際搜尋 query 已寫入同一 trace 的 `resource_outcomes.metadata.search_query`，
    可追蹤搜尋輸入與結果。
- QueryPlan metadata 與恢復點驗收（2026-09-20 10:41～10:44）：
  - 再次以 `stock-ask --live --no-llm` 實跑台積電複合問題；Google News 實際使用
    `2330 台積電 季報 (新聞 OR 法說 OR 法說會 OR 營收) when:7d`，回傳近期新聞，
    MOPS 無 2330 詳情時保持 `partial`，沒有將來源失敗解讀成「沒有資料」。
  - 最新 trace 的 `resource_outcomes.metadata.search_query` 已確認可回放；因未啟用
    Gemini，最後保守回覆「我不確定，目前沒有足夠的可驗證資料」，未寫入未驗證長期記憶。
  - 最新資料庫稽核：`source_runs=1,716`、`source_documents=611`、
    `ingestion_runs=1,712`、`evidence_items=12,214`、`memory_items=131`、
    `research_documents=14,143`、`research_chunks=14,361`、`evidence_links=12,238`、
    `llm_runs=14,186`、`claims=71,190`、`ask_runs=94`；SQLite integrity、raw
    archive 611/611、FTS、orphan 與 sync conflict 均通過。
  - 同次唯讀 research audit 為 `documents=14,143`、`chunks=14,361`、
    `errors=0`、`warnings=3`：1 筆歷史 HTML/PDF quarantine、18 組版本衝突候選、
    23 組只有抓取版本而未標定資料期間；所有原始 hash 均保留，未自動覆蓋或猜補。
  - `stock-memory` 容量檢查：short active `129/500`、`89,384/2,000,000` chars；
    long active `2/10,000`、`62,253/8,000,000` chars；兩層均未達 80% 警戒，沒有
    過期項目或 orphan memory evidence。
- 自動研究狀態傳遞 follow-up（2026-09-20 11:00～11:01）：
  - 修正 `run_full_pipeline`：macro 的來源日期混合、TAIFEX exact-cache partial、
    futures quality 與籌碼無資料不再只存在 log，而會寫入 `run.json` 的 summary／warnings。
  - 真實 `stock-auto-research --focus 2330 --no-llm --no-brief --no-calendar
    --no-auto-research`：ETF 28/28 成功、187 個焦點、錯誤 0、警告 9；其中 83 個
    標的沒有可用籌碼交易日，明確標示「不等於籌碼為 0」，整體狀態為 `partial`。
  - macro summary 現在保存 `source_date_status=mixed`、逐項 `source_dates`、
    futures `quality_status=partial`／`cached=true`／exact `source_document_id`，
    避免下游把跨日或 stale 資料當成同時點行情。
- macro evidence 品質一致化 follow-up（2026-09-20 11:15）：
  - 新增共用 `macro_quality_status`；來源日期為 mixed／unknown、存在來源品質備註，
    或 futures 為 partial／cached 時，`pipeline_shared`、`!ask` adapter、研究切片與
    raw manifest 統一標示 `quality_status=partial`，仍保留 evidence 供追蹤，不誤報為空資料。
  - aligned 且沒有降級備註的快照維持 `quality_status=ok`；相關 macro research、
    adapter、pipeline 回歸共 50 passed，完整回歸為 730 passed。
- CLI 入口 smoke（2026-09-20）：`pyproject.toml` 宣告的 30 個 `stock-*` 公開入口
  全部以 `--help` 啟動成功，30/30 exit code=0；沒有因 optional credential 或
  網路來源在載入階段崩潰。
- canonical provenance domain audit（2026-09-20）：
  - `canonical_audit` 現在除了檢查 source document 是否存在，也驗證來源域是否
    對應 canonical table；例如月營收不可連到 valuation、股利不可連到新聞、日 K
    不可連到季報來源。
  - 目前正式資料庫結果為 8 張 canonical table：`invalid=0`、`partial=0`、
     `empty=0`；raw 594/594 hash 相符、orphan=0、sync open conflict=0，沒有發現
     跨來源誤鏈。錯誤 provenance 的測試案例會被標為 `provenance_domain` error。
- research source contract audit（2026-09-20）：
  - 新增唯讀 `bot.research_audit.audit_research_database()`，檢查每份 research
    document 的 exact source、raw SHA-256、日期格式／順序、research chunks、MIME／副檔名
    與同一來源參照的版本狀態；不依 URL、標題或相似度修補來源。
  - 正式庫檢查 `research_documents=14,139`、`research_chunks=14,351`，raw archive
    為 595/595 hash 相符。原本 1 筆 MOPS 簡報 HTML/PDF 誤鏈已標記為 quarantine，
    並以真正 27 頁 PDF 建立新的 exact research chain；新的 `mops_presentation`
    ingest 也已拒絕同類 HTML 內容。
  - 另列出 22 組沒有 `as_of`、但以互異 `retrieved_at` 保存抓取版本的 raw 快照，
    以及 18 組有多個版本的來源參照；抓取版本可追溯，但資料有效期間仍未標定，
    不可把抓取時間當成市場日期。它們保留所有 hash，不覆蓋、不自動挑選「最新」。
    目前 audit gap 為 2：研究期間／隔離警告，以及 4 筆既有 source run 歷史缺口；
    研究稽核 errors=0。

### 備份、scheduler 與 dashboard

- SQLite 備份成功：`data/backups/stock_20260920_085255_full-fundamentals-final.db`
  （包含本輪外部 CWD `stock-validate` 報告與 logger root isolation、TAIFEX CSV 解析／exact provenance、TAIFEX `!ask` 資源隔離、ETF／研究管線 E2E、完整 49 項來源驗證、基本面 raw-first replay、月營收／估值 `!ask --no-llm` replay、30 個 CLI 入口 smoke、backup／preflight root isolation 實跑、exact hash provenance audit、00929 yfinance provenance 邊界修補、manifest exact-ID／raw replayability guard，以及
  既有來源／記憶／同步驗收）。備份先寫入同目錄暫存檔，再以原子替換完成，避免中斷時
  留下半份備份；主庫與本備份
  備份的 `PRAGMA integrity_check` 均為 `ok`。
- 主資料庫與備份的 `PRAGMA integrity_check` 均為 `ok`；兩者均含 582 個
  `source_documents`、6,110 個 `evidence_items`、114 個 `memory_items`、6,117 個 `llm_runs` 與 2 個
  `llm_daily_reports`。
- 最新 TAIFEX fallback 驗收後另建立備份：
  `data/backups/stock_20260920_091150_taifex-fallback-final.db`。
  主庫與此備份均為 `PRAGMA integrity_check=ok`，且 `source_documents=594`、
  `evidence_items=6,117`、`research_documents=6,083`、`research_chunks=6,294`、
  `evidence_links=6,126`、`llm_runs=6,126`、`claims=33,000`、
  `memory_items=114`；FTS 索引與 canonical table 筆數一致。
- 最新完整基本面回填後備份：
  `data/backups/stock_20260920_092130_full-fundamentals-dividend-final.db`。
  主庫與此備份均為 `PRAGMA integrity_check=ok`，且 18 個核心表／FTS 計數完全一致；
  此恢復點包含 2,141 筆 exact-provenance `dividend_history`。
- 最新問答整合 smoke 後備份：
  `data/backups/stock_20260920_093015_ask-integration-final.db`。
  主庫與此備份均為 `PRAGMA integrity_check=ok`，`ask_runs=80`、`evidence_items=12,197`、
  `memory_items=117` 與兩個 FTS 索引計數一致。
- 跨資源 smoke 完成後的最新恢復點：
  `data/backups/stock_20260920_093241_final-integration-smoke.db`。
  主庫與此備份均為 `PRAGMA integrity_check=ok`，`ask_runs=89`、`evidence_items=12,198`、
  `evidence_links=12,222`、`memory_items=126`、`research_documents=14,138`、
  `research_chunks=14,349`、`claims=71,146` 與兩個 FTS 索引計數完全一致。
- provenance domain audit 後的最新恢復點：
  `data/backups/stock_20260920_094021_provenance-domain-audit-final.db`。
  主庫與此備份均為 `PRAGMA integrity_check=ok`，核心 canonical、research、evidence、
  memory、ask 與 FTS 計數完全一致。
- research source contract audit 後的最新恢復點：
  `data/backups/stock_20260920_095807_research-contract-audit-final.db`。
  主庫與此備份均為 `PRAGMA integrity_check=ok`，`source_documents=594`、
  `research_documents=14,138`、`research_chunks=14,349`、`evidence_items=12,198`、
  `memory_items=126`、`ask_runs=89` 一致；本次只讀稽核沒有改寫業務資料。
- MOPS 修復與 derived-chain quarantine 後的最新恢復點：
  `data/backups/stock_20260920_101412_mops-repair-final.db`。
  主庫與此備份均為 `PRAGMA integrity_check=ok`，`source_documents=595`、
  `research_documents=14,139`、`research_chunks=14,351`、`evidence_items=12,199`、
  `claims=71,147`、`memory_items=126`、`ask_runs=89` 一致。
- 正式來源驗證 follow-up 後的最新恢復點：
  `data/backups/stock_20260920_103036_source-validation-followup.db`。
  49 項來源檢查為 **48 OK、1 warning、0 failure**；主庫與此備份均為
  `PRAGMA integrity_check=ok`，`source_documents=600`、`research_documents=14,143`、
  `research_chunks=14,361`、`evidence_items=12,206`、`claims=71,190`、
  `memory_items=126`、`ask_runs=89` 一致。唯一 warning 為 TAIFEX live 403，
  已回放 exact raw cache 並標記 partial/stale，不代表即時行情。
- QueryPlan metadata 驗收後的最新恢復點：
  `data/backups/stock_20260920_104452_ask-query-normalization-followup.db`。
  主庫與備份均為 `PRAGMA integrity_check=ok`，`source_documents=611`、
  `research_documents=14,143`、`research_chunks=14,361`、`evidence_items=12,214`、
  `claims=71,190`、`memory_items=131`、`ask_runs=94` 一致。
- 自動研究狀態傳遞修補後的最新恢復點：
  `data/backups/stock_20260920_110407_pipeline-status-followup.db`。
  主庫與備份均為 `PRAGMA integrity_check=ok`，`source_runs=1,803`、
  `source_documents=647`、`ingestion_runs=1,799`、`research_documents=14,179`、
  `research_chunks=14,506`、`claims=72,887`、`memory_items=131`、`ask_runs=94` 一致；
  raw archive 647/647 hash 相符，canonical、FTS、orphan 與 sync conflict 均通過。
- macro evidence 品質回填後的最新恢復點：
  `data/backups/stock_20260920_112140_macro-quality-repair-final.db`。
  48 份 `market_macro` raw manifest 以 exact `source_document_id` 重跑研究切片，
  44 筆冪等重用、4 筆建立／補齊、0 失敗；source quality 為 `ok=38`、`partial=10`。
  主庫與備份均為 `PRAGMA integrity_check=ok`，`source_documents=647`、
  `evidence_items=12,216`、`research_documents=14,183`、`research_chunks=14,510`、
  `evidence_links=12,240`、`claims=72,907`、`memory_items=131`、`ask_runs=94` 一致。
- 最新來源驗證後的恢復點：
  `data/backups/stock_20260920_113251_source-validation-final.db`。
  主庫與備份均為 `PRAGMA integrity_check=ok`，`source_documents=655`、
  `evidence_items=12,220`、`research_documents=14,189`、`research_chunks=14,516`、
  `claims=72,933`，核心計數一致。
- 最新 `!ask` 熱路徑恢復點：
  `data/backups/stock_20260920_113645_ask-cli-final.db`。
- 執行入口 follow-up（2026-09-20 11:38～11:40）：
  - `stock-scheduler --root C:\\skymiku\\stock --once --dry-run` exit code 0，排程任務
    路徑指向 C 槽，未執行交易或外部寫入任務。
  - 從 `C:\\Windows\\Temp` 使用 `uv run --project C:\\skymiku\\stock stock-dashboard`
    啟動 port 8810，`/_stcore/health=ok`、首頁 HTTP 200／5,381 bytes，測試後已停止。
- 最新自動研究實跑（2026-09-20 11:41～11:42）：
  - `stock-auto-research --root C:\\skymiku\\stock --focus 2330 --no-llm --no-brief
    --no-calendar --no-auto-research` 完成於 68.2 秒；ETF 28/28 成功、research 28/28
    reused／成功、焦點 187、錯誤 0、警告 6、狀態 partial。
  - 83 個標的沒有籌碼交易日資料，明確標示不等於籌碼為 0；macro 因來源日期混合而
    partial；LLM 未啟用時沒有產生未驗證結論。
  - `stock-data-audit` 回報 raw 658/658、SQLite ok、FTS 一致、orphan=0、sync conflict=0、
    canonical 13,324 筆無異常、research errors=0。
- preflight 資料準確性修補（2026-09-20 11:26）：
  - Shioaji simulation 不支援 `account_balance` 時，preflight 現在回傳
    `status=info`、`balance_available=false`、`balance=null`，不再把 API 空值誤報為
    `0 TWD`；正式環境仍會保留可驗證的數值 0。
  - 實際 C 槽 simulation smoke 已驗證此輸出；新增測試後完整回歸為 732 passed。
- 最新完整來源驗證（2026-09-20 11:30～11:32）：
  - `stock-validate --root C:\\skymiku\\stock --shioaji --mis-seconds 3`：49 項中
    **48 OK、1 warning、0 failure**；Shioaji login、2330 contract／snapshot、TWSE MIS
    poll 成功並正常登出，沒有下單。唯一 warning 是 TAIFEX live 403，已使用 exact raw
    cache 並標示 partial/stale。
  - 驗證後新增 8 份可追溯 raw／research 資料；`stock-data-audit` 顯示 raw 655/655
    hash 相符、orphan=0、sync conflict=0、canonical 13,324 筆 invalid/partial/empty=0、
    research errors=0、warnings=3。
- `!ask --live --no-llm` 最新熱路徑驗證（2026-09-20 11:35）：
  - `2330 台積電最近的新聞與法說` 成功建立 QueryPlan、執行 news／MOPS／TWSE MIS
    adapters，保留 exact evidence，依 freshness 排除 2 筆過期證據；MOPS 空結果被標為
    partial 而非「查無此事」，未啟用 LLM 時保守回覆不確定，沒有捏造分析。
  - 30 個公開 CLI 入口全部 `--help` 成功（30/30）；主庫與最新備份均為
    `PRAGMA integrity_check=ok`，`source_documents=657`、`evidence_items=12,223`、
    `evidence_links=12,247`、`llm_runs=14,232`、`claims=72,933`、`memory_items=132`、
    `ask_runs=95`、`memory_events=137` 一致。
- `stock-scheduler --once --dry-run` 正常完成；Windows status replace lock 已加入重試，沒有留下 `scheduler_status.tmp`。
- `stock-dashboard --server.headless true --server.port 8795` 實際啟動後，`/_stcore/health` 回傳 HTTP 200；wrapper 的 server flags 已正確轉送。
- 本輪從 `C:\\Windows\\Temp` 以 C 槽專案啟動 Dashboard（port 8802）再次驗證：health HTTP 200、首頁 HTTP 200／5,381 bytes，
  測試程序已正常停止。
- 2026-09-20 07:44 從 `C:\\Windows\\Temp` 再次啟動 Dashboard（port 8804）：health 為
  `ok`、首頁 5,381 bytes，測試程序已正常停止。
- 從 `C:\Windows\Temp`（專案外部目錄）啟動 dashboard 也完成驗證：`/_stcore/health`
  與首頁均回傳 HTTP 200，未因外部 CWD 失去專案設定或資料根目錄。
- 從 `C:\\Windows\\Temp` 執行 `stock-scheduler --root C:\\skymiku\\stock --once --dry-run`
  exit code 0，排程計畫與 company 任務路徑均指向 C 槽。
- `stock-preflight --json` 通過：Shioaji simulation 憑證存在且有效、simulation 帳戶登入成功；RUN_MODE=watch，未啟用下單。
- Google cloud-file cache 實際 round-trip 通過：local file → mirror → 刪除 local → restore；
  `gspread 6.2.1` 與 `google-auth` 也可載入。
- Dashboard 的 Google Sheets Pull 與雙向 Sync 現在會先建立 SQLite online backup；
  備份失敗會阻止本地寫入。CLI 原有的 `pre-pull` 備份行為仍保留。
- MOPS 法說會行事曆研究切片（2026-09-20 11:49～11:53）已完成：16 份 exact raw
  source documents 中，12 份成功建立 `source_document → research_document/chunk →
  evidence → source-fact claim` 鏈，4 份空白快照保留為 `empty`，沒有臆造事件；共
  2,384 筆有效活動、8 筆同快照重複資料去重、0 筆 invalid／failed。重跑具冪等性，
  不會重複建立研究或證據。
- 本輪新增 `tests/test_mops_conference_research_slice.py` 後，完整非 integration／slow
  回歸為 **735 passed, 1 warning**。最新資料稽核為 `source_runs=1,894`、
  `source_documents=658`、`research_documents=14,202`、`research_chunks=14,813`、
  `evidence_items=12,227`、`evidence_links=12,251`、`claims=75,322`；raw
  `658/658` hash 相符、SQLite integrity=ok、FTS 一致、orphan=0、research errors=0。
- 本輪正式恢復點為
  `data/backups/stock_20260920_115327_mops-calendar-research-final.db`，備份大小
  180,686,848 bytes；未修改交易／下單路徑。
- 最新 C 槽公開來源／Shioaji 唯讀驗證（2026-09-20 12:00～12:01）為 **49 OK、0
  warning、0 failure**：MOPS 254 筆、新聞 30 筆、DuckDuckGo 5 筆、macro 6 組、
  TAIFEX 1 筆、28 檔 ETF、Shioaji simulation login／contract／snapshot 與 TWSE MIS
  連續輪詢均通過；沒有呼叫下單。驗證後稽核為 `source_runs=1,924`、
  `source_documents=662`、`research_documents=14,205`、`research_chunks=14,816`、
  `evidence_items=12,229`、`evidence_links=12,253`、`claims=75,335`，raw
  `662/662` hash 相符、SQLite integrity=ok、FTS 一致、orphan=0。
- 來源驗證後正式恢復點為
  `data/backups/stock_20260920_120117_source-validation-49ok-final.db`；未發現新的
  provenance 或資料品質錯誤。
- 全部 `bot` package module import sweep（153 個模組）無 import failure；同步邊界也已
  實測：在未設定 `GOOGLE_SHEET_ID`／`GOOGLE_SA_JSON_PATH` 時，
  `stock-cloud-sync --push` 以 exit code 2 明確拒絕，SQLite SHA-256 前後一致，沒有
  產生本地資料修改。
- canonical provenance follow-up：`stock_info`、`price_history`、`monthly_revenue`、
  `valuation_daily`、`dividend_history`、`quarterly_report`、`intraday_bars` 與
  `llm_daily_reports` 的空白 `source_record_id` 都是 0；769 筆 `memory_evidence`
  關聯沒有 orphan。剩餘 43 筆是既有 legacy LLM row，保留為 unresolved，不升格為
  已驗證市場事實。
- Google News／DuckDuckGo 搜尋研究切片已完成（2026-09-20 12:14～12:17）：既有 95 份
  exact `search_response` raw 中，92 份建立 `news_search`／`web_search` research、
  evidence 與 source-excerpt claims，3 份空回應保留為 `empty`，0 份 failed。完整回歸
  更新為 **739 passed, 1 warning**；稽核為 `source_documents=664`、
  `research_documents=14,297`、`research_chunks=15,674`、`evidence_items=13,803`、
  `evidence_links=13,827`、`claims=79,239`，raw `664/664`、SQLite integrity=ok、
  FTS 一致、orphan=0、research errors=0。
- 本輪恢復點為
  `data/backups/stock_20260920_121722_web-search-research-final.db`；搜尋摘要仍只
  能支持 raw 中可核對的標題／摘要，不會被升格成全文或投資結論。
- 新切片 production 熱路徑（2026-09-20 12:18）再次查詢「台積電最近的新聞與法說」：
  Google News 取得 8 筆、MOPS 空結果保持 partial，無 LLM 時回覆「我不確定」並保留
  trace／EvidencePack，沒有產生未驗證答案。其後稽核為 `source_documents=666`、
  `research_documents=14,298`、`research_chunks=15,677`、`evidence_items=13,808`、
  `evidence_links=13,832`、`claims=79,247`，raw `666/666`、SQLite integrity=ok、
  FTS 一致、orphan=0；恢復點為
  `data/backups/stock_20260920_121902_web-search-ask-hotpath-final.db`。

### 2026-09-20 12:24：整合前置檢查入口

- `stock-preflight` 原本只負責交易可行性，因此新增唯讀
  `stock-system-preflight`，不把研究資料庫或可選外部服務混入交易
  `can_simulate` 判定。
- `uv run --no-sync python -m bot.system_preflight_cli --root C:\skymiku\stock --json`
  實跑結果為 `status=partial`：SQLite integrity、raw `666/666` hash、FTS 與
  provenance orphan 均通過；仍保留 2 個既有 warning：
  `research_semantic_partial`、`source_runs_without_raw_manifest`。
- Gemini 與 Google Sheets 缺少設定被正確標成 `info`，不會被誤報為本地核心故障；
  `--imports` 實際逐一載入 **156 個** `bot` 模組，全部成功，且不會執行資料抓取或下單。
- 新增整合檢查測試後，相關 preflight 測試為 **20 passed**；完整回歸需在本輪
  程式變更完成後為 **744 passed, 1 warning**；warning 仍是 Shioaji 第三方套件
  的 Pydantic deprecation，不是本專案測試失敗。

## 尚未能在目前環境證明的項目

以下不是程式已知失敗，而是需要外部設定或真實交易時段才能驗證：

1. `GEMINI_API_KEY` 尚未設定，因此目前可以完成 QueryPlan、資料蒐集、EvidencePack、驗證與保守 fallback，但不能宣稱已完成真實 LLM 生成回答。
2. Google Sheets／Drive 未設定 `GOOGLE_SHEET_ID` 與 `GOOGLE_SA_JSON_PATH`，所以只驗證 cloud-file cache／mock 同步，沒有假裝 Google API live sync 成功。
3. Shioaji simulation／watch 與 5 秒 MIS read-only poll 已通過；正式環境、正式憑證啟用與真實交易時段的即時行情仍未宣稱通過，也沒有測試下單。
4. 4 筆 legacy source run 與 43 筆 legacy LLM 分析仍待有可證明的原始檔才能補鏈。

## 建議的下一個驗證順序

1. 先設定並保護 `GEMINI_API_KEY`，用固定的 `!ask` 測試集驗證回答、claim、數值／日期與 abstain 行為。
2. 若需要雲端備份，再設定 Google service account；目前無憑證時 CLI 會明確拒絕且不修改本地資料，設定後先做 `--pull`／dry-run，再做小範圍 `--push`。
3. 若要驗證正式環境，另行在非下單、可控的交易時段執行 Shioaji／MIS read-only smoke；目前已完成 simulation／watch 隔離驗證。
4. 取得實際 ETF holdings raw 與法說 PDF 後重跑兩條 slice，驗證抽取結果與來源專用日期／單位／衝突規則；不把 cache-only 資料提升成 exact provenance。

### 2026-09-20 12:35～12:38：來源、問答與同步再驗收

- `stock-validate --root C:\skymiku\stock --shioaji --mis-seconds 3` 再次完成
  **49 OK、0 warning、0 failure**；本次重新抓取的 MOPS、新聞、DuckDuckGo、macro、
  TAIFEX、ETF、Shioaji simulation／contract／snapshot 與 TWSE MIS 均通過。
- 來源驗證後備份為
  `data/backups/stock_20260920_123602_post-source-revalidation.db`；主庫與備份
  `PRAGMA integrity_check` 均為 `ok`。
- 其後以已安裝的 `stock-ask --live --no-llm` 執行台積電新聞／法說查詢，Google News
  取得 8 筆、MOPS 為 partial；系統回覆「我不確定」，保存 trace／EvidencePack，
  沒有產生未驗證結論。最終恢復點為
  `data/backups/stock_20260920_123745_post-ask-integration.db`。
- 最終稽核為 `source_documents=673`、`research_documents=14,304`、
  `evidence_items=13,812`、`claims=79,278`；raw `673/673`、SQLite integrity=ok、
  FTS 一致、orphan=0，仍只有 2 個既有 warning gap。
- 未設定 Google Sheets 憑證時，`stock-cloud-sync --push` 以 exit code 2 拒絕，
  SQLite SHA-256 前後一致；同步模組不會在未配置時寫入本地資料。
- 停止遺留的 8802 測試 Dashboard 後，`uv run stock-system-preflight --json` 與
  `uv run stock-ask ...` 入口均可正常使用；完整回歸維持 **744 passed, 1 warning**。

### 2026-09-20 12:40～12:43：運行入口驗收

- 30 個非 Dashboard 的 `pyproject.toml` console entry points 逐一執行 `--help`，
  全部 exit code 0；不包含會啟動長駐服務的 Dashboard server。
- 從 `C:\Windows\Temp` 啟動 C 槽 Dashboard，`/_stcore/health` 回傳 `ok`、首頁
  HTTP 200；測試使用 8813 port，完成後已停止測試程序。
- 從外部 CWD 以明確專案解析執行
  `uv run --project C:\skymiku\stock --no-sync stock-scheduler --root C:\skymiku\stock --once --dry-run`，
  exit code 0；任務命令、log 路徑與資料根均正確指向 C 槽，且 dry-run 沒有執行寫入。
  單獨在外部 CWD 執行未安裝的 `uv run stock-scheduler` 會找不到 entry point，這是
  `uv` 專案解析限制，不是 scheduler 模組錯誤。

### 2026-09-20 12:51～12:53：每日報告 provenance 回放修復

- 發現舊版 `intraday`／`next_day_watch` 報告回放會繞過
  `persist_report_evidence`，直接把 legacy `news:*` packet 寫入報告表；這會讓
  報告記憶缺少可核對的來源狀態。
- 新增 `prepare_report_payload_for_storage` 與明確 opt-in 的
  `repair_stored_report_provenance`。現在報告檔回放會重新執行 exact raw 檢查，
  無法回放時只降為 `unresolved`，不依 URL／標題猜來源。
- 正式庫既有 2 筆每日報告已修復；每筆 31 個 evidence 均為 exact，報告標示
  `composite_exact`。因為報告是多來源聚合，`llm_daily_reports.source_record_id`
  保持空白，完整來源以 `evidence_json` 與 evidence links 表達，避免錯誤指定單一來源。
- 修復前備份為
  `data/backups/stock_20260920_125124_pre-report-provenance-repair.db`，修復後恢復點為
  `data/backups/stock_20260920_125218_post-report-provenance-repair.db`；兩者及主庫
  `PRAGMA integrity_check` 均為 `ok`。完整回歸更新為 **746 passed, 1 warning**。

### 2026-09-20 13:03：每日報告 provenance 稽核加固

- `stock-data-audit` 現在會逐筆檢查 `llm_daily_reports.evidence_json`：evidence ID
  是否存在、packet 的 `source_document_id` 是否與中央 evidence registry 一致、來源
  文件是否存在，以及 raw archive 的 SHA-256 是否仍可精確回放。
- 多來源聚合報告不再以空白的單一 `source_record_id` 判定失敗；只要每個 packet 都是
  exact 且可回放，就維持 `composite_exact`。相反地，舊版 legacy packet、缺來源 ID、
  source mismatch 或 raw 不可回放，會產生
  `llm_daily_report_unresolved_evidence` warning，不會被當成已驗證報告。
- 新增 exact composite／unresolved／malformed evidence 回歸測試；針對性測試 **21 passed**，
  完整非 integration／slow 回歸為 **749 passed, 1 warning**。壞 JSON 只計入未解析
  evidence，不會重複計入「ok 但無 evidence」。唯一 warning 仍是 Shioaji
  第三方套件的 Pydantic deprecation。
- 正式庫只讀稽核結果：每日報告 unresolved=0、無 `ok` 但沒有 evidence 的報告；raw
  `673/673`、hash mismatch=0、SQLite integrity=ok；既有 gap 維持 2 個：
  `research_semantic_partial` 與 4 筆歷史 `source_runs_without_raw_manifest`。

### 2026-09-20 13:07～13:10：來源與 `!ask` 再驗收

- `stock-validate --root C:\\skymiku\\stock --shioaji --mis-seconds 3` 實際完成
  **49 OK、0 warning、0 failure**；本輪涵蓋 TWSE／TPEx 基本面與歷史日 K、MOPS、
  重大訊息、籌碼、TDCC、鉅亨新聞、DuckDuckGo、macro、TAIFEX、31 個 ETF、
  Shioaji simulation／contract／snapshot 與 TWSE MIS 延遲行情。沒有呼叫下單路徑。
- 驗證後資料鏈仍完整：raw **677/677**、缺檔 0、hash mismatch 0、SQLite integrity=ok、
  relational orphan=0、FTS 計數一致、每日報告 unresolved=0。新增來源資料沒有造成既有
  2 個 warning gap 增加。
- `stock-ask --live --no-llm --ticker 2330 --json` 實際走過 QueryPlan、條件式新聞／MOPS／
  TWSE MIS 蒐集與 EvidencePack；MOPS 空結果被標為 partial，沒有被誤當成「查無此事」，
  並在未設定 Gemini 時保守回答「我不確定，目前沒有足夠的可驗證資料」。
- 未設定 Google Sheets 憑證時，`stock-cloud-sync --push` 回傳 exit code 2，SQLite SHA-256
  前後一致；同步功能維持 fail-closed，不會在未配置時破壞本地 SSOT。

### 2026-09-20 13:16～13:17：多版本 evidence link 整合修復

- 實際 `!ask` 回歸發現：同一新聞的穩定 evidence ID 在不同抓取版本間重用時，
  `evidence_items.source_document_id` 會指向最新版本，舊每日報告 packet 仍指向舊 raw，
  因而被稽核標為 2 筆 unresolved。這是 evidence version link 缺失，不是 raw 內容損壞。
- `persist_evidence_items` 改為以
  `(evidence_id, source_document_id)` 建立 source-scoped `evidence_links`，不再讓後一次
  抓取覆蓋歷史來源連結；每日報告稽核改以 packet 指定的 exact source link 與 raw hash
  驗證，仍保留中央 evidence row 的最新主來源。
- 修復前以 SQLite online backup 保存
  `data/backups/stock_20260920_131634_pre-evidence-source-link-repair.db`；既有 2 筆
  每日報告補回歷史來源 link，links `13,841 → 13,872`，報告資料本身未被不必要重寫。
- 修復後再次執行 live `!ask`，稽核為每日報告 unresolved=0；最新資料鏈為
  `source_documents=681`、raw `681/681`、SQLite integrity=ok、orphan=0、FTS 一致，
  warning gap 維持原本 2 個。修復後恢復點為
  `data/backups/stock_20260920_131845_post-evidence-source-link-repair.db`；主庫、修復前
  與修復後備份的 `PRAGMA integrity_check` 均為 `ok`。完整回歸最後為 **750 passed,
  1 warning**。

### 2026-09-20 13:24～13:26：研究鏈 source-link 回填與稽核

- 廣域 exact-only dry-run 發現 16,309 個候選
  `(evidence_id, source_document_id)` pair，其中 2,499 筆歷史研究 link 缺失；
  missing evidence=0、unknown source document=0、raw 不可回放=0。
- 新增 `repair_evidence_source_links`，只根據既有 `llm_runs.metadata_json`、
  `llm_run_evidence`、每日報告 packet 與 evidence primary source 建立 source-scoped
  links；不依 URL／標題／相似度猜測，不刪除或改寫 raw、claim、canonical 資料。
- 新增 `stock-evidence-link-repair` 管理入口，預設 dry-run；只有建立備份後加上
  `--apply` 才會新增 links，且可重跑。實際 console entry point 已以
  `uv run stock-evidence-link-repair --root C:\\skymiku\\stock --limit 1` 驗證，
  exit code 0。
- 建立修復前備份
  `data/backups/stock_20260920_132401_pre-research-source-link-repair.db`，實際新增
  2,499 links；`stock-data-audit` 已新增研究 run source-link 覆蓋檢查，修復後缺失=0、
  invalid=0。修復後恢復點為
  `data/backups/stock_20260920_132553_post-research-source-link-repair.db`。
- 主庫與修復後研究鏈均通過 SQLite／raw／FTS／orphan 驗證；完整非
  integration／slow 回歸更新為 **751 passed, 1 warning**。

### 2026-09-20 13:28～13:31：修復工具入口驗收

- 新增 `stock-evidence-link-repair` console entry point；預設 dry-run，只有
  `--apply` 才寫入 source links。實際 CLI dry-run exit code=0，且沒有新增資料。
- `stock-system-preflight --imports --json` 現在驗證 **157 個** bot 模組全部可 import；
  `stock-data-audit --json` 仍為 exit code=0，research source-link 缺失=0、invalid=0、
  daily report unresolved=0。
- 完整非 integration／slow 回歸最後為 **753 passed, 1 warning**；warning 仍是
  Shioaji 第三方套件的 Pydantic deprecation。

### 2026-09-20 13:35：所有運行入口與 Dashboard 驗收

- 依 `pyproject.toml` 逐一執行 **32 個** console entry point 的 `--help`，全部 exit
  code 0；包含新增的 `stock-evidence-link-repair`，沒有啟動交易或下單。
- 以本地臨時 port 8814 啟動 Dashboard，`/_stcore/health=ok`、首頁 HTTP 200；驗證後
  已停止程序並確認 8814 port 不再 listening。

### 2026-09-20 13:32：legacy raw hash 精確回溯

- 對 4 筆 `source_runs_without_raw_manifest` 逐一取得既有 SHA-256，並搜尋目前
  `data/`／`log/` 的 **1,326 個**非 SQLite 檔案；資料庫 `source_documents` 與檔案系統
  均沒有任何 exact hash 命中。
- 因此這 4 筆仍維持 `source_runs_without_raw_manifest` warning；沒有依 URL、來源名稱、
  日期或相似內容補造 `source_document_id`。目前可證明的研究鏈仍為完整，這些資料只被
  視為歷史 legacy gap，不會進入 exact evidence。

### 2026-09-21 13:07～13:09：跨日資料源再驗收

- 在 C 槽正式專案重新執行
  `stock-validate --root C:\\skymiku\\stock --shioaji --mis-seconds 3`，結果為
  **49 OK、0 warning、0 failure**；涵蓋基本面、MOPS、新聞、搜尋、macro、TAIFEX、
  ETF、Shioaji simulation／snapshot 與 TWSE MIS。
- 非交易時段的 MIS 無 tick 被明確標示為前收限制；籌碼 API 缺少必要欄位的回應被拒絕
  快取，沒有污染 canonical 或 raw evidence。
- 驗證後資料鏈為 `source_documents=705`、raw `705/705`、SQLite integrity=ok、
  orphan=0、FTS 一致、daily report unresolved=0、research source-link missing／
  invalid=0；既有 2 個歷史 warning 沒有增加。
- 恢復點為
  `data/backups/stock_20260921_130927_post-20260921-source-validation.db`；主庫與
  備份 `PRAGMA integrity_check` 均為 `ok`。D 槽空資料夾未被寫入。

### 2026-09-21 13:13～13:16：`!ask` 跨資料域整合驗收

- 以 `stock-ask --live --no-llm --json` 實際驗收基本面／營收／估值／股利、技術面／
  籌碼／總經、ETF、TAIFEX 與 MOPS 五類問題；每一類都完成 QueryPlan、條件式資源
  蒐集與 EvidencePack 建立，未呼叫下單路徑。
- 基本面、技術／籌碼／總經、ETF、TAIFEX 的 collection status 為 `ok`；MOPS 因
  本次查詢沒有回傳詳情資料而為 `partial`，並明確保留「不等於查無此事」警告，沒有
  將來源失敗誤判成不存在。
- 本輪使用 `--no-llm`，因此回答層依設計回覆「我不確定，目前沒有足夠的可驗證資料」；
  這是 LLM 關閉時的保守閘門，不是資源或證據鏈失敗。所有查詢均有 trace ID，
  exact source evidence 可回到 source document。
- 查詢後資料鏈為 `source_documents=716`、raw `716/716`、
  `evidence_items=13,871`、`evidence_links=16,480`、`claims=79,367`、
  `claim_evidence=79,382`；SQLite integrity=ok，audit 未產生新 gap，仍為既有 2 個
  warning：`research_semantic_partial` 與 4 筆歷史
  `source_runs_without_raw_manifest`。
- `stock-system-preflight --json`：SQLite、raw、provenance、FTS 均為 ok；完整非
  integration／slow 回歸為 **753 passed, 1 warning**。唯一 warning 仍是 Shioaji
  第三方套件的 Pydantic deprecation。Gemini 與 Google Sheets 憑證仍未設定，故本輪
  不宣稱已完成真實 LLM 生成或雲端同步驗證。

### 2026-09-21 13:23～13:24：自動研究主流程 smoke test

- 以 `stock-auto-research --root C:\\skymiku\\stock --focus 2330 --no-llm
  --no-brief --no-calendar --no-auto-research --upcoming-days 0 --days 2` 執行本地
  研究主流程；run `20260921_132324` 完成，`errors=0`、`warnings=7`、狀態為
  `partial`，沒有啟用交易或下單。
- 28/28 ETF 持股抓取成功並建立 research/evidence；macro 取得 6 個指數、14 個個股、
  3 組 ADR 溢價。籌碼 187 個焦點中 104 個有資料、83 個因上游回應缺少必要
  `fields/data/tables` 被拒絕快取；系統沒有將缺資料寫成數值 0，保留期間與來源警告。
- pipeline run 的 macro 明確標示 `source_date_status=mixed`，並列出 ADR 與台股不同交易日；
  因此後續分析不能把不同日期直接當成同日比較。這是目前設計的正確保守行為。
- 執行後 raw archive `744/744`、缺檔 0、hash mismatch 0、unregistered raw=0，
  SQLite integrity=ok，audit gap 仍為既有 2 個，沒有因 partial pipeline 產生新缺口。
- 已建立目前資料狀態的恢復點
  `data/backups/stock_20260921_132743_post-auto-research-20260921.db`；主庫與備份
  `PRAGMA integrity_check` 均為 `ok`，且 source／evidence／research／claim 計數一致。

### 2026-09-21 13:31～13:35：籌碼 endpoint 重複請求修復

- 發現自動研究會對每個焦點 ticker 重複請求相同日期的全市場籌碼 endpoint；當 TWSE
  回應缺少 `fields/data/tables` 時，會產生大量相同失敗請求。這不會污染快取，但會
  增加限流與外部失敗風險。
- `chips_fetcher._fetch_endpoint` 現在對同一 `requests.Session`、日期、endpoint、root
  與模式使用 process 內短生命週期 cache；成功結果與明確失敗的 `None` 都不會在同一
  執行內重複請求。失敗仍不寫入磁碟，保留 exact-only 與不把缺資料當 0 的規則。
- 新增重複失敗請求測試；focused **40 passed**，完整非 integration／slow 回歸為
  **754 passed, 1 warning**。實際自動研究重跑 `20260921_133342` 以 0 errors 完成，
  聚合 warning=7；raw `745/745`、hash mismatch=0、unregistered raw=0、SQLite
  integrity=ok，audit gap 仍只有既有 2 個。
- 最新恢復點為
  `data/backups/stock_20260921_133533_post-chip-cache-fix-20260921.db`；主庫與備份
  integrity 均為 `ok`，核心 source／evidence／research／claim 計數一致。

### 2026-09-21 13:39～13:40：修復後完整來源驗證

- 再次執行 `stock-validate --root C:\\skymiku\\stock --shioaji --mis-seconds 3`，
  結果為 **49 OK、0 warning、0 failure**；涵蓋 TWSE／TPEx 基本面、MOPS、新聞、
  DuckDuckGo、macro、TAIFEX、ETF、Shioaji simulation／contract／snapshot 與 TWSE MIS。
- 籌碼驗證在最新資料日取得 4 日資料，並明確提示最新日可能尚未發布或為休市日；
  缺 schema 的端點仍拒絕快取，沒有污染資料。
- 驗證後 raw `750/750`、hash mismatch=0、unregistered raw=0、SQLite integrity=ok，
  audit gap 維持 2 個。最新恢復點為
  `data/backups/stock_20260921_134043_post-source-validation-20260921.db`；主庫與備份
  integrity 均為 `ok` 且核心計數一致。

### 2026-09-21：正式庫短／長記憶回讀稽核

- `stock-memory stats` 實際回讀為 short active=145、long active=2；capacity 檢查均未達
  80% 警戒（short item ratio=0.29、char ratio=0.0515；long item ratio=0.0002、
  char ratio=0.0078）。以目前正式庫的 system preflight 回讀結果為準；不同驗證命令若在
  其間新增短期稽核記錄，數量可能短暫變動，但不影響 evidence／容量檢查結論。
- active memory 中 111 筆帶有 evidence；逐筆解析 `evidence_json` 並對照中央
  `evidence_items`，無任何無效 evidence ID。34 筆失敗 LLM smoke 記錄與 2 筆無證據
  conversation turn 仍保留在 active audit history，但正常搜尋預設排除低品質／不適用
  記憶，不會被當成市場事實；沒有硬刪原始記錄。

### 2026-09-21 13:48：記憶層納入 system preflight

- `stock-system-preflight` 新增唯讀的「記憶 evidence 與容量」及「記憶容量」檢查；會
  解析 active memory 的 `evidence_json`，對照中央 `evidence_items`，並依短／長記憶
  policy 檢查 80% 警戒，不會在 preflight 中執行封存或修改資料。
- 正式庫實測：active short=145、long=2，所有 evidence 引用有效，短／長容量均為
  `ok`，157 個 bot 模組全部可 import。
- 目前 audit 的 2 個 warning gap 已拆解為：1 筆刻意隔離的「PDF 副檔名但內容為 HTML」
  衍生鏈、47 組來源版本衝突與 26 組未固定 `as_of` 的多版本資料，以及 4 筆歷史
  `source_runs` 沒有可證明的 raw manifest。前者保留為不可引用的 quarantine，後兩者
  只要求回答保留版本／抓取時間限制；4 筆歷史資料則維持 exact-only，不依 URL、標題或
  相似度猜補來源。
- 新增有效 evidence 與缺失 evidence 的 preflight 測試；完整非 integration／slow 回歸
  為 **756 passed, 1 warning**。唯一 warning 仍是 Shioaji 第三方套件的 Pydantic
  deprecation。

### 2026-09-21：全模組能力清單與入口契約再驗收

- 重產 `docs/reports/system_capability_report.html`；目前依 `pyproject.toml` 列出 **32
  個 console scripts**、30 個 Dashboard 頁面與 15 個 Prompt。報告生成器現在要求每個
  console script 都有明確的能力分類，新增入口若缺 metadata 會直接失敗，不會靜默歸類為
  `other`。
- 非 Dashboard 的 31 個 console entry point 逐一執行 `--help`，全部 exit code 0；包含
  `stock-system-preflight` 與 `stock-evidence-link-repair`。這是入口可載入驗收，不代表
  需要外部憑證的 live provider 已完成驗證。
- 新增 capability metadata 回歸測試；完整非 integration／slow 回歸為 **758 passed,
  1 warning**。warning 仍是 Shioaji 第三方套件的 Pydantic deprecation。

### 2026-09-21 14:08～14:12：最終來源、問答與備份整合驗收

- 先建立驗收前恢復點
  `data/backups/stock_20260921_140846_pre-final-integration.db`，再執行
  `stock-validate --shioaji --mis-seconds 3`；實際結果為 **49 OK、0 warning、0
  failure**。本輪涵蓋 TWSE／TPEx、MOPS、新聞、DuckDuckGo、macro、TAIFEX、全部 ETF、
  Shioaji simulation／contract／snapshot 與 TWSE MIS read-only poll。
- 來源驗證後建立
  `data/backups/stock_20260921_141021_post-final-source-validation.db`；主庫與備份
  `PRAGMA integrity_check` 均為 `ok`。驗證後 raw archive 為 760/760，缺檔、hash mismatch
  與未註冊 raw 均為 0。
- 以 `stock-ask --live --no-llm --ticker 2330 --json` 實跑基本面、技術／籌碼、總經、ETF
  與 MOPS 五類查詢；前四類 `collection_status=ok`，MOPS 為 `partial` 並保留警告。所有
  查詢都有 trace ID；因未啟用 LLM，回答按設計保守回覆不確定，沒有生成未驗證市場結論。
- 問答矩陣後建立
  `data/backups/stock_20260921_141232_post-final-ask-matrix.db`；主庫與備份完整性均為
  `ok`。最新只讀稽核為 raw 767/767、缺檔 0、hash mismatch 0、unregistered raw 0、
  research errors 0；既有 2 個 warning gap 沒有增加。

### 2026-09-21 14:20～14:21：籌碼失敗 endpoint process cache 驗收

- 實際 auto-research smoke 發現不同 ticker 會建立不同 `requests.Session`，導致同一日期
  的無 schema／307 籌碼 endpoint 重複請求。新增 process 內 5 分鐘的 failure-only cache；
  只抑制明確失敗，不寫入磁碟，不把缺資料當成 0，TTL 到期後仍可重新嘗試。
- 修復後相同 smoke 從 **112.6 秒降至 72.1 秒**，仍為焦點 187、錯誤 0、警告 7；拒絕
  快取訊息由大量重複縮為每個失敗 endpoint 一次。新增跨 Session 回歸測試，相關測試
  通過，`compileall` 亦通過；完整非 integration／slow 回歸為 **759 passed, 1 warning**。
- 修復後建立恢復點
  `data/backups/stock_20260921_142143_post-chip-negative-cache-fix.db`；主庫與備份
  `PRAGMA integrity_check=ok`。最新稽核為 raw 822/822、缺檔 0、hash mismatch 0、
  unregistered raw 0、research errors 0，既有 2 個 warning gap 沒有增加。

### 2026-09-24：最新完整回歸與外部服務／交易可行性驗收

- 目前工作樹執行 `.venv/Scripts/python.exe -m pytest -m "not integration and not slow" -q`：
  **827 passed, 1 warning**，耗時 58.46 秒。測試套件沒有宣告 integration／slow marker，
  因此本次涵蓋全部收集到的測試。唯一 warning 是 Shioaji 第三方套件的
  `PydanticDeprecatedSince20`，與測試失敗無關。
- `stock-system-preflight --root C:\skymiku\stock --json --imports`：SQLite
  `integrity_check=ok`、raw **822/822** 無缺檔或 hash mismatch、provenance 無孤立關聯、
  FTS 計數一致；**160 個 bot 模組全部可 import**。整體狀態為 `partial`，保留既有兩個
  warning gap：`research_semantic_partial`、`source_runs_without_raw_manifest`。Gemini 與
  Google Sheets 未設定，因此沒有宣稱完成這兩項服務的 live 驗證。
- `stock-validate --shioaji --mis-seconds 3`：**49 OK、0 warning、0 failure**。公開資料源
  涵蓋 TWSE／TPEx、MOPS、新聞、搜尋、macro、TAIFEX 與 ETF；Shioaji simulation 登入成功，
  2330 合約與快照成功（1 筆），TWSE MIS 輪詢取得 1 筆 tick。此檢查只讀取行情並登出，
  沒有送單。
- `stock-preflight --root C:\skymiku\stock --json`：exit code 0、`can_simulate=true`、
  `can_trade_now=false`；設定為 `SIMULATION=true`、`RUN_MODE=watch`，所以本次只驗證模擬／
  watch 可行性，沒有啟用正式交易或電子憑證，也沒有送出訂單。
