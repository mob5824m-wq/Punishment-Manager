# Punishment Manager

A cross-platform [discord.py](https://discordpy.readthedocs.io/) bot that
temporarily swaps a user's role and posts Discord embeds to staff and the
punished user.

**Role flow**

```
(any roles) ──/punish apply──▶  Punish role  ──timer──▶  Post-punish role
```

* When a moderator runs `/punish apply`, the bot **adds the punish
  role** on top of whatever the user already has. The user keeps all
  their other roles.
* The bot posts a **staff embed** in the configured staff channel and
  **DMs the punished user an embed** with the same info.
* When the timer expires, the bot **removes the punish role** and
  **adds the post-punish role**, and posts a final staff embed.
* `/punish pardon` ends the punishment early by removing the punish /
  post-punish role and posts a final "pardon" embed to staff and a
  DM to the user.

**Protected users**

The bot refuses to punish:
* bots
* the bot itself
* users with the **Administrator** permission
* users with any **moderation permission** (`Moderate Members`,
  `Manage Guild`, `Kick Members`, `Ban Members`)
* users whose top role is equal to or higher than the bot's top role
* users holding the configured **staff role**

These checks run in `/punish apply` before any role change is made, so
even if a mod mis-clicks, nothing happens.

All active punishments are stored in a local SQLite database, so timers
survive a bot restart.

---

## 1. Requirements

* Python **3.9 or newer** (developed and tested on 3.11; works on 3.9+).
* A Discord application + bot token — see
  <https://discord.com/developers/applications>.
* The bot must be invited with at minimum:
  * **Manage Roles**
  * **Moderate Members** *(required by `/punish` for the runtime check)*
  * **Send Messages** *(for staff-channel embeds)*
  * **Use Application Commands**

> **Role order matters.** Drag the bot's role *above* all three configured
> roles in *Server Settings → Roles*, otherwise it cannot give or take them.

---

## 2. Setup (macOS, Linux, Windows)

The included helper scripts create a virtualenv, install dependencies,
**run the interactive installer** on first launch, then start the bot.

| OS       | Script                 |
|----------|------------------------|
| macOS    | `./scripts/run_mac.sh`    |
| Linux    | `./scripts/run_linux.sh`  |
| Windows  | `scripts\run_windows.bat` |

The installer (`installer.py`) is also runnable on its own:

```bash
python3 installer.py
```

It prompts for:

| Field             | Where to find it                                      |
|-------------------|-------------------------------------------------------|
| `bot_token`       | Discord Developer Portal -> your app -> Bot -> Token  |
| `server_id`       | Right-click the server icon -> Copy Server ID         |
| `punish_role_id`  | Right-click the role -> Copy Role ID                  |
| `post_role_id`    | "                                                    |
| `staff_role_id`   | Optional. Members with this role (and any user with admin/moderator permissions) cannot be punished. |
| `staff_channel_id`| Right-click the channel -> Copy Channel ID (optional) |
| `dm_user`         | y / n (default y)                                    |

The installer writes the result to `config.json` with `0600` permissions
on Unix so the token isn't world-readable. Re-running it preserves any
field you skip (just press Enter).

**Manual setup** (if you'd rather do it yourself):

```bash
python3 -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
python3 installer.py    # or just edit config.json by hand
python3 bot.py
```

You can also set `DISCORD_TOKEN` as an environment variable and the
launcher will skip the token prompt.

---

## 3. Configure the bot

You can configure the bot two ways: with the interactive installer
(recommended for first-time setup), or with the in-Discord `/setup`
command (for tweaking things later).

### Option A — interactive installer

```bash
python3 installer.py
```

This writes `config.json` with everything the bot needs.

### Option B — in-Discord `/setup` (easiest to change roles later)

1. Create the punish and post-punish roles in your server, e.g. `Punished`, `Suspended`.
2. (Optional) Create a `Staff` role - anyone with this role will be protected from punishment.
3. Create a "staff-logs" text channel and make sure the bot can post in it.
4. As a server administrator, run:

   ```
   /setup punish_role:@Punished post_role:@Suspended
              staff_role:@Staff staff_channel:#staff-logs dm_user:true
   ```

   `staff_role`, `staff_channel`, and `dm_user` are optional; the two
   role arguments are required.

### Option C — edit `config.json` directly

```json
{
  "bot_token":        "YOUR-BOT-TOKEN",
  "server_id":        987654321098765432,
  "punish_role_id":   222222222222222222,
  "post_role_id":     333333333333333333,
  "staff_role_id":    555555555555555555,
  "staff_channel_id": 444444444444444444,
  "dm_user":          true
}
```

The `bot_token`, `server_id`, and the role ids go at the top level
(single-server shape). The `guilds` / `token` / `log_channel_id` keys
below them are a legacy multi-server shape and are still respected for
backwards compatibility.

To find ids: enable Developer Mode in *Settings -> Advanced*, then
right-click the server/role/channel and choose "Copy ... ID".

---

## 4. Run it

```bash
# macOS / Linux
./scripts/run_mac.sh     # or run_linux.sh

# Windows
scripts\run_windows.bat
```

You should see:

```
[INFO] punishment_manager: Database initialised at data/punishments.db
[INFO] punishment_manager: Synced N global command(s).
[INFO] punishment_manager: Logged in as YourBot (id=...)
```

Slash commands may take up to a few minutes to appear globally the first
time. To make them appear instantly in one server, change
`await self.tree.sync()` to `await self.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))`
in `setup_hook`.

---

## 5. Commands

The bot uses a single slash command group plus a one-off admin command.

| Command              | Who can use it                  | What it does |
|----------------------|---------------------------------|--------------|
| `/punish apply`      | Members with *Moderate Members* | Strips the normal role, gives the punish role for a duration. Posts a staff embed and DMs the user. |
| `/punish pardon`     | Members with *Moderate Members* | Ends the punishment early and restores the normal role. Posts a staff embed and DMs the user. |
| `/punish status`     | Anyone                          | Shows the server configuration and a list of active punishments. Pass a `user` to see that user's active status + history. |
| `/setup`             | Server administrators           | Configures the three roles, the staff channel, and DM behavior. |

### `/punish apply` options

* `user` — the user to punish.
* `duration` — how long. Examples: `30m`, `2h`, `1d`, `1d12h`, `90`
  *(bare numbers are interpreted as minutes)*.
* `reason` — optional, shown in the staff embed, the DM embed, and the DB.

The maximum duration is 30 days. The minimum is 5 seconds.

---

## 6. Embeds

**Staff channel embed** (on `/punish apply`):

* Title: "Member punished"
* Color: orange
* Fields: User (mention + id), Moderator, Duration, Reason, Started
  (Discord timestamp), Ends (Discord timestamp + relative)
* Thumbnail: the punished user's avatar
* Footer: "User ID: ..."

**Punished-user DM embed** (on `/punish apply`):

* Title: "You've been punished in `<server name>`"
* Color: red
* Fields: Duration, Reason, Started, Ends, Issued by
* Friendly message pointing the user to talk to a mod if they think it
  was a mistake

**Staff channel embed** (on timer expiry):

* Title: "Punishment timer expired"
* Color: blue
* Description showing the user moved from punish -> post role
* Started (relative time)

**Staff channel embed** (on `/punish pardon`):

* Title: "Member pardoned"
* Color: green
* Fields: User, Moderator, Outcome

**Pardoned-user DM embed**:

* Title: "Your punishment in `<server name>` has been lifted"
* Color: green
* Description confirming roles are restored
* "Issued by" field

---

## 7. Building native installers

The bot can be packaged as a `.dmg` (macOS), `.exe` installer (Windows),
or `.deb` (Linux). Each platform must be built on its own host — there
is no cross-compile.

| Platform | Build script                | Output                                  |
|----------|------------------------------|------------------------------------------|
| macOS    | `build/build_macos.sh`       | `dist/PunishmentManager-1.0.0.dmg`       |
| Linux    | `build/build_linux.sh`       | `dist/punishment-manager_1.0.0_amd64.deb`|
| Windows  | `build\build_windows.bat`    | `dist\PunishmentManager-Setup-1.0.0.exe` |

All three flow through `build/pyinstaller.spec` which bundles
`bot.py` + `installer.py` into a single self-contained binary, then
wraps that binary in the OS-native installer format.

### macOS

Requirements: Python 3.9+, `pyinstaller`, optionally `create-dmg`
(`brew install create-dmg`) for a styled `.dmg` window. Otherwise
`hdiutil` is used as a fallback.

```bash
build/build_macos.sh
open dist/PunishmentManager-1.0.0.dmg
```

The result is a real `.app` bundle (`Punishment Manager.app`) inside a
`.dmg` that users can drag into `/Applications`. The bundle id is
`com.arena.punishment-manager` and the binary is at
`Punishment Manager.app/Contents/MacOS/punishment-manager`.

To codesign, uncomment the `codesign` lines in `build_macos.sh` and
set `CODESIGN_IDENTITY` to your Developer ID.

### Linux (.deb)

Requirements: Python 3.9+, `pyinstaller`, `dpkg`, `fakeroot`,
`lintian` (optional).

```bash
build/build_linux.sh
sudo dpkg -i dist/punishment-manager_1.0.0_amd64.deb
sudo systemctl start punishment-manager
```

The package installs the bot to `/opt/punishment-manager/`, symlinks
the binary into `/usr/bin/`, registers a desktop entry, and installs
a systemd unit (`/lib/systemd/system/punishment-manager.service`).
The unit is enabled (not started) by `postinst`; the user runs the
bot once to configure it, then enables the service.

### Windows

Requirements: Python 3.9+, `pyinstaller`, NSIS 3.x in PATH.

```
build\build_windows.bat
dist\PunishmentManager-Setup-1.0.0.exe
```

The NSIS installer copies the PyInstaller output to
`%ProgramFiles64%\Punishment Manager`, creates Start Menu and Desktop
shortcuts, adds the install dir to `PATH`, and registers an
uninstaller in Add/Remove Programs.

To sign with `signtool`, uncomment the `signtool sign` line in
`build_windows.bat`.

### Background service

After install, the bot can be configured to start automatically:

```bash
# Linux (after sudo dpkg -i ...):
sudo punishment-manager --install-service
sudo systemctl start punishment-manager
sudo systemctl enable punishment-manager

# macOS:
sudo punishment-manager --install-service
launchctl load -w ~/Library/LaunchAgents/com.arena.punishment-manager.plist

# Windows (run as Administrator):
punishment-manager.exe --install-service
sc start PunishmentManager
```

The `--install-service` command registers the bot with the OS service
manager (systemd / launchd / NSSM). The bot will then start on boot
and restart automatically if it crashes. `--uninstall-service`
removes the registration.

For development, you can also just run the binary directly with no
arguments — it will auto-run the installer on first launch.

## 8. Files

```
Punishment-Manager/
├── bot.py                  # the bot
├── installer.py            # interactive first-run installer
├── requirements.txt
├── config.json             # token + per-guild role config
├── .gitignore
├── scripts/                # dev launchers
│   ├── run_mac.sh
│   ├── run_linux.sh
│   └── run_windows.bat
├── build/                  # native installer build artifacts
│   ├── pyinstaller.spec
│   ├── build_macos.sh
│   ├── build_linux.sh
│   ├── build_windows.bat
│   ├── linux/
│   │   ├── punishment-manager.service
│   │   ├── punishment-manager.desktop
│   │   ├── postinst
│   │   ├── prerm
│   │   └── postrm
│   ├── macos/
│   │   ├── Info.plist
│   │   └── com.arena.punishment-manager.plist
│   └── windows/
│       └── installer.nsi
└── data/                   # created at runtime
    ├── punishments.db
    └── bot.log
```

## 9. Troubleshooting

* **"Installer exited without saving a config"** — re-run
  `python3 installer.py` and answer the prompts. If your terminal hides
  input (e.g. when piping from a file), the token will be read as empty
  and you'll be asked again.
* **`Missing Permissions`** when running `/punish apply` — the bot's
  role isn't above the configured roles. Move it up in
  *Server Settings → Roles*.
* **The user never receives the DM** — they have DMs disabled or the
  bot is blocked. Set `dm_user: false` in `/setup` to suppress the DM
  attempt, or ask the user to enable DMs.
* **Slash commands don't appear** — global commands can take up to an
  hour to propagate. As a fast alternative, change
  `await self.tree.sync()` to
  `await self.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))` in
  `setup_hook`.
* **No token / Login failed** — make sure `DISCORD_TOKEN` is set or
  `config.json` has a non-empty `bot_token` (or the legacy `token`).
* **`.deb` build complains about `dpkg-deb` or `fakeroot`** — install
  them with `sudo apt install fakeroot dpkg`.
* **NSIS errors with `MUI2.nsh` not found** — install NSIS 3.x
  (https://nsis.sourceforge.io) and ensure `${NSISDIR}` is set.
* **The `.dmg` says "this app is from an unidentified developer"** —
  that means the `.app` isn't codesigned. Either sign it with a
  Developer ID or right-click the `.app` and choose "Open" the
  first time to bypass Gatekeeper.

## 10. License

MIT.
