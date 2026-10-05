# Sentinel

A cross-platform [discord.py](https://discordpy.readthedocs.io/) server
management bot: timed punishment roles, warnings, a published rules post with
reaction-role acceptance, reaction-role menus, and an authenticated
server-side web dashboard for managing connected servers, moderation,
warnings, rules, configuration and history.

Everything is one slash-command tree, `/manage` — so a moderator can learn the
whole bot by typing `/manage` and reading the list:

```
/manage punish        /manage warnings     /manage rules publish
/manage pardon        /manage status       /manage rules disable
/manage warn          /manage setup        /manage rules list
                                           /manage fixcommands
```

**Moderation: the role flow**

```
(any roles) ──/manage punish──▶  Punish role  ──timer──▶  Post-punish role
(any roles) ──/manage warn───▶  unchanged roles + a recorded warning
```

* When a moderator runs `/manage punish`, the bot **adds the punish
  role** on top of whatever the member already has. The member keeps all
  their other roles.
* The bot posts a **staff embed** in the configured staff channel and
  **DMs the punished member an embed** with the same info.
* When the timer expires, the bot **removes the punish role** and
  **adds the post-punish role**, and posts a final staff embed.
* `/manage pardon` ends the punishment early by removing the punish /
  post-punish role and posts a final "pardon" embed to staff and a
  DM to the member.

**Protected members**

The bot refuses to punish:
* bots
* the bot itself
* members with the **Administrator** permission
* members with any **moderation permission** (`Moderate Members`,
  `Manage Guild`, `Kick Members`, `Ban Members`)
* members whose top role is equal to or higher than the bot's top role
* members holding the configured **staff role**

These checks run in `/manage punish` before any role change is made, so
even if a mod mis-clicks, nothing happens.

All active punishments are stored in a local SQLite database, so timers
survive a bot restart. Warnings and completed punishments live in the same
database, which is what `/manage status`, `/manage warnings`, and the
dashboard's history read from.

**Who can run what**

`/manage` is visible to members with **Moderate Members**. The administrative
commands — `/manage setup`, `/manage fixcommands` and everything under
`/manage rules` — check for **Administrator** when they run, because Discord
applies a single permission to a whole command group.

---

## Download

Pre-built native installers are attached to every GitHub release:

[**Latest release →**](https://github.com/mob5824m-wq/Sentinel/releases/latest)

| Platform | File | Notes |
|----------|------|-------|
| **macOS**   | `Sentinel-X.Y.Z.dmg`             | Open the `.dmg`, drag the `.app` into `/Applications` |
| **Linux**   | `sentinel_X.Y.Z_amd64.deb`      | `sudo dpkg -i ...` and you're done |
| **Windows** | `Sentinel-Setup-X.Y.Z.exe`       | Run the installer; it adds the bot to your Start Menu |
| **Source**  | `Source code (zip)` / `Source code (tar.gz)` | For everyone who'd rather run from source |

Releases are produced automatically by GitHub Actions whenever a
`v*` tag is pushed. See `.github/workflows/release.yml` for the
build pipeline, and the `VERSION` file for where the number comes from.

Every merge to `main` is published too, without waiting for a version bump:

| Release | What it is |
|---------|------------|
| [`latest-build`](https://github.com/mob5824m-wq/Sentinel/releases/tag/latest-build) | Rolling prerelease whose three installers are replaced on every merge — one URL always has the newest build from `main` |
| `v<VERSION>-build.<run>` | One prerelease per merge (e.g. `v3.0.0-build.42`), so a specific build stays downloadable afterwards |

Both are marked *prerelease*, so
[`releases/latest`](https://github.com/mob5824m-wq/Sentinel/releases/latest)
keeps pointing at the newest versioned release rather than at an unreleased
build. `.github/workflows/merge-release.yml` runs this after each merge, and
`build.yml` sanity-checks the same build on pull requests.

> **Current release: v3.0.0** — the Sentinel rename. Read
> [Upgrading from Punishment Manager (v2)](#upgrading-from-punishment-manager-v2)
> before you update an install that already holds a config or database.
>
> Older note: the v1.0.0 and v2.0.0 installers crash on first launch on a
> packaged install (`PermissionError: [Errno 13] Permission denied:
> '/opt/sentinel/_internal/data'`), because they tried to create their
> database and log inside the read-only install directory. v2.1.0 stores that
> state in a writable per-platform location instead.

### Upgrading from Punishment Manager (v2)

Sentinel is Punishment Manager with a new name, one `/manage` command tree and
a reskinned dashboard. v3.0.0 is a clean cut rather than an in-place
migration, so give it five minutes:

| What changed                        | What to do |
|-------------------------------------|------------|
| Commands moved under `/manage`      | Nothing: the new tree is synced to every connected server at startup. `/punish apply` is now `/manage punish`, `/setup` is `/manage setup`, `/rules …` is `/manage rules …`. |
| Data moved to a `sentinel` directory (`/var/lib/sentinel`, `~/Library/Application Support/Sentinel`, `%LOCALAPPDATA%\Sentinel`, …) | Copy the old `config.json` and `punishments.db` into the new data dir, or re-run `sentinel --install`. |
| Service names are `sentinel` / `com.arena.sentinel` | Uninstall the old service first (`sudo punishment-manager --uninstall-service`), then `sudo sentinel --install-service`. |
| Environment variables are `SENTINEL_*` (were `PUNISHMENT_MANAGER_*`) | Update service units, containers and scripts. |

`sentinel --paths` prints the exact locations this build uses, and the log
names any location it had to fall back to.

---

## 1. Requirements

* Python **3.9 or newer** (developed and tested on 3.11; works on 3.9+).
* A Discord application + bot token — see
  <https://discord.com/developers/applications>.
* The bot must be invited with at minimum:
  * **Manage Roles** *(for punishment and rules-acceptance roles)*
  * **Moderate Members** *(required by `/manage`)*
  * **Send Messages** *(for staff-channel embeds and rules posts)*
  * **Embed Links** and **Add Reactions** *(for rules posts)*
  * **Use Application Commands**

Enable **Server Members Intent** in the Discord Developer Portal under
*Bot → Privileged Gateway Intents*; the bot uses it for member/role updates.

> **Role order matters.** Drag the bot's role *above* the punish, post-punish,
> and rules-acceptance roles in *Server Settings → Roles*, otherwise it cannot
> give or take them.

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
| `server_id`       | Optional for command sync. Enter it for the installer's single-server role settings; right-click the server icon -> Copy Server ID |
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
(recommended for first-time setup), or with the in-Discord `/manage setup`
command (for tweaking things later).

### Option A — interactive installer

```bash
python3 installer.py
```

This writes `config.json` with everything the bot needs.

### Option B — in-Discord `/manage setup` (easiest to change roles later)

1. Create the punish and post-punish roles in your server, e.g. `Punished`, `Suspended`.
2. (Optional) Create a `Staff` role - anyone with this role will be protected from punishment.
3. Create a "staff-logs" text channel and make sure the bot can post in it.
4. As a server administrator, run:

   ```
   /manage setup punish_role:@Punished post_role:@Suspended
              staff_role:@Staff staff_channel:#staff-logs dm_user:true
   ```

   `staff_role`, `staff_channel`, and `dm_user` are optional; the two
   role arguments are required.

### Configure rules and a reaction role

1. Create a basic `Verified` / `Member` role. Keep it below the bot's role and
   do not give it moderation or server-management permissions.
2. As a server administrator, publish the rules:

   ```text
   /manage rules publish channel:#rules role:@Verified rules_text:"1. Be respectful. 2. No spam or harassment."
   ```

   The bot posts this message, an embed containing the rules, and adds a ✅
   reaction:

   > By reacting to this you acknowledge the rules and will abide by them.

   Members who react receive the configured role; removing their reaction
   removes that role. Publishing again under the same name replaces that set's
   post. Existing role assignments are not changed by republishing; if you
   change the acceptance role, remove the old role from existing members as
   needed. `/manage rules disable` turns off reaction handling for a set and leaves
   assignments unchanged.

#### Several rule sets on one server

A server can run more than one rule set — a full "Server rules" post plus a
short "Event rules" or "Contest rules" post, each with its own channel, role
and text. Give each set a name and it stays independent:

```text
/manage rules publish channel:#rules role:@Verified name:"Server rules" rules_text:"1. Be respectful. 2. No spam."
/manage rules publish channel:#events role:@Events name:"Event rules" rules_text:"1. Keep chat on topic. 2. No spoilers."
```

* `/manage rules publish` with an existing name (case-insensitive) replaces only that
  set's post; other sets are untouched. The default name is `Server rules`, so
  existing single-post installs are unchanged.
* `/manage rules disable name:"Event rules"` disables one set — its post is marked
  disabled and its ✅ is removed, but roles already granted are left alone.
  With only one set published, `name` can be omitted.
* `/manage rules list` shows every set, its channel, its role and its message id.
* Reactions are routed by message, so ✅ on the "Event rules" post grants the
  events role and never the server-rules role.
* Up to 25 sets per server.

The prompt sentence lives in `rules.py` as `RULES_POST_CONTENT`, so
`/manage rules publish` and the dashboard's publish button always post the same
wording (edit it there to change it everywhere). Rules text can be up to
4,096 characters. The bot stores every set per server in `config.json`; no
manual config edit is needed, and a config written by an older version (a
single object instead of a list, no names) keeps working as one set named
`Server rules`.

The rules post is sent with mentions disabled, so no `@` in the rules can ping
anyone, and the post is only edited or deleted by the bot itself.

In the dashboard, the **Rules & reactions** page lists every published rule set
with **Edit** (rename it, move it to another channel, change the role, or
rewrite the text — the message is updated in place) and **Disable**, and the
editor below publishes a new one.

Need more than one reaction role per post, or a custom message of your own?
See [Reaction role menus](#reaction-role-menus-dashboard) below.

#### Markdown in the rules

The rules text is regular Discord Markdown, rendered by Discord's own client.
Anything Discord supports inside an embed works:

| Syntax | Result |
|--------|--------|
| `**bold**`, `*italic*` / `_italic_`, `__underline__`, `~~strikethrough~~`, `\|\|spoiler\|\|` | inline formatting |
| `# Heading`, `## Heading`, `### Heading` | headings (`####` and more are shown as text) |
| `- item`, `* item`, `1. item` | bulleted / numbered lists |
| `> quote`, `>>> quote` | block quotes (the second quotes everything after it) |
| `` `code` ``, ```` ```code``` ```` | inline code and code blocks |
| `[label](https://example.com)`, `<https://example.com>` | clickable links |
| `-# small note` | subtext |
| `<@user>`, `<@&role>`, `<#channel>` | mentions (rendered, never pinged) |

Discord does **not** render tables, images, task lists, horizontal rules
(`---`), `####`+ headings or nested lists inside an embed — those are shown
literally, so the bot's preview does not pretend otherwise.

**In the dashboard.** The Rules page has a formatting toolbar (bold, italic,
underline, strikethrough, spoiler, headings, lists, quote, code, code block,
link — with `Ctrl`/`Cmd` + `B`, `I`, `E` shortcuts) and a **live preview** that
shows the whole post exactly as Discord renders it: the prompt message, the
embed body, the embed title and the footer. The preview is rendered by the bot
(`discord_markdown.py`) rather than the browser, so what you see is what the
published post looks like. While you
type, the editor also flags syntax Discord would show as plain text — an
unclosed `**`, a `####` heading, a missing space after `#`, or a non-http link.

<sub>Want to verify the renderer without the browser?</sub>

```bash
python3 -c "from discord_markdown import render_markdown_html; print(render_markdown_html('# Rules\n**be kind**'))"
```

### Reaction role menus (dashboard)

The rules post grants one role for one ✅. For anything more — a ping-picker, a
colour menu, an events role — open the dashboard's **Rules & reactions** page
and use the **Reaction role posts** section. There is no limit on the number of
posts; each one is a separate bot message with its own mapping.

1. Pick the **post channel**.
2. Choose the **post style**: an embed (optional title + text) or a plain
   message.
3. Write the **message** members will read. It is regular Discord Markdown —
   the same formatting toolbar, live preview and linter as the rules editor —
   and it is posted with mentions disabled, so nothing in it can ping anyone.
   Leave it empty to publish the default prompt
   (`React with an emoji below to add or remove a role.`).
4. Add the **emoji → role pairs** (up to 20 per post). Type or paste any emoji,
   or a custom emoji as `name:id` / `<:name:id>`; the quick-add row and
   **Insert emoji list** button write the role key into the message for you.
5. Pick what each pair **does** when someone reacts:

   | Action | Reacting | Un-reacting |
   |--------|----------|-------------|
   | **Give role** (default) | hands the member the role | takes it back |
   | **Remove role** | strips the role from the member | hands it back |

   *Give* is the ping-picker case. *Remove* is the opt-out case: an
   "🔕 react to stop being pinged for events" emoji, or a "clear my own
   access" reaction. Give and remove pairs mix freely on one post, and a
   member who never reacts is never touched — the bot only ever changes the
   role of the person who reacted.

6. Press **Publish post**. The bot posts the message and adds every reaction.

Un-reacting reverses whatever the pair did, whether it gave or removed the
role, unless **Undo the change when a member removes their reaction** is
cleared for that post (then reactions are one-way: they apply once and
un-reacting does nothing). Roles are validated exactly like the acceptance
role: they must sit below the bot's role, must not be managed by an
integration, and must not carry moderation or server-management permissions.

Each post listed under **Published reaction role posts** has **Edit** (change
the text, the pairs, or the style — the message is updated in place and the
reactions are re-synced) and **Remove** (drops the configuration and deletes
the bot's message). Moving a post to another channel reposts it. Members keep
any role they already picked, so removing a pair or a post never strips roles —
take them off manually if that is what you want. If the Discord message is
deleted, the next **Save changes** posts a fresh one under the same entry.

The mapping is stored per server in `config.json` under `reaction_roles`, next
to `rules`, so it survives restarts:

```json
"reaction_roles": {
  "987654321098765432": [
    {
      "post_id": "6f1c0b3a",
      "channel_id": 111111111111111111,
      "message_id": 222222222222222222,
      "title": "Choose your roles",
      "message": "React below to pick your pings.",
      "use_embed": true,
      "remove_on_unreact": true,
      "entries": [
        { "emoji": "🎮", "role_id": 333333333333333333, "action": "add" },
        { "emoji": "🔕", "role_id": 444444444444444444, "action": "remove" }
      ]
    }
  ]
}
```

`action` is `add` (give the role on react) or `remove` (take it away on
react); an entry without the key is treated as `add`, so menus written by
older versions keep working unchanged.

### Option C — edit `config.json` directly

```json
{
  "bot_token":        "YOUR-BOT-TOKEN",
  "server_id":        987654321098765432,
  "punish_role_id":   222222222222222222,
  "post_role_id":     333333333333333333,
  "staff_role_id":    555555555555555555,
  "staff_channel_id": 444444444444444444,
  "dm_user":          true,
  "rules":            {},
  "reaction_roles":   {},
  "dashboard_enabled": true,
  "dashboard_host":    "127.0.0.1",
  "dashboard_port":    8765
}
```

The `bot_token`, `server_id`, and role ids go at the top level
(single-server shape). `server_id` is optional for command syncing; if it is
omitted, use `/manage setup` to associate role settings with each server. The bot
fills the `rules` map when a rule set is published (a list of named sets per
server), the `reaction_roles` map when a reaction-role post is published from
the dashboard, and generates `dashboard_token` on first startup. Keep `config.json` private; it contains
credentials. The `guilds`, `token`, and `log_channel_id` keys are a legacy
multi-server shape and are still respected for backwards compatibility.

Publishing rule sets from Discord or the dashboard fills `rules` like this:

```json
"rules": {
  "987654321098765432": [
    {
      "ruleset_id": "6f1c0b3a",
      "name": "Server rules",
      "channel_id": 111111111111111111,
      "message_id": 222222222222222222,
      "role_id": 333333333333333333,
      "rules_text": "1. Be respectful."
    }
  ]
}
```

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
[INFO] sentinel: Database initialised at data/punishments.db
[INFO] sentinel: Global command registry was already empty (startup; commands are registered per guild).
[INFO] sentinel: Logged in as YourBot (id=...)
[INFO] sentinel: Synced 1 command(s) to guild X (instant).
```

### Server-side web dashboard

When the bot is running, open **<http://127.0.0.1:8765>** on the bot host.
The dashboard covers connected servers, active punishments, warnings,
pardon/apply actions, the rules post, multi-role reaction-role menus, server
role/channel settings, slash-command sync, and punishment history.

**Finding the dashboard key.** The first startup generates a private
dashboard key and saves it as `dashboard_token` in the bot's `config.json`.
Until you paste that key into the login screen, the dashboard stays locked.
To retrieve it, open a terminal **on the machine that runs the bot** and run
the command for your install:

| How the bot is installed | Command |
|--------------------------|---------|
| Packaged build (`.dmg`, `.deb`, Windows Setup) | `sentinel --dashboard-token` |
| Running from source | `python3 bot.py --dashboard-token` |

The command prints the key on a single line (and creates it if it doesn't
exist yet). Not sure where `config.json` lives? `sentinel --paths`
(or `python3 bot.py --paths`) prints the resolved config, database, and log
paths. The dashboard key is **not** the Discord bot token — pasting the bot
token into the dashboard will not work.

The default listener is loopback-only; the key has access to **every server
connected to this bot**, so treat it like a bot-owner credential. For a remote
server, prefer an SSH tunnel rather than opening a port:

```bash
ssh -L 8765:127.0.0.1:8765 user@your-server
```

Then open <http://127.0.0.1:8765> on your workstation. If you deliberately
place it behind an HTTPS reverse proxy, set `dashboard_host` to `0.0.0.0`, set
`dashboard_secure_cookie` to `true`, and set `dashboard_allowed_hosts` to the
proxy's exact hostname. Restrict it with a firewall and **never expose the
plain-HTTP dashboard directly to the internet**. Dashboard moderation entries
are tagged `[Dashboard]`; since the dashboard uses a host key rather than
Discord OAuth, its moderator ID is recorded as the server owner.

Slash commands are registered **per server only** — that is the scope that
appears immediately, so they show up without waiting and without setting
`server_id`. Newly joined servers are synced as soon as they become
available.

Discord keeps *two* independent command registries per app (the global one
and the per-guild one) and the Discord client lists a command from each of
them, so registering a command in both is exactly what makes every
`/manage` entry appear **twice**. That is why the bot never
uploads commands globally; if an older version left any behind, startup
deletes them (you will see a "Removed N duplicate global command(s)" line),
and the admin-only `/manage fixcommands` command does the same thing on demand.

---

## 5. Commands

Everything lives under `/manage`. Reaction-role menus are configured from
the dashboard (see
[Reaction role menus](#reaction-role-menus-dashboard)) rather than a command,
since they need a message box, a live preview, and one role picker per emoji.

| Command              | Who can use it                  | What it does |
|----------------------|---------------------------------|--------------|
| `/manage punish`      | Members with *Moderate Members* | Adds the punish role for the configured duration. Posts a staff embed and DMs the member. |
| `/manage warn`       | Members with *Moderate Members* | Records a warning (reason + moderator + time). No role is changed. Posts a staff embed and DMs the member. |
| `/manage warnings`   | Members with *Moderate Members* | Lists a member's recorded warnings. Pass `clear:true` to delete them all (this is logged to the staff channel). |
| `/manage pardon`     | Members with *Moderate Members* | Ends the punishment early and removes the punish / post-punish role. Posts a staff embed and DMs the member. |
| `/manage status`     | Members with *Moderate Members* | Shows the server configuration and a list of active punishments. Pass a `user` to see that member's active status, history, and warnings. |
| `/manage rules publish`     | Server administrators           | Posts a named rule set and sets its ✅ acceptance reaction role. Publishing the same name replaces that set. |
| `/manage rules disable`     | Server administrators           | Stops handling reactions for one rule set (pass `name` when several exist). Existing roles are unchanged. |
| `/manage rules list`        | Server administrators           | Lists the published rule sets with their channel, role and message. |
| `/manage setup`             | Server administrators           | Configures the punishment roles, staff channel, and DM behavior. |
| `/manage fixcommands`       | Server administrators           | Removes duplicated slash commands (e.g. doubled `/manage` entries) and re-syncs this server. |

Discord offers the whole group to anyone with **Moderate Members**, so the
administrator rows above are enforced *when the command runs*: a moderator
who tries `/manage setup` gets an ephemeral "administrators only" reply.

### `/manage punish` options

* `user` — the member to punish.
* `duration` — how long. Examples: `30m`, `2h`, `1d`, `1d12h`, `90`
  *(bare numbers are interpreted as minutes)*.
* `reason` — optional, shown in the staff embed, the DM embed, and the DB.

The maximum duration is 30 days. The minimum is 5 seconds.

### Warnings

`/manage warn <user> <reason>` is the light-weight option: unlike
`/manage punish` it changes **no roles** and has no timer. Each warning is
stored permanently in the bot's database with its reason, moderator, and
timestamp, so a member's record survives restarts and pardons.

* `/manage warnings <user>` lists a member's warnings (newest first) with
  the running total. Moderators can send the same command with `clear:true` to
  delete every warning for that member; the clear is announced in the staff
  channel so it is never silent.
* `/manage status <user>` includes the warning total and the three most recent
  warnings next to the punishment history.
* The dashboard's **Moderation** page has the same two actions:
  a *Warn a member* form and a *Recent warnings* table with a **Clear**
  button per member. Dashboard warnings are tagged `[Dashboard]` and are
  attributed to the server owner, exactly like dashboard punishments.

Protected members (admins, moderators, staff role holders, bots, and anyone
above the bot's role) cannot be warned, and nobody can warn themselves.
Warnings are DMed to the member and posted to the staff channel using the same
`dm_user` / `staff_channel_id` settings as punishments.

---

## 6. Embeds

**Staff channel embed** (on `/manage punish`):

* Title: "Member punished"
* Color: orange
* Fields: User (mention + id), Moderator, Duration, Reason, Started
  (Discord timestamp), Ends (Discord timestamp + relative)
* Thumbnail: the punished user's avatar
* Footer: "User ID: ..."

**Punished-user DM embed** (on `/manage punish`):

* Title: "You've been punished in `<server name>`"
* Color: red
* Fields: Duration, Reason, Started, Ends, Issued by
* Friendly message pointing the member to talk to a mod if they think it
  was a mistake

**Staff channel embed** (on timer expiry):

* Title: "Punishment timer expired"
* Color: blue
* Description showing the member moved from punish -> post role
* Started (relative time)

**Staff channel embed** (on `/manage pardon`):

* Title: "Member pardoned"
* Color: green
* Fields: User, Moderator, Outcome

**Pardoned-user DM embed**:

* Title: "Your punishment in `<server name>` has been lifted"
* Color: green
* Description confirming roles are restored
* "Issued by" field

**Staff channel embed** (on `/manage warn`):

* Title: "Member warned"
* Color: gold
* Fields: User (mention + id), Moderator, Total warnings, Reason, Time
* Thumbnail: the warned user's avatar
* Footer: "User ID: ..."

**Warned-user DM embed** (on `/manage warn`):

* Title: "You've been warned in `<server name>`"
* Color: gold
* Fields: Reason, Total warnings, Time, Issued by
* Friendly message about repeated warnings and talking to a moderator

**Staff channel embed** (on `/manage warnings clear:true` and the dashboard's
**Clear** button):

* Title: "Warnings cleared"
* Color: grey
* Fields: User, Moderator, Warnings removed

---

## 7. Building native installers

The bot can be packaged as a `.dmg` (macOS), `.exe` installer (Windows),
or `.deb` (Linux). Each platform must be built on its own host — there
is no cross-compile.

| Platform | Build script                | Output                                  |
|----------|------------------------------|------------------------------------------|
| macOS    | `build/build_macos.sh`       | `dist/Sentinel-1.0.0.dmg`       |
| Linux    | `build/build_linux.sh`       | `dist/sentinel_1.0.0_amd64.deb`|
| Windows  | `build\build_windows.bat`    | `dist\Sentinel-Setup-1.0.0.exe` |

All three flow through `build/pyinstaller.spec`, which bundles `bot.py` and
its imported modules, plus `installer.py` and `dashboard.html`, into a
self-contained build before wrapping it in the OS-native installer format.

### Cutting a release

The release workflow is fully automated. The version lives in one place -
the `VERSION` file at the project root - which every build script reads
(`build_linux.sh`, `build_macos.sh`, `build_windows.bat`,
`build/pyinstaller.spec`), so the `.deb`/`.dmg`/`.exe` filenames, the `.app`
plist, the NSIS metadata and `sentinel --version` can never disagree
with the release they belong to.

Bump it, then push a semver tag from the `main` branch:

```bash
echo 3.0.0 > VERSION
git commit -am "chore: bump version to 3.0.0"
git push origin main

./scripts/make_release.sh 3.0.0     # or: ./scripts/make_release.sh 3.1.0-rc1
```

The script checks that the tag matches `VERSION`, validates the working
tree, creates an annotated `v3.0.0` tag, and pushes it. Pushing the tag triggers `.github/workflows/release.yml`,
which builds all three platforms in parallel and attaches the artifacts
to a new GitHub Release.

Main doesn't have to wait for that, though: every merge to `main` builds the
same installers through `merge-release.yml` and publishes them as the rolling
`latest-build` prerelease plus a `v<VERSION>-build.<run>` prerelease for that
merge (see [Download](#download)). Those generated tags are skipped by
`release.yml`, so only a tag you push can produce a versioned release.

All three workflows call the same reusable build
(`.github/workflows/build-installers.yml`), so the `.deb`/`.dmg`/`.exe` steps
exist in exactly one file.

You can also just run the same commands by hand:

```bash
git tag -a v3.0.0 -m "Release 3.0.0"
git push origin v3.0.0
```

Either way, the release page appears at
`https://github.com/mob5824m-wq/Sentinel/releases/tag/v3.0.0`
a few minutes later with the `.dmg`, `.deb`, and `.exe` ready to
download.

### macOS

Requirements: Python 3.9+, `pyinstaller`, optionally `create-dmg`
(`brew install create-dmg`) for a styled `.dmg` window. Otherwise
`hdiutil` is used as a fallback.

```bash
build/build_macos.sh
open dist/Sentinel-1.0.0.dmg
```

The result is a real `.app` bundle (`Sentinel.app`) inside a
`.dmg` that users can drag into `/Applications`. The bundle id is
`com.arena.sentinel` and the binary is at
`Sentinel.app/Contents/MacOS/sentinel`.

To codesign, uncomment the `codesign` lines in `build_macos.sh` and
set `CODESIGN_IDENTITY` to your Developer ID.

### Linux (.deb)

Requirements: Python 3.9+, `pyinstaller`, `dpkg`, `fakeroot`,
`lintian` (optional).

```bash
build/build_linux.sh
sudo dpkg -i dist/sentinel_1.0.0_amd64.deb
sudo systemctl start sentinel
```

The package installs the bot to `/opt/sentinel/`, symlinks
the binary into `/usr/bin/`, registers a desktop entry, and installs
a systemd unit (`/lib/systemd/system/sentinel.service`).
The unit is enabled (not started) by `postinst`; the user runs the
bot once to configure it, then enables the service.

### Windows

Requirements: Python 3.9+, `pyinstaller`, NSIS 3.x in PATH.

```
build\build_windows.bat
dist\Sentinel-Setup-1.0.0.exe
```

The NSIS installer copies the PyInstaller output to
`%ProgramFiles64%\Sentinel`, creates Start Menu and Desktop
shortcuts, adds the install dir to `PATH`, and registers an
uninstaller in Add/Remove Programs.

To sign with `signtool`, uncomment the `signtool sign` line in
`build_windows.bat`.

### Background service

After install, the bot can be configured to start automatically:

```bash
# Linux (after sudo dpkg -i ...):
sudo sentinel --install-service
sudo systemctl start sentinel
sudo systemctl enable sentinel

# macOS:
sudo sentinel --install-service
launchctl load -w ~/Library/LaunchAgents/com.arena.sentinel.plist

# Windows (run as Administrator):
sentinel.exe --install-service
sc start Sentinel
```

The `--install-service` command registers the bot with the OS service
manager (systemd / launchd / NSSM). The bot will then start on boot
and restart automatically if it crashes. `--uninstall-service`
removes the registration.

For development, you can also just run the binary directly with no
arguments — it will auto-run the installer on first launch.

## 8. Files

### Where config, database and logs live

Running from a source checkout keeps everything in the repo (`./data`,
`./config.json`). A **packaged install must not write next to the binary** -
`/opt/sentinel`, `C:\Program Files\Sentinel` and the
macOS `.app` are read-only (and world-readable, which would leak the token),
so `paths.py` picks a writable location at startup:

| Install          | Data (db + log)                                             | Config read from                                        |
|------------------|-------------------------------------------------------------|----------------------------------------------------------|
| source checkout  | `./data/`                                                    | `./config.json`                                          |
| Linux (`.deb`)   | `/var/lib/sentinel`, else `$XDG_STATE_HOME/sentinel`, else `~/.local/state/sentinel` | `~/.local/state/.../config.json`, then `/etc/sentinel/config.json` |
| macOS (`.dmg`)   | `~/Library/Application Support/Sentinel`           | there, else `/Library/Application Support/Sentinel` |
| Windows          | `%LOCALAPPDATA%\Sentinel`, else the install dir    | there, else `config.json` next to `sentinel.exe` |

The first writable candidate wins; if none is writable it falls back to a
temp dir and says so in the log. A config that exists but is read-only (the
`.deb` ships one in `/etc`, mode `0640 root:sentinel`) is read from
there, and the first save copies it to the writable data dir - which then
takes precedence. A candidate the current user can't even look into (for
example the `.deb`'s `0750` `/etc/sentinel` when a portable build is
run by a user outside the `sentinel` group) is skipped with a note
in the log, and the later candidates - such as `config.json` next to the
executable - are still tried (v2.1.1; v2.1.0 stopped at the first such
candidate).

Print the resolved locations any time:

```bash
sentinel --paths        # or: python3 bot.py --paths
sentinel --version      # which build is actually installed
```

Or pin them explicitly (useful for containers and custom service units):

```bash
SENTINEL_HOME=/srv/sentinel sentinel              # data + config base
SENTINEL_DATA=/srv/sentinel/data ...             # db + log dir only
SENTINEL_CONFIG=/etc/sentinel/config.json ...    # config.json path
```

Because the systemd service runs as the `sentinel` user, configure
it with `sudo` so the file lands where the service can read it:

```bash
sudo sentinel --install     # writes /var/lib or /etc, service-visible
sudo systemctl start sentinel
```

Running the installer as your own user only configures *your* user (the
installer tells you when that's the case).

```
Sentinel/
├── bot.py                  # bot entry point and command registration
├── command_tree.py         # the shared /manage group + admin check
├── dashboard.py            # authenticated server-side dashboard/API
├── dashboard.html          # dashboard UI
├── rules.py                # rules publishing and the acceptance reaction role
├── reaction_roles.py       # dashboard-published reaction-role menus
├── installer.py            # interactive first-run installer
├── paths.py                # where config/db/logs live at runtime
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
│   │   ├── sentinel.service
│   │   ├── sentinel.desktop
│   │   ├── postinst
│   │   ├── prerm
│   │   └── postrm
│   ├── macos/
│   │   ├── Info.plist
│   │   └── com.arena.sentinel.plist
│   └── windows/
│       └── installer.nsi
├── tests/
│   ├── test_commands.py         # slash-command descriptions and guild sync
│   ├── test_dashboard.py        # dashboard login/session security + endpoints
│   ├── test_markdown.py         # Discord Markdown renderer/linter
│   ├── test_paths.py            # packaged-install path resolution (read-only app dir)
│   ├── test_reaction_roles.py   # reaction-role menus (storage, emoji, give/remove)
│   ├── test_release_workflow.py # merge/tag release publishing (workflows + scripts)
│   ├── test_rules.py            # rules acceptance/reaction-role behavior
│   ├── test_version.py          # VERSION plumbing (build scripts, tag, --version)
│   └── test_warnings.py         # warning escalation behavior
└── data/                    # created at runtime, source checkouts only
    ├── punishments.db
    └── bot.log
```

```bash
python3 -m pytest -q                 # run the full test suite
python3 tests/test_dashboard.py      # run dashboard auth tests alone
```

## 9. Troubleshooting

* **`PermissionError: [Errno 13] Permission denied:
  '/opt/sentinel/_internal/data'`** at startup, usually followed by
  `[PYI-...:ERROR] Failed to execute script 'bot'` — that build predates
  `paths.py` and tried to create its data directory inside the read-only
  install tree. Update to a build that ships `paths.py` (state then lives in
  `/var/lib/sentinel`, or your user's state dir). On the old build
  you can work around it:

  ```bash
  sudo mkdir -p /var/lib/sentinel
  sudo chown sentinel:sentinel /var/lib/sentinel
  sudo SENTINEL_DATA=/var/lib/sentinel sentinel
  ```

  `sentinel --paths` prints where the current build keeps its
  files.
* **"Installer exited without saving a config"** — re-run
  `python3 installer.py` and answer the prompts. If your terminal hides
  input (e.g. when piping from a file), the token will be read as empty
  and you'll be asked again.
* **`Missing Permissions`** when running `/manage punish` — the bot's
  role isn't above the configured roles. Move it up in
  *Server Settings → Roles*.
* **`/manage rules publish` can't post or react** — grant the bot **View Channel**,
  **Send Messages**, **Embed Links**, and **Add Reactions** in the selected
  channel. The acceptance role must be below the bot's role and must not have
  moderation or server-management permissions.
* **A rule set doesn't grant its role** — check `/manage rules list`: the set's
  message id changes when you republish or move it, so an old post stops
  reacting on purpose. Reactions are matched per message, and each set grants
  only its own role. If `/manage rules disable` says nothing was configured, the set
  was already removed from `config.json`.
* **A reaction-role post doesn't grant roles** — the same limits as the
  acceptance role apply: the role must sit below the bot's role, the bot needs
  **Manage Roles**, and the role must not be managed by an integration. The
  message must also still exist (a deleted message can't receive reactions
  any more), so press **Edit** → **Save changes** on that post and the
  dashboard reposts it under the same entry. `data/bot.log` names the failed
  check (`Cannot manage reaction role …`, `Configured reaction role … is
  missing`).
* **Dashboard won't open** — it binds to `127.0.0.1:8765` by default, so open
  it on the bot host or use the documented SSH tunnel. Check `data/bot.log`
  for a port or config error; remote reverse-proxy hosts must be in
  `dashboard_allowed_hosts`.
* **Dashboard login says "Invalid dashboard key"** — you are entering the
  wrong credential. The login key is printed by
  `sentinel --dashboard-token` (packaged build) or
  `python3 bot.py --dashboard-token` (source) and is stored as
  `dashboard_token` in the bot's `config.json`; the Discord **bot token**
  will not work. `--paths` shows where `config.json` is.
* **The member never receives the DM** — they have DMs disabled or the
  bot is blocked. Set `dm_user: false` in `/manage setup` to suppress the DM
  attempt, or ask the member to enable DMs.
* **Slash commands don't appear** — the bot syncs instantly to every
  connected server, so `server_id` is not required. Make sure the bot was
  invited with the `applications.commands` scope and check `data/bot.log`
  for a "Synced N command(s) to guild X" line for your server.
* **Slash commands appear twice / doubled** (`/manage` listed two or three
  times) — Discord is showing the same command from both of its
  command registries. This is what older versions of the bot caused by
  syncing globally *and* per server; it is fixed now. Startup deletes the
  duplicate global registrations automatically (look for a "Removed N
  duplicate global command(s)" line in `data/bot.log`), or an admin can run
  `/manage fixcommands` to do it on the spot. Discord can take a few minutes to
  refresh the command list — restarting Discord (Ctrl+R) shows it
  immediately. If the duplicates come back, another copy of the bot is still
  running with the same token; stop it and run `/manage fixcommands` again.
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
