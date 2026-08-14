# Setting up the GitHub Actions workflows

This repo includes GitHub Actions workflow files that build the
native installers (as a sanity check on every push) and attach them
to a GitHub Release (when you cut a tag).

## What's included

| File | Purpose |
|------|---------|
| `.github/workflows/build.yml` | Sanity-checks the build on every push and PR. Produces three platform artifacts (`.deb`, `.dmg`, `.exe`) for 3 days. |
| `.github/workflows/release.yml` | Builds installers on every `v*` tag and attaches them to a GitHub Release. |
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

- `build.yml` is already on `main` and **passing** on all three
  platforms.
- `release.yml` is on `main` but was authored before the build
  pipeline was finalised. A handful of small fixes in
  `release.yml` (point NSIS install at `.github/scripts/`, point
  Linux at the helper script, drop the redundant duplicate
  `.github/workflows/install-nsis.ps1`) have to be applied by a
  human with `workflows` permission.
- For day-to-day work, the `build.yml` artifacts are the source
  of truth. The `Actions` tab on the PR shows a 3-day-retention
  download link for each `.deb` / `.dmg` / `.exe` after every
  commit, and that link is what users actually install from.

## How to cut a release

```bash
./scripts/make_release.sh 1.0.0
```

That validates the working tree, creates an annotated `v1.0.0` tag,
and pushes it. The release workflow picks up the tag, builds the
three installers in parallel, and attaches them to a new GitHub
Release at:

```
https://github.com/mob5824m-wq/Punishment-Manager/releases/tag/v1.0.0
```

A plain version like `1.0.0` becomes a normal release. If you push
a `v1.0.0-rc1` tag (anything with a hyphen), the release is marked
as a prerelease automatically.

## Verifying the workflows work

1. **Build workflow** runs on every push / PR. Watch it at
   `https://github.com/mob5824m-wq/Punishment-Manager/actions`.

2. **Release workflow** runs on `v*` tags. To trigger it manually:
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

## If `release.yml` fails on the first tag

The release workflow was committed before the build pipeline was
finalised, so it may need a one-time human fix-up. The most
common things to check are:

1. **NSIS install path:** the Windows step should call
   `pwsh -File .github/scripts/install-nsis.ps1` (which exports
   `MAKENSIS_PATH` via `$GITHUB_ENV`). The legacy
   `.github/workflows/install-nsis.ps1` does not.
2. **Linux deps:** the Linux step should call
   `bash $GITHUB_WORKSPACE/.github/scripts/install-linux-deps.sh`
   so it can fall back to `libpython3.11-dev` on Ubuntu 24.04+.
3. **Shell on Windows:** if the workflow uses a matrix with
   `shell: cmd`, make sure no `if/else if (...)` blocks are
   inside the Windows step (cmd.exe's parser misreads the `(` in
   `Program Files (x86)`).
