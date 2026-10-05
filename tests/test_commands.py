"""
Tests for the slash-command definitions (bot.py).

Motivation: ``/punish status`` shipped with a 101-character description.
Discord limits every command and option description to 1-100 characters, so at
startup ``tree.sync()`` was rejected as a whole::

    discord.app_commands.errors.CommandSyncFailure: Failed to upload commands
    to Discord (HTTP status 400, error code 50035)
    In group 'punish' defined in module '__main__'
      In command 'punish status' defined in function 'PunishmentCog.punish_status'
        description: Must be between 1 and 100 in length.

The bot logs that and keeps running (it has to: the scheduler that releases
punished users lives in the same process), so it looked healthy while Discord
never received the command list. discord.py does not length-check a description
that is passed explicitly and nothing exercised the tree before a release, so
the only way to find out was to deploy. These tests check the real command tree
and the automatic per-guild sync path offline, instead.

Motivation for the duplicate half of this file: the sync used to upload the
whole tree twice - once to Discord's *global* registry (``tree.sync()``) and
once per guild (``copy_global_to()`` + ``tree.sync(guild=...)``). Discord keeps
those two registries separate and the client lists a command from each of them,
so every server showed doubled ``/punish`` and ``/setup`` entries. The bot now
syncs per guild only and empties the global registry; the tests below pin that
down, including the empty global upload that removes existing duplicates.

Run with either:
    python -m pytest tests/test_commands.py
    python tests/test_commands.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Discord's limit for the description of a slash command, group, subcommand or
# option: https://discord.com/developers/docs/interactions/application-commands
DESCRIPTION_MIN = 1
DESCRIPTION_MAX = 100

# Values of the `type` field in an application command payload.
CHAT_INPUT = 1          # top-level slash command (context menus are 2 and 3)
SUB_COMMAND = 1         # option types...
SUB_COMMAND_GROUP = 2
USER = 6

# Runs in a child process so that importing bot.py (it opens a log file and may
# write a default config) cannot touch the real user's files or leak state into
# the other tests. Prints nothing: the payload goes to the file in argv[1].
_DUMP_PAYLOAD = r"""
import asyncio, inspect, json, sys

sys.path.insert(0, sys.argv[2])
import bot


async def main():
    # Register the cog exactly like PunishmentBot.setup_hook() does.
    await bot._register_cog()
    tree = bot.bot.tree
    payload = []
    meta = {}
    for cmd in tree.get_commands():
        # discord.py 2.4 made `tree` a required argument of to_dict().
        if "tree" in inspect.signature(cmd.to_dict).parameters:
            payload.append(cmd.to_dict(tree))
        else:
            payload.append(cmd.to_dict())
        # Visibility flags are assigned in cog_load(), after the decorators ran.
        perms = getattr(cmd, "default_permissions", None)
        meta[cmd.name] = {
            "guild_only": bool(getattr(cmd, "guild_only", False)),
            "default_member_permissions": None if perms is None else str(perms.value),
        }
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump({"commands": payload, "meta": meta}, fh)


asyncio.run(main())
"""

# Exercise the real guild-sync path against a recording tree, so the test sees
# exactly what would reach Discord without making any request: copy-before-sync,
# one-time syncing, and the *empty* global upload that removes duplicates.
_DUMP_GUILD_SYNC_EVENTS = r"""
import asyncio, json, sys

sys.path.insert(0, sys.argv[2])
import bot


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id


def record_tree(tree, events, registry):
    # Patch the tree's network endpoints so this test makes no Discord
    # requests. ``sync`` is the upload: what the real tree would send is
    # recorded as a count, so the test can prove the global upload is empty
    # (that is the duplicate cleanup) and each guild upload carries commands.
    # get_commands / add_command / walk_commands stay the real ones.
    real_copy = tree.copy_global_to
    real_clear = tree.clear_commands
    real_get = tree.get_commands

    def copy_global_to(*, guild):
        events.append(["copy", guild.id])
        real_copy(guild=guild)

    def clear_commands(*, guild):
        events.append(["clear", None if guild is None else guild.id])
        real_clear(guild=guild)

    async def sync(*, guild=None):
        count = len(real_get(guild=guild))
        events.append(["sync", None if guild is None else guild.id, count])
        if guild is None:
            # An empty global upload replaces the registry: duplicates gone.
            registry["global"] = []
        return [object()] * count

    tree.copy_global_to = copy_global_to
    tree.clear_commands = clear_commands
    tree.sync = sync


class FakeResponse:
    def __init__(self):
        self.deferred = False

    def is_done(self):
        return self.deferred

    async def defer(self, *, ephemeral=False):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, *, ephemeral=False):
        self.sent.append(content)


class FakeInteraction:
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.response = FakeResponse()
        self.followup = FakeFollowup()


async def main():
    # Register the cog exactly like PunishmentBot.setup_hook() does.
    await bot._register_cog()
    client = bot.bot
    # Discord still holds the two global commands an older version registered.
    registry = {"global": [{"id": 1}, {"id": 2}]}
    events = []
    record_tree(client.tree, events, registry)
    client.config = {"server_id": 101}
    client._connection.application_id = 987654321

    async def fake_get_global_commands(application_id):
        events.append(["get-global", None])
        return list(registry["global"])

    client.http.get_global_commands = fake_get_global_commands
    client._connection._guilds.update({
        101: FakeGuild(101),
        202: FakeGuild(202),
    })

    await client._sync_commands()           # startup: clear duplicates, sync server_id
    await client._sync_connected_guilds()   # READY: sync every connected guild
    # A repeated READY event must not re-upload commands to the same guilds.
    await client._sync_connected_guilds()
    # Guilds joined after startup receive the same immediate sync.
    await client.on_guild_join(FakeGuild(303))
    await client.on_guild_available(FakeGuild(303))

    # /fixcommands: on-demand cleanup, after a fresh duplicate appeared.
    registry["global"] = [{"id": 3}]
    cog = client.get_cog("PunishmentCog")
    interaction = FakeInteraction(202)
    fix_start = len(events)
    await cog._handle_fixcommands(interaction)

    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump({
            "events": events,
            # The cleanup must not gut the local tree: guild copies are what
            # keeps the commands alive after the global registry is emptied.
            "local_commands": sorted(
                c.qualified_name for c in client.tree.get_commands(guild=None)
            ),
            "fix_events": events[fix_start:],
            "fix_reply": interaction.followup.sent,
        }, fh)


asyncio.run(main())
"""


def _isolated_test_env(home: Path) -> dict[str, str]:
    """Keep bot import side effects inside a temporary test home."""
    return {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PUNISHMENT_MANAGER_HOME": "",
        "PUNISHMENT_MANAGER_DATA": str(home / "data"),
        "PUNISHMENT_MANAGER_CONFIG": str(home / "config.json"),
        "PYTHONIOENCODING": "utf-8",
    }


def dump_command_payload() -> dict:
    """The command list ``tree.sync()`` would upload to Discord (no network).

    ``{"commands": [...payload...], "meta": {name: {"guild_only": ...,
    "default_member_permissions": ...}}}``.
    """
    with tempfile.TemporaryDirectory(prefix="pm-commands-test-") as tmp:
        home = Path(tmp).resolve()
        out = home / "payload.json"
        # Don't let the developer's real config/data in or out of the test.
        env = _isolated_test_env(home)
        res = subprocess.run(
            [sys.executable, "-c", _DUMP_PAYLOAD, str(out), str(REPO_ROOT)],
            capture_output=True, encoding="utf-8", errors="replace",
            env=env, cwd=str(home),
        )
        if res.returncode != 0 or not out.is_file():
            raise AssertionError(
                f"could not build the command tree from bot.py (exit {res.returncode}):\n"
                f"{res.stdout}{res.stderr}"
            )
        return json.loads(out.read_text(encoding="utf-8"))


def dump_guild_sync_events() -> dict:
    """Run the whole startup / READY / join / ``/fixcommands`` path offline.

    Keys: ``events`` (everything that would reach Discord), ``local_commands``
    (the tree after the global cleanup), ``fix_events`` (requests made by
    ``/fixcommands``) and ``fix_reply`` (what the admin sees).
    """
    with tempfile.TemporaryDirectory(prefix="pm-guild-sync-test-") as tmp:
        home = Path(tmp).resolve()
        out = home / "events.json"
        res = subprocess.run(
            [sys.executable, "-c", _DUMP_GUILD_SYNC_EVENTS, str(out), str(REPO_ROOT)],
            capture_output=True, encoding="utf-8", errors="replace",
            env=_isolated_test_env(home), cwd=str(home),
        )
        if res.returncode != 0 or not out.is_file():
            raise AssertionError(
                f"could not exercise automatic guild sync (exit {res.returncode}):\n"
                f"{res.stdout}{res.stderr}"
            )
        return json.loads(out.read_text(encoding="utf-8"))


def walk(commands: list[dict]):
    """Yield ``(label, node, is_option)`` for everything in a sync payload.

    Covers each slash command, group, subcommand and option. ``label`` is how a
    user would refer to it: ``/punish status`` or ``/punish status option 'user'``.
    """

    def visit(node: dict, label: str, is_option: bool):
        yield label, node, is_option
        for opt in node.get("options", []):
            if opt.get("type") in (SUB_COMMAND, SUB_COMMAND_GROUP):
                yield from visit(opt, f"{label} {opt['name']}", False)
            else:
                yield from visit(opt, f"{label} option '{opt['name']}'", True)

    for cmd in commands:
        # Context-menu commands (types 2 and 3) carry no description at all.
        if cmd.get("type", CHAT_INPUT) == CHAT_INPUT:
            yield from visit(cmd, f"/{cmd['name']}", False)


def command_names(commands: list[dict]) -> set[str]:
    """Qualified names of every command, group and subcommand: ``punish status``."""
    return {
        label.lstrip("/")
        for label, _node, is_option in walk(commands)
        if not is_option
    }


def find_description_problems(commands: list[dict]) -> list[str]:
    """One message per description Discord would refuse (empty or too long)."""
    problems = []
    for label, node, _is_option in walk(commands):
        desc = node.get("description")
        if isinstance(desc, str) and DESCRIPTION_MIN <= len(desc) <= DESCRIPTION_MAX:
            continue
        size = f"{len(desc)} characters" if isinstance(desc, str) else "missing"
        problems.append(
            f"{label}: description is {size}, Discord requires "
            f"{DESCRIPTION_MIN}-{DESCRIPTION_MAX}: {desc!r}"
        )
    return problems


class CheckerTests(unittest.TestCase):
    """The checker itself. It has to catch the regression that shipped."""

    @staticmethod
    def tree(*, group: str = "Group.", sub: str = "Sub.", option: str = "Option.") -> list[dict]:
        return [{
            "name": "punish", "description": group, "type": CHAT_INPUT,
            "options": [{
                "name": "status", "description": sub, "type": SUB_COMMAND,
                "options": [{"name": "user", "description": option, "type": USER}],
            }],
        }]

    def test_accepts_the_limits_themselves(self) -> None:
        for n in (DESCRIPTION_MIN, DESCRIPTION_MAX):
            with self.subTest(length=n):
                text = "x" * n
                self.assertEqual(
                    find_description_problems(self.tree(group=text, sub=text, option=text)), []
                )

    def test_rejects_empty_and_overlong_at_every_level(self) -> None:
        for level in ("group", "sub", "option"):
            for bad in ("", "x" * (DESCRIPTION_MAX + 1)):
                with self.subTest(level=level, length=len(bad)):
                    problems = find_description_problems(self.tree(**{level: bad}))
                    self.assertEqual(len(problems), 1, problems)

    def test_flags_the_description_that_broke_sync(self) -> None:
        original = (
            "Show this server's configuration and active punishments. "
            "Pass a user to see their punishment history."
        )
        self.assertEqual(len(original), 101, "one character over Discord's limit")
        problems = find_description_problems(self.tree(sub=original))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("/punish status", problems[0])
        self.assertIn("101 characters", problems[0])

    def test_context_menu_commands_are_not_held_to_the_rule(self) -> None:
        menu = [{"name": "Report", "type": 2}]  # USER context menu: no description
        self.assertEqual(find_description_problems(menu), [])


class CommandTreeTests(unittest.TestCase):
    """The real tree from bot.py: what tree.sync() uploads at startup."""

    payload: list[dict]
    meta: dict[str, dict]
    sync: dict

    @classmethod
    def setUpClass(cls) -> None:
        dumped = dump_command_payload()
        cls.payload = dumped["commands"]
        cls.meta = dumped["meta"]
        cls.sync = dump_guild_sync_events()

    def test_documented_commands_are_registered(self) -> None:
        # Keeps the limit check below from passing vacuously (say, if the cog
        # stopped registering, or subcommands stopped being walked).
        names = command_names(self.payload)
        for expected in (
            "punish", "punish apply", "punish pardon", "punish status",
            "rules", "rules publish", "rules disable", "rules list",
            "setup", "fixcommands",
        ):
            self.assertIn(expected, names, f"command tree has: {sorted(names)}")

    def test_every_description_fits_discords_limit(self) -> None:
        problems = find_description_problems(self.payload)
        self.assertEqual(
            problems, [],
            "Discord would reject tree.sync() with HTTP 400 (error 50035) "
            "and register none of the commands:\n  " + "\n  ".join(problems),
        )

    def test_admin_commands_are_guild_only_and_admin_only(self) -> None:
        # Administrator = 1 << 3 = 8.
        for name in ("setup", "fixcommands", "rules"):
            with self.subTest(command=name):
                self.assertTrue(self.meta[name]["guild_only"], f"/{name} is guild-only")
                self.assertEqual(
                    self.meta[name]["default_member_permissions"], str(1 << 3),
                    f"/{name} must be administrator-only",
                )
        # /punish stays available to users with Moderate Members.
        self.assertEqual(
            self.meta["punish"]["default_member_permissions"], str(1 << 40)
        )
        self.assertFalse(self.meta["punish"]["guild_only"])

    def test_commands_are_synced_per_guild_and_never_registered_globally(self) -> None:
        """No double commands: guild scope only, and the global registry emptied.

        The startup sequence must (1) read what is registered globally, (2)
        upload an *empty* global command set to delete those duplicates, and
        (3) sync each connected guild exactly once - including the configured
        ``server_id``, which is synced before login even finishes.
        """
        events = self.sync["events"]
        self.assertEqual(
            events,
            [
                ["get-global", None],
                ["clear", None], ["sync", None, 0],   # duplicates deleted
                ["copy", 101], ["sync", 101, 4],      # configured server_id
                ["copy", 202], ["sync", 202, 4],      # other connected guild
                ["copy", 303], ["sync", 303, 4],      # joined after startup
                # /fixcommands: fresh duplicate found and removed ...
                ["get-global", None], ["clear", None], ["sync", None, 0],
                # ... and this server's copy re-uploaded, then verified empty.
                ["sync", 202, 4], ["get-global", None],
            ],
            "unexpected command registration traffic",
        )
        # Every upload to the global scope is empty; only guild uploads carry
        # commands. A non-empty global upload is what brings duplicates back.
        for name, guild_id, count in [e for e in events if e[0] == "sync"]:
            if guild_id is None:
                self.assertEqual(count, 0, "global command upload was not empty")

    def test_duplicate_cleanup_keeps_the_local_tree_intact(self) -> None:
        # Guild copies are made from the local global tree, so emptying the
        # Discord-side registry must not remove the commands locally.
        self.assertEqual(
            self.sync["local_commands"], ["fixcommands", "punish", "rules", "setup"]
        )

    def test_fixcommands_removes_duplicates_and_reports_it(self) -> None:
        reply = "\n".join(self.sync["fix_reply"])
        self.assertIn("Removed 1 duplicated command(s).", reply)
        self.assertIn("Re-synced this server's commands.", reply)
        # The duplicate really was deleted, and this guild re-synced.
        self.assertEqual(self.sync["events"][-2][0:2], ["sync", 202])


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
