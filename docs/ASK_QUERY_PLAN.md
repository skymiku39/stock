# `!ask` QueryPlan／EvidencePack

日期：2026-09-17

## 目的

`!ask` 不直接把問題丟給回答模型。每次請求都會先建立一份可追蹤的
`QueryPlan`，再依計畫檢索可用的本地記憶與中央證據。若 Gemini 已啟用，會先
呼叫 `query_plan` 補充資源判斷；該輸出只能選擇程式已註冊的資源名稱，不能注入
新的工具或取消引用要求。

CLI 從非專案工作目錄啟動時，使用 `stock-ask --root C:\skymiku\stock ...`；
這會讓 SQLite、`.env` 與來源快取使用同一個專案根目錄。

目前已註冊的資源：

```text
memory / historical_evidence / channel_knowledge / user_memory
ticker_snapshot / technical / fundamental / chips / etf / taifex / macro
news / mops / web_search / professional_knowledge
```

## 執行流程

```text
問題
 ↓
deterministic QueryPlan
 ↓（Gemini 可用時）
LLM query_plan + schema normalization
 ↓
SQLite memory/evidence retrieval
 ↓
EvidencePack
 ↓（先登錄 evidence registry）
中央 evidence_items
 ↓
LLM ask_answer
 ↓
evidence_id 與 claim linkage 驗證
 ├─ 通過：回答，並只保存短期問答稽核記錄
 ├─ 失敗：最多一次受同一 EvidencePack 約束的 repair attempt
 └─ 仍失敗／不足：我不確定，不進長期記憶
```

`EvidencePack` 每筆資料至少包含 `id`、類型、來源、來源識別、`as_of`、
`published_at`、`retrieved_at`、引用片段、品質狀態與 `provenance_status`。
`quality_status` 描述內容品質；`provenance_status`（`exact`／`composite_exact`／
`cache_only`／`unresolved`／`legacy_unlinked`）描述能否回到 raw document。缺少
`source_document_id` 不代表資料內容錯誤，但 cache-only 證據必須在回答中可辨識。

`as_of` 表示資料所屬時間，`published_at` 表示來源發布時間，`retrieved_at`
表示本機取得時間；不能以其中一個欄位冒充另一個語意。回答的 `evidence_ids`
必須存在於本次 pack 且品質為 `ok` 或 `partial`；若有 `claims`，每個 claim
也必須列出支援 ID。對話記憶（`conversation_turn`）不可作為市場事實 claim
的唯一或直接來源。

### 狀態語意

| status | 意義 |
|---|---|
| `no_evidence` | 來源成功完成，但沒有可用資料 |
| `resource_failed` | 來源明確失敗 |
| `timeout` | 來源逾時 |
| `live_disabled` | 即時來源未啟用 |
| `partial` | 任一來源明確只取得部分內容，或部分來源失敗／逾時／未啟用 |
| `uncertain` | 無法產生可驗證回答 |
| `answered` | 通過驗證 |
| `verification_failed` | 驗證失敗 |

timeout／failed／live_disabled **不得**被解讀成「查無此事」。

`freshness` 也會在 EvidencePack 階段執行：嚴格窗口（`24h`、`7d`、`30d`）若
證據缺少可解析的時間或已超過窗口，就會保留在稽核資料中但標成 `stale`，不會
送進回答模型。月營收與季報的 `as_of` 通常是資料所屬期間，因此嚴格 freshness
會優先使用 `retrieved_at` 判斷最近是否重新取得；缺少取得時間時採保守淘汰。
中文「最新／今天／即時」預設為 `24h`；「最近／近期／近來」預設為 `7d`。
若 query 只寫公司名稱（例如「台積電最近消息」），AskEngine 會只從既有
SQLite `stock_info` 名冊反查 ticker；名稱有歧義或名冊沒有命中時不猜代號。

跨市場資料另有 `temporal_policy`：預設為 `warn_mixed`，保留跨日證據但在
EvidencePack 警告；問題明確要求「同日／同一交易日／同一時點」時會產生
`same_date`，只接受 adapter 明確標記為同日的證據。LLM planner 不能把這個
嚴格政策放寬成跨日混用。

### 外部搜尋 query 正規化

原始問題仍完整保存於 `QueryPlan.query`，但新聞／網頁 adapter 不會把整段
對話式指令直接送給搜尋引擎。`build_search_query()` 會保留 ticker、公司名、
註冊主題詞與少量自由詞；新聞查詢另外使用小型 `OR` 意圖群組，並把
`freshness=24h/7d/30d` 轉成 `when:1d/7d/30d`。這避免「請標示資料日期」等格式
要求污染搜尋詞，也避免多個資料域以 AND 串接後零召回。實際 query 會寫入
`resource_outcomes[].metadata.search_query`，可由同一個 `trace_id` 重播與稽核。

EvidencePack 會在 `ask_answer` 前寫入 `evidence_items`；LLM 呼叫的 audit metadata
同步保存本次 `evidence_ids` 與 `trace_id`。因此回答失敗、LLM 未啟用或之後進行
repair 時，仍可查到本次實際使用過的來源索引。這個 registry 只證明「回答引用了
哪些來源」，不把 LLM 結論自動升格成事實。

LLM planner 只能補充資源，不能刪除 deterministic planner 已根據使用者明確用語
判定的必要資源，也不能放寬 freshness 或把事實問題改成預測問題。

## 本地使用

```text
uv run stock-ask "2330 最近營收如何？"
uv run stock-ask "這個頻道以前主要討論什麼？" --no-llm --json
uv run stock-ask "2330 最新基本面" --root C:\skymiku\stock --json
```

沒有 `GEMINI_API_KEY` 時，命令仍會產生 QueryPlan、檢索本地證據與回傳完整
稽核 JSON，但回答會保守回覆「我不確定」。本地已接入：

- SQLite：`stock_info`、日 K、月營收／季報（`fundamental`）
- Snapshot adapters（`bot.ask_adapters`，只讀、不打外網）：
  `technical`／`chips`／`etf`／`taifex`／`macro`／快取基本面摘要。
  `taifex` 是市場級資源，不需要 ticker；只在 evidence registry 找到 exact
  `taifex_futures` source document 時納入回答，缺少時維持不確定。

Snapshot adapter 的唯讀邊界是明確傳入 `allow_live_sources=False`；籌碼缺少當日
快取時使用 `cache_only=True`，不會因為缺檔而偷偷打 API。技術面／ticker snapshot
會在使用的 K 線全部具備 exact manifest 時標示 `composite_exact`；基本面總摘要也會
在月營收期間與估值日能和 canonical exact rows 對齊時標示 `composite_exact`。基本面除了
`data/fundamentals/<ticker>/` 個股快取，也會唯讀回退到已存在的全市場快取；個股
快取優先，回退資料會在 evidence metadata 保留實際 cache path 與取得時間。這些
cache artifact 若尚未有 upstream raw manifest，不能被誤宣稱成官方原始文件，
`source_document_id` 仍會保持空白。

`professional_knowledge` 已接入保守的權威來源 adapter。它只接受預先核准的官方、
監管或學術網域（例如 `gov.tw`、TWSE、TPEx、TAIFEX、SEC、arXiv、PubMed、IMF、
World Bank 等），優先抓取頁面原文；一般搜尋結果與未列入 allowlist 的網域不會被
冒充專業證據。若只取得搜尋摘要，會標示為 `quality=partial`；沒有核准來源時保留
resource need 並 abstain，明確表示未找到可驗證的專業來源。

預設仍以本地證據為主；若 QueryPlan 明確要求 `news`／`mops`／`web_search`，或在啟用
`STOCK_ASK_LIVE=1`／`AskEngine(enable_live_sources=True)` 時要求
`ticker_snapshot`，才會在有限時間預算內呼叫即時 adapter。ticker 的 live adapter
使用 TWSE MIS 公開延遲報價，會把 quote date/time 與延遲標記帶入 evidence，不能
繞過 freshness policy。逾時、失敗、跳過都會保留狀態，不會被解讀為查無此事。
新聞 refresh 仍會先將完整 JSON 快取寫入 `data/raw/cnyes/<sha256>.json`，並登錄到
`source_documents`，再寫入 `data/news/news_YYYY-MM-DD.json`。

## 邊界

- QueryPlan 是資源選擇，不是投資建議。
- Evidence ID linkage 加上原文片段、數字與關鍵詞的保守核對，是目前的最低來源
  驗證，不等於完整語意真偽證明；回答還要求 `confidence` 為 0 到 1 的有限數字，
  格式或主張支持不合格就 abstain。後續若部署本地 NLI／embedding 模型，也應放在
  這個 verifier 後面，不能取代來源 ID 與時間／品質閘門。
- 回答驗證失敗時最多再呼叫一次 `ask_answer`；第二次仍不合格就停止，避免無限
  回爐與 token／延遲失控。repair 不會取得新的來源，也不能放寬原本的 QueryPlan。
- 未驗證回答只保存於短期稽核層；長期記憶仍需品質通過或管理者核准。
- 沒有任何下單路徑；`stock-ask` 只做規劃、檢索、回答與記憶稽核。
