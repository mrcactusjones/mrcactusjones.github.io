# One day's work: spend the credit budget, re-rank, snapshot.
# Logging and log-file naming are handled by run.py --log, so this stays trivial.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 run.py daily --provider ppt --log
} else {
    python run.py daily --provider ppt --log
}
exit $LASTEXITCODE
