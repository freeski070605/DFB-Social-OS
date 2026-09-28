param([Parameter(Mandatory = $true)][string]$Archive)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$path = (Resolve-Path $Archive).Path
if ([System.IO.Path]::GetDirectoryName($path) -ne [System.IO.Path]::GetFullPath((Join-Path $root 'data\backups')).TrimEnd('\')) {
    throw 'Restore only accepts an archive under data\backups. Copy the archive there first.'
}
$confirmation = Read-Host "Restore $path? Stop the server first. A safety backup will be made. Type RESTORE"
if ($confirmation -cne 'RESTORE') { throw 'Restore cancelled.' }
$env:PYTHONPATH = (Join-Path $root 'backend')
& (Join-Path $root '.venv\Scripts\python.exe') -m app.cli restore $path
if ($LASTEXITCODE -ne 0) { throw 'Restore failed. Confirm the server is stopped and inspect the error.' }