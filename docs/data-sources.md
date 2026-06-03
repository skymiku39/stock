# 資料源清單與健康度驗證

本文件列出系統使用的所有外部資料源、用途、是否需要憑證、以及失敗時的 fallback。
可用 `uv run stock-validate` 一鍵做「只讀」健康度檢查 (見最後一節)。

> 三層驗證概念：**link** 連結有填且格式正確 → **http** 能抓到有效內容 → **parse** 解析後可被流程使用。

## 公開資料源 (免憑證)

| 資料源 | 端點 / URL | 用途 | 憑證 | 失敗 fallback |
|--------|-----------|------|:----:|--------------|
| **公司基本資料 (上市)** | `openapi.twse.com.tw/v1/opendata/t187ap03_L` | 名稱/簡稱/**產業別**/上市日 → 持股分類 | 否 | 每日快取 `data/meta/company_info.json`；舊快取 |
| **公司基本資料 (上櫃)** | `tpex.org.tw/openapi/v1/mopsfin_t187ap03_O` | 同上 (上櫃) | 否 | 同上 |
| 月營收 (上市) | `openapi.twse.com.tw/v1/opendata/t187ap05_L` | 基本面月營收 YoY/MoM | 否 | 本地 cache；上櫃另抓 TPEx |
| 月營收 (上櫃) | `tpex.org.tw/openapi/v1/mopsfin_t187ap05_O` | 同上 (上櫃) | 否 | 本地 cache |
| 估值 PER/PBR/殖利率 (上市) | `openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL` | 估值面 | 否 | 本地 cache |
| 估值 (上櫃) | `tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis` | 估值面 (上櫃) | 否 | 本地 cache |
| **股利分派 (上市)** | `openapi.twse.com.tw/v1/opendata/t187ap45_L` | 現金/股票股利 (依股利年度彙總) | 否 | 舊端點 `t187ap46_L_ex` 已失效；本地 history 累積舊年度 |
| **股利分派 (上櫃)** | `mopsfin.twse.com.tw/opendata/t187ap45_O.csv` | 股利 (上櫃 CSV) | 否 | 本地 cache |
| 季報 EPS/三率 | `openapi.twse.com.tw/v1/opendata/t187ap06_L_*` (+ TPEx) | EPS、毛利率/營益率/淨利率 | 否 | 手動匯入 `data/fundamentals_manual/<ticker>.json` |
| 日 K 線 | TWSE `STOCK_DAY` / TPEx | 技術面 K 線 | 否 | 本地 SQLite `price_history` |
| 全市場收盤均價 | `openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_AVG_ALL` | ADR 溢價算現貨 | 否 | yfinance `.TW` |
| 三大法人籌碼 | TWSE OpenAPI (法人/借券/融資) | 籌碼面 | 否 | 本地 cache |
| 集保股權分散 | TDCC OpenData | 大戶 vs 散戶 | 否 | 本地 cache |
| **法說會行事曆** | `mopsov.twse.com.tw/mops/web/ajax_t100sb02_1` | 法說會日曆 | 否 | 舊主機 `mops.twse.com.tw` 會被安全性阻擋；可用 `MOPS_HOST` 覆寫主機 |
| **重大訊息** | `openapi.twse.com.tw/v1/opendata/t187ap04_L` (每日全市場) | 個股重大訊息 | 否 | fallback 走 `mopsov` 個股歷史 `ajax_t05st02`；遇安全性阻擋會明確標記失敗 |
| 新聞 | 鉅亨網 cnyes | 題材/情緒 | 否 | 空清單 |
| 網路搜尋 | DuckDuckGo HTML | 法說/題材研究 | 否 | 空清單 (DDG 偶回 202 限流) |
| 美股/指數/VIX/ADR | yfinance | 跨市場連動 | 否 | TWSE 收盤 fallback；未安裝則「無資料」 |
| **台指期正逆價差** | `openapi.taifex.com.tw/v1/DailyMarketReportFut` | 期貨領先指標 (正逆價差) | 否 | 抓不到則 macro note 標記、不影響其他分析 |

> **產業分類 (持股分析「未分類」修復)**：上述公司基本資料的「產業別」原始格式是**數字代碼**
> (例如 `24`=半導體業)，由 `bot/company_info.py` 的 `INDUSTRY_CODE_MAP` 轉成中文後寫入
> 本地 `stock_info`。持股分析頁查 DB 缺名稱/產業時會**自動補抓**；亦可手動全市場補齊：
>
> ```bash
> uv run stock-company-update            # 只補缺漏 (保留人工分類，如 散熱 / PCB / ABF)
> uv run stock-company-update --all       # 以官方資料全覆寫
> uv run stock-company-update --refresh   # 強制重抓來源 (略過當日快取)
> ```
>
> `stock-scheduler` 也會以 `SCHEDULER_COMPANY_INTERVAL_MIN` (預設一天一次) 自動補齊。
> ETF/基金不在公司清單，會標記為「ETF / 基金」而非「未分類」。

## 主動式 ETF 持股

| 來源 | URL 樣式 | 適用 | 失敗 fallback |
|------|---------|------|--------------|
| etfinfo.tw | `etfinfo.tw/etf/{symbol}/holdings` | 多數已掛牌主動式 ETF (22 檔) | 改 MoneyDJ |
| MoneyDJ | `moneydj.com/etf/x/basic/basic0007.xdjhtm?etfid={symbol}.tw` | etfinfo 未收錄者 (00402A/00404A/00405A/00406A/00407A/00998A)、海外型 | 來源頁顯示「查無資料」→ 標記 `no_data_yet` (略過 LLM，待後續自動補齊) |

> **已知限制（新掛牌）**：2026/5 下旬募集、6 月初才掛牌的 5 檔
> (00402A、00404A、00405A、00406A、00407A) 連發行商與第三方 (etfinfo / MoneyDJ) **都尚未公開每日成分股**
> (MoneyDJ 頁面顯示「查無資料」)。URL 雖回 HTTP 200，但持股表為空。
> - 程式會將其標記為 **`no_data_yet`**（與真正解析失敗 `no_holdings_parsed` 區分），且**不浪費 LLM 呼叫**。
> - **背景排程器 (`stock-scheduler`) 會定期重抓**，一旦來源公開持股即自動產生 CSV，**無需人工介入**。
> - 26/27 檔（含已修復的 00998A）可正常解析；屬資料可得性限制，非程式錯誤。
>
> 持股解析依賴 Gemini (`extract_etf_holdings` prompt)，需 `GEMINI_API_KEY`。

## 需憑證 / 即時的資料源

| 資料源 | 用途 | 憑證 | 備註 / fallback |
|--------|------|:----:|----------------|
| Shioaji 即時行情/下單 | trade/watch 模式 | `API_KEY`/`SECRET_KEY`(+CA 實單) | 模擬環境 snapshot 常回 0 筆 → 明確回報限制，不假裝可用 |
| TWSE MIS 延遲報價 | report 模式 tick | 否 | 盤中取得 tick；非盤中至少取得前收價並標示限制 |
| Google Sheets / Drive | 多機同步 | Service Account JSON | 未設定則停用；`stock-validate --cloud` 才 live ping |

## 一鍵健康度驗證

```bash
uv run stock-validate                  # 全部公開資料源 (read-only，不碰正式 data/)
uv run stock-validate --json out.json  # 另存 JSON 摘要
uv run stock-validate --md out.md      # 另存 Markdown 摘要
uv run stock-validate --shioaji        # 額外嘗試 Shioaji 登入/contract/snapshot (需憑證)
uv run stock-validate --mis-seconds 30 # MIS 即時 tick 連續輪詢 30 秒
uv run stock-validate --cloud          # 對 Google Sheets 做 live ping
```

特性：
- **只讀**：所有抓取導向暫存資料夾，不覆蓋正式 `data/`、不下單、不寫雲端。
- **不洩漏金鑰**：僅顯示憑證「是否存在 (present=true/false)」，絕不印出金鑰內容。
- **三層輸出**：每項回報 `layer / ok / rows / sample_date / error / note`。
- **誠實標記**：MOPS 安全性阻擋、Shioaji 模擬 0 筆、ETF 新掛牌無持股等，皆以
  `error` / `note` 明確標記，不會被當成「資料可用」。
- 結束碼：有「真正失敗 (非警告)」回傳非 0，方便接 CI。
