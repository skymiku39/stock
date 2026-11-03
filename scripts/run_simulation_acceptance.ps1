param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvPath = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
if (-not (Test-Path -LiteralPath $uvPath)) { throw "uv is missing: $uvPath" }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.venv\Scripts\python.exe'))) {
    throw 'Project environment is missing'
}
if ($CheckOnly) {
    Write-Output "OK root=$projectRoot backend=shioaji simulation=true symbol=0050 lots=1 timeout=60"
    exit 0
}
$output = Join-Path $projectRoot ('work\simulation-broker-opening-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $output -Force | Out-Null
$taipeiNow = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow, 'Taipei Standard Time')
if ($taipeiNow.DayOfWeek -in @('Saturday','Sunday') -or
    $taipeiNow.TimeOfDay -lt [TimeSpan]::FromHours(9) -or
    $taipeiNow.TimeOfDay -gt [TimeSpan]::FromMinutes(795)) {
    @{ result='blocked'; simulation=$true; production_orders=0; reason='Outside bounded Taiwan trading session' } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'result.json') -Encoding UTF8
    exit 2
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
# The Python entry point has no production option and constructs a fenced
# simulation-only broker. Do not start the unbounded stock-bot monitor here.
$arguments = @('--cache-dir', ('"' + (Join-Path $projectRoot 'work\.uv-cache') + '"'),
               'run', '--offline', '--no-sync', 'python', '-m', 'bot.simulation_check',
               '--backend', 'shioaji', '--timeout', '60', '--root', ('"' + $projectRoot + '"'),
               '--output', ('"' + $output + '"'))
$process = Start-Process -FilePath $uvPath -ArgumentList $arguments -WorkingDirectory $projectRoot `
    -WindowStyle Hidden -Wait -PassThru `
    -RedirectStandardOutput (Join-Path $output 'stdout.log') `
    -RedirectStandardError (Join-Path $output 'stderr.log')
exit $process.ExitCode
