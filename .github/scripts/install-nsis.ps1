# install-nsis.ps1
# Downloads and installs a portable copy of NSIS 3.x for the build
# workflow on Windows. Tries Chocolatey first, then a portable zip
# download, then a GitHub release mirror. Retries on transient
# network errors.
#
# Why this lives in its own file: it's a PowerShell script, so it
# doesn't go through YAML's literal block scalar parsing. Pasting
# it into the web editor as a single `pwsh <path>` call is robust.
#
# Output: the LAST line of stdout is the absolute path of the
# makensis.exe that the calling batch script should use. The
# script also sets a `MAKENSIS_PATH` environment variable in
# the current process so a subsequent cmd.exe can read it via
# %MAKENSIS_PATH% without parsing stdout.
$ErrorActionPreference = 'Stop'

# `3.10` is the source-archive version we use for portable downloads
# (the SourceForge zip is named `nsis-3.10.zip` with no patch). For
# Chocolatey we need the full `3.10.0` - the `nsis` package on
# chocolatey.org only publishes a three-part version.
$nsisVersion      = '3.10'
$nsisChocoVersion = '3.10.0'

# Helper: set MAKENSIS_PATH and print the path on stdout (the
# last line of stdout is what the caller captures).
function Set-MakensisPath($Path) {
    [Environment]::SetEnvironmentVariable('MAKENSIS_PATH', $Path, 'Process')
    if ($env:GITHUB_ENV) {
        "MAKENSIS_PATH=$Path" | Out-File -FilePath $env:GITHUB_ENV -Append -Encoding utf8
    }
    $dir = Split-Path -Parent $Path
    $env:PATH = "$dir;$env:PATH"
    Write-Output $Path
}

# Look for makensis in the well-known install locations.
function Find-Makensis {
    $pf86 = [System.Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
    if (-not $pf86) { $pf86 = 'C:\Program Files (x86)' }
    $pf = [System.Environment]::GetEnvironmentVariable('ProgramFiles')
    $candidates = @(
        "C:\nsis-$nsisVersion\makensis.exe",
        "C:\nsis-3.09\makensis.exe",
        "C:\nsis-3.08\makensis.exe",
        "$pf86\NSIS\makensis.exe",
        "$pf\NSIS\makensis.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

# If makensis is already installed, just emit its path and exit.
$existing = Find-Makensis
if ($existing) {
    Write-Host "NSIS already installed at $existing"
    Set-MakensisPath $existing
    return
}

# Try Chocolatey first (preinstalled on GitHub-hosted Windows runners).
# Use the full version (`3.10.0`) - the public chocolatey.org package
# for `nsis` only has the three-part version.
if (Get-Command choco -ErrorAction SilentlyContinue) {
    Write-Host "Installing NSIS via Chocolatey (version $nsisChocoVersion)"
    choco install nsis --version=$nsisChocoVersion -y --no-progress 2>&1 |
        ForEach-Object { Write-Host "  choco: $_" }
    $existing = Find-Makensis
    if ($existing) {
        Write-Host "NSIS installed via Chocolatey at $existing"
        Set-MakensisPath $existing
        return
    }
    Write-Host "Chocolatey install did not produce a working makensis; falling back to portable download."
}

# Try portable downloads in order. Each download is retried up to
# three times; if all downloads fail, the script throws.
function Install-NsisPortable($url, $label) {
    Write-Host "Downloading NSIS $nsisVersion from $url ($label)"
    $downloadDir = "$env:TEMP\nsis-install"
    $zipPath     = "$downloadDir\nsis.zip"
    if (-not (Test-Path $downloadDir)) {
        New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    }
    $maxAttempts = 3
    for ($i = 1; $i -le $maxAttempts; $i++) {
        try {
            Write-Host "  attempt $i of $maxAttempts"
            Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing -TimeoutSec 120
            if (Test-Path $zipPath) {
                $size = (Get-Item $zipPath).Length
                if ($size -gt 100000) {
                    Write-Host "  downloaded $size bytes"
                    break
                }
                Write-Host "  file too small ($size bytes); retrying"
                Remove-Item -Force $zipPath -ErrorAction SilentlyContinue
            }
        } catch {
            Write-Host "  attempt $i failed: $($_.Exception.Message)"
        }
        if ($i -eq $maxAttempts) {
            throw "All $maxAttempts download attempts failed for $url"
        }
        Start-Sleep -Seconds 5
    }
    $installDir = "C:\nsis-$nsisVersion"
    if (Test-Path $installDir) {
        Remove-Item -Recurse -Force $installDir
    }
    Expand-Archive -Path $zipPath -DestinationPath 'C:\' -Force
    if (-not (Test-Path "$installDir\makensis.exe")) {
        throw "Extracted zip but $installDir\makensis.exe not found"
    }
    Write-Host "NSIS installed at: $installDir"
    Set-MakensisPath "$installDir\makensis.exe"
}

$urls = @(
    "https://sourceforge.net/projects/nsis/files/NSIS%203/${nsisVersion}/nsis-${nsisVersion}.zip/download",
    "https://github.com/lordmulder/nsis/releases/download/v${nsisVersion}/nsis-${nsisVersion}.zip"
)
$labels = @("SourceForge", "GitHub mirror")

for ($i = 0; $i -lt $urls.Length; $i++) {
    try {
        Install-NsisPortable $urls[$i] $labels[$i]
        return
    } catch {
        Write-Host "Failed via $($labels[$i]): $($_.Exception.Message)"
    }
}

throw "All install attempts failed. Please install NSIS manually."
