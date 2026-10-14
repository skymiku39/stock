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

## 簡報輸入證據

每日與美股簡報在送入模型前，必須通過 `pipeline_brief` 的第二版輸入檢查。排程、命令列與儀表板使用同一條路徑；直接呼叫 `gemini_call` 而未提供合格收據會拒絕執行。

- ETF 必須能從已登錄 HTML 重新解析出與持股快照相同的日期、股票及數值，再以驗證過的快照重算共識與變動。不同快照日期不稱為當日資金流。
- 籌碼由 hash 正確且回應日期一致的原始 API 回應重建，保留每一觀測日與來源。缺少的欄位不補零，衝突版本不合併，鉅額成交量不當成淨買超。
- Macro 行情必須有來源日期及有效價格；期貨基差另核對 TAIFEX raw，且現貨、期貨日期一致才重算。沒有來源證據的 ADR 換股比率、供應鏈設定及先前模型分析不納入。

完整篩選後輸入、來源 hash、evidence IDs 與排除原因，保存於 `pipeline_llm_input` 原始文件。模型呼叫前再次驗證輸入完全相同、來源檔仍存在且 hash 正確。沒有可驗證資料時不呼叫模型。

舊簡報若沒有第二版收據，一般分析歷史、pipeline 載入及儀表板不沿用。原文與完整歷史保留；`include_archived=True` 可供管理稽核，但不是日常分析輸入。新簡報若來源後來毀損，也會停止沿用。不能從日期或內容相似度推定舊模型實際使用的來源。

`stock-macro-update --brief` 將新簡報保存為含產出時間的獨立 Markdown 檔，並附 `.provenance.json`，不覆蓋同日舊簡報。輸入可驗證不代表模型結論已驗證；模型輸出仍維持 `is_publishable=0`。
