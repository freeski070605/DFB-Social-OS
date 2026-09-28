$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Run .\scripts\setup.ps1 first.' }
if (-not (Test-Path 'frontend\dist\index.html')) {
    Push-Location (Join-Path $root 'frontend')
    npm run build
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw 'Frontend build failed.' }
    Pop-Location
}
$env:PYTHONPATH = (Join-Path $root 'backend')
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000