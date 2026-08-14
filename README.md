# Punishment Manager

A cross-platform [discord.py](https://discordpy.readthedocs.io/) bot that
temporarily swaps a user's role.

**Role flow**

```
Normal role  ──/punish──▶  Punish role  ──timer──▶  Post-punish role
```

* When a moderator runs `/punish`, the bot **removes the user's normal role**
  and **adds the punish role**.
* When the timer expires, the bot **removes the punish role** and **adds the
  post-punish role**.
* `/pardon` ends the punishment early and restores the normal role.

All active punishments are stored in a local SQLite database, so timers
survive a bot restart.

---

## 1. Requirements

* Python **3.9 or newer** (developed and tested on 3.11; works on 3.9+).
* A Discord application + bot token — see
  <https://discord.com/developers/applications>.
* The bot must be invited with at minimum:
  * **Manage Roles**
  * **Moderate Members** *(only required if you want the `moderate_members`
    permission check on `/punish`)*
  * **Send Messages**
  * **Use Application Commands**

> **Role order matters.** Drag the bot's role *above* all three configured
> roles in *Server Settings → Roles*, otherwise it cannot give or take them.

---

## 2. Setup (macOS, Linux, Windows)

The included helper scripts create a virtualenv, install dependencies, and
launch the bot. Pick the one that matches your OS.

| OS       | Script             |
|----------|--------------------|
| macOS    | `scripts/run_mac.sh`   |
| Linux    | `scripts/run_linux.sh` |
| Windows  | `scripts/run_windows.bat` |

**Manual setup** (if you'd rather do it yourself):

```bash
python3 -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

---

## 3. Configure the bot

You can configure the bot two ways: in `config.json`, or with the in-Discord
`/setup` command (recommended for most people).

### Option A — in-Discord `/setup` (easiest)

1. Create three roles in your server, e.g. `Member`, `Punished`, `Suspended`.
2. Run `python bot.py` once with a valid token (see step 4) so the slash
   commands appear in your server.
3. As a server administrator, run:

   ```
   /setup normal_role:@Member punish_role:@Punished post_role:@Suspended
   ```

   (Optionally pass a `log_channel:` for punishment notifications.)

### Option B — edit `config.json`

```json
{
  "token": "YOUR-BOT-TOKEN",
  "log_channel_id": 123456789012345678,
  "guilds": {
    "987654321098765432": {
      "normal_role_id":  111111111111111111,
      "punish_role_id":  222222222222222222,
      "post_role_id":    333333333333333333
    }
  }
}
```

`guild_id` and role ids can be obtained by right-clicking the server/role in
Discord with developer mode enabled (*Settings → Advanced → Developer Mode*).

---

## 4. Set the bot token

Either:

* put it in `config.json` under `"token"`, **or**
* set the `DISCORD_TOKEN` environment variable (preferred for production):

  ```bash
  # macOS / Linux
  export DISCORD_TOKEN=YOUR-TOKEN
  # Windows (PowerShell)
  $env:DISCORD_TOKEN = "YOUR-TOKEN"
  ```

---

## 5. Run it

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
time. To make them appear instantly in one server, set the `guild_ids`
argument in `bot.tree.sync()` (see comments in `bot.py`).

---

## 6. Commands

The bot uses a single slash command group plus a one-off admin command.

| Command         | Who can use it                  | What it does |
|-----------------|---------------------------------|--------------|
| `/punish apply` | Members with *Moderate Members* | Strips the normal role, gives the punish role for a duration. |
| `/punish pardon`| Members with *Moderate Members* | Ends the punishment early and restores the normal role. |
| `/punish status`| Anyone                          | Shows the server configuration and a list of active punishments. Pass a `user` to see that user's active status + history. |
| `/setup`        | Server administrators           | Configures the three roles (and optional log channel) for the server. |

### `/punish apply` options

* `user` — the user to punish.
* `duration` — how long. Examples: `30m`, `2h`, `1d`, `1d12h`, `90`
  *(bare numbers are interpreted as minutes)*.
* `reason` — optional, shown in the log channel and stored in the DB.

The maximum duration is 30 days. The minimum is 5 seconds.

---

## 7. Files

```
Punishment-Manager/
├── bot.py                  # The bot
├── requirements.txt
├── config.json             # Token + per-guild role config
├── .gitignore
├── scripts/
│   ├── run_mac.sh
│   ├── run_linux.sh
│   └── run_windows.bat
└── data/                   # Created at runtime
    ├── punishments.db
    └── bot.log
```

---

## 8. Troubleshooting

* **`Missing Permissions`** when running `/punish` — the bot's role isn't
  above the configured roles. Move it up in *Server Settings → Roles*.
* **Slash commands don't appear** — global commands can take up to an hour
  to propagate. As a fast alternative, change
  `await self.tree.sync()` to `await self.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))`
  in `setup_hook`.
* **No token / Login failed** — make sure `DISCORD_TOKEN` is set or the
  `token` field in `config.json` is filled in. The token must be kept
  private.

---

## 9. License

MIT.
