param([Parameter(Mandatory=$true)][string]$BackupFile, [Parameter(Mandatory=$true)][string]$TargetDb)
$ErrorActionPreference = "Stop"
& python (Join-Path $PSScriptRoot "db_admin.py") restore --source $BackupFile --target $TargetDb
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
