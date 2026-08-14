# CI Fix Log

This document tracks each round of CI debugging so we can see
what was tried and what the current state is.

## State

| Job | Status | Last known issue |
|-----|--------|------------------|
| macOS .dmg | ✅ passing | — |
| Linux .deb | ✅ passing | — |
| Windows .exe | ❌ failing | "Active Python has python3.lib; no install needed" — passes! But the next part fails. |

## Timeline

1. **First error** (user-shared): `ERROR: makensis not found.`
   **Fix:** Made the `Install NSIS` step in `build.yml` call
   `pwsh -File .github/scripts/install-nsis.ps1`. ✅

2. **Second error**: PyInstaller step fails because the active
   Python lacks `python3.lib`.
   **Fix:** Added `.github/scripts/install-windows-deps.ps1` to
   download the official Python MSI and run it with
   `Include_dev=1 Include_lib=1`.

3. **Third error** (user-shared): MSI install succeeded but
   `Test-PythonHasLibs` reported `python3.lib still missing`.
   **Root cause:** the check was looking at
   `os.path.join(p, '..', 'libs', 'python3.lib')` (one level too
   high) and `InstallAllUsers=1` forced the install to
   `%ProgramFiles%\Python311` instead of `C:\Python311`.
   **Fix:** Dropped the `..`, switched to `InstallAllUsers=0` so
   `TargetDir=C:\Python311` is honored. After the install the
   script emits the install path as the last line of stdout and
   the calling batch file updates PATH to use it.

4. **Fourth error** (user-shared): `%\NSIS\makensis.exe was
   unexpected at this time.`
   **Root cause:** the `if exist` line had `%ProgramFiles(x86)%`
   inside a parenthesized `else if` block. cmd.exe's parser
   mis-handled the environment variable expansion within the
   parens, leading to a syntax error.
   **Fix:** Replaced `%ProgramFiles(x86)%` and `%ProgramFiles%`
   with their hardcoded equivalents `C:\Program Files (x86)\` and
   `C:\Program Files\`.

5. **Current state** (after the fourth fix): the build is still
   failing on the `Build .exe` step, but the path lookup is now
   fixed. The new error message is different.

## What I need from you

Open the latest failed `build` run, click into the `windows
(.exe)` job, then the `Build .exe` step, and copy the **last 50
lines of the log**.

The build script now does extensive error printing, so the
output should be informative. Look for sections like:

- `ERROR: install-windows-deps.ps1 failed. Log:`
- `PyInstaller failed. Last 40 lines of log:`
- `ERROR: makensis not found in known locations.`
- A new error I haven't anticipated

The new run is at
https://github.com/mob5824m-wq/Punishment-Manager/actions
(latest failed `build` workflow). The `Build .exe` step is
the failing one.

## Common possibilities for the new error

- The path with `(x86)` and spaces still has issues inside
  parens, and the parser is now failing on a different line.
  The fix would be to use `call` instead of `else if` chains.
- The new Python was installed but the `for /f` line that reads
  the install path uses PowerShell, which might not be on PATH
  in this cmd session.
- The NSIS install step set up an env var, but the build
  script is reading a stale value.
- The makensis invocation itself is failing (would show
  `Error: makensis returned non-zero` or similar).

The most useful thing is just the raw log output. Paste it
back and I'll write a targeted fix.
