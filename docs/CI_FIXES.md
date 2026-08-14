# Fixes needed for the CI workflows

## Current state

The CI build is still failing on the Windows job (`Build .exe`
step). The Linux and macOS jobs are passing.

The agent can no longer access the workflow logs to see exactly
what's failing (the sandbox blocks the Azure blob storage that
hosts them). The most recent change adds error logging to
`build/build_windows.bat` so the next run should print the
actual error to the build log.

## What to do

1. Open the latest failed run:
   https://github.com/mob5824m-wq/Punishment-Manager/actions
2. Click into the `windows (.exe)` job.
3. Click into the `Build .exe` step.
4. Scroll to the bottom of the log. There should be a section
   like "PyInstaller failed. Last 40 lines of log:" followed by
   the actual error.
5. Copy the error and share it back (paste it in chat or open
   an issue).

## Most likely failures

Based on the symptoms (the build step is failing, NSIS install
succeeded), the most likely culprits are:

### 1. `python3.lib` is missing

The slim Python that `actions/setup-python` installs often lacks
`python3.lib`. The build script detects this and runs
`.github/scripts/install-windows-deps.ps1` to install a full
Python from python.org. If that script is also failing (e.g. due
to a network timeout), the error log will show "install helper
failed" with the download details.

**Fix:** if the helper log shows a download error, you can run
the build locally once with `python -m pip install pyinstaller`
followed by `pyinstaller --noconfirm --clean build\pyinstaller.spec`
to confirm it works, then push any local fixes.

### 2. PyInstaller can't find a module

If the build log shows "ModuleNotFoundError: No module named 'X'"
or "hidden import 'X' not found", the `.github/scripts/` or
`pyinstaller.spec` needs more `hiddenimports`.

**Fix:** add the missing module to the `hiddenimports` list in
`pyinstaller.spec` and push the change.

### 3. PyInstaller's bootloader is missing

If the build log shows something like "failed to execute script"
or "bootloader not found", the PyInstaller install itself is
broken. Reinstalling pyinstaller in a fresh venv usually fixes
this.

**Fix:** add `python -m venv .venv && .venv\Scripts\activate && pip
install pyinstaller` to the build script before the pyinstaller
invocation.

## Sharing the error

The fastest way to make progress: open the Actions tab, find the
most recent failed `windows (.exe)` run, copy the **last 30-50
lines of the log output** from the `Build .exe` step, and paste it
back. The agent can then propose a targeted fix.
