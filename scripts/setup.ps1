$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Python 3.12+ is required. Install it from python.org and enable the py launcher.'
}
$pythonVersion = & py -3 -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0 -or [Version]$pythonVersion -lt [Version]'3.12') {
    throw 'Python 3.12 or newer was not found. Install it from python.org and enable the py launcher.'
}

if (-not (Test-Path '.venv\Scripts\python.exe')) { py -3 -m venv .venv }
if (-not (Test-Path '.env')) { Copy-Item .env.example .env }

$python = Join-Path $root '.venv\Scripts\python.exe'
& $python -m pip install --upgrade pip
& $python -m pip install -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }

$envPath = Join-Path $root '.env'
$envText = [System.IO.File]::ReadAllText($envPath)
if ($envText -match '(?m)^DFB_ENCRYPTION_KEY=\s*$') {
    $key = & $python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
    $envText = [regex]::Replace($envText, '(?m)^DFB_ENCRYPTION_KEY=.*$', "DFB_ENCRYPTION_KEY=$key")
    [System.IO.File]::WriteAllText($envPath, $envText, [System.Text.UTF8Encoding]::new($false))
}

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw 'Node.js LTS with npm is required.' }
Push-Location (Join-Path $root 'frontend')
npm install
if ($LASTEXITCODE -ne 0) { Pop-Location; throw 'Frontend dependency installation failed.' }
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw 'Frontend production build failed.' }
Pop-Location

New-Item -ItemType Directory -Force -Path 'data', 'logs' | Out-Null
$env:PYTHONPATH = (Join-Path $root 'backend')
& $python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
& $python -m app.cli seed
if ($LASTEXITCODE -ne 0) { throw 'Brand seed failed.' }

Write-Host 'Setup complete. Create the first administrator, then start with .\scripts\run.ps1.'