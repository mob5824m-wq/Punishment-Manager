"""Offline tests for the warning feature.

Warnings are the light-weight half of moderation: nothing is timed and no role
is swapped, but every warning is persisted with its moderator, reason and time
so `/punish warnings <user>` and the dashboard can show (or clear) the record.

Two surfaces are covered here:

* ``bot.py`` — the SQLite helpers and the `/punish warn` / `/punish warnings`
  command handlers, exercised in a child process so importing ``bot.py`` cannot
  touch the developer's real config, database or log file (see
  ``tests/test_commands.py`` for the same pattern).
* ``dashboard.py`` — the ``/api/guilds/<id>/warnings`` endpoints over a real
  aiohttp test client, with the same session/CSRF rules as every other API
  route.

Run with either:
    python -m pytest tests/test_warnings.py
    python tests/test_warnings.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import discord  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from dashboard import DashboardServer  # noqa: E402


class _FakeHttpResponse:
    """Minimal stand-in for the aiohttp response ``discord.HTTPException`` wants."""

    status = 404
    reason = "Not Found"


# --------------------------------------------------------------------------- #
# bot.py (child process: import side effects stay in a temporary home)
# --------------------------------------------------------------------------- #
_DUMP_BOT_WARNINGS = r"""
import asyncio, json, sys
from datetime import datetime, timezone

sys.path.insert(0, sys.argv[2])
import bot


class FakePerms:
    def __init__(self, **kw):
        self.administrator = False
        self.moderate_members = False
        self.manage_guild = False
        self.kick_members = False
        self.ban_members = False
        for key, value in kw.items():
            setattr(self, key, value)


class FakeRole:
    def __init__(self, role_id, position=1):
        self.id = role_id
        self.position = position
        self.mention = f"<@&{role_id}>"

    def __ge__(self, other): return self.position >= other.position
    def __gt__(self, other): return self.position > other.position
    def __lt__(self, other): return self.position < other.position
    def __le__(self, other): return self.position <= other.position


class FakeMember:
    def __init__(
        self, user_id, *, name="Member", perms=None, top_role=None, bot_user=False,
        roles=(),
    ):
        self.id = user_id
        self.display_name = name
        self.name = name
        self.bot = bot_user
        self.mention = f"<@{user_id}>"
        self.roles = list(roles)
        self.top_role = top_role or FakeRole(1, 1)
        self.guild_permissions = perms or FakePerms()
        self.display_avatar = None
        self.dms = []

    async def send(self, content=None, embed=None):
        self.dms.append({"content": content, "title": getattr(embed, "title", None)})


class FakeGuild:
    def __init__(self, guild_id, me, members, roles=()):
        self.id = guild_id
        self.name = "Test Guild"
        self.me = me
        self._members = {member.id: member for member in members}
        self._roles = {role.id: role for role in roles}

    def get_member(self, user_id):
        return self._members.get(int(user_id))

    def get_role(self, role_id):
        return self._roles.get(int(role_id))

    def get_channel(self, _channel_id):
        return None


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.sent = []

    def is_done(self):
        return self.deferred

    async def defer(self, *, ephemeral=False):
        self.deferred = True

    async def send_message(self, content, *, ephemeral=False):
        self.sent.append(content)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, *, ephemeral=False):
        self.sent.append(content)


class FakeInteraction:
    def __init__(self, guild, user):
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    @property
    def replies(self):
        return self.response.sent + self.followup.sent


async def main():
    bot.init_db()
    guild_id = 101
    me = FakeMember(999, name="Bot", top_role=FakeRole(900, 50))
    target = FakeMember(202, name="Target", top_role=FakeRole(10, 2))
    moderator = FakeMember(
        303,
        name="Mod",
        perms=FakePerms(moderate_members=True),
        top_role=FakeRole(11, 10),
    )
    staff = FakeMember(
        404,
        name="Staff",
        perms=FakePerms(administrator=True),
        top_role=FakeRole(12, 20),
    )
    plain = FakeMember(505, name="Plain", top_role=FakeRole(13, 3))
    staff_role = FakeRole(606, 30)
    # Holds the configured staff role and *no* Discord moderation permission:
    # exactly who the staff-role rule is meant to let in.
    staff_only = FakeMember(
        707, name="StaffOnly", top_role=FakeRole(14, 4), roles=[staff_role]
    )
    guild = FakeGuild(
        guild_id, me, [me, target, moderator, staff, plain, staff_only],
        roles=[staff_role],
    )
    bot.bot.config = {
        "server_id": guild_id,
        "punish_role_id": 555,
        "post_role_id": 556,
        # Phase 1: no staff role configured yet - the historical permission
        # rule applies, so the existing flow below keeps working.
        "staff_role_id": None,
        "dm_user": True,
    }
    cog = bot.SentinelCog(bot.bot)
    results = {}

    first = FakeInteraction(guild, moderator)
    await cog._handle_warn(first, target, "  spam in #general  ")
    results["first_replies"] = first.replies
    results["first_count"] = bot.count_warnings(guild_id, target.id)
    rows = bot.list_warnings(guild_id, target.id)
    results["stored_reason"] = rows[0]["reason"]
    results["stored_moderator"] = int(rows[0]["moderator_id"])
    results["dm_titles"] = [dm["title"] for dm in target.dms]
    results["dm_reasons"] = [dm["content"] for dm in target.dms]

    second = FakeInteraction(guild, moderator)
    await cog._handle_warn(second, target, "second warning")
    results["second_replies"] = second.replies
    results["second_count"] = bot.count_warnings(guild_id, target.id)

    self_warn = FakeInteraction(guild, moderator)
    await cog._handle_warn(self_warn, moderator, "warn myself")
    results["self_replies"] = self_warn.replies

    protected = FakeInteraction(guild, moderator)
    await cog._handle_warn(protected, staff, "warn staff")
    results["protected_replies"] = protected.replies

    empty = FakeInteraction(guild, moderator)
    await cog._handle_warn(empty, target, "   ")
    results["empty_replies"] = empty.replies
    results["count_after_refusals"] = bot.count_warnings(guild_id, target.id)

    listing = FakeInteraction(guild, moderator)
    await cog._handle_warnings(listing, target, False)
    results["list_replies"] = listing.replies

    denied = FakeInteraction(guild, plain)
    await cog._handle_warnings(denied, target, True)
    results["clear_denied_replies"] = denied.replies
    results["count_after_denied_clear"] = bot.count_warnings(guild_id, target.id)

    cleared = FakeInteraction(guild, moderator)
    await cog._handle_warnings(cleared, target, True)
    results["clear_replies"] = cleared.replies
    results["count_after_clear"] = bot.count_warnings(guild_id, target.id)
    results["clear_again_replies"] = None
    again = FakeInteraction(guild, moderator)
    await cog._handle_warnings(again, target, True)
    results["clear_again_replies"] = again.replies

    # /punish status <user> must include the warning total and recent reasons.
    bot.add_warning(guild_id, target.id, moderator.id, "status check")
    status = FakeInteraction(guild, moderator)
    await cog._handle_status(status, target)
    results["status_replies"] = status.replies

    created = datetime.now(timezone.utc)
    embed = bot.bot._build_warn_staff_embed(
        member=target, moderator=moderator, reason="field test",
        created_at=created, total=3,
    )
    results["staff_embed"] = {
        "title": embed.title,
        "fields": {field.name: field.value for field in embed.fields},
    }
    dm = bot.bot._build_warn_dm_embed(
        guild_name="Test Guild", moderator_name="Mod", reason="field test",
        total=3, created_at=created,
    )
    results["dm_embed"] = {
        "title": dm.title,
        "fields": {field.name: field.value for field in dm.fields},
    }
    clear_embed = bot.bot._build_warn_clear_staff_embed(
        member=target, moderator=moderator, removed=2,
    )
    results["clear_embed"] = {
        "title": clear_embed.title,
        "fields": {field.name: field.value for field in clear_embed.fields},
    }

    # ---- Phase 2: a staff role is configured ------------------------- #
    # From here on the staff role is the gate: a member who has Discord's
    # Moderate Members permission but not the role may not moderate, and a
    # member who has the role (and nothing else) may.
    bot.bot.config["staff_role_id"] = staff_role.id
    results["count_before_phase2"] = bot.count_warnings(guild_id, target.id)

    refused = {}
    for label, interaction, call in (
        ("warn", FakeInteraction(guild, moderator), lambda i: cog._handle_warn(i, target, "nope")),
        ("warnings", FakeInteraction(guild, moderator), lambda i: cog._handle_warnings(i, target, True)),
        ("punish", FakeInteraction(guild, moderator), lambda i: cog._handle_punish(i, target, "1h", "nope")),
        ("pardon", FakeInteraction(guild, moderator), lambda i: cog._handle_pardon(i, target)),
        ("status", FakeInteraction(guild, moderator), lambda i: cog._handle_status(i, target)),
    ):
        await call(interaction)
        refused[label] = interaction.replies
    results["non_staff_refusals"] = refused
    results["count_after_non_staff_attempts"] = bot.count_warnings(guild_id, target.id)

    # Plain members are refused too, and told the same thing.
    plain_attempt = FakeInteraction(guild, plain)
    await cog._handle_warn(plain_attempt, target, "nope")
    results["plain_refusal"] = plain_attempt.replies

    # Holding the staff role is enough, with no Discord permission at all.
    allowed = FakeInteraction(guild, staff_only)
    await cog._handle_warn(allowed, target, "staff role warning")
    results["staff_only_replies"] = allowed.replies
    results["staff_only_count"] = bot.count_warnings(guild_id, target.id)
    staff_status = FakeInteraction(guild, staff_only)
    await cog._handle_status(staff_status, target)
    results["staff_only_status"] = staff_status.replies

    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(results, fh)


asyncio.run(main())
"""


def _isolated_test_env(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "SENTINEL_HOME": "",
        "SENTINEL_DATA": str(home / "data"),
        "SENTINEL_CONFIG": str(home / "config.json"),
        "PYTHONIOENCODING": "utf-8",
    }


def dump_bot_warnings() -> dict:
    with tempfile.TemporaryDirectory(prefix="pm-warnings-test-") as tmp:
        home = Path(tmp).resolve()
        out = home / "warnings.json"
        # Pointed at a file that does not exist, paths.py would fall back to
        # the checkout's ./config.json; give the child its own empty one.
        (home / "config.json").write_text("{}\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-c", _DUMP_BOT_WARNINGS, str(out), str(REPO_ROOT)],
            capture_output=True, encoding="utf-8", errors="replace",
            env=_isolated_test_env(home), cwd=str(home),
        )
        if result.returncode != 0 or not out.is_file():
            raise AssertionError(
                f"could not exercise the warning flow from bot.py "
                f"(exit {result.returncode}):\n{result.stdout}{result.stderr}"
            )
        return json.loads(out.read_text(encoding="utf-8"))


class BotWarningCommandTests(unittest.TestCase):
    """`/manage warn`, `/manage warnings` and the SQLite helpers behind them."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = dump_bot_warnings()

    def test_warn_records_a_trimmed_warning_and_confirms_it(self) -> None:
        self.assertEqual(self.result["first_count"], 1)
        self.assertEqual(self.result["stored_reason"], "spam in #general")
        self.assertEqual(self.result["stored_moderator"], 303)
        reply = "\n".join(self.result["first_replies"])
        self.assertIn("Warned <@202>", reply)
        self.assertIn("warning **#1**", reply)
        self.assertIn("spam in #general", reply)

    def test_second_warning_increments_the_running_total(self) -> None:
        self.assertEqual(self.result["second_count"], 2)
        self.assertIn("warning **#2**", "\n".join(self.result["second_replies"]))

    def test_warned_user_is_dmed_the_reason(self) -> None:
        self.assertEqual(self.result["dm_titles"], ["You've been warned in Test Guild"])
        # The warning DM is an embed; the fake records the embed title.
        self.assertEqual(self.result["dm_reasons"], [None])

    def test_refusals_do_not_record_anything(self) -> None:
        self.assertIn(
            "You can't warn yourself.", "\n".join(self.result["self_replies"])
        )
        self.assertIn(
            "server administrator", "\n".join(self.result["protected_replies"])
        )
        self.assertIn(
            "provide a reason", "\n".join(self.result["empty_replies"])
        )
        # Still exactly the two successful warnings.
        self.assertEqual(self.result["count_after_refusals"], 2)

    def test_warnings_list_shows_reason_and_moderator(self) -> None:
        listing = "\n".join(self.result["list_replies"])
        self.assertIn("**Warnings for <@202>:** 2", listing)
        self.assertIn("spam in #general", listing)
        self.assertIn("<@303>", listing)

    def test_clearing_is_refused_on_a_server_with_no_staff_role_yet(self) -> None:
        """No staff role configured: the refusal says how to set one.

        The old permission rule still applies in that state (see the phase-1
        flow), so a member with no permissions at all is refused and pointed at
        ``/manage setup staff_role:``.
        """
        reply = "\n".join(self.result["clear_denied_replies"])
        self.assertIn("staff role", reply)
        self.assertIn("/manage setup staff_role:", reply)
        self.assertEqual(self.result["count_after_denied_clear"], 2)

    # ---- Phase 2: the staff role is the gate --------------------------- #

    def test_every_moderation_command_refuses_a_non_staff_moderator(self) -> None:
        """Moderate Members is not enough once a staff role is configured.

        The harness calls all five handlers as a member who has Discord's
        *Moderate Members* permission but not the staff role; each must refuse
        with the same message (the role is mentioned) and change nothing.
        """
        refusals = self.result["non_staff_refusals"]
        self.assertEqual(
            sorted(refusals), ["pardon", "punish", "status", "warn", "warnings"]
        )
        for label, replies in refusals.items():
            with self.subTest(command=label):
                reply = "\n".join(replies)
                self.assertIn("Only members with the <@&606> role", reply)
                self.assertNotIn("Moderate Members", reply)
        # The refused attempts recorded no warning at all.
        self.assertEqual(
            self.result["count_after_non_staff_attempts"],
            self.result["count_before_phase2"],
        )
        self.assertIn(
            "Only members with the <@&606> role", "\n".join(self.result["plain_refusal"])
        )

    def test_the_staff_role_alone_is_enough(self) -> None:
        """A staff-role holder with no Discord permissions can moderate."""
        self.assertIn(
            "Warned <@202>", "\n".join(self.result["staff_only_replies"])
        )
        self.assertEqual(
            self.result["staff_only_count"], self.result["count_before_phase2"] + 1
        )
        # ... and can read the command that shows member history.
        self.assertIn(
            "Punishment status for <@202>", "\n".join(self.result["staff_only_status"])
        )

    def test_clear_removes_every_warning_and_reports_the_count(self) -> None:
        self.assertIn("Cleared **2** warning(s)", "\n".join(self.result["clear_replies"]))
        self.assertEqual(self.result["count_after_clear"], 0)
        self.assertIn(
            "no warnings to clear", "\n".join(self.result["clear_again_replies"])
        )

    def test_status_includes_the_warning_total(self) -> None:
        status = "\n".join(self.result["status_replies"])
        self.assertIn("**Warnings:** 1", status)
        self.assertIn("status check", status)

    def test_embeds_carry_the_expected_fields(self) -> None:
        staff = self.result["staff_embed"]
        self.assertEqual(staff["title"], "Member warned")
        self.assertEqual(staff["fields"]["Total warnings"], "3")
        self.assertEqual(staff["fields"]["Reason"], "field test")
        self.assertEqual(staff["fields"]["User"], "<@202> (`202`)")
        dm = self.result["dm_embed"]
        self.assertEqual(dm["title"], "You've been warned in Test Guild")
        self.assertEqual(dm["fields"]["Reason"], "field test")
        self.assertEqual(dm["fields"]["Total warnings"], "3")
        cleared = self.result["clear_embed"]
        self.assertEqual(cleared["title"], "Warnings cleared")
        self.assertEqual(cleared["fields"]["Warnings removed"], "2")


# --------------------------------------------------------------------------- #
# dashboard.py (aiohttp test client with an in-memory warnings table)
# --------------------------------------------------------------------------- #
GUILD_ID = 1234567890123456789
OWNER_ID = 222222222222222222
MEMBER_ID = 333333333333333333


class MemoryWarningsDb:
    """Just enough sqlite for the dashboard's injected db_* callables."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE warnings ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, "
            "user_id INTEGER, moderator_id INTEGER, reason TEXT, created_at TEXT)"
        )

    def fetchall(self, query, params=()):
        return list(self.conn.execute(query, params).fetchall())

    def fetchone(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def execute(self, query, params=()):
        self.conn.execute(query, params)
        self.conn.commit()


class WarnMember:
    def __init__(self, user_id: int, name: str) -> None:
        self.id = user_id
        self.display_name = name
        self.mention = f"<@{user_id}>"
        self.avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/x.png"
        self.display_avatar = type("Avatar", (), {"url": self.avatar_url})()


class WarnGuild:
    id = GUILD_ID
    name = "Warn Guild"
    owner_id = OWNER_ID

    def __init__(self, members: list[WarnMember]) -> None:
        self._members = {member.id: member for member in members}

    def get_member(self, user_id: int):
        return self._members.get(int(user_id))

    async def fetch_member(self, user_id: int):
        raise discord.NotFound(
            _FakeHttpResponse(), {"message": "Unknown Member", "code": 10007}
        )


class WarnFakeBot:
    def __init__(self, protected_reason=None) -> None:
        self.guild = WarnGuild([WarnMember(MEMBER_ID, "Warn Target")])
        self.config = {"server_id": GUILD_ID}
        self.user = None
        self.protected_reason = protected_reason
        self.staff_embeds: list = []
        self.warn_dms: list = []

    def get_guild(self, guild_id):
        return self.guild if int(guild_id) == GUILD_ID else None

    def is_ready(self):
        return True

    def _build_warn_staff_embed(self, **kwargs):
        self.staff_embeds.append(kwargs)
        return "staff-embed"

    def _build_warn_dm_embed(self, **kwargs):
        return "dm-embed"

    def _build_warn_clear_staff_embed(self, **kwargs):
        self.staff_embeds.append(kwargs)
        return "clear-embed"

    async def _send_staff_embed(self, guild, embed, *, content=None):
        return None

    async def _dm_embed(self, member, embed, *, content=None):
        self.warn_dms.append((member.id, embed))
        return True


class DashboardWarningApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = MemoryWarningsDb()
        self.bot = WarnFakeBot()
        self.server = DashboardServer(
            self.bot,
            save_config=lambda _config: None,
            db_fetchall=self.db.fetchall,
            db_fetchone=self.db.fetchone,
            db_execute=self.db.execute,
            archive_punishment=lambda *_args, **_kwargs: None,
            get_guild_config=lambda *_args: {"punish_role_id": 1, "post_role_id": 2},
            get_staff_channel_id=lambda *_args: None,
            should_dm_user=lambda *_args: True,
            is_protected_member=lambda *_args, **_kwargs: self.bot.protected_reason,
            parse_duration=lambda _value: None,
            format_duration=lambda value: str(value),
        )
        self.server._token = "T" * 48

    async def _login(self, client) -> str:
        login = await client.post("/api/login", json={"token": self.server._token})
        self.assertEqual(login.status, 200)
        return (await login.json())["csrfToken"]

    async def test_warn_list_and_clear_round_trip(self) -> None:
        async with TestClient(TestServer(self.server._build_app())) as client:
            csrf = await self._login(client)
            url = f"/api/guilds/{GUILD_ID}/warnings"

            created = await client.post(
                url,
                json={"userId": str(MEMBER_ID), "reason": "spam"},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(created.status, 201)
            body = await created.json()
            self.assertTrue(body["warned"])
            self.assertEqual(body["totalForMember"], 1)
            self.assertEqual(body["userId"], str(MEMBER_ID))

            second = await client.post(
                url,
                json={"userId": str(MEMBER_ID), "reason": "still spamming"},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual((await second.json())["totalForMember"], 2)

            listing = await client.get(url)
            self.assertEqual(listing.status, 200)
            payload = await listing.json()
            self.assertEqual(payload["total"], 2)
            self.assertEqual(len(payload["warnings"]), 2)
            newest = payload["warnings"][0]
            # Dashboard actions are attributed to the owner and marked.
            self.assertEqual(newest["moderator_id"], str(OWNER_ID))
            self.assertEqual(newest["reason"], "[Dashboard] still spamming")
            self.assertEqual(newest["memberName"], "Warn Target")
            self.assertEqual(newest["totalForMember"], 2)
            # Snowflakes must cross the wire as strings (the browser rounds
            # JSON numbers above 2**53).
            self.assertIsInstance(newest["user_id"], str)

            cleared = await client.delete(
                f"{url}/{MEMBER_ID}", headers={"X-CSRF-Token": csrf}
            )
            self.assertEqual(cleared.status, 200)
            self.assertEqual((await cleared.json())["cleared"], 2)
            empty = await (await client.get(url)).json()
            self.assertEqual(empty["total"], 0)
            self.assertEqual(empty["warnings"], [])
            # The clear is announced to staff and is not silent.
            self.assertEqual(self.bot.staff_embeds[-1]["removed"], 2)

    async def test_warn_sends_staff_embed_and_dm(self) -> None:
        async with TestClient(TestServer(self.server._build_app())) as client:
            csrf = await self._login(client)
            response = await client.post(
                f"/api/guilds/{GUILD_ID}/warnings",
                json={"userId": str(MEMBER_ID), "reason": "reason text"},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(response.status, 201)
        self.assertEqual(len(self.bot.staff_embeds), 1)
        self.assertEqual(self.bot.staff_embeds[0]["reason"], "[Dashboard] reason text")
        self.assertEqual(self.bot.staff_embeds[0]["total"], 1)
        self.assertEqual(self.bot.warn_dms, [(MEMBER_ID, "dm-embed")])

    async def test_warn_validates_reason_and_protected_members(self) -> None:
        async with TestClient(TestServer(self.server._build_app())) as client:
            csrf = await self._login(client)
            url = f"/api/guilds/{GUILD_ID}/warnings"

            blank = await client.post(
                url,
                json={"userId": str(MEMBER_ID), "reason": "   "},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(blank.status, 400)
            self.assertIn("reason", (await blank.json())["error"].lower())

            self.bot.protected_reason = "That user is a server administrator."
            protected = await client.post(
                url,
                json={"userId": str(MEMBER_ID), "reason": "nope"},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(protected.status, 403)

            self.bot.protected_reason = None
            unknown = await client.post(
                url,
                json={"userId": "999999999999999999", "reason": "who?"},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(unknown.status, 404)
        self.assertEqual(self.db.fetchone("SELECT COUNT(*) AS c FROM warnings")["c"], 0)

    async def test_warn_endpoints_require_auth_and_csrf(self) -> None:
        async with TestClient(TestServer(self.server._build_app())) as client:
            url = f"/api/guilds/{GUILD_ID}/warnings"
            anonymous = await client.get(url)
            self.assertEqual(anonymous.status, 401)

            csrf = await self._login(client)
            no_csrf = await client.post(
                url, json={"userId": str(MEMBER_ID), "reason": "x"}
            )
            self.assertEqual(no_csrf.status, 403)

            missing = await client.delete(
                f"{url}/{MEMBER_ID}", headers={"X-CSRF-Token": csrf}
            )
            self.assertEqual(missing.status, 404)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
