# 計畫表（PROGRESS.md）工作流程：Prompt 與 Skill 說明

用途：讓 Claude 跨 session 接手。計畫放在根目錄 `PROGRESS.md`，規則放在 `CLAUDE.md`，我說「繼續」就從「進行中 / 下一步」開始。
目前這兩份檔案與 subagent 都已建立（見下方「現況」），以下 Prompt 供新專案或重建時重複使用。

## 現況（2026-11-09）
- `PROGRESS.md`：六個區塊（目標、已定決策、目前計畫、已完成、進行中/下一步、已知問題）。
- `CLAUDE.md`：開工讀 PROGRESS.md、完成階段即更新、不通讀專案、委派規則。
- `.claude/agents/worker-haiku.md`（haiku，唯讀工具）、`worker-sonnet.md`（sonnet，可編輯）。
- 注意：新 agent 定義可能要重開 session 才載入；Haiku 的 Bash 只能靠提示詞約束唯讀。

## 使用順序
1. 新專案：Prompt 1 → Prompt 2。
2. 工作中途想問小問題：用 `/btw`。
3. 階段結束：Prompt 3 → `/clear` → Prompt 4。

## Prompt 1：建立進度記錄機制（最優先）
作用：建立 PROGRESS.md 與 CLAUDE.md 規則，讓之後只說「繼續」就能接手。

```text
請為這個專案建立「進度記錄機制」,讓之後我只說「繼續」你就能接手:

1. 先快速瀏覽專案結構(只讀 README、設定檔、主要入口,不要通讀全部程式碼)。
2. 在專案根目錄建立 PROGRESS.md,包含這些區塊:
   - 專案目標(1~3 句)
   - 已定決策(每條附「為什麼」)
   - 目前計畫(待辦清單,標明優先順序)
   - 已完成
   - 進行中 / 下一步(要具體到檔案與動作)
   - 已知問題與注意事項
3. 建立或更新 CLAUDE.md,加入以下規則:
   - 開工先讀 PROGRESS.md,再決定做什麼;我說「繼續」時,就從「下一步」開始。
   - 每完成一個階段、或做出重要決策,立即更新 PROGRESS.md。
   - 不要為了回答問題而通讀整個專案,優先查 PROGRESS.md,不足時才讀相關檔案。
4. 完成後用 5 行內回報你寫了什麼,不要貼出全文。
```

## Prompt 2：建立便宜模型 subagent
作用：把搜尋、摘要、跑測試等機械性工作交給便宜模型，主對話只做決策，省 token。

```text
請在這個專案建立 subagent 定義(放在 .claude/agents/),把機械性工作交給便宜模型:

1. worker-haiku:model 設為 haiku。負責搜尋檔案、讀取大量檔案後摘要、整理日誌、跑測試並回報結果。
2. worker-sonnet:model 設為 sonnet。負責明確規格的程式碼修改、批次重構、補測試。

每個 subagent 的要求:
- description 要寫清楚「何時該用它」,讓你能自動判斷委派。
- 系統提示要求它:只回報結論與關鍵證據(檔案路徑:行號),不要貼大段程式碼;遇到規格不明確時回報問題而不是自行猜測。
- 工具權限只給必要的(搜尋類只給唯讀工具)。

並在 CLAUDE.md 加入委派規則:
- 搜尋、讀大量檔案、跑測試、機械性批次修改 → 交給對應 subagent。
- 架構決策、需求釐清、與我對話 → 留在主對話。
- 委派時簡報必須包含:目標、相關檔案、完成標準、不可更動的範圍。

完成後簡短回報建立了哪些檔案。
```

## Prompt 3：日常收尾（每次告一段落時）
作用：把決策、完成項目、下一步寫進 PROGRESS.md，之後可以安全 `/clear`。

```text
目前階段先收尾:
1. 把這段的決策(含原因)、完成項目、下一步更新到 PROGRESS.md。
2. 確認「下一步」寫得夠具體,讓新 session 只憑這份文件就能接手。
3. 回報更新了什麼(3 行內),然後我會 /clear。
```

## Prompt 4：新 session 接續（`/clear` 之後）
作用：CLAUDE.md 規則生效時，一個字就能接手。

```text
繼續
```

備用（若 session 沒讀到 CLAUDE.md 規則）：

```text
請讀 PROGRESS.md,從「下一步」繼續,不要通讀專案。
```

## 相關 Skill（本環境已有，說明用途）
| Skill | 用途 | 與計畫表的關係 |
|---|---|---|
| `wrap-up` | 以繁體中文整理本段決策、成果證據、待辦與下輪接續摘要；說「wrap-up／收尾／先到這裡／交接」時觸發 | 功能接近 Prompt 3，可擇一使用；它產出摘要，Prompt 3 則直接寫入 PROGRESS.md |
| `git-commit-workflow` | 每次程式更新用 Conventional Commits、非 main/master 分支、原子化分組提交 | 提交階段成果後再更新 PROGRESS.md 的「已完成」 |
| `grilling` | 分輪深入追問，釐清設計、計畫或決策 | 在制定「目前計畫」與「已定決策」前使用 |
| `file-issue` / `file-pr` / `pr-formatting` | 起草 GitHub issue、PR 標題與本文，經核准才建立／推送 | 計畫項目要對外追蹤時使用 |

## 補充
- 專案慣例（寫在 CLAUDE.md）：測試用 `uv run --offline --no-sync pytest -q`；保持 `SIMULATION=true`；提交逐檔 `git add`；未經同意不推送。
- TokenEconomizer 來源未驗證，故未納入。
