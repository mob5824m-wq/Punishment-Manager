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

$nsisVersion = '3.10'

function Set-MakensisPath {
    param([string]$Path)
    # Set the env var for any subsequent child process (like cmd.exe).
    [Environment]::SetEnvironmentVariable('MAKENSIS_PATH', $Path, 'Process')
    # Also export it via $GITHUB_ENV so GitHub Actions propagates
    # it to subsequent workflow steps.
    if ($env:GITHUB_ENV) {
        "MAKENSIS_PATH=$Path" | Out-File -FilePath $env:GITHUB_ENV -Append -Encoding utf8
    }
    # Also update the current process's PATH so 'makensis' works
    # from this PowerShell session if anyone uses it.
    $dir = Split-Path -Parent $Path
    $env:PATH = "$dir;$env:PATH"
    # Print as the LAST line of stdout so a calling batch script
    # can capture it.
    Write-Output $Path
}

function Test-MakensisExists {
    # Look for makensis in common install locations.
    $candidates = @(
        "C:\nsis-$nsisVersion\makensis.exe",
        "C:\nsis-3.09\makensis.exe",
        "C:\nsis-3.08\makensis.exe",
        "${env:ProgramFiles(x86)}\NSIS\makensis.exe",
        "${env:ProgramFiles}\NSIS\makensis.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

# If makensis is already installed, just emit its path and exit.
$existing = Test-MakensisExists
if ($existing) {
    Write-Host "NSIS already installed at $existing"
    Set-MakensisPath $existing
    return
}

# Try Chocolatey first (preinstalled on GitHub-hosted Windows runners).
if (Get-Command choco -ErrorAction SilentlyContinue) {
    Write-Host "Installing NSIS via Chocolatey"
    choco install nsis --version $nsisVersion -y --no-progress 2>&1 | Out-Null
    $existing = Test-MakensisExists
    if ($existing) {
        Write-Host "NSIS installed via Chocolatey at $existing"
        Set-MakensisPath $existing
        return
    }
}

# Try portable downloads in order.
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
            Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing -TimeoutSec 60
            break
        } catch {
            Write-Host "Attempt $i of $maxAttempts failed: $($_.Exception.Message)"
            if ($i -eq $maxAttempts) { throw }
            Start-Sleep -Seconds 5
        }
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
    "https://sourceforge.net/projects/nsis/files/NSIS%203/${nsisVersion}/nsis-${nsisVersion}.zip/download"
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
