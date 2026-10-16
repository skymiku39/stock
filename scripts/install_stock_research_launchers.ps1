param([string]$Destination = 'C:\start_server')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root 'scripts\start_stock_research.ps1'
if (-not (Test-Path -LiteralPath $launcher)) { throw "Missing launcher: $launcher" }
$folder = Join-Path $Destination '台股研究與微笑曲線'
$null = New-Item -ItemType Directory -Path $folder -Force
function Save-LauncherFile([string]$Path, [string]$Content, [bool]$Ascii = $false) {
    if (Test-Path -LiteralPath $Path) {
        if ([IO.File]::ReadAllText($Path) -eq $Content) { return }
        Copy-Item -LiteralPath $Path -Destination ($Path + '.bak.' + (Get-Date -Format 'yyyyMMddHHmmssfff'))
    }
    $encoding = if ($Ascii) { [Text.Encoding]::ASCII } else { New-Object Text.UTF8Encoding($true) }
    [IO.File]::WriteAllText($Path, $Content, $encoding)
    Write-Output "Installed: $Path"
}
$entries = [ordered]@{
    '啟動_每日全量採集與分析.cmd' = 'start'
    '查看_每日採集與分析狀態.cmd' = 'status'
    '補跑_今日未完成分析.cmd' = 'retry'
    '開啟_今日採集分析進度.cmd' = 'progress'
    '開啟_採集分析日誌.cmd' = 'logs'
}
foreach ($entry in $entries.GetEnumerator()) {
    $content = "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -Action $($entry.Value)`r`npause`r`n"
    Save-LauncherFile (Join-Path $folder $entry.Key) $content $true
}
$content = "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -Action start`r`npause`r`n"
Save-LauncherFile (Join-Path $Destination '啟動_股票每日採集與分析.cmd') $content $true
$instructions = @"
每日全追蹤採集與分析

平常雙擊「啟動_每日全量採集與分析.cmd」即可。
它會啟動既有模型服務與每日排程；已在執行時不重啟、不另開第二份。
關閉啟動視窗不會停止背景排程。

「查看_每日採集與分析狀態.cmd」：查看服務、心跳與今天進度。
「補跑_今日未完成分析.cmd」：手動執行今日流程。若背景已有同一工作則不重複執行。
  手動補跑請保留視窗直到結束；平常使用背景排程即可。
「開啟_今日採集分析進度.cmd」：開啟當日資料夾，status.json 為全體進度；
  verified 子目錄保存個股報告、原始輸入關聯與本次補跑驗證結果。
「開啟_採集分析日誌.cmd」：開啟 log，查看 scheduler_research 開頭的最新日誌。

每天（含週末）檢查全部追蹤標的；一般每 30 分鐘檢查，失敗先隔 5 分鐘重試兩次。
成功分析驗證後沿用，未完成部分補跑；無法驗證來源者列 excluded，不納入分析。
complete=false 表示仍未全部完成。Running 僅表示程序在執行，不等於分析成功。
電腦需開機並登入 Windows，且網路、模型服務可用。

唯一專案與資料位置：$root
啟動程式：$launcher
這裡只有啟動入口，不複製 .env、模型金鑰、原始資料或另一套程式。
原有台股儀表板（8501）與微笑曲線（8502）入口保留。
"@
Save-LauncherFile (Join-Path $folder '每日採集與分析_使用說明.txt') $instructions
