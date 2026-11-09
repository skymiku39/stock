# 微笑買賣方案（smile_curve）自動化買賣檢查報告

日期：2026-11-09　分支：`codex/evidence-and-raw-recovery`　範圍：只檢查，未改程式碼。

「微笑買賣方案」對應到唯一的實作：微笑曲線策略 `smile_curve`（`docs/trading-rules.md:11`）。
- 盤中自動買賣：`src/bot/strategy_smile.py`（`SmileCurveStrategy`）
- 狀態機／回測核心：`src/bot/smile_curve.py`（`SmileCurveEngine`）
- 日 K 執行代理：`src/bot/smile_execution.py`；選股與回測 CLI：`smile_curve_screener*.py`、`smile_curve_backtest*.py`
- 共用下單／風控：`src/bot/strategy.py`（`BaseStrategy`）、`src/bot/risk_guard.py`、`src/bot/broker.py`
- 啟動入口：`uv run stock-bot --simulate --strategy smile_curve`（`src/bot/main.py:83-111`）

測試：相關 10 個測試檔（smile_* 5 個、paper_follow、simulation_check、tick_continuation、event_chain、event_bus）共 `91 passed`（5.3 秒）。這次沒有重跑全套測試。

## 一、已完善

1. **模擬模式防護（多層，失效時一律拒絕）**
   - 入口：`smile_curve` 搭配 `trade` 且 `SIMULATION` 不是 true，登入前就 `sys.exit(1)`（`src/bot/main.py:109-111`）；`--simulate` 只在記憶體中強制 `simulation=True`，不會改 `.env`（`main.py:103-107`）。
   - 券商：`smile_curve` 一律用 `simulation_only` 建 `SjBroker`（`main.py:160`）；登入、重連、下單、取消都會再檢查 `settings.simulation` 與 `api.simulation is True`（`src/bot/broker.py:99`、`broker.py:320`、`broker.py:338`、`broker.py:394-396`、`broker.py:668-670`）。
   - 策略：建構時拒絕正式模式（`src/bot/strategy_smile.py:20-21`）；每個 tick 都再檢查 broker 是否接在模擬環境（`strategy_smile.py:117-119`）。
   - 測試：`tests/test_simulation_check.py:209`、`:215`、`:246`、`:269`。
2. **進場條件**：從標準價下跌達各階門檻（預設 `1:1,3:2,5:3,8:4`）才買，每階只買一次；跌破最深一階後每天最多再加碼一次（`strategy_smile.py:140-153`、`src/bot/smile_curve.py:75-94`）。只在 09:00–13:30 處理、行情品質 `ok`、價格為有限正數時才動作（`strategy_smile.py:120-123`）。買價向下取整到升降單位，股票與 0050 的升降單位分開處理（`src/bot/smile_execution.py:11-23`）。
3. **出場條件**：價格回到標準價以上，且每一批扣手續費與證交稅後都有淨利，才送限價賣單（`strategy_smile.py:135-139`、`smile_curve.py:156-161`）；0050 的賣出稅率用 0.1%（`smile_curve.py:120-121`）。出清後重設標準價並重新開始一輪（`strategy_smile.py:95-96`）。依設計不停損、不套用當沖收盤強制平倉（`strategy_smile.py:155-161`、`docs/trading-rules.md:15`），有對應測試 `test_simulation_check.py:162`、`:198`。
4. **部位與資金控管**：每階的股數受單檔上限（換算成股數，零股也算）、零股上限與剩餘資金限制（`strategy_smile.py:102-114`）；下單前一定經過 `RiskGuard.check_entry`（總資金、單筆金額、檔數、黑名單、下單頻率、Kill Switch，見 `src/bot/risk_guard.py:1-35`、`src/bot/strategy.py:1125-1152`）。送單前先保留這筆的資金，取消或失敗時釋放（`strategy.py:1166-1172`、`strategy.py:988-993`）。測試：`test_simulation_check.py:173`、`:149`、`:225`。
5. **成交驅動與重啟對帳**：階段狀態依實際成交推進，部分成交照實際數量入帳，同一筆成交回報不會重複入帳（`strategy.py:686-689`、`strategy_smile.py:77-100`）；狀態檔用暫存檔再替換的方式寫入（`strategy_smile.py:44-51`）。重啟時，批次與持倉對不上，或還有未結案的模擬委託，就拒絕啟動（`strategy_smile.py:63-75`）。不是本次策略送出的委託，其成交回報一律忽略（`strategy_smile.py:56-61`）。測試：`test_simulation_check.py:59-136`、`:184`、`:287`。
6. **例外處理**：單一 tick 出錯只記錄、不會讓行情執行緒中斷（`strategy.py:485-488`）；委託回報處理出錯也只記錄（`strategy.py:612-618`）；券商回傳沒有委託編號時，拉 Kill Switch 並中止（`strategy.py:642-646`）。
7. **回測的保守假設**：用前一日成交量限制可成交量、漲跌停時擋單、滑價、不用當天的高低點決定成交（`smile_execution.py`，測試 `tests/test_smile_execution.py`、`tests/test_smile_portfolio.py` 共 23 項）。

## 二、缺漏或風險

1. **【中】限價單沒有逾時取消，未成交的單會卡住整天**：只有成交、取消、失敗三種狀態會清除未完成委託（`src/bot/strategy.py:963-993`）；只要該檔還有未完成委託，就不會再下任何單（`strategy_smile.py:132-133`、`strategy.py:1062-1066`）。買單價格向下取整、賣單向上取整（`strategy_smile.py:138`、`:148`），價格一走開，ROD 限價單可能一直掛到收盤，這段時間不會再加碼，也不會賣出。目前只有驗收腳本會取消逾時單（`src/bot/simulation_check.py:169`），正式流程沒有這個機制。
2. **【中】盤中出場規則和回測不一致**：盤中要「全部批次都有淨利」才一次全賣（`strategy_smile.py:136`）；回測引擎則是把有淨利的批次逐筆賣掉，留下的批次重新開放各階加碼（`smile_curve.py:252-269`）。手動固定標準價（`SMILE_REFERENCE_PRICES`）時，可能出現某一批的成本高於標準價，這時盤中永遠不會賣，回測卻會賣出其他批次。結果是回測報酬不能直接代表盤中行為。
3. **【中】沒有停損，熔斷也碰不到**：損失熔斷只看「已實現」虧損（`src/bot/risk_guard.py:17-20`），而微笑策略依設計從不認賠賣出，所以熔斷不會觸發。最大虧損只受資金上限與單檔上限約束；跌破最深一階後還會每天加碼一次（`strategy_smile.py:144-145`），直到碰到資金上限。這是設計上的選擇，但沒有「最大浮虧」或「加碼總次數」的護欄。
4. **【低，待確認】儀表板的 Kill Switch 可能拉不到模擬策略**：模擬時策略的 `RiskGuard` 根目錄是 `data/simulation`（`strategy.py:116-123`、`strategy.py:193-197`，`main.py:204` 會傳入 `project_root`），所以旗標檔在 `data/simulation/data/.kill_switch`；儀表板的按鈕則用專案根目錄（`src/bot/dashboard/pages.py:358`），寫的是 `data/.kill_switch`。尚未實際操作驗證；而且 Kill Switch 本來就只擋進場。
5. **【低】不用 `--simulate` 啟動時，觀察清單可能自動併入不支援的代號**：只有 `--simulate` 會關掉 `symbols_auto_merge`（`main.py:106`）。直接在 `.env` 設 `STRATEGY_TYPE=smile_curve` 時，併入 0050 以外的 ETF，`_engine` 會丟出 ValueError（`strategy_smile.py:38-39`）。在 tick 裡只會一直刷錯誤紀錄；如果發生在重啟對帳的迴圈（`strategy_smile.py:65`），則會讓啟動失敗。
6. **【低】狀態檔損毀時直接啟動失敗**：讀 `smile_state.json` 沒有錯誤處理（`strategy_smile.py:29-35`）。這樣不會錯下單，但錯誤訊息不清楚，也沒有寫好的復原步驟。
7. **【低】階段意圖只存在記憶體**：哪一階還沒成交的意圖存在 `_smile_intent`（`strategy_smile.py:24`、`:151`），重啟後就沒了。有未結案委託時重啟會被擋下（`strategy_smile.py:72-75`），目前可以接受；但如果之後放寬重啟條件，就會失去「哪一階已完成」的資訊。
8. **【缺測試】** 找不到以下情境的測試：限價單一直沒成交卡住同一檔（第 1 項）；手動標準價造成部分批次虧損時的盤中與回測差異（第 2 項）；跌破最深一階後連續多日加碼直到資金上限；`smile_state.json` 損毀；自動併入不支援的 ETF。
9. **【範圍外】** 券商端的實際成交還沒驗收：`local` 模式只證明程式流程正確（`docs/trading-rules.md:37`），這件事已列在 PROGRESS P3。

## 三、建議下一步（都需要改程式碼，請使用者先決定）

1. 加上未成交限價單的逾時取消（例如 N 分鐘未成交就取消，並在價格偏離時重新報價），並補測試。這項影響最大。
2. 統一盤中與回測的出場規則：盤中改成逐批賣出，或者回測改成全部有淨利才一起賣。請使用者先決定哪一種才是方案原意。
3. 決定是否加上「最大浮虧」或「最深一階之後加碼次數上限」，並讓這個條件可以拉 Kill Switch。
4. 統一 Kill Switch 路徑：儀表板依模擬或正式模式選擇正確的 `data/` 根目錄，或者兩邊同時檢查。
5. 以 `.env` 啟動 `smile_curve` 時也強制關閉 `symbols_auto_merge`，或改成略過不支援的代號。
6. 補上第二段第 8 項列出的測試，然後重跑全套測試。
