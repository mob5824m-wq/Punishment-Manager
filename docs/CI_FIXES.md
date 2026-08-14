# CI Fix Log

This document tracks each round of CI debugging so we can see
what was tried and what the current state is.

## State

| Job | Status | Last known issue |
|-----|--------|------------------|
| macOS .dmg | ✅ passing | — |
| Linux .deb | ✅ passing | — |
| Windows .exe | ❌ failing | "Active Python lacks python3.lib" check now passes; failure has moved. |

## Timeline

1. **First error** (user-shared): `ERROR: makensis not found.`
   **Fix:** Make the `Install NSIS` step in `build.yml` call
   `pwsh -File .github/scripts/install-nsis.ps1` (which sets
   `$env:PATH` inside the PowerShell session). ✅ done by user.

2. **Second error** (user-shared): PyInstaller step fails because
   the active Python lacks `python3.lib`.
   **Fix:** Add `.github/scripts/install-windows-deps.ps1` that
   downloads the official Python MSI from python.org and runs
   it with `Include_dev=1 Include_lib=1` so the dev files are
   installed.

3. **Third error** (user-shared): the MSI install succeeded but
   `Test-PythonHasLibs` reported `python3.lib` was still missing.
   **Root cause:** the check was looking at
   `os.path.join(p, '..', 'libs', 'python3.lib')` (one level too
   high) and the `InstallAllUsers=1` flag was forcing the install
   to `%ProgramFiles%\Python311` instead of `C:\Python311`.
   **Fix:**
   - Both checks now use `os.path.join(p, 'libs', 'python3.lib')`.
   - Switched to `InstallAllUsers=0` so `TargetDir=C:\Python311`
     is honored.
   - The script emits the install path as the last line of
     stdout so the calling batch script can update PATH.

4. **Current state** (after the third fix): the build is still
   failing on the `Build .exe` step, but the new error message
   is different. Need the actual log to diagnose further.

## What I need from you

Open the latest failed `windows (.exe)` build run, click into
the `Build .exe` step, and copy the **last 50 lines of the log**.

The build script now does extensive error printing, so the
output should be informative. Look for sections like:

- `ERROR: install-windows-deps.ps1 failed. Log:` (install helper
  is still failing)
- `PyInstaller failed. Last 40 lines of log:` (PyInstaller
  itself is failing)
- `ERROR: makensis not found in known locations.` (NSIS lookup
  failed despite the previous fix)
- Or some other error

The new run is at
https://github.com/mob5824m-wq/Punishment-Manager/actions
(latest failed `build` workflow). The `Build .exe` step is
the failing one.

## Common possibilities for the new error

- The official Python MSI may have a different layout in a newer
  version (e.g. `python311.lib` instead of `python3.lib`). The
  check would need to look for both.
- The MSI install may have timed out at 60s. The script retries
  3 times but if the network is slow, all three could time out.
- PyInstaller itself may be failing for a module reason (would
  show `ModuleNotFoundError` in the dumped log).
- The build step may have a syntax error after the recent edits
  (would show a Windows batch parser error).
