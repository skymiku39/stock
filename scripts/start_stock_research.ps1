param(
    [ValidateSet('start', 'status', 'retry', 'progress', 'logs')][string]$Action = 'start',
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$schedulerName = 'SkyMiku Stock Daily Scheduler'
$modelName = 'SkyMiku Codex LLM App Server'
$today = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'Taipei Standard Time').ToString('yyyy-MM-dd')
$dailyDir = Join-Path $projectRoot "data\daily_research\$today"
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) { throw 'uv is missing from PATH.' }
$uvPath = $uv.Source
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.venv\Scripts\python.exe'))) {
    throw 'Project environment missing. Run uv sync in the project first.'
}
function Start-ExistingTask([string]$Name) {
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
    if ($task.State -eq 'Disabled') { throw "Task disabled: $Name" }
    if ($task.State -eq 'Running') { Write-Output "Already running: $Name"; return }
    Start-ScheduledTask -TaskName $Name
    Write-Output "Start requested: $Name"
}
function Show-Status {
    foreach ($name in @($modelName, $schedulerName)) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
        Write-Output "$name : $($task.State)"
    }
    $heartbeat = Join-Path $projectRoot 'data\scheduler_status.json'
    if (Test-Path -LiteralPath $heartbeat) {
        $s = Get-Content -LiteralPath $heartbeat -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Output "Scheduler heartbeat: $($s.heartbeat_at) ; PID=$($s.pid)"
        Write-Output "Research job: $($s.jobs.research.state) ; log=$($s.jobs.research.log_path)"
    }
    $status = Join-Path $dailyDir 'status.json'
    if (Test-Path -LiteralPath $status) {
        $d = Get-Content -LiteralPath $status -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Output "Taipei date=$today ; phase=$($d.phase) ; complete=$($d.complete)"
        $completed = if ($null -eq $d.completed) { 'not recorded yet' } else { $d.completed }
        $excluded = if ($null -eq $d.excluded) { 'not recorded yet' } else { @($d.excluded).Count }
        $pending = if ($null -eq $d.pending) { 'not recorded yet' } else { @($d.pending).Count }
        Write-Output "Requested=$($d.requested) ; completed=$completed ; excluded=$excluded ; pending=$pending"
        Write-Output "Progress: $status"
    } else { Write-Output "No daily progress file yet for $today." }
    Write-Output 'Task Running is process state; use daily complete/results for analysis completion.'
}
if ($CheckOnly) {
    foreach ($name in @($modelName, $schedulerName)) { $null = Get-ScheduledTask -TaskName $name -ErrorAction Stop }
    Write-Output "OK action=$Action root=$projectRoot daily=$dailyDir"
    exit 0
}
switch ($Action) {
    'start' {
        Start-ExistingTask $modelName
        Start-ExistingTask $schedulerName
        Show-Status
        Write-Output 'Background tasks continue after this launcher window closes.'
    }
    'status' { Show-Status }
    'progress' {
        $folder = if (Test-Path -LiteralPath $dailyDir) { $dailyDir } else { Join-Path $projectRoot 'data\daily_research' }
        if (-not (Test-Path -LiteralPath $folder)) { throw 'No daily research folder yet. Start the scheduler first.' }
        Start-Process explorer.exe -ArgumentList ('"' + $folder + '"')
    }
    'logs' { Start-Process explorer.exe -ArgumentList ('"' + (Join-Path $projectRoot 'log') + '"') }
    'retry' {
        Start-ExistingTask $modelName
        Push-Location -LiteralPath $projectRoot
        try {
            & $uvPath run --no-sync python -m bot.daily_research --root $projectRoot
            $code = $LASTEXITCODE
        } finally { Pop-Location }
        if ($code -eq 4) { Write-Output 'Another daily research run is active; no duplicate was started.' }
        elseif ($code -ne 0) { Write-Output "Partial/failed: exit=$code. Check daily status; unverifiable inputs stay excluded." }
        exit $code
    }
}
