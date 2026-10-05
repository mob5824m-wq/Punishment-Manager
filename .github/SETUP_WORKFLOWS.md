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

All five files are committed and on `main`.

## Workflow status

Both workflows work:

- **build.yml** runs on every push / PR and passes on all three platforms.
- **release.yml** runs on every `v*` tag, builds the `.deb` / `.dmg` / `.exe`
  and attaches them to the release. `v2.0.0` and `v2.1.0` were both produced
  this way; no manual drag-and-drop is needed.

The earlier `matrix.shell` parse error in `release.yml` (three jobs replaced
the matrix) is fixed on `main`; `docs/CI_FIXES.md` keeps the history.

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
the newest release, so users always get the fixed build.

## A note about the agent's GitHub App token

The agent's GitHub App token has `contents: write` (so it can push
normal code) but not `workflows` (so it can't add or modify files
under `.github/workflows/`). This is a security default GitHub
enforces to prevent a compromised integration from adding a
malicious workflow that exfiltrates secrets.

In practice this means:

- Workflow edits may have to reach `main` through a PR (or the web
  editor) rather than a direct push. Both `build.yml` and `release.yml`
  are correct on `main` now, so nothing is pending.

## Verifying the workflows work

1. **Build workflow** runs on every push / PR. Watch it at
   <https://github.com/mob5824m-wq/Sentinel/actions>.

2. **Release workflow** runs on `v*` tags. To smoke-test it with a
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
