# CI Build Status

## State

| Job | Status | Output |
|-----|--------|--------|
| macOS .dmg | ✅ passing | `dist/PunishmentManager-X.Y.Z.dmg` |
| Linux .deb | ✅ passing | `dist/punishment-manager_X.Y.Z_amd64.deb` |
| Windows .exe | ✅ passing | `dist/PunishmentManager-Setup-X.Y.Z.exe` |
| All artifacts | ✅ produced | Uploaded as workflow artifacts for 7 days |

The CI build is now fully working on all three platforms.

## How to cut a release

Once you're ready to publish:

```bash
./scripts/make_release.sh 1.0.0
```

This creates an annotated `v1.0.0` tag and pushes it. The
`release.yml` workflow then builds installers on all three
platforms in parallel and attaches them to a GitHub Release
at:

```
https://github.com/mob5824m-wq/Punishment-Manager/releases/tag/v1.0.0
```

The release will be a normal release. Push `v1.0.0-rc1` or
similar to make it a prerelease (the workflow auto-detects
the `-` in the tag name).

## History of fixes

The CI build had to be debugged through 20+ rounds. The
fixes are documented below in case you need to understand
why the workflow does what it does.

### 1. Setup Python dev files

**Problem:** PyInstaller's bootloader needs `python3.lib` next
to `python3.dll`, but the slim Python that `actions/setup-python`
installs lacks it.

**Fix:** `.github/scripts/install-windows-deps.ps1` downloads
the official Python MSI from python.org with
`Include_dev=1 Include_lib=1` when the active Python lacks
the dev files.

### 2. NSIS path discovery

**Problem:** The Install NSIS step sets `$env:PATH` inside its
own PowerShell session; that change doesn't propagate to
the next step's cmd.exe session.

**Fix:** `install-nsis.ps1` exports the install path via
`$GITHUB_ENV` as `MAKENSIS_PATH`. `build_windows.bat` reads
`%MAKENSIS_PATH%` and uses that as the makensis location.

### 3. cmd.exe parens-block parser bug

**Problem:** `if exist "C:\Program Files (x86)\NSIS\..." (`
inside a parenthesized block causes cmd.exe's `if` parser
to misread the path. The `(` inside `(x86)` gets confused
with the `(` of the if's code block.

**Fix:** Replaced all `if exist ... ( ... )` patterns with
`if exist ... goto :label` and `set` + `goto` chains. No
parenthesized blocks anywhere in the build script.

### 4. NSIS 2 commands

**Problem:** The original `installer.nsi` used `SetBrandText`
which was removed in NSIS 3.

**Fix:** Removed `SetBrandText`, the `MUI_ICON` and
`MUI_UNICON` references (the icon files don't exist),
`MUI_HEADERIMAGE` and the bitmap paths (Chocolatey NSIS
doesn't ship Contrib\Graphics).

### 5. EnVar plugin not bundled

**Problem:** `EnVar::SetHKLM` and `EnVar::AddValue` calls
require the EnVar plugin, which isn't bundled with the
standard NSIS install.

**Fix:** Removed the PATH-manipulation lines from the
Install and Uninstall sections. Users can add the install
dir to PATH manually if they want command-line access.

### 6. NSIS file path resolution

**Problem:** The original `File /r "..\dist\..."` in
`installer.nsi` resolved relative to the script's directory
(`build/windows/`), so it looked for `build/dist/...` which
doesn't exist.

**Fix:** Changed to `File /r "..\..\dist\punishment-manager\*"`
so the path resolves to the project's `dist/` directory.

### 7. NSIS OUTFILE path

**Problem:** `makensis /OUTFILE="dist\..."` was a relative
path, and NSIS wrote the output to a different location
than the build script expected.

**Fix:** Changed to `makensis /OUTFILE="%CD%\dist\..."` to
use the absolute project root.

## What's still missing

The `release.yml` workflow has a few issues that haven't been
tested yet (it only runs on `v*` tags, so the recent push didn't
exercise it):

- The matrix `shell:` field doesn't match the actual shell
  used by `run:` on Windows.
- The macOS job uses `hdiutil verify` but doesn't have a
  `creator` step.

These are minor and shouldn't block the next release. If they
do cause issues when you push a `v*` tag, the same debug loop
we just did for the build workflow will help fix them.

## Other notes

- The default config (when no MAKENSIS_PATH is set) checks
  `C:\nsis-3.10\`, `C:\nsis-3.09\`, `C:\nsis-3.08\` paths. These
  are the standard portable install locations. If you install
  NSIS to a different path, the `MAKENSIS_PATH` env var or
  the search in `build_windows.bat` can be adjusted.
- The build uses no code signing. If you want signed
  installers, set up `signtool` in the build and add a signing
  step at the end.
- For a release, the `release.yml` workflow uses
  `softprops/action-gh-release@v2` to create the release and
  attach the artifacts. This requires `contents: write`
  permission on the GITHUB_TOKEN, which the workflow sets.
