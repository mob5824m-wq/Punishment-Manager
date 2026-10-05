# install-windows-deps.ps1
# Ensures a full Python 3 (with the dev files PyInstaller needs) is
# available on a GitHub-hosted Windows runner, for the architecture the
# build targets.
#
# Why this lives in its own file: actions/setup-python installs a
# slim Python that lacks python3.lib. PyInstaller's bootloader then
# fails with "Python library not found" or "failed to load dynlib"
# during the build. The fix is to install the full Python from
# the official python.org installer, which includes the .lib files.
#
# Architecture matters as much as the dev files: on Windows on ARM, an x64
# Python runs happily under emulation and PyInstaller would then produce an
# x64 binary while the build is labelled arm64. So the installer URL, the
# install directory and the checks below are all architecture-specific - the
# arm64 build needs the native ARM64 interpreter.
#
# Usage:
#   pwsh -File install-windows-deps.ps1 [amd64|arm64]
#
# The argument defaults to the machine's architecture. Output: the LAST line
# of stdout is the install path of the Python that should be used. The
# calling batch script reads this to update PATH.
param(
    [Parameter(Position = 0)]
    [string]$Arch = ''
)
$ErrorActionPreference = 'Stop'

$PythonVersion = '3.11.9'

function Get-HostArch {
    # OSArchitecture describes the machine; PROCESSOR_ARCHITECTURE the current
    # process. Either is enough here: we only need a default.
    $arch = ''
    try {
        $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    } catch {
        $arch = $env:PROCESSOR_ARCHITECTURE
    }
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    switch -Regex ($arch) {
        '^(?i)arm64$' { return 'arm64' }
        '^(?i)(amd64|x64|x86_64)$' { return 'amd64' }
        default { return '' }
    }
}

if (-not $Arch) {
    $Arch = Get-HostArch
}
$Arch = $Arch.ToLower()
if ($Arch -ne 'amd64' -and $Arch -ne 'arm64') {
    throw "Unknown architecture '$Arch'; expected amd64 or arm64."
}

function Get-PythonArch {
    param([string]$PythonExe)
    if (-not $PythonExe -or -not (Test-Path $PythonExe)) { return '' }
    # The interpreter's own architecture: an emulated x64 Python reports
    # AMD64 even on an ARM64 machine, which is exactly what we must catch.
    $reported = & $PythonExe -c "import platform; print(platform.machine())" 2>$null
    switch -Regex ("$reported".Trim()) {
        '^(?i)(AMD64|x86_64|x64)$' { return 'amd64' }
        '^(?i)(ARM64|aarch64)$' { return 'arm64' }
        default { return '' }
    }
}

function Test-PythonHasLibs {
    param([string]$PythonExe)
    if (-not $PythonExe) { return $false }
    $dir = Split-Path -Parent $PythonExe
    $dll = Join-Path $dir 'python3.dll'
    $lib = Join-Path $dir 'libs\python3.lib'
    return ((Test-Path $dll) -and (Test-Path $lib))
}

function Find-InstalledPython {
    param([string]$ExpectedArch)
    $candidates = @(
        'C:\Python311',
        'C:\Python311-arm64',
        "$env:ProgramFiles\Python311",
        "${env:ProgramFiles(x86)}\Python311",
        "$env:LOCALAPPDATA\Programs\Python\Python311",
        "$env:LOCALAPPDATA\Programs\Python\Python311-32"
    )
    # Prefer a Python of the requested architecture over one that merely has
    # the dev files, so an emulated x64 install is never picked for arm64.
    $fallback = $null
    foreach ($c in $candidates) {
        $exe = Join-Path $c 'python.exe'
        if (-not (Test-Path $exe)) { continue }
        if ((Get-PythonArch $exe) -eq $ExpectedArch) { return $c }
        if (-not $fallback -and (Test-PythonHasLibs $exe)) { $fallback = $c }
    }
    return $fallback
}

# Check the active Python first.
$activePython = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if ($activePython -and (Test-PythonHasLibs $activePython) -and
    ((Get-PythonArch $activePython) -eq $Arch)) {
    Write-Host "Active Python at $activePython has dev files and is $Arch - no install needed."
    Write-Output (Split-Path -Parent $activePython)
    exit 0
}

if ($activePython) {
    Write-Host "Active Python is unusable for a $Arch build (dev files: $(Test-PythonHasLibs $activePython), arch: $(Get-PythonArch $activePython)); installing full Python $PythonVersion $Arch..."
} else {
    Write-Host "No Python on PATH; installing full Python $PythonVersion $Arch..."
}

$installerArch = if ($Arch -eq 'arm64') { 'arm64' } else { 'amd64' }
$msiUrl  = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-$installerArch.exe"
$targetDir = if ($Arch -eq 'arm64') { 'C:\Python311-arm64' } else { 'C:\Python311' }
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
        # Chocolatey's python3 package is a wrapper around the python.org
        # installer; on arm64 it resolves the ARM64 build.
        choco install python3 --version=$PythonVersion -y --no-progress 2>&1 | Out-Null
        $found = Find-InstalledPython $Arch
        if ($found -and (Test-PythonHasLibs (Join-Path $found 'python.exe')) -and
            ((Get-PythonArch (Join-Path $found 'python.exe')) -eq $Arch)) {
            $env:PATH = "$found;$found\Scripts;" + $env:PATH
            Write-Host "Chocolatey Python at $found has dev files and is $Arch - good."
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
    "TargetDir=$targetDir",
    'Include_test=0', 'Include_doc=0', 'Include_launcher=0',
    'Include_dev=1', 'Include_lib=1', 'Include_pip=1',
    'Include_tcltk=0', 'Include_benchmarks=0'
) -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    throw "Python installer failed with exit code $($proc.ExitCode)"
}

# Find where it actually installed.
$installPath = Find-InstalledPython $Arch
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

# Verify the new Python has the dev files *and* the right architecture.
$newPython = Join-Path $installPath 'python.exe'
if (-not (Test-PythonHasLibs $newPython)) {
    throw "Python install completed but python3.lib still missing in $installPath."
}
$newArch = Get-PythonArch $newPython
if ($newArch -ne $Arch) {
    throw "Python install completed but $newPython is $newArch, not $Arch."
}
Write-Host "Full Python $PythonVersion ($Arch) installed at $installPath with dev files."
& $newPython --version

# LAST LINE: emit the install path for the calling batch script.
Write-Output $installPath
