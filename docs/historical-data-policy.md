# 日常使用與歷史封存

資料是否可用以來源可追溯、品質及 raw 可回放性判斷，不依日期刪除舊資料。

`uv run stock-history-archive` 預覽當下確定的歷史缺口。備份資料庫後，使用 `--apply` 封存該次列出的 LLM 紀錄、缺 manifest 的 source run、未登錄 raw，以及已隔離的個別研究文件警告。原始檔、原文及 run 狀態都保留；不能排程反覆執行 `--apply`，否則會把之後的新缺口也列入新的封存操作。

日常 `stock-data-audit` / system preflight 只把未封存或內容已改變的缺口列為待處理；封存資訊在 `historical_archive`。要查看完整歷史，執行：

```powershell
uv run stock-data-audit --include-archived --json
```

LLM 分析清單預設排除封存 ID，管理程式可使用 `include_archived=True` 查看原文。日常記憶檢索排除相關歷史分析與未經查證的 LLM 呼叫紀錄。

啟用封存政策後，日常自動分析快取要求每一份 evidence 都已登錄、具有 exact/composite_exact provenance、品質可用，且原始文件 hash 可回放；缺少任何一項就不沿用快取。完整且可信的舊資料仍能使用，原快取檔不刪除。

來源多版本、資料有效期間不明等語意警告不會整類隱藏，仍需在引用時保留版本與日期限制。封存不代表資料已修復，也不等於現場採集驗收成功。
