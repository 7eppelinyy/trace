param([string]$DbPath="data/trace.db", [string]$BackupDir="data/backups", [int]$Keep=7)
$ErrorActionPreference = "Stop"
& python (Join-Path $PSScriptRoot "db_admin.py") backup --source $DbPath --directory $BackupDir --keep $Keep
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
