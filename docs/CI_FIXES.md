# CI Fix Log

## State

| Job | Status | Last known issue |
|-----|--------|------------------|
| macOS .dmg | ✅ passing | — |
| Linux .deb | ✅ passing | — |
| Windows .exe | ❌ failing | `%\NSIS\makensis.exe was unexpected at this time.` (from before the latest fixes) |

The build still fails on the `Build .exe` step in the Windows job,
but the latest commits should have removed the parens-block that
was producing the `\NSIS\makensis.exe was unexpected` error. The
new error is something different that I can't see from this
sandbox.

## What I need from you

Open the latest failed `build` run at
https://github.com/mob5824m-wq/Punishment-Manager/actions

Click into the `windows (.exe)` job, then the `Build .exe` step,
and copy the **last 50 lines of the log**.

The build script now does extensive error printing, so the
output should be informative. Look for sections like:

- `ERROR: install-windows-deps.ps1 failed. Log:`
- `PyInstaller failed. Last 40 lines of log:`
- `ERROR: makensis not found at C:\nsis-3.10\makensis.exe.`
- A new error I haven't anticipated

The latest commit is `1aa8f6b` which has the goto-based NSIS
path lookup. If the error still mentions `\NSIS\makensis.exe`,
then the build is using a cached or stale copy of the file
(which would be unusual for GitHub Actions). If the error is
something different, paste it and I'll write a targeted fix.

## Most recent fixes (already pushed)

1. **Hardcoded NSIS path** (`3b1cc16`) — The `if exist` search
   for makensis kept triggering cmd.exe parser bugs. Replaced
   with a single hardcoded `C:\nsis-3.10\makensis.exe` path.

2. **Goto-based if-not-exist** (`1aa8f6b`) — Even the hardcode
   version had `if not exist "..." (` (parens block). Replaced
   with `if not exist "..." goto :label`.

If those didn't help, the error is somewhere completely
different (not in the NSIS section) and we need the actual
log to diagnose.

## Common possibilities for the new error

- **Python check failing**: The `python -c "import sys..."` line
  at the top of `build_windows.bat` could be failing if the
  active Python is one that doesn't have `sys` (impossible) or
  has `python3.lib` in a different relative path than expected.
- **PyInstaller step**: The PyInstaller command itself could
  be failing now. The error would show in the dumped log
  (`type pyinstaller.log`).
- **cmd.exe syntax error elsewhere**: A previous edit to the
  bat file might have introduced a syntax bug. The error
  message would be different.
