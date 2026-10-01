# 資料源清單與健康度驗證

本文件列出系統使用的所有外部資料源、用途、是否需要憑證、以及失敗時的 fallback。
可用 `uv run stock-validate` 一鍵做健康度檢查 (見最後一節；**省略 `--root` 才是對正式 `data/` 的安全預設**)。

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
| **法說簡報原文** | MOPS 行事曆 `presentation_url` → `data/mops_downloads/` | `research_ticker` 的 `presentation_block` | 否 | 先讀本地 TXT/HTML/PDF；不足時自動下載近 2 場並抽字；失敗則該段為「(無)」 |
| **量化證據** | 本地 fundamentals / chips / etf_holdings | `research_ticker` 的 `quant_block` | 否 | 基本面先讀 cache，空才補抓；籌碼用傳入或近 5 日；ETF 共識只讀本地持股 |
| **重大訊息** | `openapi.twse.com.tw/v1/opendata/t187ap04_L` (每日全市場) | 個股重大訊息 | 否 | fallback 走 `mopsov` 個股歷史 `ajax_t05st02`；遇安全性阻擋會明確標記失敗 |
| 新聞 | 鉅亨網 cnyes | 題材/情緒 | 否 | 空清單 |
| 網路搜尋 | DuckDuckGo HTML | 法說/題材研究 | 否 | 空清單 (DDG 偶回 202 限流) |
| 美股/指數/VIX/ADR | yfinance | 跨市場連動 | 否 | TWSE 收盤 fallback；未安裝則「無資料」 |
| **台指期正逆價差** | `openapi.taifex.com.tw/v1/DailyMarketReportFut` | 期貨領先指標 (正逆價差) | 否 | 抓不到則 macro note 標記、不影響其他分析 |

### Macro 時間與來源判定

macro 快照的 `asof_date` 是快照／抓取日期，不等於每一筆行情的觀測日期。
指數、ADR、台股現貨與 USDTWD 會各自保存 `source_date`；ADR 溢價若跨市場
日期未對齊，結果仍可供參考，但會在 `notes` 標示日期差異。台股現貨若退回
TWSE `STOCK_DAY_AVG_ALL` 且回應未提供可可靠確認的觀測日，系統會保留空白，
不以抓取時間冒充市場日期。

重大訊息 OpenAPI 的「有效回應但沒有該股票」與「HTTP／JSON 失敗」也會分開
記錄：前者是 `openapi_valid=true` 且 `openapi_count=0`，後者保留
`openapi_valid=false` 與 `openapi_error`，不會把來源故障當成沒有重大訊息。

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
| etfinfo.tw | `etfinfo.tw/etf/{symbol}/holdings` | 目前 27 檔主動式 ETF；Nuxt payload／完整表先做 deterministic parse | 頁面契約改版時保留 raw，才允許受控 LLM fallback |
| MoneyDJ ETF | `moneydj.com/etf/x/basic/basic0007.xdjhtm?etfid={symbol}.tw` | 使用者覆寫來源時的 fallback；前十大摘要不可當完整持股 | 標記 `source_incomplete_top_n`，保留 raw 且禁止 LLM 假補全 |

## 共同基金持股（納入共識追蹤）

| 來源 | URL 樣式 | 適用 | 解析方式 |
|------|---------|------|----------|
| MoneyDJ FundDJ | `moneydj.com/funddj/yp/yp013000.djhtm?a=<fund_id>` | **ALI006 安聯台灣科技基金**（`fund_id=acdd04`） | `fund_holdings_parser` 確定性解析 HTML table（免 LLM）；公司名 → 代號走 `company_info.lookup_symbol_by_name` |

> 共同基金不是 ETF，但持股對共識加碼／跟單候選仍有參考價值，故以虛擬代號
> `ALI006` 掛進 `DEFAULT_ACTIVE_ETFS`，快照同樣寫入 `data/etf_holdings/ALI006/`。
> Shioaji 查不到契約時會自動略過報價，不影響持股共識計算。

> **目前狀態（2026-09-19）**：27 檔 ETF 都已能由 etfinfo.tw 的完整 SSR payload
> 產生可重播的 canonical CSV；其中 00404A／00406A 的選擇權／衍生品列保留於 raw，
> 但明確排除於股票持股模型。若未來來源退化成只有前十大，程式會標記
> **`source_incomplete_top_n`**，不把摘要誤當完整快照，也不會交給 LLM 假補全。
> `stock-scheduler` 仍會定期重抓；只有 parser 契約失敗且來源不是已知 top-N 時，才進入
> 受控的 `extract_etf_holdings` fallback，因此目前正常 batch 不需要 `GEMINI_API_KEY`。
> 主動式 ETF 清單也會在完整研究管線啟動時以 `sync_etf_meta_to_db()` 物化到
> `etf_meta`；這些列是 `config://`、`quality_status=unknown` 的設定 metadata，
> 不會被當成外部市場證據，已有 exact 外部資料則不覆蓋。

## 需憑證 / 即時的資料源

| 資料源 | 用途 | 憑證 | 備註 / fallback |
|--------|------|:----:|----------------|
| Shioaji 即時行情/下單 | trade/watch 模式 | `API_KEY`/`SECRET_KEY`(+CA 實單) | 模擬環境 snapshot 常回 0 筆 → 明確回報限制，不假裝可用 |
| TWSE MIS 延遲報價 | report 模式 tick | 否 | 盤中取得 tick；非盤中至少取得前收價並標示限制 |
| Google Sheets / Drive | 多機同步 | Service Account JSON | 未設定則停用；`stock-validate --cloud` 才 live ping |

## 一鍵健康度驗證

```bash
# 安全預設：省略 --root → 暫存資料根（結束後清理），不碰正式 data/
uv run stock-validate
uv run stock-validate --json out.json  # 另存 JSON 摘要
uv run stock-validate --md out.md      # 另存 Markdown 摘要
uv run stock-validate --shioaji        # 額外嘗試 Shioaji 登入/contract/snapshot (需憑證)
uv run stock-validate --mis-seconds 30 # MIS 即時 tick 連續輪詢 30 秒
uv run stock-validate --cloud          # 對 Google Sheets 做 live ping

# 明確 --root：該路徑會傳給收集器，可能寫入 cache / raw / provenance
# （僅在你有意對該樹落地資料時使用；不要誤當成「只讀」）
uv run stock-validate --root C:\path\to\project
```

### `--root` 語意（與實作對齊）

| 呼叫方式 | 資料 root | 副作用 |
|----------|-----------|--------|
| **省略 `--root`** | `tempfile` 暫存目錄，檢查結束後刪除 | 不寫正式 `data/`；設定（`.env` 等）仍讀 **目前工作目錄** |
| **`--root PATH`** | 明確傳入的 `PATH` | 收集器可寫入該 root 下的 **cache / raw / provenance**；設定也讀該 root |

安全／近似只讀驗證請**省略 `--root`**（或指向用完即丟的目錄）。對正式專案樹加 `--root` 不是 read-only。

其他特性：
- **不下單**；雲端 live ping 僅在加 `--cloud` 時打到遠端。
- **不洩漏金鑰**：僅顯示憑證「是否存在 (present=true/false)」，絕不印出金鑰內容。
- **三層輸出**：每項回報 `layer / ok / rows / sample_date / error / note`。
- **誠實標記**：MOPS 安全性阻擋、Shioaji 模擬 0 筆、ETF 新掛牌無持股、MIS 盤後殘留列等，皆以
  `error` / `note` 明確標記，不會被當成「即時資料可用」。
- 結束碼：有「真正失敗 (非警告)」回傳非 0，方便接 CI。
