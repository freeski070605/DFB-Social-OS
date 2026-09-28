$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONPATH = (Join-Path $root 'backend')
& (Join-Path $root '.venv\Scripts\python.exe') -m app.cli backup
if ($LASTEXITCODE -ne 0) { throw 'Backup failed.' }