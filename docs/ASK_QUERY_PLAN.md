# `!ask` QueryPlan／EvidencePack

日期：2026-09-10

## 目的

`!ask` 不直接把問題丟給回答模型。每次請求都會先建立一份可追蹤的
`QueryPlan`，再依計畫檢索可用的本地記憶與中央證據。若 Gemini 已啟用，會先
呼叫 `query_plan` 補充資源判斷；該輸出只能選擇程式已註冊的資源名稱，不能注入
新的工具或取消引用要求。

目前已註冊的資源：

```text
memory / historical_evidence / channel_knowledge / user_memory
ticker_snapshot / technical / fundamental / chips / etf / macro
news / web_search / professional_knowledge
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
 ↓
LLM ask_answer
 ↓
evidence_id 與 claim linkage 驗證
 ├─ 通過：回答，並只保存短期問答稽核記錄
 └─ 失敗／不足：我不確定，不進長期記憶
```

`EvidencePack` 每筆資料至少包含 `id`、類型、來源、來源識別、觀測時間、引用
片段與品質狀態。回答的 `evidence_ids` 必須存在於本次 pack 且品質為 `ok` 或
`partial`；若有 `claims`，每個 claim 也必須列出支援 ID。

`freshness` 也會在 EvidencePack 階段執行：嚴格窗口（`24h`、`7d`、`30d`）若
證據缺少可解析的 `observed_at` 或已超過窗口，就會保留在稽核資料中但標成
`stale`，不會送進回答模型。月營收與季報的日期通常是資料所屬期間，因此現階段
視為 period-based evidence，不會用新聞的 24 小時規則誤刪；之後仍應補上
`as_of`、`published_at`、`retrieved_at` 欄位來做更精確的來源策略。

## 本地使用

```text
uv run stock-ask "2330 最近營收如何？"
uv run stock-ask "這個頻道以前主要討論什麼？" --no-llm --json
uv run stock-ask "2330 最新基本面" --root D:\skymiku\stock --json
```

沒有 `GEMINI_API_KEY` 時，命令仍會產生 QueryPlan、檢索本地證據與回傳完整
稽核 JSON，但回答會保守回覆「我不確定」。本地已接入：

- SQLite：`stock_info`、日 K、月營收／季報（`fundamental`）
- Snapshot adapters（`bot.ask_adapters`，只讀、不打外網）：
  `technical`／`chips`／`etf`／`macro`／快取基本面摘要

新聞與網路搜尋仍需預先寫入 evidence／memory，或之後另接 adapter；Ask
路徑本身不會現場爬網。

## 邊界

- QueryPlan 是資源選擇，不是投資建議。
- Evidence ID linkage 是最低來源驗證，不等於完整語意真偽證明；回答目前還
  要求 `confidence` 為 0 到 1 的有限數字，格式不合格就 abstain。
- 未驗證回答只保存於短期稽核層；長期記憶仍需品質通過或管理者核准。
- 沒有任何下單路徑；`stock-ask` 只做規劃、檢索、回答與記憶稽核。
