# Setting up the GitHub Actions workflows

This repo includes GitHub Actions workflow files that build the
native installers (as a sanity check on every pull request), publish
them on every merge to `main`, and attach them to a GitHub Release
(when you cut a tag).

## What's included

| File | Purpose |
|------|---------|
| `.github/workflows/build-installers.yml` | Reusable workflow: builds the `.deb`, `.dmg` and `.exe` for **both architectures** — one matrix per platform, so amd64 and arm64 run side by side (ubuntu-24.04 / ubuntu-24.04-arm, macos-latest / macos-15-intel, windows-latest / windows-11-arm) — and uploads each as a workflow artifact. Holds the only copy of the platform build steps. |
| `.github/workflows/merge-release.yml` | Runs on every merge to `main`: calls the reusable build, then publishes the rolling `latest-build` prerelease and a `v<VERSION>-build.<run>` prerelease for that merge. |
| `.github/workflows/build.yml` | Sanity-checks the same build on pull requests and `arena/*` pushes (artifacts kept for 3 days). Main is deliberately not listed — merge-release.yml already builds it. |
| `.github/workflows/release.yml` | Builds installers on every `v*` tag and attaches them to a GitHub Release (skipping the generated `-build.` tags). |
| `.github/scripts/publish-merge-release.sh` | Publishes the merge releases (rolling + per-merge) from the downloaded artifacts. |
| `.github/scripts/publish-tag-release.sh` | Attaches the artifacts to the `v*` tag's release. |
| `.github/scripts/install-linux-deps.sh` | Helper for the Linux runners: installs `fakeroot`, `dpkg`, `lintian` and the `libpythonX.Y` matching the interpreter doing the build (so it works on amd64 and arm64 alike). |
| `.github/scripts/install-windows-deps.ps1` | Helper for the Windows runners: ensures a full Python with `python3.lib`, of the architecture being built (an emulated x64 interpreter is rejected in favour of the native ARM64 one). |
| `.github/scripts/install-nsis.ps1` | Helper for the Windows runner: downloads and installs NSIS 3.10 portably and exports the path to subsequent steps via `$GITHUB_ENV`. |

All of them are committed and on `main`.

## Workflow status

All four workflows work:

- **build.yml** runs on every pull request and `arena/*` push and passes on
  all three platforms, both architectures.
- **merge-release.yml** runs on every merge to `main`. It builds the same
  six installers and publishes them, so `main` always has a downloadable
  build without a version bump.
- **release.yml** runs on every `v*` tag, builds the `.deb` / `.dmg` / `.exe`
  for amd64/x86_64 and arm64, and attaches them to the release. `v2.0.0` and
  `v2.1.0` were both produced this way; no manual drag-and-drop is needed.

The earlier `matrix.shell` parse error in `release.yml` (three jobs replaced
the matrix) is fixed on `main`; `docs/CI_FIXES.md` keeps the history.

## What a merge to main publishes

`merge-release.yml` keeps two prereleases up to date through
`publish-merge-release.sh`:

| Release | Tag | Behaviour |
|---------|-----|-----------|
| Rolling | `latest-build` | The tag is force-moved to the newest merge and its six assets (three platforms × two architectures) are replaced (stale assets from an earlier version are removed), so one URL always has the newest installers. |
| Per merge | `v<VERSION>-build.<run_number>` | Created once per merge from the `VERSION` file and the workflow run number, so an older build stays downloadable after the next merge lands. |

Both are marked **prerelease**, so
`https://github.com/mob5824m-wq/Sentinel/releases/latest` keeps
pointing at the newest versioned release instead of at a build from `main`.
Merges queue (`cancel-in-progress: false`), so a burst of merges publishes
every build rather than cancelling all but the last one.

## Artifacts

Each build leg uploads one artifact, so a caller can pull a platform and
architecture without guessing:

| Runner | Artifact | Installer |
|--------|----------|-----------|
| `ubuntu-22.04` | `sentinel-linux-amd64` | `sentinel_<VERSION>_amd64.deb` |
| `ubuntu-22.04-arm` | `sentinel-linux-arm64` | `sentinel_<VERSION>_arm64.deb` |
| `macos-latest` (Apple Silicon) | `sentinel-macos-arm64` | `Sentinel-<VERSION>-arm64.dmg` |
| `macos-15-intel` | `sentinel-macos-x86_64` | `Sentinel-<VERSION>-x86_64.dmg` |
| `windows-latest` | `sentinel-windows-x64` | `Sentinel-Setup-<VERSION>.exe` |
| `windows-11-arm` | `sentinel-windows-arm64` | `Sentinel-Setup-<VERSION>-arm64.exe` |

Every leg sets `SENTINEL_TARGET_ARCH` and fails if the runner's interpreter is
not that architecture (`python scripts/check_arch.py host --expect ...`); the
build scripts verify the artifact they produced as well, so a leg cannot
publish a mislabelled installer. The arm64 runners are GitHub's standard
arm64 machines, free for public repositories.

The two Linux legs are pinned to **Ubuntu 22.04** (Python 3.10, the newest
jammy packages a `libpython` for, and the app supports 3.9+). PyInstaller
bundles the build runner's CPython runtime, so the runner's glibc is the
artifact's floor: jammy's 2.35 keeps the `.deb` runnable on Debian 12 —
Raspberry Pi OS 64-bit "Bookworm" — while Ubuntu 24.04's 2.39 would not.
`scripts/check_glibc.py` reads the requirement out of every ELF file in the
bundle and **fails the build above 2.36** (publishing the measured value as a
check annotation), so bumping the runner cannot silently drop older targets,
and the bundle is additionally executed inside `debian:bookworm-slim` and
`debian:trixie-slim` when Docker is available on the runner.

## Cutting a release

`VERSION` at the project root is the single source of truth for the version, so
the artifact filenames, the `.app` plist and the NSIS metadata can't drift from
the tag.

```bash
echo 2.1.1 > VERSION
git commit -am "chore: bump version to 2.1.1" && git push origin main
./scripts/make_release.sh 2.1.1     # refuses to tag if VERSION and the tag disagree
```

A plain version becomes a normal release; a `v2.2.0-rc1` style tag is marked
prerelease. `./scripts/make_release.sh` is idempotent-safe: it checks the tree
is clean, that the tag doesn't exist, and that the tag matches `VERSION` before
creating anything.

This is only needed for a *versioned* release. Merges to `main` are published
automatically (see "What a merge to main publishes" above), so a fix is
downloadable as `latest-build` as soon as it lands — the tag is what marks it
as the stable, version-named build.

## Releases

Live releases:

| Tag | Installers | Notes |
|-----|-----------|-------|
| `v3.0.0` | `.deb`, `.dmg`, `.exe` attached by CI | Current. Sentinel: everything moves under the `/manage` command group, the app/data directories are renamed to `sentinel`, env vars become `SENTINEL_*`, and the dashboard is reskinned. Existing installs must move `config.json` + `punishments.db` and reinstall the service - see the README's upgrade table. |
| `v2.1.1` | `.deb`, `.dmg`, `.exe` attached by CI | Superseded by v3.0.0. Path resolution skips a candidate the user can't access instead of stopping there: portable installs next to a `.deb`'s `0750 /etc/sentinel` find their own `config.json` again, and the startup note names the skipped candidate instead of saying "resolution failed". |
| `v2.1.0` | `.deb`, `.dmg`, `.exe` attached by CI | Fixes packaged installs crashing at startup (`PermissionError` on `/opt/sentinel/_internal/data`). Safe to run; superseded by v2.1.1. |
| `v2.0.0` | attached by CI | **Broken for installed builds** — crashes on first launch; superseded by v2.1.0. |
| `v1.0.0` | source archives only | Created by hand before `release.yml` worked. |

<https://github.com/mob5824m-wq/Sentinel/releases/latest> points at
the newest release, so users always get the fixed build. Because the merge
builds are prereleases, they are skipped by `/releases/latest` and by
`gh release list --exclude-pre-releases`; the rolling one lives at
<https://github.com/mob5824m-wq/Sentinel/releases/tag/latest-build>.

## A note about the agent's GitHub App token

The agent's GitHub App token has `contents: write` (so it can push
normal code) but not `workflows` (so it can't add or modify files
under `.github/workflows/`). This is a security default GitHub
enforces to prevent a compromised integration from adding a
malicious workflow that exfiltrates secrets.

In practice this means:

- Workflow edits may have to reach `main` through a PR (or the web
  editor) rather than a direct push. Every workflow file is correct on
  `main`, so nothing is pending.

## Verifying the workflows work

1. **Build workflow** runs on every pull request and `arena/*` push.
   Watch it at
   <https://github.com/mob5824m-wq/Sentinel/actions>.

2. **Merge release** runs on every merge to `main`; the quickest smoke test is
   to merge anything (even a docs change) and watch
   `merge-release.yml`. Afterwards:

   ```bash
   gh release view latest-build                       # rolling assets
   gh release list --limit 5                          # v2.5.0-build.NN entries
   ```

   `.github/scripts/publish-merge-release.sh` also runs offline against
   stubbed `gh`/`git` binaries in `tests/test_release_workflow.py`, which is
   the fastest way to check the tag/asset handling without pushing anything.

3. **Release workflow** runs on `v*` tags. To smoke-test it with a
   throwaway tag:
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
