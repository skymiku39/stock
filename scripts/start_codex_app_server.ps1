param([string]$Root = '')
$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Root = (Resolve-Path -LiteralPath $Root).Path
$binary = Join-Path $env:APPDATA 'npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe'
if (-not (Test-Path -LiteralPath $binary)) { throw 'Installed Codex executable is missing.' }
$folder = Join-Path $Root 'log'
[IO.Directory]::CreateDirectory($folder) | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$statePath = Join-Path $Root 'data\model_service_status.json'
# Replace fails while another process is reading the status file. Skip that
# heartbeat instead of throwing: with ErrorActionPreference=Stop an exception
# ends this script, the finally block kills the model server, and the .tmp file
# is left behind (9 such files from 2026-10-30 to 11-08).
# Keep this file ASCII: it has no BOM, so Windows PowerShell reads it as ANSI.
function Save-State($Value) {
    [IO.Directory]::CreateDirectory((Split-Path -Parent $statePath)) | Out-Null
    $tmp = $statePath + '.' + $PID + '.tmp'
    [IO.File]::WriteAllText($tmp, ($Value | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            if (Test-Path -LiteralPath $statePath) { [IO.File]::Replace($tmp, $statePath, [NullString]::Value) }
            else { [IO.File]::Move($tmp, $statePath) }
            return $true
        } catch {
            Start-Sleep -Milliseconds (200 * $attempt)
        }
    }
    try { [IO.File]::Delete($tmp) } catch { }
    return $false
}
$child = Start-Process -FilePath $binary -ArgumentList @('app-server','--listen','ws://127.0.0.1:4500') -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $folder "model_server_$stamp.stdout.log") -RedirectStandardError (Join-Path $folder "model_server_$stamp.stderr.log")
$state = @{state='running';pid=$child.Id;created_at=$child.StartTime.ToUniversalTime().ToString('o');executable=$binary;root=$Root;started_at=[DateTimeOffset]::Now.ToString('o')}
try {
    do {
        $state.heartbeat_at = [DateTimeOffset]::Now.ToString('o')
        Save-State $state | Out-Null
    } while (-not $child.WaitForExit(15000))
    $child.Refresh()
    $code = $child.ExitCode
    $state.state = 'stopped'
    $state.exit_code = $code
    $state.finished_at = [DateTimeOffset]::Now.ToString('o')
    Save-State $state | Out-Null
    exit $code
} finally {
    if (-not $child.HasExited) { $child.Kill(); $child.WaitForExit(5000) | Out-Null }
}
