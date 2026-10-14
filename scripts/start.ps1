[CmdletBinding()]
param(
    [switch]$Setup,
    [switch]$Check,
    [switch]$SmokeUi,
    [switch]$Console
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pythonWindowPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$requirementsPath = Join-Path $projectRoot 'requirements.txt'
$portablePath = Join-Path $projectRoot 'dist\YuliaoWallpaper\YuliaoWallpaper.exe'

function Invoke-PortableCheck {
    param([string[]]$CheckArguments, [string]$SuccessMessage)
    $checkProcess = Start-Process -FilePath $portablePath -ArgumentList $CheckArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
    if (-not $checkProcess.WaitForExit(30000)) {
        $checkProcess.Kill()
        throw 'The portable check did not finish within 30 seconds. See README.md for troubleshooting.'
    }
    if ($checkProcess.ExitCode -ne 0) {
        throw ('The portable check failed with exit code ' + $checkProcess.ExitCode + '. No wallpaper was applied by this check.')
    }
    Write-Host $SuccessMessage
}

function Find-BasePython {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3.11 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Executable = $launcher.Source; Prefix = @('-3.11') }
        }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python -and ($python.Source -notmatch '\\WindowsApps\\')) {
        & $python.Source -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info < (3,15) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Executable = $python.Source; Prefix = @() }
        }
    }
    throw 'Python 3.11-3.14 was not found. Install full CPython from https://www.python.org/downloads/windows/ (include Tcl/Tk), then run start.cmd -Setup.'
}

try {
    Set-Location -LiteralPath $projectRoot
    if (-not $Setup -and (Test-Path -LiteralPath $portablePath -PathType Leaf) -and (-not $Console -or -not (Test-Path -LiteralPath $pythonPath -PathType Leaf))) {
        Invoke-PortableCheck -CheckArguments @('--self-check') -SuccessMessage 'Portable environment check passed. No wallpaper was changed.'
        if ($Check) { exit 0 }
        if ($SmokeUi) {
            Invoke-PortableCheck -CheckArguments @('--smoke-ui', '--data-dir', ('"' + $projectRoot + '"')) -SuccessMessage 'Portable window smoke check passed. No wallpaper was changed.'
            exit 0
        }
        if ($Console) {
            Write-Host 'This portable GUI has no Python console. Private runtime logs are in %LOCALAPPDATA%\YuliaoWallpaper\logs. Use -Setup to prepare source diagnostics.'
        }
        $portableProcess = Start-Process -FilePath $portablePath -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
        if ($portableProcess.WaitForExit(1500) -and $portableProcess.ExitCode -ne 0) {
            throw ('The portable app exited during startup with code ' + $portableProcess.ExitCode + '. Read README.md and the private runtime logs.')
        }
        Write-Host 'Yuliao Wallpaper portable was launched. Keep the whole dist\YuliaoWallpaper folder together.'
        exit 0
    }
    if ($Setup) {
        if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
            $basePython = Find-BasePython
            Write-Host 'Creating a private Python environment in .venv ...'
            $venvArguments = @($basePython.Prefix) + @('-m', 'venv', (Join-Path $projectRoot '.venv'))
            & $basePython.Executable @venvArguments
            if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv. See the Python error above.' }
        }
        Write-Host 'Installing pinned runtime packages from official PyPI (HTTPS) ...'
        & $pythonPath -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: --no-deps --disable-pip-version-check -r $requirementsPath
        if ($LASTEXITCODE -ne 0) { throw 'Package installation failed. Check your connection to pypi.org and files.pythonhosted.org, then run start.cmd -Setup again.' }
    }

    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw 'The project environment is missing. Open a terminal in this folder and run: .\start.cmd -Setup. See README.md for first-time setup.'
    }

    & $pythonPath -m yuliao_wallpaper --self-check
    if ($LASTEXITCODE -ne 0) { throw 'Startup checks failed. See the error above. Repair packages with: .\start.cmd -Setup' }
    if ($Check) { exit 0 }

    if ($SmokeUi) {
        & $pythonPath -m yuliao_wallpaper --smoke-ui --data-dir $projectRoot
        if ($LASTEXITCODE -ne 0) { throw 'The window smoke check failed. See the error above.' }
        exit 0
    }

    if ($Console) {
        & $pythonPath -m yuliao_wallpaper
        exit $LASTEXITCODE
    }

    if (-not (Test-Path -LiteralPath $pythonWindowPath -PathType Leaf)) {
        throw 'pythonw.exe is missing. Use .\start.cmd -Console to see diagnostics, or recreate the project environment.'
    }
    $logRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'YuliaoWallpaper\logs'
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $launchStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stderrPath = Join-Path $logRoot ('startup-' + $launchStamp + '.stderr.log')
    $stdoutPath = Join-Path $logRoot ('startup-' + $launchStamp + '.stdout.log')
    $appProcess = Start-Process -FilePath $pythonWindowPath -ArgumentList @('-m', 'yuliao_wallpaper') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardError $stderrPath -RedirectStandardOutput $stdoutPath -PassThru
    if ($appProcess.WaitForExit(1500) -and $appProcess.ExitCode -ne 0) {
        throw ('The app exited during startup. Read this private log: ' + $stderrPath + '. Or run .\start.cmd -Console.')
    }
    Write-Host 'Yuliao Wallpaper was launched. Use the window or tray icon; opening the app does not apply a wallpaper.'
    exit 0
}
catch {
    Write-Host ''
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
