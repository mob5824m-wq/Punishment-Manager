# install-windows-deps.ps1
# Ensures a full Python 3 (with the dev files PyInstaller needs) is
# available on a GitHub-hosted Windows runner.
#
# Why this lives in its own file: actions/setup-python installs a
# slim Python that lacks python3.lib. PyInstaller's bootloader then
# fails with "Python library not found" or "failed to load dynlib"
# during the build. The fix is to install the full Python from
# the official python.org MSI, which includes the .lib files.
#
# Strategy:
#   1. If the active Python has python3.lib next to python3.dll,
#      we're done - no install needed.
#   2. Otherwise, install the official Python 3.11 from python.org
#      into C:\Python311 with the dev files included.
$ErrorActionPreference = 'Stop'

function Test-PythonHasLibs {
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $python) { return $false }
    $dir = Split-Path -Parent $python
    $parent = Split-Path -Parent $dir
    $dll = Join-Path $dir 'python3.dll'
    $lib = Join-Path $parent 'libs\python3.lib'
    return ((Test-Path $dll) -and (Test-Path $lib))
}

if (Test-PythonHasLibs) {
    Write-Host "Active Python has python3.dll and python3.lib - no install needed."
    exit 0
}

Write-Host "Active Python is missing python3.lib. Installing full Python 3.11..."

$msiUrl  = 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe'
$tmpDir  = "$env:TEMP\python-install"
$msiPath = "$tmpDir\python-installer.exe"

if (-not (Test-Path $tmpDir)) {
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
}

# Download the official installer.
$maxAttempts = 3
for ($i = 1; $i -le $maxAttempts; $i++) {
    try {
        Invoke-WebRequest -Uri $msiUrl -OutFile $msiPath -UseBasicParsing -TimeoutSec 60
        break
    } catch {
        Write-Host "Download attempt $i failed: $($_.Exception.Message)"
        if ($i -eq $maxAttempts) {
            # Try Chocolatey as a fallback.
            if (Get-Command choco -ErrorAction SilentlyContinue) {
                Write-Host "Falling back to Chocolatey"
                choco install python3 --version=3.11.9 -y --no-progress 2>&1 | Out-Null
                if (Test-PythonHasLibs) {
                    Write-Host "Chocolatey Python has the dev files - good."
                    exit 0
                }
            }
            throw "All install attempts failed: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 5
    }
}

# Run the installer. The /quiet flag suppresses the GUI; the
# Include_lib=1 and Include_dev=1 flags are the important ones.
Write-Host "Running official Python installer (this takes ~1 minute)"
$proc = Start-Process -FilePath $msiPath -ArgumentList @(
    '/quiet', 'InstallAllUsers=1', 'PrependPath=1',
    'TargetDir=C:\Python311',
    'Include_test=0', 'Include_doc=0', 'Include_launcher=0',
    'Include_dev=1', 'Include_lib=1', 'Include_pip=1',
    'Include_tcltk=0', 'Include_doc=0', 'Include_launcher=0',
    'Include_benchmarks=0'
) -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    throw "Python installer failed with exit code $($proc.ExitCode)"
}

# Add the new Python to PATH for subsequent steps.
$env:PATH = 'C:\Python311;C:\Python311\Scripts;' + $env:PATH

if (-not (Test-PythonHasLibs)) {
    throw "Python install completed but python3.lib still missing."
}
Write-Host "Full Python 3.11 installed at C:\Python311 with dev files."
python --version
