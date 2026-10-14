# raw 登錄恢復

`SourceRegistry.register_document` 在發布 raw 前，先把完整來源欄位、原始日期、hash、路徑及 ingestion ID 原子保存至 `data/source_registration_receipts/`。manifest、run 關聯與收據確認同一交易提交；任何一步失敗都保留 raw 與收據。這個目錄應與 raw 和 SQLite 一起備份。

先預覽：

```powershell
uv run stock-source-recovery --root C:\skymiku\stock
```

預設使用唯讀連線，不建資料庫、不修 schema、不連外。`recoverable` 代表有原始收據且 raw hash 相符；`unresolved` 代表收據、raw 或原始 run 無法驗證。`unresolved_raw` 是沒有可驗證登錄收據的未登錄檔案，保留原檔，不猜測來源。

確認預覽後，先備份再套用：

```powershell
uv run stock-db-backup --keep 0 --label before-source-recovery
uv run stock-source-recovery --root C:\skymiku\stock --apply
uv run stock-data-audit --root C:\skymiku\stock --json
```

重播保持原始日期，不覆寫較新的既有 manifest，重複執行不新增重複 manifest。恢復登錄不等於完成解析、canonical 寫入或驗證；原 ingestion/source run 的失敗狀態與錯誤保留。沒有收據的歷史 raw 仍屬 unresolved，不能當成可引用的來源證據。

issue #14 的歷史 6 個 raw 缺少原始登錄資訊，這個工具不會以檔名、URL 或相近時間幫它们配對。實際筆數以當次預覽為準。
