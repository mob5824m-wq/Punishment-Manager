# CI Build Status

## State

| Job | Status | Output |
|-----|--------|--------|
| macOS `.dmg` | passing | `dist/PunishmentManager-X.Y.Z.dmg` |
| Linux `.deb` | passing | `dist/punishment-manager_X.Y.Z_amd64.deb` |
| Windows `.exe` | passing | `dist/PunishmentManager-Setup-X.Y.Z.exe` |
| All artifacts | produced | Uploaded as workflow artifacts (3-day retention) |

The `build.yml` CI build is fully working on all three platforms.
The `release.yml` workflow is currently **broken** (see below) and
the **v1.0.0 release** had to be created manually. See
`.github/SETUP_WORKFLOWS.md` for the workflow state and the
maintainer-side fix needed.

## How a release is currently cut

Until `release.yml` is fixed, releases are created by hand. The
recipe that produced v1.0.0:

```bash
# 1. Tag the commit you want to release.
git tag -a v1.0.0 -m "Release 1.0.0"
git push origin v1.0.0

# 2. Create the GitHub Release with release notes.
gh release create v1.0.0 \
    --title "Punishment Manager 1.0.0" \
    --notes-file /path/to/release-notes.md \
    --target <commit-sha>

# 3. Drag-and-drop the build artifacts onto the release page
#    in the web UI. The artifacts are on the Actions tab of
#    the matching commit, named:
#      punishment-manager-linux   (the .deb)
#      punishment-manager-macos   (the .dmg)
#      punishment-manager-windows (the .exe)
#    The web-UI upload goes through github.com (reachable from
#    anywhere), so this works in environments where
#    uploads.github.com is firewalled.
```

Once `release.yml` is fixed, releases can go back to the automated
flow:

```bash
./scripts/make_release.sh 1.0.0
# creates an annotated v1.0.0 tag, pushes it, and the
# release.yml workflow builds the installers and attaches them
# to a GitHub Release.
```

A plain version like `1.0.0` becomes a normal release. A
`v1.0.0-rc1` tag (anything with a hyphen) is marked prerelease.

## The open bug: `release.yml` is broken

`release.yml` triggers on every `v*` tag push, but it never
actually runs. GitHub rejects the workflow file at parse time:

> Invalid workflow file: `.github/workflows/release.yml#L1`
> (Line: 83, Col: 16): Unrecognized named-value: 'matrix'.

The offending line is:

```yaml
- name: Build
  run: ${{ matrix.build_script }}
  shell: ${{ matrix.shell }}   # <-- matrix is not bound here
```

`shell:` on a step is evaluated before the matrix strategy is
resolved, so `${{ matrix.shell }}` is not a valid value. The
correct pattern (already used in `build.yml`) is to replace the
matrix with three explicit per-platform jobs, one per runner.

The agent's GitHub App token cannot push files into
`.github/workflows/` (it lacks the `workflows` permission), so
the fix has to be applied by a maintainer. The corrected
`release.yml` is in the PR branch history; merging PR #1 (or
applying the change by hand through the web editor) is the last
step to make the release flow automated again.

## History of fixes

The CI build was debugged through many rounds. The fixes are
documented below in case you need to understand why the scripts
do what they do.

### 1. Setup Python dev files

**Problem:** PyInstaller's bootloader needs `python3.lib` next
to `python3.dll`, but the slim Python that `actions/setup-python`
installs lacks it.

**Fix:** `.github/scripts/install-windows-deps.ps1` downloads
the official Python MSI from python.org with
`Include_dev=1 Include_lib=1` when the active Python lacks
the dev files. The helper was rewritten during the debug round
to look for `python3.lib` at
`os.path.join(sys.executable_dir, "libs", "python3.lib")` (one
`libs` segment, not two — the earlier two-segment path was a
real bug that left the dev files uninstalled).

### 2. NSIS path discovery

**Problem:** The "Install NSIS" step sets `$env:PATH` inside
its own PowerShell session, but that change doesn't propagate
to the next step's cmd.exe session — cmd.exe can't see
`makensis` even after a successful install.

**Fix:** `install-nsis.ps1` exports the install path via
`$GITHUB_ENV` as `MAKENSIS_PATH`. `build_windows.bat` reads
`%MAKENSIS_PATH%` and uses that as the primary source of
truth, with `C:\nsis-3.10\`, `C:\nsis-3.09\`, `C:\nsis-3.08\`
as fallbacks.

### 3. cmd.exe parens-block parser bug

**Problem:** `if exist "C:\Program Files (x86)\NSIS\..." (`
inside a parenthesized block causes cmd.exe's `if` parser to
misread the path. The `(` inside `(x86)` gets confused with
the `(` of the `if`'s code block, and the whole line dies with
`%\Program Files (x86)\NSIS\... was unexpected at this time.`

**Fix:** Replaced all `if exist ... ( ... )` patterns in
`build_windows.bat` with `if exist ... goto :label` and
`set` + `goto` chains. No parenthesized blocks anywhere in
the build script. This is fragile to write but reliable to
run.

### 4. NSIS 2 commands

**Problem:** The original `installer.nsi` used `SetBrandText`,
which was removed in NSIS 3.

**Fix:** Removed `SetBrandText`. Also removed the `MUI_ICON`
and `MUI_UNICON` references (the icon files don't exist),
`MUI_HEADERIMAGE` and the bitmap paths (Chocolatey's NSIS
doesn't ship `Contrib\Graphics\`). The installer still looks
clean without the cosmetic extras.

### 5. EnVar plugin not bundled

**Problem:** `EnVar::SetHKLM` and `EnVar::AddValue` calls
require the EnVar plugin, which isn't bundled with the
standard NSIS install. The plugin error is:

> Plugin not found, cannot call EnVar::SetHKLM
> Plugin directories: C:\Program Files (x86)\NSIS\Plugins\x86-unicode

**Fix:** Removed the PATH-manipulation lines from both the
Install and Uninstall sections. Users can add the install
dir to their PATH manually if they want command-line access;
the trade-off is worth not depending on a non-bundled plugin.

### 6. NSIS file path resolution

**Problem:** The original `File /r "..\dist\punishment-manager\*"`
in `installer.nsi` resolved relative to the script's directory
(`build/windows/`), so it was looking for `build/dist/...`
which doesn't exist.

**Fix:** Changed to `File /r "..\..\dist\punishment-manager\*"`.
The two `..` are required because NSIS resolves the path
relative to the directory containing the script.

### 7. NSIS OUTFILE path

**Problem:** `makensis /OUTFILE="dist\..."` was a relative
path, and NSIS wrote the installer to
`<project_root>\build\windows\dist\...` instead of
`<project_root>\dist\...`.

**Fix:** Pass the absolute project root via the `%CD%` env
var: `makensis /DOUTFILE="%CD%\dist\..."`.

### 8. Chocolatey NSIS version

**Problem:** The original `install-nsis.ps1` did
`choco install nsis --version 3.10`, but the public
`chocolatey.org/packages/nsis` only publishes a three-part
version (`3.10.0`). The shorter version silently no-ops —
the install never happens, the script falls back to the
portable download, and the whole step is flaky as a result.

**Fix:** Pass the full version to Chocolatey:
`choco install nsis --version=3.10.0`. The portable downloads
still use the two-segment `3.10` since the SourceForge zip is
named `nsis-3.10.zip`.

### 9. `install-nsis.ps1` swallowed choco errors

**Problem:** `choco install ... 2>&1 | Out-Null` hid any
Chocolatey error so the script couldn't tell why the install
failed, and `Test-MakensisExists` always returned `$null`,
forcing the script to fall back to the portable download
every time. Also, piping to `Out-Host` in a non-interactive
PowerShell session is unreliable.

**Fix:** Capture choco's output into a variable with the `&`
call operator and log each line through `Write-Host` explicitly.
Now the step log shows exactly what Chocolatey said.

### 10. SourceForge HTML error pages

**Problem:** SourceForge occasionally serves an HTML error
page on a `200 OK` response (a few hundred bytes, not the
expected ~2.4 MB zip). The old `Invoke-WebRequest` would
succeed and `Expand-Archive` would then fail with a confusing
"end of central directory record not found" error.

**Fix:** After downloading, check that the file is at least
100 KB before extracting; retry on size-check failure.

### 11. `release.yml` matrix.shell

**Problem:** As described in "The open bug" above, the
`shell: ${{ matrix.shell }}` on the `Build` step is not valid
GitHub Actions syntax. The matrix context isn't bound at the
point `shell:` is evaluated, so GitHub rejects the workflow
file at parse time. The workflow never runs.

**Fix:** Replace the matrix with three explicit per-platform
jobs, like `build.yml` already does. The agent has the
corrected `release.yml` in the PR branch but cannot push it
(no `workflows` permission on the GitHub App token). A
maintainer needs to apply the change.

## Other notes

- The build uses no code signing. If you want signed
  installers, set up `signtool` in the build and add a signing
  step at the end.
- The `softprops/action-gh-release@v2` action requires
  `contents: write` on the GITHUB_TOKEN, which `release.yml`
  declares. Once the matrix bug is fixed, this should "just
  work" — all three matrix jobs independently call the action
  to upload their own artifact.
- Build artifacts on the Actions tab are retained for 3 days
  (the `retention-days: 3` in `build.yml`). The previous
  default of 7 days was reduced to keep the artifact budget
  under GitHub's free-tier limit.
