# 只看不買：啟動背景蒐集 + 儀表板（不會下單）
# 用法: .\scripts\start-watch-only.ps1
Set-Location $PSScriptRoot\..

Write-Host "=== Stock Bot 只看不買模式 ===" -ForegroundColor Cyan
Write-Host "RUN_MODE=watch | SCHEDULER_MONITOR_MODE=watch | 當沖已封存"
Write-Host ""

# 若已有 trade 子行程，嘗試停止（透過 dashboard runner 同路徑）
$logDir = Join-Path (Get-Location) "log"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

Write-Host "[1/4] 補齊量化 / 微笑曲線樣本資料 (日K、基本面、籌碼)..."
uv run stock-watch-data-fetch

Write-Host "[2/4] 儲存一輪觀察快照..."
uv run stock-watch-snapshot

Write-Host "[3/4] 啟動背景排程 (另開視窗)..."
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$((Get-Location).Path)'; uv run stock-scheduler"
)

Write-Host "[4/4] 啟動儀表板..."
Write-Host "  左側: 熱門個股期貨 -> 約10元成交熱度"
uv run stock-dashboard
