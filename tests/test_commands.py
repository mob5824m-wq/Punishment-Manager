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
the only way to find out was to deploy. These tests check the real command tree,
offline, instead.

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
    for cmd in tree.get_commands():
        # discord.py 2.6 made `tree` a required argument of to_dict().
        if "tree" in inspect.signature(cmd.to_dict).parameters:
            payload.append(cmd.to_dict(tree))
        else:
            payload.append(cmd.to_dict())
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


asyncio.run(main())
"""


def dump_command_payload() -> list[dict]:
    """The command list ``tree.sync()`` would upload to Discord (no network)."""
    with tempfile.TemporaryDirectory(prefix="pm-commands-test-") as tmp:
        home = Path(tmp).resolve()
        out = home / "payload.json"
        env = {
            **os.environ,
            # Don't let the developer's real config/data in or out of the test.
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

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = dump_command_payload()

    def test_documented_commands_are_registered(self) -> None:
        # Keeps the limit check below from passing vacuously (say, if the cog
        # stopped registering, or subcommands stopped being walked).
        names = command_names(self.payload)
        for expected in ("punish", "punish apply", "punish pardon", "punish status", "setup"):
            self.assertIn(expected, names, f"command tree has: {sorted(names)}")

    def test_every_description_fits_discords_limit(self) -> None:
        problems = find_description_problems(self.payload)
        self.assertEqual(
            problems, [],
            "Discord would reject tree.sync() with HTTP 400 (error 50035) "
            "and register none of the commands:\n  " + "\n  ".join(problems),
        )


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
