# CI Fix Log

## State

| Job | Status | Notes |
|-----|--------|-------|
| macOS .dmg | ✅ passing | — |
| Linux .deb | ✅ passing | — |
| Windows .exe | ❌ failing | Getting further each round - PyInstaller + NSIS now reach the script phase. Need next error. |

## What I just fixed

The user-shared error was:
```
Invalid command: "SetBrandText"
Error in script "build\windows\installer.nsi" on line 31
```

`SetBrandText` is a NSIS 2 command that was removed in NSIS 3.
Removed.

Then I also removed:
- `!define MUI_ICON "icon.ico"` (the icon file doesn't exist in `build/`)
- `!define MUI_UNICON "icon.ico"` (same)
- `!define MUI_HEADERIMAGE` (required the missing header bitmap)
- `!define MUI_HEADERIMAGE_BITMAP "..."` (Chocolatey NSIS doesn't ship the Contrib\Graphics folder)
- `!define MUI_WELCOMEFINISHPAGE_BITMAP "..."` (same)

NSIS will use the default MUI look. The install will still be
functional, just less branded.

## What I need from you

The latest commit is `e1577df`. I expect the next run to either:

1. **Succeed** — in which case we have a working Windows build!
2. **Fail with a different NSIS error** — most likely the
   `EnVar::SetHKLM` / `EnVar::AddValue` plugin calls or the
   `WriteRegDWORD` / `SectionIn RO` syntax. Paste the new error
   and I'll fix it.

Open https://github.com/mob5824m-wq/Punishment-Manager/actions,
click into the most recent failed `build` run, then the
`windows (.exe)` job, then the `Build .exe` step. Copy the
last 30-50 lines of the log.

## What's likely left

- The `EnVar::SetHKLM` and `EnVar::AddValue` calls in the
  Install section require the EnVar plugin which is bundled
  with NSIS 3 by default but might need a different path.
- The `SectionIn RO` is fine.
- `WriteRegDWORD` is fine in NSIS 3.
- The `$INSTDIR\punishment-manager.exe` path is a single file
  (not a folder of files), so the `File /r` should work.

Most likely culprit if there's still an error: the EnVar
plugin macros. Those were marked as "use EnVar" but if the
plugin isn't loaded, they fail. Removing the EnVar PATH
manipulation (letting the install complete without modifying
PATH) would be a safe simplification since the user can
always add the install dir to PATH manually.
