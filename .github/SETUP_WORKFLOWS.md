# Setting up the GitHub Actions workflows

This repo includes GitHub Actions workflow files that build the
native installers (as a sanity check on every push) and attach them
to a GitHub Release (when you cut a tag).

## What's included

| File | Purpose |
|------|---------|
| `.github/workflows/build.yml` | Sanity-checks the build on every push and PR. Produces three platform artifacts (`.deb`, `.dmg`, `.exe`) for 3 days. |
| `.github/workflows/release.yml` | Builds installers on every `v*` tag and attaches them to a GitHub Release. **(Currently broken — see below.)** |
| `.github/scripts/install-linux-deps.sh` | Helper for the Linux runner: installs `libpython3.11`, `fakeroot`, `dpkg`, `lintian`. |
| `.github/scripts/install-windows-deps.ps1` | Helper for the Windows runner: ensures a full Python with `python3.lib`. |
| `.github/scripts/install-nsis.ps1` | Helper for the Windows runner: downloads and installs NSIS 3.10 portably and exports the path to subsequent steps via `$GITHUB_ENV`. |

All five files are committed and on `main` via PR #1.

## A note about the agent's GitHub App token

The agent's GitHub App token has `contents: write` (so it can push
normal code) but not `workflows` (so it can't add or modify files
under `.github/workflows/`). This is a security default GitHub
enforces to prevent a compromised integration from adding a
malicious workflow that exfiltrates secrets.

In practice this means:

- `build.yml` is on `main` and **passing** on all three platforms.
- `release.yml` is on `main` but is **broken** — it contains a
  YAML expression error on line 83:
  ```yaml
  shell: ${{ matrix.shell }}   # 'matrix' is not a valid context
                                # in a step's `shell:` field
  ```
  GitHub rejects the workflow file at parse time, so the workflow
  never runs and never creates a release. The error is visible at
  the top of any run:
  > Invalid workflow file: `.github/workflows/release.yml#L1`
  > (Line: 83, Col: 16): Unrecognized named-value: 'matrix'.

  A maintainer with `workflows` permission can fix this by replacing
  the matrix job with three separate per-platform jobs (the same
  pattern `build.yml` uses). The agent's PR #1 already has the
  corrected `release.yml`; the agent just can't push it.

## Releases

The **v1.0.0** release is live at
<https://github.com/mob5824m-wq/Punishment-Manager/releases/tag/v1.0.0>.
It was created manually with `gh release create` and has the source
archives GitHub auto-generates for every tag:

- [Source code (zip)](https://github.com/mob5824m-wq/Punishment-Manager/archive/refs/tags/v1.0.0.zip)
- [Source code (tar.gz)](https://github.com/mob5824m-wq/Punishment-Manager/archive/refs/tags/v1.0.0.tar.gz)

**The release is missing the binary installers (`.deb`, `.dmg`,
`.exe`).** This is because the agent's sandbox can't reach
`uploads.github.com` to upload them. Three ways to fix this:

1. **Fix `release.yml`** (per the bug above), push a `v1.0.1` tag,
   and the workflow will build the installers and attach them to
   the new release.
2. **Re-run the `build` workflow** on the `v1.0.0` commit, then
   download the artifacts from the Actions tab and drag-and-drop
   them onto the release page in the web UI. The web-UI upload
   goes through `github.com`, which is reachable from anywhere.
3. **Build the installers locally** on a Linux / macOS / Windows
   host using `build/build_linux.sh`, `build/build_macos.sh`, or
   `build\build_windows.bat`, then drag-and-drop them onto the
   release page.

## How to cut a release

```bash
./scripts/make_release.sh 1.0.0
```

That validates the working tree, creates an annotated `v1.0.0` tag,
and pushes it. (The release workflow should then create a GitHub
Release and attach the installers — but only after the `release.yml`
fix above is in place.)

A plain version like `1.0.0` becomes a normal release. If you push
a `v1.0.0-rc1` tag (anything with a hyphen), the release is marked
as a prerelease automatically.

## Verifying the workflows work

1. **Build workflow** runs on every push / PR. Watch it at
   <https://github.com/mob5824m-wq/Punishment-Manager/actions>.

2. **Release workflow** runs on `v*` tags. To trigger it manually
   (once the workflow is fixed):
   ```bash
   git tag v0.1.0-test
   git push origin v0.1.0-test
   ```
   Then check the Actions tab. If everything works, delete the
   test tag and cut a real one:
   ```bash
   git tag -d v0.1.0-test
   git push origin :refs/tags/v0.1.0-test
   ```

## Fixing `release.yml` (for a maintainer)

The minimum-viable fix: replace the matrix job with three explicit
per-platform jobs, like `build.yml` does. The full diff against
the current `release.yml` is in PR #1's branch
(`arena/019ffe80-punishment-manager`); the user just needs to
either:

- merge PR #1 (if the diff is in scope), or
- apply the diff by hand using the GitHub web editor (the path is
  exactly `.github/workflows/release.yml`; the leading dot must
  be there or GitHub silently ignores the file).
