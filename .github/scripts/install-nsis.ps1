# install-nsis.ps1
# Downloads and installs a portable copy of NSIS 3.x for the build
# workflow on Windows. Tries Chocolatey first, then a portable zip
# download, then a GitHub release mirror. Retries on transient
# network errors.
#
# Why this lives in its own file: it's a PowerShell script, so it
# doesn't go through YAML's literal block scalar parsing. Pasting
# it into the web editor as a single `pwsh <path>` call is robust.
$ErrorActionPreference = 'Stop'

$nsisVersion = '3.10'
$installDir  = "C:\nsis-${nsisVersion}"

if (Test-Path "$installDir\makensis.exe") {
    Write-Host "NSIS already installed at $installDir"
    $env:PATH = "${installDir};${env:PATH}"
    exit 0
}

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
    if (Test-Path $installDir) {
        Remove-Item -Recurse -Force $installDir
    }
    Expand-Archive -Path $zipPath -DestinationPath 'C:\' -Force
    $env:PATH = "${installDir};${env:PATH}"
    & "${installDir}\makensis.exe" /VERSION
    Write-Host "NSIS installed at: $installDir"
}

# Try Chocolatey first (preinstalled on GitHub-hosted Windows runners).
if (Get-Command choco -ErrorAction SilentlyContinue) {
    Write-Host "Installing NSIS via Chocolatey"
    choco install nsis --version $nsisVersion -y --no-progress 2>&1 | Out-Null
    if (Test-Path "${env:ProgramFiles(x86)}\NSIS\makensis.exe") {
        $env:PATH = "${env:ProgramFiles(x86)}\NSIS;${env:PATH}"
        & "${env:ProgramFiles(x86)}\NSIS\makensis.exe" /VERSION
        Write-Host "NSIS installed via Chocolatey at ${env:ProgramFiles(x86)}\NSIS"
        exit 0
    }
}

# Try portable downloads in order.
$urls = @(
    "https://sourceforge.net/projects/nsis/files/NSIS%203/${nsisVersion}/nsis-${nsisVersion}.zip/download"
    "https://github.com/lordmulder/nsis/releases/download/v${nsisVersion}/nsis-${nsisVersion}.zip"
)

$labels = @("SourceForge", "GitHub mirror")
for ($i = 0; $i -lt $urls.Length; $i++) {
    try {
        Install-NsisPortable $urls[$i] $labels[$i]
        exit 0
    } catch {
        Write-Host "Failed via $($labels[$i]): $($_.Exception.Message)"
    }
}

Write-Host "All install attempts failed. Please install NSIS manually."
exit 1
