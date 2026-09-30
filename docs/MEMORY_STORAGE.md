# 短期／長期記憶儲存規格

日期：2026-09-10

本專案的記憶不等於行情資料庫。股票價格、營收與財報仍以 canonical tables
為準；記憶層只保存 LLM 工作上下文、研究摘要、每日報告、偏好與決策，並且
每一筆都保留來源、時間、品質與 evidence reference。

## 分層

### 短期記憶

來源：每次 LLM 呼叫的完整 prompt、output、metadata 與錯誤狀態。

特性：

- 預設保存 14 天。
- 內容完整保存，不用摘要覆蓋原文。
- 失敗或尚未確認品質的呼叫仍保存，但 `quality_status=failed/unknown`，一般搜尋會排除。
- 適合目前工作上下文、除錯與回放。

### 長期記憶

目前會保存：

- 通過新版 `research_ticker` 驗證的個股研究結果。
- `llm_daily_reports` 中品質為 `ok` 或 `partial` 的每日研究報告。
- 品質為 `failed`、`invalid` 或 `unknown` 的報告只留在報告表供稽核，
  不會進入正常長期記憶。
- 由 `MemoryStore.promote()` 或 `consolidate()` 明確升級的記憶。升級到長期記憶時，
必須同時符合：

- `quality_status` 為 `ok` 或 `partial`（`failed`／`unknown`／`unresolved` 一律拒絕）
- `evidence_json` 內每個 evidence_id 都存在於中央 registry
- 若 evidence 帶 `source_document_id`，必須是可在本地 raw archive 回放的有效
  exact ID：manifest、原始檔、專案內路徑與 SHA-256 都必須一致（不臆造）
- 沒有未解決的 `conflicted` claim
- `conversation_turn` 不得升級為長期市場／研究事實

自動研究的長期記憶也會經過 evidence gate：只保留已登錄、
`provenance_status=exact`／`composite_exact` 且 source document 的 raw archive
仍可通過存在性與 SHA-256 回放檢查的證據。
若研究包混入 unresolved 或 cache-only 證據，會降為 `partial` 並排除那些證據；
若沒有任何合格證據，研究結果只保留在短期／LLM history／cache，不升級為長期記憶。

不合格時保留原始短期記憶、不刪除，並回傳清楚拒絕原因；CLI 與 Python API
共用同一套 `PromoteOutcome` 規則。

長期記憶不應直接取代原始資料；它保留完整 `content`，`summary` 只用於檢索。

`conversation_turn` 可作為後續對話上下文，但不可因品質通過就自動變成股價、
營收、估值或新聞等市場事實證據。

每日報告是多來源聚合物件時，不把任一來源硬塞進單一
`llm_daily_reports.source_record_id`。報告的完整 provenance 以
`evidence_json` 與中央 evidence／evidence links 保存，並以 payload 的
`provenance_status=composite_exact` 表示所有參與來源都能回放；舊報告回放也必須
重新經過 exact raw gate，legacy packet 只能標示 `unresolved`。

## SQLite 表

### `memory_items`

保存記憶本體與檢索 metadata：

- `memory_layer`：`short` / `long`
- `memory_type`：`llm_call`、`research_summary`、`daily_report`、`preference`、`decision`
- `scope_type`／`scope_id`：`global`、`ticker`、`user`、`channel`、`session`
- `content`／`summary`
- `importance`／`confidence`
- `quality_status`／`memory_status`
- `observed_at`／`expires_at`
- `source`／`source_ref`／`source_record_id`
- `evidence_json`／`metadata_json`

### `memory_events`

追加式保存建立、更新、升級、封存與軟刪除事件；存取次數與最後存取時間則記在
`memory_items`。任何容量清理都只會
把項目改成 `archived`，不直接刪除原文。

### `evidence_items` 與 `memory_evidence`

`evidence_items` 是中央證據登錄表，保存證據類型、來源、來源識別、URL、觀測
時間、可引用片段、品質與內容 hash；原始 API response 或檔案仍由各資料來源的
archive 管理。`memory_evidence` 是多對多關聯，讓同一份新聞、官方公告或歷史
原句可以支援多筆短／長記憶。`memory_items.evidence_json` 保留相容用的 ID
清單，但查證與回放應優先使用中央 registry。

### `memory_proposals`

針對人工／管理型記憶異動，管理者或 LLM 先建立 proposal，不能直接覆寫既有長期
記憶。提案保留完整內容、scope、品質、來源與證據清單，並記錄 proposer、
reviewer、審核意見與狀態。只有該 proposal `approve` 才會寫入 `memory_items`；
`reject` 也會留下 audit event。經過既有品質驗證的自動研究仍可依既定 pipeline
直接建立新的長期研究項目，但不會覆蓋人工記憶。刪除提案同樣只執行軟刪除。

SQLite FTS5 只作為可重建索引；即使 FTS5 不可用，仍會退回 `LIKE` 搜尋。

## 管理指令

```text
uv run stock-memory stats
uv run stock-memory search 2330 --layer both
uv run stock-memory list --layer long --status active
uv run stock-memory archive-expired
uv run stock-memory backfill-llm --log-dir log/llm_calls
uv run stock-memory backfill-db
uv run stock-memory promote <memory_id> --summary "人工確認的長期摘要"
uv run stock-memory delete <memory_id>
uv run stock-memory propose --content "需要人工確認的記憶" --summary "摘要" \
  --scope-type channel --scope-id demo --quality-status ok
uv run stock-memory propose --proposal-type update \
  --target-memory-id <memory_id> --content "更新後完整內容" --summary "新摘要"
uv run stock-memory propose --proposal-type consolidate \
  --target-memory-id <memory_id_1>,<memory_id_2> \
  --content "合併後完整內容" --summary "合併摘要" --quality-status ok
uv run stock-memory list-proposals --status pending
uv run stock-memory evidence --memory-id <memory_id>
uv run stock-memory approve <proposal_id> --reviewer owner --note "已核對來源"
uv run stock-memory reject <proposal_id> --reviewer owner --note "證據不足"
```

Dashboard 也有「記憶與證據管理」頁面。未設定 `MEMORY_ADMIN_TOKEN` 時為唯讀；
設定後仍需在目前 Streamlit session 輸入 token，才可核准、拒絕或軟刪除。這是
本地優先的操作閘門，不等同完整多人登入／RBAC；若要對外部署，仍需在反向代理
或應用層加上正式身份驗證。

`delete` 是軟刪除；管理者可以從 `memory_events` 回看變更。`backfill-llm` 可
把既有 JSONL 呼叫補進短期記憶，使用 deterministic memory id，重複執行不會
產生重複本體。`backfill-db` 則補上既有 `llm_analysis_history` 與
`llm_daily_reports` 可升級的長期記憶；回傳的 `skip_reasons` 會區分
`llm_failed`、`missing_ticker`、`low_quality` 與 `memory_gate`，避免長期記憶為 0
時無法判斷是資料失敗還是流程閘門刻意拒絕。`stock-memory stats` 會固定輸出
短期／長期的 active、archived、superseded、deleted 計數，即使某個分層目前為 0
也不省略欄位，避免 Dashboard 把空分層誤讀成統計缺漏。

`update` 必須指定既有的 active `memory_id`；若沒有在提案中附新 evidence，
系統會保留原有 evidence linkage。`consolidate` 必須明確指定至少兩筆 active
子記憶（`--target-memory-id` 以逗號分隔，或在 `--metadata-json` 提供
`children` 陣列）；核准後建立新的長期記憶、合併所有 evidence，並只封存子項目，
不刪除原文。

## 上下文使用

```python
from bot.memory_store import MemoryStore

context = MemoryStore(db).build_context(
    "2330 最近的研究風險",
    scope_type="ticker",
    scope_id="2330",
    max_chars=24_000,
)
```

`context.short` 與 `context.long` 保持分開，呼叫端不可把兩者拼成沒有標籤的
純文字。長期記憶若沒有 evidence 或品質不合格，不應被當成 canonical market
fact。`auto_llm` 研究同一 ticker 時會將可靠的短／長記憶以分層標籤帶入新
研究上下文，並要求 LLM 以本次來源重新驗證，不會盲目複製舊結論。

## 邊界與下一步

目前「摘要／合併」需要由呼叫端提供已驗證的摘要文字；系統不會讓 LLM 自行
覆蓋長期記憶，也不會把失敗或 fallback 結果升級成正式研究記憶。下一步若要
自動整理，應增加：

1. proposal 的 dashboard 權限角色與多人操作鎖。
2. 摘要輸入範圍與 evidence 完整性檢查。
3. 版本衝突、supersede 與證據新鮮度的 dashboard。
4. 依使用者／頻道 scope 的權限隔離。
5. 在記憶量增大後再評估向量／語意檢索；SQLite FTS5 目前是可重建的第一階段索引。
