# 安裝與設定指南

## 前置條件

- Python 3.11 以上
- Windows 10/11（`trade` 實單模式需使用 Shioaji 電子憑證）
- 永豐金證券帳戶（`trade/watch` 模式需要；`report` 模式不需要）

## Step 1: 永豐金開戶與 API 申請（trade/watch 模式）

如果你只要先跑 `RUN_MODE=report`，可以略過本步驟；report 模式不登入 Shioaji，也不會下單。

### 1.1 開戶

如果你還沒有永豐金證券帳戶：
- 線上開戶: https://www.sinotrade.com.tw/
- 或至任一永豐金營業據點臨櫃辦理

### 1.2 申請 API 權限

1. 登入永豐金「iLeader」交易平台
2. 進入「API 管理」頁面
3. 申請「API 交易權限」
4. 系統會產生一組 **API Key** 和 **Secret Key**
5. 妥善保管這兩組金鑰，不要外洩

參考文件: https://sinotrade.github.io/zh/tutor/prepare/token/

### 1.3 下載電子憑證

1. 同樣在 iLeader 內，進入「憑證管理」
2. 下載 `.pfx` 電子憑證檔案
3. 記下憑證密碼
4. 將 `.pfx` 檔放到安全的本機路徑，例如 `C:\certs\Sinopac.pfx`

**注意**: 沒有電子憑證只能接收行情，無法下單。模擬模式則不需要憑證。

## Step 2: 安裝專案

### 2.1 安裝 uv

如果你還沒安裝 uv：

```powershell
pip install uv
```

### 2.2 克隆或進入專案

```powershell
cd d:\skymiku\stock
```

### 2.3 安裝依賴

```powershell
uv sync
```

這會自動建立虛擬環境（`.venv`）並安裝所有依賴套件。

## Step 3: 設定環境變數

### 3.1 建立 .env

```powershell
Copy-Item .env.example .env
```

### 3.2 編輯 .env

用任何文字編輯器開啟 `.env`，填入你的資訊：

```ini
# === 執行模式 ===
# trade  = 自動交易
# watch  = Shioaji 看盤不下單
# report = TWSE 公開延遲資料報表，不需 API key
RUN_MODE=trade
REPORT_POLL_SECONDS=5
REPORT_OUTPUT_DIR=data/reports

# === Shioaji 登入資訊 ===
API_KEY=你的API_Key_這裡貼上
SECRET_KEY=你的Secret_Key_這裡貼上

# === 電子憑證 ===
CA_PATH=C:/certs/Sinopac.pfx
CA_PASSWORD=你的憑證密碼
PERSON_ID=你的身分證字號

# === 模擬模式 ===
SIMULATION=true

# === 監控股票 ===
SYMBOLS=2330,0050

# === 策略參數 (可先用預設值) ===
ENTER_CUTOFF_TIME=09:30
EXIT_TIME=13:15
STOP_LOSS_PCT=-3.0
TAKE_PROFIT_PCT=6.0
TRAILING_STOP_PCT=2.0
MAX_FUND=500000
MAX_LOT_PER_SYMBOL=2

# === Telegram 通知 (選填) ===
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### 3.3 重要注意事項

- `.env` 已在 `.gitignore` 中，不會被提交到 Git
- `CA_PATH` 使用正斜線 `/` 分隔路徑（Windows 也適用）
- **第一次務必設定 `SIMULATION=true`**，確認系統正常後再改為 `false`
- 只看盤不下單請用 `RUN_MODE=watch`；完全不使用 Shioaji API 請用 `RUN_MODE=report`

## Step 4: 設定 Telegram 通知（選填）

### 4.1 建立 Telegram Bot

1. 在 Telegram 搜尋 `@BotFather`
2. 發送 `/newbot`
3. 依提示設定 Bot 名稱
4. 取得 Bot Token（格式如 `123456789:ABCdefGhIjKlMnOpQrStUvWxYz`）

### 4.2 取得 Chat ID

1. 在 Telegram 搜尋你剛建立的 Bot，發送任意訊息
2. 瀏覽器開啟: `https://api.telegram.org/bot你的TOKEN/getUpdates`
3. 找到 `"chat":{"id":12345678}` 中的數字
4. 將 Token 和 Chat ID 填入 `.env`

## Step 5: 驗證安裝

```powershell
# 確認模組可正常匯入
uv run python -c "from bot.main import main; print('OK')"
```

如果看到 `OK` 就表示安裝成功。

## Step 6: 首次執行

### 6.1 報表模式（不需 API Key）

```powershell
$env:RUN_MODE="report"; $env:SYMBOLS="2330,0050"; uv run stock-bot
```

此模式會輪詢 TWSE 公開延遲資料，結束時輸出 `data/reports/signals_YYYY-MM-DD.csv` 與 `report_YYYY-MM-DD.csv`。

### 6.2 看盤模式（需 API Key，不下單）

```powershell
$env:RUN_MODE="watch"; uv run stock-bot
```

此模式使用 Shioaji 即時行情，但不啟用 CA，也不送出委託。

### 6.3 自動交易/模擬交易模式

```powershell
# 確認 .env 中 SIMULATION=true
uv run stock-bot
```

你應該會看到：
1. 登入成功的 log
2. 前日收盤價載入
3. Tick 訂閱成功
4. 開始接收即時行情或公開延遲資料

按 `Ctrl+C` 可隨時停止。

## 目錄結構說明

```
d:\skymiku\stock\
  .env                 ← 你的設定（不會被 Git 追蹤）
  .env.example         ← 設定範本
  .gitignore           ← Git 忽略規則
  pyproject.toml       ← 專案與依賴定義
  README.md            ← 快速入門
  docs/                ← 技術文件
  src/bot/             ← 原始碼
  log/                 ← Log 檔案（自動建立）
  data/                ← 交易紀錄 CSV（自動建立）
  data/reports/        ← watch/report 訊號與報表（自動建立）
  .venv/               ← 虛擬環境（uv sync 建立）
```
