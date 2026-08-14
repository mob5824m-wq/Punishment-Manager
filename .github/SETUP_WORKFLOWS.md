# Setting up the GitHub Actions workflows

This repo includes three GitHub Actions workflow files that build
the native installers and attach them to GitHub Releases. Because
the agent's GitHub App token has restricted scopes and can't push
files into `.github/workflows/`, you'll need to add these files
yourself through the GitHub web UI (or by pushing them as a user
with `workflows` permission).

The files are already in your working tree (look in
`.github/workflows/`). The instructions below explain how to get
them onto the `main` branch.

## What's included

| File (in your working tree) | Purpose |
|------|---------|
| `.github/workflows/release.yml` | Builds installers on every `v*` tag and attaches them to a GitHub Release. |
| `.github/workflows/build.yml` | Sanity-checks the build on every push / PR. |
| `.github/workflows/install-nsis.ps1` | Helper for the Windows runner: downloads and installs NSIS 3.10 portably. |

## How to enable them

### Option A — Web UI (fastest, ~1 minute)

1. Go to https://github.com/mob5824m-wq/Punishment-Manager/tree/main/.github
2. Click **Add file → Create new file**.
3. In the "Name your file..." box, type the path **relative to the
   current directory**. Since you're already inside `.github/`,
   type just `workflows/release.yml`. **Do not** include the leading
   `.github/` prefix — GitHub adds it automatically, and including
   it will create a duplicate `.github/.github/workflows/` path that
   GitHub Actions does not pick up. (That's the bug this file's
   commit history documents.)
4. Open the file from your local checkout
   (`Punishment-Manager/.github/workflows/release.yml`) in any text
   editor, copy its entire contents, and paste into the GitHub
   editor.
5. Scroll down, leave "Commit directly to the `main` branch"
   selected, click **Commit new file**.
6. Repeat for `build.yml` and `install-nsis.ps1`.

GitHub will pick up the workflows automatically. You should see a
green check mark on the next push.

> **Path gotcha:** Workflows must live at exactly
> `.github/workflows/<name>.yml`. If a file ends up at
> `.github/.github/workflows/<name>.yml` instead, GitHub silently
> ignores it and the workflow never runs. The fix is to `git mv` the
> file to the correct path and push a new commit.

### Option B — Local git push as a maintainer

If you have a personal access token or a maintainer account with
`workflows` permission (the same permissions that let you approve
PRs that touch `.github/workflows/`):

```bash
cd /path/to/Punishment-Manager
git checkout main
# The workflow files should already be in the working tree from
# the agent's last commit. Stage and push them.
git add .github/workflows/
git status   # should show three new files staged
git commit -m "Add release + build workflows"
git push origin main
```

### Option C — Use the GitHub CLI as a maintainer

```bash
gh workflow add release.yml
gh workflow add build.yml
```

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

A plain version like `1.0.0` becomes a normal release. If you push
a `v1.0.0-rc1` tag (anything with a hyphen), the release is marked
as a prerelease automatically.

## Why can't the agent push these files?

The GitHub App token used by the agent has `contents: write`
(so it can push normal code) but not `workflows` (so it can't add
or modify files in `.github/workflows/`). This is a security
default GitHub enforces to prevent a compromised integration from
adding a malicious workflow that exfiltrates secrets.

The fix is a one-time, ~30-second step for a human to copy these
files in. From then on, the agent can cut releases with
`./scripts/make_release.sh` without needing the special permission,
because **pushing a tag** doesn't require `workflows` write — only
**creating or modifying workflow files** does.

## Verifying the workflows work

After copying the files in:

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
