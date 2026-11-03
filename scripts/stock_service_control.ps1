param(
    [Parameter(Mandatory=$true)][string]$Root,
    [ValidateSet('inspect','start','stop')][string]$Action = 'inspect',
    [ValidateSet('scheduler','model')][string]$Service = 'scheduler',
    [int]$ExpectedPid = 0,
    [string]$ExpectedCreated = ''
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$Root = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
$names = @{scheduler='SkyMiku Stock Daily Scheduler';model='SkyMiku Codex LLM App Server'}
$expectedUser = (Get-ScheduledTask -TaskName $names.scheduler).Principal.UserId.Split('\')[-1]
if ($expectedUser -match '^S-1-') {
    $sid = New-Object System.Security.Principal.SecurityIdentifier($expectedUser)
    $expectedUser = $sid.Translate([System.Security.Principal.NTAccount]).Value.Split('\')[-1]
}
$modelBase = Join-Path $env:APPDATA 'npm\node_modules\@openai\codex'
$pythonPaths = @((Join-Path $Root '.venv\Scripts\python.exe'))
$venvConfig = Join-Path $Root '.venv\pyvenv.cfg'
if (Test-Path -LiteralPath $venvConfig) {
    foreach ($line in (Get-Content -LiteralPath $venvConfig)) {
        if ($line -match '^home\s*=\s*(.+)$') { $pythonPaths += (Join-Path $Matches[1] 'python.exe') }
    }
}
function Read-Json([string]$Path) {
    if (Test-Path -LiteralPath $Path) { try { return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json } catch {} }
    return $null
}
function Epoch($Date) { return ([DateTimeOffset]([datetime]$Date).ToUniversalTime()).ToUnixTimeSeconds() }
function Owns($Process, [string]$Kind) {
    if (-not $Process -or -not $Process.ExecutablePath) { return $false }
    $owner = Invoke-CimMethod -InputObject $Process -MethodName GetOwner
    if ($owner.ReturnValue -ne 0 -or $owner.User -ne $expectedUser) { return $false }
    if ($Kind -eq 'model') {
        return ($Process.Name -eq 'codex.exe' -and $Process.ExecutablePath.StartsWith($modelBase + '\', [StringComparison]::OrdinalIgnoreCase) -and
            $Process.CommandLine -match '\bapp-server\s+--listen\s+ws://127\.0\.0\.1:4500(?:\s|$)')
    }
    return ($pythonPaths -contains $Process.ExecutablePath -and $Process.CommandLine -match '(?:^|\s)-m\s+bot\.scheduler(?:\s|$)' -and
        $Process.CommandLine -match ('--root\s+"?' + [regex]::Escape($Root) + '"?(?:\s|$)'))
}
$processes = @(Get-CimInstance Win32_Process)
$schedulerState = Read-Json (Join-Path $Root 'data\scheduler_status.json')
$observations = @{}
foreach ($kind in @('model','scheduler')) {
    $candidates = @($processes | Where-Object {
        if ($kind -eq 'model') { $_.Name -eq 'codex.exe' -and $_.CommandLine -match 'app-server.*4500' }
        else { $_.Name -eq 'python.exe' -and $_.CommandLine -match '\bbot\.scheduler\b' -and $_.CommandLine -like ('*' + $Root + '*') }
    })
    $valid = @($candidates | Where-Object { Owns $_ $kind })
    # venv launchers and the real Python process share a command; count their connected tree once.
    $ids = @($valid | ForEach-Object { $_.ProcessId })
    $leaders = @($valid | Where-Object { $_.ParentProcessId -notin $ids })
    $selected = $null
    if ($leaders.Count -eq 1) {
        $selected = $leaders[0]
        if ($kind -eq 'scheduler' -and $schedulerState) {
            $actual = @($valid | Where-Object { $_.ProcessId -eq $schedulerState.pid })
            if ($actual.Count -eq 1) { $selected = $actual[0] }
        }
    }
    $status = if ($kind -eq 'scheduler') { $schedulerState } else { Read-Json (Join-Path $Root 'data\model_service_status.json') }
    $heartbeat = 0
    if ($status -and $selected -and $status.pid -eq $selected.ProcessId -and $status.heartbeat_at) { $heartbeat = Epoch $status.heartbeat_at }
    $obs = @{alive=($null -ne $selected);ambiguous=($leaders.Count -gt 1);identity_error=($candidates.Count -ne $valid.Count);
        heartbeat_epoch=$heartbeat;owned_job_active=$false;unconfirmed_job=$false}
    if ($selected) {
        $obs.pid = [int]$selected.ProcessId
        $obs.created_at = $selected.CreationDate.ToUniversalTime().ToString('o')
        $obs.created_epoch = Epoch $selected.CreationDate
        $obs.executable = $selected.ExecutablePath
        $obs.owner = $expectedUser
    }
    if ($kind -eq 'scheduler') {
        $children = @($processes | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '\bbot\.(daily_research|daily_collection)\b' -and $_.CommandLine -like ('*' + $Root + '*') })
        foreach ($child in $children) {
            $matched = $false
            if ($schedulerState -and $schedulerState.jobs) {
                foreach ($prop in $schedulerState.jobs.PSObject.Properties) {
                    $job = $prop.Value
                    if ($job.process_id -eq $child.ProcessId -and $job.process_created_at -and
                        [math]::Abs((Epoch $child.CreationDate) - (Epoch $job.process_created_at)) -le 1) {
                        $matched = $true
                        if ($job.deadline_at -and (Epoch $job.deadline_at) -gt [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) { $obs.owned_job_active = $true }
                    }
                }
            }
            # A venv wrapper's child is accounted for by its recorded leader.
            if (-not $matched -and $child.ParentProcessId -notin @($children.ProcessId)) { $obs.unconfirmed_job = $true }
        }
    }
    $observations[$kind] = $obs
}
if ($Action -eq 'inspect') { $observations | ConvertTo-Json -Depth 5 -Compress; exit 0 }
if ($Action -eq 'start') {
    if ($observations[$Service].identity_error -or $observations[$Service].ambiguous) { throw 'Ambiguous service identity; refusing start.' }
    if (-not $observations[$Service].alive) { Start-ScheduledTask -TaskName $names[$Service] }
    @{requested='start';service=$Service} | ConvertTo-Json -Compress
    exit 0
}
$current = $observations[$Service]
if (-not $current.alive -or $current.pid -ne $ExpectedPid -or $current.created_at -ne $ExpectedCreated -or $current.identity_error -or $current.ambiguous) {
    throw 'Process identity changed; refusing stop.'
}
if ($current.owned_job_active -or $current.unconfirmed_job) { throw 'An active or unverified research child prevents stop.' }
if ($Service -eq 'model') {
    $lockPath = Join-Path $Root 'data\.llm-call.lock'
    if (Test-Path -LiteralPath $lockPath) {
        try { $probe = [IO.File]::Open($lockPath, 'Open', 'ReadWrite', 'None'); $probe.Dispose() }
        catch { throw 'A model call is active; refusing stop.' }
    }
}
$tree = @($processes | Where-Object { $_.ProcessId -eq $ExpectedPid })
do {
    $more = @($processes | Where-Object { $_.ParentProcessId -in @($tree.ProcessId) -and $_.ProcessId -notin @($tree.ProcessId) })
    $tree += $more
} while ($more.Count -gt 0)
Stop-ScheduledTask -TaskName $names[$Service] -ErrorAction SilentlyContinue
foreach ($owned in ($tree | Sort-Object CreationDate -Descending)) {
    $live = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $owned.ProcessId)
    if ($live -and $live.CreationDate -eq $owned.CreationDate) { Stop-Process -Id $live.ProcessId -Force }
}
foreach ($owned in $tree) {
    $live = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $owned.ProcessId)
    if ($live -and $live.CreationDate -eq $owned.CreationDate) { throw 'Process termination not confirmed.' }
}
@{stopped=$ExpectedPid;service=$Service} | ConvertTo-Json -Compress
