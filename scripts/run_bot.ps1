# Keep the context-graph Slack bot running; auto-restart if it crashes.
# Tokens are read from the project's .env (the bot loads it automatically).
$ErrorActionPreference = "Continue"

# project root = parent of this scripts/ folder
$root = Split-Path -Parent $PSScriptRoot
Set-Location -Path $root

while ($true) {
    Write-Host "[$(Get-Date -Format o)] starting context-graph bot..."
    python -m context_graph.slackbot
    $code = $LASTEXITCODE
    Write-Host "[$(Get-Date -Format o)] bot exited (code $code). Restarting in 5s..."
    Start-Sleep -Seconds 5
}
