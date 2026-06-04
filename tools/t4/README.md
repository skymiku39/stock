# T4 DLL 驗證工具（非 production bot）

此目錄為說明文件。實作已移至 [`src/t4tools/`](../../src/t4tools/)，與 `stock-bot` 主交易流程分離。

## 用途

- 驗證永豐 T4 DLL 登入、CA、下單路徑
- 廠商範例與 `t4.ini` 見 [`docs/T4_10142/`](../../docs/T4_10142/)

## 指令

```bash
uv run stock-t4-validate
uv run stock-t4-validate --read-queries
```

請在 `.env` 設定 `T4_LOGIN_ID`、`T4_LOGIN_PASSWORD`、`T4_PERSON_ID`、`T4_CA_PATH`、`T4_CA_PASSWORD` 等（見根目錄 `.env.example`）。

主程式 `stock-bot` 僅支援 Shioaji；勿將 `BROKER_BACKEND` 設為已移除的 `t4`。
