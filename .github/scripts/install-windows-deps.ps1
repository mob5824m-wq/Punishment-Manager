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
# Layout assumption: the official Python installer puts python.exe,
# python3.dll, and libs/python3.lib all in the SAME directory
# (e.g. C:\Python311\). So the check looks for libs/python3.lib
# relative to the python.exe directory.
#
# Output: the LAST line of stdout is the install path of the
# Python that should be used. The calling batch script reads
# this to update PATH.
$ErrorActionPreference = 'Stop'

function Test-PythonHasLibs {
    param([string]$PythonExe)
    if (-not $PythonExe) { return $false }
    $dir = Split-Path -Parent $PythonExe
    $dll = Join-Path $dir 'python3.dll'
    $lib = Join-Path $dir 'libs\python3.lib'
    return ((Test-Path $dll) -and (Test-Path $lib))
}

function Find-InstalledPython {
    $candidates = @(
        'C:\Python311',
        "$env:ProgramFiles\Python311",
        "${env:ProgramFiles(x86)}\Python311",
        "$env:LOCALAPPDATA\Programs\Python\Python311",
        "$env:LOCALAPPDATA\Programs\Python\Python311-32"
    )
    foreach ($c in $candidates) {
        if (Test-Path "$c\python.exe") {
            return $c
        }
    }
    return $null
}

# Check the active Python first.
$activePython = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if ($activePython -and (Test-PythonHasLibs $activePython)) {
    Write-Host "Active Python at $activePython has dev files - no install needed."
    Write-Output (Split-Path -Parent $activePython)
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
$downloaded = $false
for ($i = 1; $i -le $maxAttempts; $i++) {
    try {
        Write-Host "Downloading from $msiUrl (attempt $i)"
        Invoke-WebRequest -Uri $msiUrl -OutFile $msiPath -UseBasicParsing -TimeoutSec 60
        $downloaded = $true
        break
    } catch {
        Write-Host "Download attempt $i failed: $($_.Exception.Message)"
        if ($i -eq $maxAttempts) { break }
        Start-Sleep -Seconds 5
    }
}

if (-not $downloaded) {
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        Write-Host "Falling back to Chocolatey"
        choco install python3 --version=3.11.9 -y --no-progress 2>&1 | Out-Null
        $found = Find-InstalledPython
        if ($found -and (Test-PythonHasLibs (Join-Path $found 'python.exe'))) {
            $env:PATH = "$found;$found\Scripts;" + $env:PATH
            Write-Host "Chocolatey Python at $found has dev files - good."
            Write-Output $found
            exit 0
        }
    }
    throw "All install attempts failed."
}

# Run the installer. InstallAllUsers=0 forces the per-user install
# path which honors TargetDir. With InstallAllUsers=1 the MSI
# ignores TargetDir and uses %ProgramFiles%.
Write-Host "Running official Python installer (this takes ~1 minute)"
$proc = Start-Process -FilePath $msiPath -ArgumentList @(
    '/quiet', 'InstallAllUsers=0', 'PrependPath=0',
    'TargetDir=C:\Python311',
    'Include_test=0', 'Include_doc=0', 'Include_launcher=0',
    'Include_dev=1', 'Include_lib=1', 'Include_pip=1',
    'Include_tcltk=0', 'Include_benchmarks=0'
) -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    throw "Python installer failed with exit code $($proc.ExitCode)"
}

# Find where it actually installed.
$installPath = Find-InstalledPython
if (-not $installPath) {
    $python = Get-ChildItem -Path 'C:\', "$env:ProgramFiles" -Filter 'python.exe' -Recurse -ErrorAction SilentlyContinue -Depth 5 | Select-Object -First 1
    if ($python) { $installPath = Split-Path -Parent $python.FullName }
}
if (-not $installPath) {
    throw "Python installer completed but couldn't find the install directory."
}
Write-Host "Python installed at: $installPath"

# Add the new Python to PATH for subsequent steps in this session.
$env:PATH = "$installPath;$installPath\Scripts;" + $env:PATH
[Environment]::SetEnvironmentVariable('PATH', $env:PATH, 'Process')

# Verify the new Python has the dev files.
$newPython = Join-Path $installPath 'python.exe'
if (-not (Test-PythonHasLibs $newPython)) {
    throw "Python install completed but python3.lib still missing in $installPath."
}
Write-Host "Full Python 3.11 installed at $installPath with dev files."
& $newPython --version

# LAST LINE: emit the install path for the calling batch script.
Write-Output $installPath
