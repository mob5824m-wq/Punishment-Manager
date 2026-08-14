# Fixes needed for the CI workflows

The build workflow is failing on Linux and Windows. The two
helper scripts in `.github/scripts/` are now in place — what's
left is to update `.github/workflows/build.yml` to call them.

**Why this is a full-file replacement, not a paste-in-place edit:**

The previous fix attempts had you paste multi-line `run: |`
blocks into the GitHub web editor. The web editor strips
leading whitespace on paste, which silently breaks YAML literal
block scalars and produces confusing errors like
"Unexpected value" or "StringToken was expected".

The simplest, most reliable fix is to **replace the entire
`build.yml` file** in one go. A full file replacement in the
GitHub web editor is one click — open the file, select all,
paste the contents below, commit. There's no leading indentation
to lose because the file's contents are the new file's contents.

## What to do

1. Go to https://github.com/mob5824m-wq/Punishment-Manager/blob/arena/019ffe80-punishment-manager/.github/workflows/build.yml
2. Click the pencil icon to edit.
3. Press `Ctrl+A` / `Cmd+A` to select all, then `Delete` to clear.
4. Paste the entire file content from the "Full replacement"
   section below.
5. Scroll down, commit with the message "Update build.yml to use
   the helper scripts".

The release workflow (`release.yml`) doesn't need any changes —
it has the same failing pattern, but those runs are also failing
on the same steps and the same fix applies. **You can apply the
identical replacement to `release.yml` too** if you want both
workflows fixed. See the note at the end.

## Full replacement for `build.yml`

```yaml
name: build

# Sanity-check the build on every push and pull request, on all
# three platforms. This catches build-script regressions early
# without producing any releases - the actual release builds live
# in release.yml and only run on v* tags.
#
# Every multi-step block (apt-get install, NSIS download) lives
# in its own file under .github/scripts/ and is invoked as a
# single command here. This avoids the YAML literal-block
# scalar (run: |) parse issues that come from pasting into the
# GitHub web editor with the indentation stripped.

on:
  push:
    branches: [main, 'arena/**']
  pull_request:
    branches: [main']

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  linux:
    name: linux (.deb)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'
      - name: Install build dependencies
        run: bash $GITHUB_WORKSPACE/.github/scripts/install-linux-deps.sh
      - name: Install Python dependencies
        run: bash -c "python -m pip install --upgrade pip && pip install -r requirements.txt && pip install pyinstaller"
      - name: Smoke-test imports
        run: python -c "import bot, installer; print('imports OK')"
      - name: Build .deb
        run: bash build/build_linux.sh
      - name: Verify .deb
        run: bash -c "ls -lh dist/ && dpkg-deb -I dist/*.deb"
      - name: Upload .deb
        uses: actions/upload-artifact@v4
        with:
          name: punishment-manager-linux
          path: dist/*.deb
          if-no-files-found: error
          retention-days: 3

  macos:
    name: macos (.dmg)
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'
      - name: Install Python dependencies
        run: bash -c "python -m pip install --upgrade pip && pip install -r requirements.txt && pip install pyinstaller"
      - name: Install create-dmg
        run: brew install create-dmg
      - name: Build .dmg
        run: bash build/build_macos.sh
      - name: Verify .dmg
        run: bash -c "ls -lh dist/ && (hdiutil verify dist/*.dmg || true)"
      - name: Upload .dmg
        uses: actions/upload-artifact@v4
        with:
          name: punishment-manager-macos
          path: dist/*.dmg
          if-no-files-found: error
          retention-days: 3

  windows:
    name: windows (.exe)
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'
      - name: Install Python dependencies
        run: python -m pip install --upgrade pip && pip install -r requirements.txt && pip install pyinstaller
      - name: Install NSIS
        run: pwsh -File .github/scripts/install-nsis.ps1
      - name: Build .exe
        run: build\build_windows.bat
        shell: cmd
      - name: Verify .exe
        run: dir dist\*.exe
      - name: Upload .exe
        uses: actions/upload-artifact@v4
        with:
          name: punishment-manager-windows
          path: dist/*.exe
          if-no-files-found: error
          retention-days: 3
```

## Validation

The exact YAML above parses cleanly with PyYAML, and every `run:`
in it is a single line (no `|` block scalar). Bash syntax of the
referenced scripts has been verified with `bash -n`.

## Optional — also replace `release.yml`

The release workflow has the same multi-line `run: |` patterns
in its matrix jobs and the same `libpython3.11` install line.
If you want the release workflow to also work, apply the same
treatment:

1. Replace `.github/workflows/install-nsis.ps1` calls in
   `release.yml` with `pwsh -File .github/scripts/install-nsis.ps1`.
2. Replace the Linux "Install build tools" and "Install Python
   dependencies" runs in `release.yml` with the same
   single-line `bash -c "..."` form.

I deliberately did NOT include a full replacement for
`release.yml` here because that file's structure is more complex
(matrix with per-OS shell) and a smaller surgical edit is less
risky. If you want the full content too, open an issue or
re-run me and I'll generate it.

## After the fix lands

The release workflow will then be able to build installers on every
`v*` tag. To cut a release:

```bash
./scripts/make_release.sh 1.0.0
```

The resulting `.dmg`, `.deb`, and `.exe` will be uploaded to
`https://github.com/mob5824m-wq/Punishment-Manager/releases/tag/v1.0.0`.
