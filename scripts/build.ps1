[CmdletBinding()]
param([ValidateSet('dist', 'dist-0.2')][string]$OutputDirectory = 'dist')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run start.cmd -Setup first.' }
& $pythonPath scripts\build_portable.py --output $OutputDirectory
if ($LASTEXITCODE -ne 0) { throw 'Portable build failed.' }
Write-Host ('Portable folder: ' + $OutputDirectory + '\YuliaoWallpaper. Keep the entire folder together.')
