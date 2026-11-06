param(
    [string]$Root = 'C:\skymiku\stock',
    [Parameter(Mandatory=$true)][string]$Output,
    [Parameter(Mandatory=$true)][string]$Settings,
    [Parameter(Mandatory=$true)][string]$ParentManifest,
    [string]$SeedStream = ''
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $Root
$env:PYTHONIOENCODING = 'utf-8'
$Parents = @(Get-Content -LiteralPath $ParentManifest -Raw -Encoding UTF8 | ConvertFrom-Json)
$arguments = @('run','--offline','--no-sync','python','-m','bot.paper_follow',
    '--root',$Root,'--output',$Output,'--settings',$Settings,'--parents') + $Parents
if ($SeedStream) { $arguments += @('--seed-stream',$SeedStream) }
New-Item -ItemType Directory -Path $Output -Force | Out-Null
while ($true) {
    # Windows PowerShell represents native stderr as ErrorRecord objects. A
    # terminated child must reach the retry loop instead of killing this host.
    try {
        $ErrorActionPreference = 'Continue'
        & uv @arguments >> (Join-Path $Output 'process.log') 2>&1
        $followExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = 'Stop'
    }
    if ($followExit -eq 73) { break }
    $statusPath = Join-Path $Output 'status.json'
    if (Test-Path -LiteralPath $statusPath) {
        $status = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($status.lifecycle_complete -eq $true) { break }
    }
    # Recovery always rebuilds from raw observations; never manufactures fills.
    Start-Sleep -Seconds 30
}
