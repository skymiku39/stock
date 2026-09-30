$ErrorActionPreference = 'Stop'

# Keep in sync with CODEX_APP_SERVER_URL in the project .env.
$listenUrl = 'ws://127.0.0.1:4500'
$codexCommand = Join-Path $env:APPDATA 'npm\codex.cmd'
if (-not (Test-Path -LiteralPath $codexCommand)) {
    throw "Codex CLI command shim was not found at $codexCommand"
}

Set-Location -LiteralPath 'C:\skymiku\stock'
& $codexCommand app-server --listen $listenUrl
exit $LASTEXITCODE
