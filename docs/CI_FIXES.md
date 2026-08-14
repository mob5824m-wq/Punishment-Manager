# Fixes needed for the CI workflows

## Current state

- Linux job: **fixed** — uses the helper script at
  `.github/scripts/install-linux-deps.sh`. The previous commit
  updated this successfully.
- Windows job: **broken** — the `Install NSIS` step still calls
  the old single-source script at
  `.github/workflows/install-nsis.ps1`, which is failing because
  SourceForge is rate-limiting CI traffic.
- Old script `.github/workflows/install-nsis.ps1` is still
  present. Don't try to **create** `.github/scripts/install-nsis.ps1`
  again — that path already exists. Just edit the existing file.

## Fix 1 — Update the existing `install-nsis.ps1`

1. Open
   https://github.com/mob5824m-wq/Punishment-Manager/blob/arena/019ffe80-punishment-manager/.github/workflows/install-nsis.ps1
2. Click the pencil icon to edit.
3. Press `Ctrl+A` / `Cmd+A` then `Delete` to clear the file.
4. Open the **existing** file at
   https://github.com/mob5824m-wq/Punishment-Manager/blob/arena/019ffe80-punishment-manager/.github/scripts/install-nsis.ps1
   and copy its entire contents (click "Raw" then `Ctrl+A` `Ctrl+C`).
5. Paste into the editor for the workflows/ version.
6. Commit with the message "Make NSIS install more robust".

## Fix 2 — Update the Windows job in `build.yml`

1. Open
   https://github.com/mob5824m-wq/Punishment-Manager/edit/arena/019ffe80-punishment-manager/.github/workflows/build.yml
2. Find the windows job. It currently has:
   ```yaml
         - name: Install NSIS
           shell: pwsh
           run: ./.github/workflows/install-nsis.ps1
   ```
3. Replace those three lines with a single-line run:
   ```yaml
         - name: Install NSIS
           run: pwsh -File .github/scripts/install-nsis.ps1
   ```
4. Commit with the message "Use the new NSIS install path".

## Why this is two small edits, not one big replacement

The previous "replace the whole file" approach kept tripping the
GitHub web editor's "strip leading whitespace" behavior on
multi-line `run: |` blocks. With these two surgical edits, you
only touch 3-4 lines in `build.yml` and the existing
`install-nsis.ps1` file's content. Both edits are single-line
in nature and don't have indentation that the editor can strip.

If the same "Implicit map keys" parse error comes back, the
likely cause is that the web editor is dropping one indent level
on the lines you pasted. In that case, retry by clicking the
file's edit button, manually re-indenting the lines you paste
(matching the surrounding context), and committing again.

## After both fixes land

The release workflow has the same `Install NSIS` pattern that
will also need updating, but only after `build.yml` is working
on all three platforms. Once `build.yml` is green, you can do a
similar two-line edit to `release.yml` to make the release
workflow work too.

To cut a release once everything is green:

```bash
./scripts/make_release.sh 1.0.0
```
