<#
.SYNOPSIS
    一鍵啟動完整自動化環境：AI 閘道 + stock-scheduler + 儀表板。

.DESCRIPTION
    依序啟動：
      1. Gemini 瀏覽器閘道 (port 8816)
      2. Cursor 閘道 (port 8815)
      3. 等待閘道就緒（/health）
      4. stock-scheduler（背景常駐）
      5. stock-dashboard（前景，Ctrl+C 可結束）

    每個子行程在獨立視窗運行，關閉本視窗不影響已啟動的服務。

.PARAMETER NoDashboard
    不啟動儀表板（僅啟動閘道與排程器）。

.PARAMETER HealthOnly
    只檢查各服務健康狀態，不啟動任何東西。
#>

param(
    [switch]$NoDashboard,
    [switch]$HealthOnly
)

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$GeminiGatewayRoot = "D:\skymiku\蹭google的geminiAI"
$CursorGatewayRoot = "D:\skymiku\蹭cursor的AI"

# --- Health check helper ---
function Test-Gateway {
    param([string]$Name, [string]$Url)
    try {
        $resp = Invoke-RestMethod -Uri $Url -TimeoutSec 5 -ErrorAction Stop
        if ($resp.ready) {
            Write-Host "  ✓ $Name READY" -ForegroundColor Green
            return $true
        } else {
            Write-Host "  ✗ $Name NOT READY (ready=false)" -ForegroundColor Yellow
            return $false
        }
    } catch {
        Write-Host "  ✗ $Name OFFLINE ($($_.Exception.Message))" -ForegroundColor Red
        return $false
    }
}

if ($HealthOnly) {
    Write-Host "=== 服務健康檢查 ===" -ForegroundColor Cyan
    Test-Gateway "Gemini閘道(8816)" "http://127.0.0.1:8816/health" | Out-Null
    Test-Gateway "Cursor閘道(8815)" "http://127.0.0.1:8815/health" | Out-Null

    # Check scheduler heartbeat
    $heartbeat = Join-Path $ProjectRoot "data\scheduler.state.json"
    if (Test-Path $heartbeat) {
        $state = Get-Content $heartbeat -Raw | ConvertFrom-Json
        $lastBeat = $state.last_heartbeat
        Write-Host "  ✓ Scheduler 心跳: $lastBeat" -ForegroundColor Green
    } else {
        Write-Host "  ✗ Scheduler 未執行 (無心跳檔)" -ForegroundColor Yellow
    }
    exit 0
}

Write-Host "=== Stock-bot 全自動啟動 ===" -ForegroundColor Cyan
Write-Host "專案: $ProjectRoot"
Write-Host ""

# --- 1. 啟動 Gemini 閘道 ---
if (Test-Path $GeminiGatewayRoot) {
    $geminiRunning = Test-Gateway "Gemini閘道" "http://127.0.0.1:8816/health"
    if (-not $geminiRunning) {
        Write-Host "  啟動 Gemini 閘道..." -ForegroundColor Cyan
        Start-Process -FilePath "uv" -ArgumentList "run gemini-gateway" `
            -WorkingDirectory $GeminiGatewayRoot -WindowStyle Minimized
        Start-Sleep -Seconds 3
    }
} else {
    Write-Host "  SKIP Gemini 閘道（目錄不存在）" -ForegroundColor DarkYellow
}

# --- 2. 啟動 Cursor 閘道 ---
if (Test-Path $CursorGatewayRoot) {
    $cursorRunning = Test-Gateway "Cursor閘道" "http://127.0.0.1:8815/health"
    if (-not $cursorRunning) {
        Write-Host "  啟動 Cursor 閘道..." -ForegroundColor Cyan
        Start-Process -FilePath "uv" -ArgumentList "run cursor-gateway" `
            -WorkingDirectory $CursorGatewayRoot -WindowStyle Minimized
        Start-Sleep -Seconds 3
    }
} else {
    Write-Host "  SKIP Cursor 閘道（目錄不存在）" -ForegroundColor DarkYellow
}

# --- 3. 等待閘道就緒 ---
Write-Host "`n等待閘道就緒..." -ForegroundColor Cyan
$retries = 0
$maxRetries = 10
while ($retries -lt $maxRetries) {
    $geminiOk = Test-Gateway "Gemini" "http://127.0.0.1:8816/health"
    $cursorOk = Test-Gateway "Cursor" "http://127.0.0.1:8815/health"
    if ($geminiOk -or $cursorOk) {
        Write-Host "`n  至少一個閘道已就緒！" -ForegroundColor Green
        break
    }
    $retries++
    if ($retries -lt $maxRetries) {
        Write-Host "  等待 5 秒後重試... ($retries/$maxRetries)" -ForegroundColor DarkGray
        Start-Sleep -Seconds 5
    }
}

if ($retries -ge $maxRetries) {
    Write-Host "`n  警告：所有閘道都未就緒。Scheduler 仍會啟動，但 LLM 功能將無法使用。" -ForegroundColor Yellow
}

# --- 4. 啟動 Scheduler ---
Write-Host "`n啟動 stock-scheduler..." -ForegroundColor Cyan
Start-Process -FilePath "uv" -ArgumentList "run stock-scheduler" `
    -WorkingDirectory $ProjectRoot -WindowStyle Minimized
Write-Host "  ✓ Scheduler 已在背景啟動" -ForegroundColor Green

# --- 5. 啟動 Dashboard ---
if (-not $NoDashboard) {
    Write-Host "`n啟動 stock-dashboard (Ctrl+C 結束)..." -ForegroundColor Cyan
    Set-Location $ProjectRoot
    uv run stock-dashboard
} else {
    Write-Host "`n完成。所有服務已在背景執行。" -ForegroundColor Green
    Write-Host "  手動檢查: .\scripts\start_full_auto.ps1 -HealthOnly"
}
