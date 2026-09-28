$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONPATH = (Join-Path $root 'backend')
& (Join-Path $root '.venv\Scripts\python.exe') -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }