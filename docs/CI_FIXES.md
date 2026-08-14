# Fixes needed for the CI workflows

The release and build workflows are running, but two of the three
platform jobs are failing because of toolchain issues that depend
on which Ubuntu / Windows runner image GitHub has shipped. These
fixes need to be applied by a maintainer with the `workflows`
permission (the agent's GitHub App token can't push to
`.github/workflows/`).

## What's failing

The latest CI run showed:
- **macOS .dmg** job: passed
- **Linux .deb** job: failed on "Install build dependencies"
- **Windows .exe** job: failed on "Install NSIS"

Both failures are environment issues, not logic bugs in the workflows.

## Strategy: put scripts in their own files

The previous fix attempt had you paste multi-line `run: |` blocks
into the GitHub web editor. The web editor strips leading
whitespace on paste, which silently breaks YAML literal block
scalars. The result was confusing parse errors like
"Unexpected value" or "StringToken was expected".

**The fix for the fix**: put all the multi-line logic into separate
files under `.github/scripts/`, and have the workflow invoke them
with a single-line `run:`. This commit adds those scripts; you
just need to update `build.yml` to call them.

Two new files have been added to the branch:
- `.github/scripts/install-linux-deps.sh` (the apt-get install logic)
- `.github/scripts/install-nsis.ps1` (a more robust NSIS installer)

Both files are already in this commit.

## Fix 1 — Update `.github/workflows/build.yml`

In `.github/workflows/build.yml`, find the "Install build
dependencies" step in the linux job. It currently looks like:

```yaml
      - name: Install build dependencies
        run: |
          sudo apt-get update
          sudo apt-get install -y libpython3.11 fakeroot dpkg lintian
```

**Replace it** with this single-line `run:` (no block scalar, no
indentation to lose on paste):

```yaml
      - name: Install build dependencies
        run: bash $GITHUB_WORKSPACE/.github/scripts/install-linux-deps.sh
```

Then find the "Install Python dependencies" step in the same job
and replace it with a single-line run too. The current version:

```yaml
      - name: Install Python dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          pip install pyinstaller
```

becomes:

```yaml
      - name: Install Python dependencies
        run: bash -c "python -m pip install --upgrade pip && pip install -r requirements.txt && pip install pyinstaller"
```

Same for "Verify .deb" (currently `run: | ls -lh dist/ dpkg-deb -I
dist/*.deb`) which becomes
`run: bash -c "ls -lh dist/ && dpkg-deb -I dist/*.deb"`.

## Fix 2 — Update the Windows NSIS install path

The current build.yml calls the old install-nsis.ps1 at
`.github/workflows/install-nsis.ps1`. That file uses a single
SourceForge URL and is failing. The new one is at
`.github/scripts/install-nsis.ps1` and has retry logic plus
three fallback sources.

**Two changes** are needed in the windows job's "Install NSIS" step:

1. Change the path from `.github/workflows/install-nsis.ps1` to
   `.github/scripts/install-nsis.ps1`.
2. Change `shell: pwsh` to `run: pwsh -File ...`.

Current:
```yaml
      - name: Install NSIS
        shell: pwsh
        run: ./.github/workflows/install-nsis.ps1
```

Replace with:
```yaml
      - name: Install NSIS
        run: pwsh -File .github/scripts/install-nsis.ps1
```

(No `shell:` line — the `pwsh` in the `run:` is the executable,
so the default shell doesn't need to be set.)

## Optional — delete the old `install-nsis.ps1`

The old `.github/workflows/install-nsis.ps1` is no longer
referenced after applying fix 2. You can delete it via the web UI
to keep the repo tidy.

## How to apply all changes

Two files to edit, one file to delete:

1. **Edit `.github/workflows/build.yml`**:
   - Replace the linux "Install build dependencies" run block.
   - Replace the linux "Install Python dependencies" run block.
   - Replace the linux "Verify .deb" run block.
   - Replace the windows "Install NSIS" step (path + syntax).
2. **Edit `.github/workflows/build.yml`** to use single-line
   runs in the macos job too (optional but recommended):
   - "Install Python dependencies" run block.
   - "Verify .dmg" run block.
3. **Delete `.github/workflows/install-nsis.ps1`** (optional).

Each edit is a single click + paste in the GitHub web editor. If
any paste gets mangled, just retry — the new scripts on disk are
self-contained and the workflow just needs to call them.

## After the fix lands

The release workflow will then be able to build installers on every
`v*` tag. To cut a release:

```bash
./scripts/make_release.sh 1.0.0
```

The resulting `.dmg`, `.deb`, and `.exe` will be uploaded to
`https://github.com/mob5824m-wq/Punishment-Manager/releases/tag/v1.0.0`.
