# CI Build Status

## State

| Job | Status | Output |
|-----|--------|--------|
| macOS `.dmg` | passing | `dist/Sentinel-X.Y.Z.dmg` |
| Linux `.deb` | passing | `dist/sentinel_X.Y.Z_amd64.deb` |
| Windows `.exe` | passing | `dist/Sentinel-Setup-X.Y.Z.exe` |
| All artifacts | produced | Uploaded as workflow artifacts (3-day retention) |

The `build.yml` CI build is fully working on all three platforms, and so is
`release.yml`: the matrix bug described under ~~"The open bug"~~ below was
applied to `main` (three explicit per-platform jobs), so pushing a `v*` tag
builds the installers and attaches them to the release by itself - as it did
for v2.0.0, and again for **v2.1.0** (the crash fix below).

## How a release is cut

`VERSION` at the project root is the single source of truth for the version;
every build script reads it (see "Fix 13"), so the artifact names, the `.app`
plist and the NSIS metadata always match the tag.

```bash
# 1. Bump the version and land it on main.
echo 2.1.1 > VERSION
git commit -am "chore: bump version to 2.1.1" && git push origin main

# 2. Tag it - this runs release.yml end to end.
./scripts/make_release.sh 2.1.1     # refuses to tag if VERSION disagrees
```

To fall back to a manual release (only needed if release.yml breaks again):

```bash
# 1. Tag the commit you want to release.
git tag -a v1.0.0 -m "Release 1.0.0"
git push origin v1.0.0

# 2. Create the GitHub Release with release notes.
gh release create v1.0.0 \
    --title "Sentinel 1.0.0" \
    --notes-file /path/to/release-notes.md \
    --target <commit-sha>

# 3. Drag-and-drop the build artifacts onto the release page
#    in the web UI. The artifacts are on the Actions tab of
#    the matching commit, named:
#      sentinel-linux   (the .deb)
#      sentinel-macos   (the .dmg)
#      sentinel-windows (the .exe)
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

## ~~The open bug: `release.yml` is broken~~ (resolved)

**Resolved.** The corrected file is on `main` (three explicit jobs, no
`matrix.shell`), and v1.0.1 was released through it. The original failure,
kept for context:

`release.yml` triggers on every `v*` tag push, but it never
actually ran. GitHub rejected the workflow file at parse time:

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

The agent's GitHub App token could not push files into
`.github/workflows/` (no `workflows` permission), so a maintainer applied the
corrected file; releases are automated again. If the permission is still
missing on the App, `.github/workflows/*` edits must go in through the web
editor or a maintainer's token.

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

**Problem:** The original `File /r "..\dist\sentinel\*"`
in `installer.nsi` resolved relative to the script's directory
(`build/windows/`), so it was looking for `build/dist/...`
which doesn't exist.

**Fix:** Changed to `File /r "..\..\dist\sentinel\*"`.
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

### 12. Packaged builds crashed on first launch (read-only app dir)

**Problem:** the installed `.deb` binary died at import time with

```
File "bot.py", line 35, in <module>
File "pathlib.py", line 1116, in mkdir
PermissionError: [Errno 13] Permission denied: '/opt/sentinel/_internal/data'
[PYI-49989:ERROR] Failed to execute script 'bot' due to unhandled exception!
```

`bot.py` derived every path from `Path(__file__).parent`. In a PyInstaller
onedir build that is `_internal/` inside the install tree — root-owned and
read-only (and explicitly read-only for the service, since the unit sets
`ProtectSystem=strict`). So `DATA_DIR.mkdir()` raised before the logger even
existed. The same applied to `config.json` (written into the install dir,
world-readable, with the bot token in it) and to the Windows `.exe` under
`C:\Program Files`. CI did not catch it because the installers are only
*built*, never run.

**Fix:** new `paths.py` resolves the data dir and config file once, at import:
env overrides, then the platform state dir (`/var/lib/sentinel`,
`~/.local/state/sentinel`, `~/Library/Application Support/...`,
`%LOCALAPPDATA%\Sentinel`), then `<app dir>/data`, then a temp dir —
each candidate only used if it is actually creatable and writable. A config
found in a read-only place is copied on write to the writable location. The
systemd unit, launchd agent and NSSM service are now generated against the
running executable (with the packaged resources looked up via
`paths.resource_path`), and `--install-service` chowns the state dirs to the
service user. `bot.py --paths` prints the resolved locations.

**Regression gate:** `tests/test_paths.py` re-creates the packaged layout in a
temp dir (read-only app tree, `sys.frozen`/`_MEIPASS`/`sys.executable`
patched, isolated `HOME`) and asserts `import bot` succeeds and nothing is
written into the install tree. It runs in `build.yml` (all three OSes) and in
`release.yml`'s linux job, so a path regression fails the build instead of the
customer's first launch. Read-only-permission cases skip when running as root
or on Windows, where `chmod` can't emulate them.

### 13. Version drift between the tag and the installers

**Problem:** the version was hardcoded in seven places - `build_linux.sh`,
`build_macos.sh`, `build_windows.bat`, the `pyinstaller.spec` plist, the
macOS `Info.plist`, `installer.nsi` and the generated `.deb` control file -
and nothing tied them to the git tag. Cutting `v1.0.1` would have shipped
`sentinel_1.0.0_amd64.deb` attached to a release named v1.0.1, with
a `.app` reporting a third version, and no way to ask a user which build they
had installed.

**Fix:** a `VERSION` file at the project root is the single source of truth.
`scripts/pm_version.sh` resolves it (`$PM_VERSION` override > `VERSION` file >
matching git tag > `0.0.0+unknown`) for the shell scripts, the `.bat` reads the
file directly, and the spec reads it for the plist *and* bundles a copy so
`sentinel --version` / the startup log line report the *installed*
build rather than a nearby checkout. `make_release.sh` refuses to tag when the
tag and the file disagree. `tests/test_version.py` fails if any of those
scripts hardcodes a version again.

### 14. An inaccessible path candidate ended the search (v2.1.1)

**Problem:** before Python 3.13 (the builds use 3.11), `Path.exists()` /
`is_file()` only swallow `ENOENT`/`ENOTDIR`/`EBADF`/`ELOOP` and raise
everything else - notably `EACCES` for a path under a directory the user may
not traverse. The `.deb`'s `postinst` makes `/etc/sentinel`
`0750 root:sentinel`, so for anyone outside that group (typically a
portable/unzipped build run next to a `.deb` install) the
`/etc/sentinel/config.json` candidate raised `PermissionError` out of
`_pick_config_path()`'s loop. v2.1.0 was still safe to run - the import-time
guard caught it - but every candidate *after* the inaccessible one was dropped
(the portable install's own `<app dir>/config.json` was never tried and an
empty default was created in the data dir instead), and the note was
mislabelled `WARNING: config resolution failed (...)`. The data-dir loop had
the same bug: an inaccessible `$XDG_STATE_HOME` candidate demoted the bot to
the temp-dir fallback with "data directory resolution failed".

**Fix:** `paths._inspect()` classifies a path without raising
(`dir`/`file`/`other`/`missing`/`inaccessible` plus the OS reason) and backs
every probe in the resolution code. Both loops skip an inaccessible candidate
with an accurate per-candidate note (`... cannot be accessed (Permission
denied); using another location` / `Config candidate ... cannot be accessed
(...); skipped`) and carry on; the import-time `except` clauses are a true
last resort again.

**Regression gate:** `tests/test_paths.py::InaccessibleCandidateTests`
re-creates a portable install next to a locked system config dir and a locked
per-user state dir (skipped as root / on Windows like the other permission-bit
fixtures).

### 15. No slash commands were registered: a description over Discord's limit

**Problem:** `/manage status` had a 101-character description. Discord allows
1-100 characters for every command and option description and validates the
whole list at once, so the startup `tree.sync()` failed with HTTP 400 / error
50035 (`In command 'punish status' ... description: Must be between 1 and 100
in length`) and **none** of the commands were registered or updated. The bot
logged `Failed to sync global commands.` and carried on - it has to, the
scheduler that releases punished users runs in the same process - so it looked
healthy. discord.py shortens descriptions it derives itself (docstrings,
`@app_commands.describe(...)`), but a `description=` passed explicitly to
`command()` / `Group()` is sent as-is, and nothing exercised the command tree
before a release.

**Fix:** the description is 94 characters now ("config" instead of
"configuration").

**Regression gate:** `tests/test_commands.py` builds the real command tree in a
sandboxed subprocess - the exact payload `tree.sync()` would upload, no network
or token needed - and fails, naming the command, if any command, group,
subcommand or option description is outside 1-100 characters. It also feeds the
checker the string that broke sync, so the gate can't quietly become a no-op.

## Test gate in CI

`build.yml` (all three platforms) and `release.yml` (linux) run
`python -m pytest tests`, which covers runtime path resolution (fixes 12 and 14),
the version plumbing (fix 13) and the slash-command definitions (fix 15). The
suites are plain `unittest`, so they also run standalone:
`python tests/test_paths.py`.

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
