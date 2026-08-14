# Setting up the GitHub Actions workflows

This repo includes three GitHub Actions workflow files that build
the native installers and attach them to GitHub Releases. Because
the bot's GitHub token has restricted scopes and can't push files
to `.github/workflows/`, you'll need to add these files yourself
through the GitHub web UI (or by pushing them as a user with
`workflows` permission).

## What's included

| File | Purpose |
|------|---------|
| `.github/workflows/release.yml` | Builds installers on every `v*` tag and attaches them to a GitHub Release. |
| `.github/workflows/build.yml` | Sanity-checks the build on every push / PR. |
| `.github/workflows/install-nsis.ps1` | Helper for the Windows runner: downloads and installs NSIS 3.10 portably. |

## How to enable them

### Option A — Copy-paste via the web UI

1. Open the file you want to add in the GitHub web editor:
   - [`release.yml`](https://github.com/mob5824m-wq/Punishment-Manager/new/main/.github/workflows/release.yml?filename=.github%2Fworkflows%2Frelease.yml)
   - [`build.yml`](https://github.com/mob5824m-wq/Punishment-Manager/new/main/.github/workflows/build.yml?filename=.github%2Fworkflows%2Fbuild.yml)
   - [`install-nsis.ps1`](https://github.com/mob5824m-wq/Punishment-Manager/new/main/.github/workflows/install-nsis.ps1?filename=.github%2Fworkflows%2Finstall-nsis.ps1)
2. Paste the file contents.
3. Commit directly to `main`.

GitHub will pick up the workflows automatically. You should see a
green check on the next push.

### Option B — Local git push as a maintainer

If you have a personal access token or a maintainer account with
`workflows` permission:

```bash
cd /path/to/Punishment-Manager
git checkout main
cp .github/workflows/*.yml .github/workflows/*.ps1 . # already in working tree
git add .github/workflows/
git commit -m "Add release + build workflows"
git push origin main
```

### Option C — Use the GitHub CLI as a maintainer

```bash
gh workflow add release.yml
gh workflow add build.yml
```

(Or just create them via `gh api` against the contents API.)

## Cutting a release

Once the workflows are in place, you can cut a release with:

```bash
./scripts/make_release.sh 1.0.0
```

That validates the working tree, creates an annotated `v1.0.0` tag,
and pushes it. The release workflow picks up the tag, builds the
three installers, and attaches them to a new GitHub Release at:

```
https://github.com/mob5824m-wq/Punishment-Manager/releases/tag/v1.0.0
```

The release will be a normal release. If you push a `v1.0.0-rc1`
tag (anything with a hyphen), the release is marked as a prerelease
automatically.

## Why can't the agent push these files?

The GitHub App token used by the agent has `contents: write` (so
it can push normal code) but not `workflows` (so it can't add or
modify files in `.github/workflows/`). This is a security default
GitHub enforces to prevent compromised integrations from adding
malicious workflow files that exfiltrate secrets.

The fix is a one-time, ~30-second step for a human to copy these
files in. From then on, the agent can cut releases with
`./scripts/make_release.sh` without needing the special permission
because tag pushes don't require `workflows` write.

## Verifying the workflows work

After copying the files in:

1. **Build workflow** runs on every push / PR. You can watch it at
   `https://github.com/mob5824m-wq/Punishment-Manager/actions`.

2. **Release workflow** runs on `v*` tags. To trigger it manually:
   ```bash
   git tag v0.1.0-test
   git push origin v0.1.0-test
   ```
   Then check the Actions tab. If everything works, delete the test
   tag and cut a real one:
   ```bash
   git tag -d v0.1.0-test
   git push origin :refs/tags/v0.1.0-test
   ```
