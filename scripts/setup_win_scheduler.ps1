<#
.SYNOPSIS
    建立 Windows 工作排程器任務，開機自動啟動 stock-scheduler 與 AI 閘道。

.DESCRIPTION
    此腳本會建立三個排程任務：
      1. StockBot-GeminiGateway  — 登入後啟動 Gemini 瀏覽器閘道 (port 8816)
      2. StockBot-CursorGateway  — 登入後啟動 Cursor 閘道 (port 8815)
      3. StockBot-Scheduler      — 登入後 60 秒啟動 stock-scheduler（等閘道就緒）

    執行需要管理員權限。

.PARAMETER Remove
    移除所有 StockBot-* 排程任務。

.EXAMPLE
    # 安裝
    .\scripts\setup_win_scheduler.ps1

    # 移除
    .\scripts\setup_win_scheduler.ps1 -Remove
#>

param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$GeminiGatewayRoot = "D:\skymiku\蹭google的geminiAI"
$CursorGatewayRoot = "D:\skymiku\蹭cursor的AI"

$TaskPrefix = "StockBot"
$TaskNames = @(
    "$TaskPrefix-GeminiGateway",
    "$TaskPrefix-CursorGateway",
    "$TaskPrefix-Scheduler"
)

if ($Remove) {
    foreach ($name in $TaskNames) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "已移除: $name" -ForegroundColor Yellow
        } else {
            Write-Host "不存在: $name" -ForegroundColor DarkGray
        }
    }
    Write-Host "`n完成。" -ForegroundColor Green
    exit 0
}

# --- 找 uv ---
$uvPath = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uvPath) {
    $uvPath = "$env:USERPROFILE\.local\bin\uv.exe"
    if (-not (Test-Path $uvPath)) {
        Write-Error "找不到 uv，請先安裝：irm https://astral.sh/uv/install.ps1 | iex"
    }
}
Write-Host "uv: $uvPath"

# --- Helper: 建立排程任務 ---
function New-StockTask {
    param(
        [string]$Name,
        [string]$WorkDir,
        [string]$Command,
        [string]$Arguments,
        [int]$DelaySeconds = 0
    )

    $action = New-ScheduledTaskAction `
        -Execute $Command `
        -Argument $Arguments `
        -WorkingDirectory $WorkDir

    $trigger = New-ScheduledTaskTrigger -AtLogOn
    if ($DelaySeconds -gt 0) {
        $trigger.Delay = "PT${DelaySeconds}S"
    }

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 5) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 24)

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "Stock-bot 自動化: $Name" `
        -RunLevel Highest | Out-Null

    Write-Host "已建立: $Name" -ForegroundColor Green
}

# --- 1. Gemini 閘道 ---
if (Test-Path $GeminiGatewayRoot) {
    New-StockTask `
        -Name "$TaskPrefix-GeminiGateway" `
        -WorkDir $GeminiGatewayRoot `
        -Command $uvPath `
        -Arguments "run gemini-gateway" `
        -DelaySeconds 10
} else {
    Write-Host "SKIP Gemini 閘道：目錄不存在 $GeminiGatewayRoot" -ForegroundColor DarkYellow
}

# --- 2. Cursor 閘道 ---
if (Test-Path $CursorGatewayRoot) {
    New-StockTask `
        -Name "$TaskPrefix-CursorGateway" `
        -WorkDir $CursorGatewayRoot `
        -Command $uvPath `
        -Arguments "run cursor-gateway" `
        -DelaySeconds 10
} else {
    Write-Host "SKIP Cursor 閘道：目錄不存在 $CursorGatewayRoot" -ForegroundColor DarkYellow
}

# --- 3. Scheduler ---
New-StockTask `
    -Name "$TaskPrefix-Scheduler" `
    -WorkDir $ProjectRoot `
    -Command $uvPath `
    -Arguments "run stock-scheduler" `
    -DelaySeconds 60

Write-Host "`n=== 完成 ===" -ForegroundColor Green
Write-Host "排程器會在登入後 60 秒自動啟動。"
Write-Host "閘道會在登入後 10 秒啟動。"
Write-Host ""
Write-Host "手動操作："
Write-Host "  查看: Get-ScheduledTask -TaskName 'StockBot-*'"
Write-Host "  移除: .\scripts\setup_win_scheduler.ps1 -Remove"
