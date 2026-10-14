# 歷史 LLM 證據缺口處理

先執行 `uv run stock-llm-evidence-review`。預覽只用 SQLite `mode=ro`，不抓資料、不呼叫模型、不建立資料庫。報告依 analysis/run、任務、原始執行狀態及產生路徑分類。

有明確記錄的輸入 evidence IDs 才能使用既有 `stock-provenance-backfill` 精確回填；模型輸出的 ID、URL、標題、相近時間與文字相似度都不能證明原始輸入。缺少原始資訊時，不執行猜補。

確認缺口後可保存未驗證標記：

```powershell
uv run stock-db-backup --keep 0 --label before-llm-evidence-review
uv run stock-llm-evidence-review --apply
uv run stock-data-audit --json
```

標記寫入 `llm_evidence_reviews`，保留原始分析文字、模型輸出與執行狀態；相關 `llm_outputs.is_publishable` 設為 0。標記與限制同一交易提交，重複套用不重複新增。資料或證據關聯改變後，舊標記不會自動套用到新版本。

`stock-data-audit` 的 `database.llm_evidence_review` 與 `known_limits` 顯示已審視的未驗證紀錄。原本的證據缺口仍保留，不能把「已標記」當成「已找回證據」，也不能把執行成功當成內容已驗證。記憶升級原有 exact/raw 可回放閘門繼續生效。

新研究流程的離線回歸涵蓋：呼叫模型前保存輸入 evidence、分析與 run 保存相同 IDs、寫入失敗完整回滾、保留 JSONL 精確重播、模型自行輸出的 IDs 不被採為輸入，以及未驗證輸出不可發布。這不代表外部 LLM 服務的現場驗收。
