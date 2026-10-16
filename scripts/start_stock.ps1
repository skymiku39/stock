param(
    [ValidateSet('dashboard', 'replay')][string]$Action = 'dashboard',
    [switch]$CheckOnly,
    [switch]$Headless
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvPath = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
if (-not (Test-Path -LiteralPath $uvPath)) { throw "uv is missing: $uvPath" }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.venv\Scripts\python.exe'))) {
    throw "Project environment missing. Run uv sync in $projectRoot first."
}
$port = 8501
$baseline = Join-Path $projectRoot 'data\reports\smile_execution_20261016'
if ($Action -eq 'replay') {
    foreach ($name in @('inputs.json','comparison.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $baseline $name))) { throw "Missing baseline: $name" }
    }
}
if ($CheckOnly) {
    Write-Output "OK action=$Action root=$projectRoot uv=$uvPath port=$(if ($Action -eq 'dashboard') {$port} else {'none'})"
    exit 0
}
Push-Location -LiteralPath $projectRoot
try {
    if ($Action -eq 'dashboard') {
        $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($busy) { throw "Port $port is occupied. No process was stopped. Check the existing server first." }
        Write-Output "Open http://127.0.0.1:$port ; Ctrl+C stops this server."
        $headlessValue = if ($Headless) { 'true' } else { 'false' }
        & $uvPath run --no-sync stock-dashboard --root $projectRoot --server.address 127.0.0.1 --server.port $port --server.headless $headlessValue
    } else {
        $stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
        $output = Join-Path $projectRoot "data\reports\smile_friction_$stamp"
        & $uvPath run --no-sync python scripts/replay_smile_friction.py --root $projectRoot --baseline $baseline --output $output
        if ($LASTEXITCODE -eq 0) { Write-Output "Saved report: $output\comparison.json" }
    }
    $resultCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $resultCode
