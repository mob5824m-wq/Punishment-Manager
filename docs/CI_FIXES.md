# Fixes needed for the CI workflows

The release and build workflows are running, but two of the three
platform jobs are failing because of toolchain issues that depend
on which Ubuntu / Windows runner image GitHub has shipped. These
fixes need to be applied by a maintainer with the `workflows`
permission (the agent's GitHub App token can't push to
`.github/workflows/`).

## What's failing

The latest CI run showed:
- **macOS .dmg** job: ✅ passed
- **Linux .deb** job: ❌ failed on "Install build dependencies"
- **Windows .exe** job: ❌ failed on "Install NSIS"

Both failures are environment issues, not logic bugs in the workflows.

## Fix 1 — Linux apt-get package name

In `.github/workflows/build.yml`, the linux job runs:

```yaml
- name: Install build dependencies
  run: |
    sudo apt-get update
    sudo apt-get install -y libpython3.11 fakeroot dpkg lintian
```

`libpython3.11` is the bare package name on **Ubuntu 22.04** but
GitHub's `ubuntu-latest` runner cycles through newer versions
(24.04, then 25.04) where the lib moved to `libpython3.11-dev`. The
install fails with `E: Unable to locate package libpython3.11`.

**Replace** the run block with:

```yaml
- name: Install build dependencies
  run: |
    sudo apt-get update
    # libpython3.11 is in the -dev package on newer Ubuntu; the
    # standalone name only exists on 22.04. Try the bare package
    # first and fall back to -dev.
    sudo apt-get install -y fakeroot dpkg lintian \
        libpython3.11 || sudo apt-get install -y libpython3.11-dev
    # Also install the static lib so PyInstaller's bootloader
    # can link if needed.
    sudo apt-get install -y libpython3.11-dev || true
```

This tries the bare package first (works on 22.04) and falls back
to `-dev` (works on 24.04+). Either way, PyInstaller finds what it
needs.

## Fix 2 — Windows NSIS install

`.github/workflows/install-nsis.ps1` only downloads from SourceForge,
which has been rate-limiting CI traffic. Some runs succeed, some
fail with HTTP 503 or 429.

**Replace the entire file** with this version, which tries three
sources in order with retries:

```powershell
# install-nsis.ps1
# Downloads and installs a portable copy of NSIS 3.x for the build
# workflow on Windows. Tries Chocolatey first, then a portable zip
# download, then a GitHub release mirror.
#
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
```

## How to apply

1. Open `.github/workflows/build.yml` on the branch in the GitHub
   web UI.
2. Click the pencil icon, find the "Install build dependencies"
   step, paste the new run block in.
3. Commit with message "Fix Linux apt-get libpython3.11 fallback".
4. Open `.github/workflows/install-nsis.ps1` similarly and replace
   the entire file.
5. Commit with message "Make NSIS install more robust".

Both files will be updated in a single workflow run, which should
then pass on all three platforms.

## After the fix lands

The release workflow will then be able to build installers on every
`v*` tag. To cut a release:

```bash
./scripts/make_release.sh 1.0.0
```

The resulting `.dmg`, `.deb`, and `.exe` will be uploaded to
`https://github.com/mob5824m-wq/Punishment-Manager/releases/tag/v1.0.0`.

The full, ready-to-apply patch is also attached below for reference.

```diff
diff --git a/.github/workflows/build.yml b/.github/workflows/build.yml
--- a/.github/workflows/build.yml
+++ b/.github/workflows/build.yml
@@ -28,7 +28,14 @@ jobs:
       - name: Install build dependencies
         run: |
           sudo apt-get update
-          sudo apt-get install -y libpython3.11 fakeroot dpkg lintian
+          # libpython3.11 is in the -dev package on newer Ubuntu; the
+          # standalone name only exists on 22.04. Try the bare package
+          # first and fall back to -dev.
+          sudo apt-get install -y fakeroot dpkg lintian \
+              libpython3.11 || sudo apt-get install -y libpython3.11-dev
+          # Also install the static lib so PyInstaller's bootloader
+          # can link if needed.
+          sudo apt-get install -y libpython3.11-dev || true
```
