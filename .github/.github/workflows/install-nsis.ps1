# install-nsis.ps1
# Downloads and installs a portable copy of NSIS 3.x for the build
# workflow on Windows. Chocolatey's NSIS package is non-deterministic
# across versions, so we install a specific build.
#
$ErrorActionPreference = 'Stop'

$nsisVersion = '3.10'
$nsisUrl     = "https://sourceforge.net/projects/nsis/files/NSIS%203/${nsisVersion}/nsis-${nsisVersion}.zip/download"
$downloadDir = "$env:TEMP\nsis-install"
$zipPath     = "$downloadDir\nsis.zip"
$installDir  = "C:\nsis-${nsisVersion}"

if (Test-Path "$installDir\makensis.exe") {
    Write-Host "NSIS already installed at $installDir"
    exit 0
}

if (-not (Test-Path $downloadDir)) {
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
}

Write-Host "Downloading NSIS $nsisVersion from $nsisUrl"
Invoke-WebRequest -Uri $nsisUrl -OutFile $zipPath -UseBasicParsing

Write-Host "Extracting to $installDir"
if (Test-Path $installDir) {
    Remove-Item -Recurse -Force $installDir
}
Expand-Archive -Path $zipPath -DestinationPath 'C:\' -Force

# Add to PATH for this and subsequent steps.
$env:PATH = "${installDir};${env:PATH}"
Write-Host "NSIS installed at: $installDir"
& "${installDir}\makensis.exe" /VERSION
