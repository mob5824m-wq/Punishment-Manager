"""Tests for the ticket system (tickets.py).

The behaviour worth pinning down here is the *permission split* the feature was
asked for:

* a normal member can open a ticket — that is the whole point of the panel —
  and cannot do anything else: no listing, no viewing somebody else's, no
  claiming, no reopening;
* staff can do all of it, and the check is re-run when a button is pressed, not
  just when a command is shown (buttons outlive the configuration that produced
  them, and a server can relax what a command is shown to);
* tickets really are private: thread tickets add the opener, channel tickets
  deny `@everyone`.

They also cover the two shapes ("thread" and "channel", including per-category
overrides — the "both" configuration), claim/close/reopen state changes, and the
panel buttons themselves (one per category, resolved against the *current*
configuration, so a stale panel explains itself instead of breaking).

Run with either:
    python -m pytest tests/test_tickets.py
    python tests/test_tickets.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import discord  # noqa: E402

import bot as bot_module  # noqa: E402
import store  # noqa: E402
import tickets  # noqa: E402
from tickets import TicketError, TicketMixin  # noqa: E402


def _patch_channel_types() -> list:
    """Make the channel fakes satisfy the module's ``isinstance`` checks.

    ``tickets.py`` refuses to create a thread unless the panel channel really is
    a text channel (and the same for threads and categories); that is the check
    that keeps a misconfigured panel from ending in a traceback. A fake cannot
    pass it without either a live gateway connection or this swap of the names
    the module looks up, so each is replaced for the duration of one test.
    """
    patchers = []
    for name, fake in (
        ("TextChannel", _FakeTextChannel),
        ("Thread", _FakeThread),
        ("CategoryChannel", _FakeCategoryChannel),
    ):
        patcher = patch.object(discord, name, fake)
        patcher.start()
        patchers.append(patcher)
    return patchers


class _IsolatedStoreMixin:
    """Run a test against a temporary database and a stubbed config writer.

    ``paths`` resolves its locations when it is first imported, so setting
    ``SENTINEL_DATA`` inside a test would be too late (another test module has
    already imported it) and ``bot.init_db()`` would create ``data/`` inside the
    checkout. Patching the two callables is narrower and reliably restored:
    ``store.db_path`` is what tickets.py reads, and ``tickets.save_config`` is
    what the config writers call — so a test can neither pollute the developer's
    ``config.json`` nor leave a database behind.
    """

    def _isolate(self) -> None:
        self.home = tempfile.TemporaryDirectory(prefix="pm-tickets-test-")
        self.db_file = Path(self.home.name) / "sentinel.db"
        self.saved_configs: list[dict] = []
        for target, replacement in (
            (store, lambda: str(self.db_file)),
            (bot_module, str(self.db_file)),
            (tickets, lambda config: self.saved_configs.append(config)),
        ):
            patcher = (
                patch.object(store, "db_path", replacement)
                if target is store
                else patch.object(
                    target,
                    "DB_PATH" if target is bot_module else "save_config",
                    replacement,
                )
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in _patch_channel_types():
            self.addCleanup(patcher.stop)
        bot_module.init_db()

    def _cleanup(self) -> None:
        self.home.cleanup()


GUILD_ID = 123456789012345678
OPENER_ID = 111111111111111111
STAFF_ID = 222222222222222222
OUTSIDER_ID = 333333333333333333
BOT_ID = 999999999999999999
STAFF_ROLE_ID = 444444444444444444
OTHER_ROLE_ID = 555555555555555555
PANEL_CHANNEL_ID = 666666666666666666
LOG_CHANNEL_ID = 777777777777777777
CATEGORY_CHANNEL_ID = 888888888888888888


def _permissions(**flags) -> SimpleNamespace:
    """A Permissions-like object with every flag defaulting to False."""
    names = (
        "administrator",
        "manage_guild",
        "moderate_members",
        "manage_channels",
        "manage_roles",
    )
    values = {name: bool(flags.get(name, False)) for name in names}
    return SimpleNamespace(**values)


class _FakeRole:
    """A role the permission overwrites can use as a dict key.

    Discord roles hash by id; ``types.SimpleNamespace`` is unhashable, which is
    why the overwrite map cannot use one.
    """

    def __init__(self, role_id: int, name: str = "role", members=()) -> None:
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"
        self.members = list(members)

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        return getattr(other, "id", None) == self.id


class _FakeMember:
    """A member good enough for the ticket module: ids, roles, permissions, DMs."""

    def __init__(self, member_id: int, *, roles=(), **permissions) -> None:
        self.id = member_id
        self.bot = False
        self.guild_permissions = _permissions(**permissions)
        self.roles = [_FakeRole(role_id) for role_id in roles]
        self.mention = f"<@{member_id}>"
        self.display_name = f"member-{member_id}"
        self.dms: list[object] = []

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        return getattr(other, "id", None) == self.id

    async def send(self, *, embed=None, allowed_mentions=None) -> None:
        self.dms.append(embed)


def _member(member_id: int, *, roles=(), **permissions) -> _FakeMember:
    return _FakeMember(member_id, roles=roles, **permissions)


class _FakeThread:
    """Stands in for discord.Thread (patched into discord during a test).

    Subclassing the real classes is not an option — discord.py exposes its
    channel state through read-only properties — so the tests swap the names
    ``tickets.py`` checks with ``isinstance`` instead (see
    :func:`_patch_channel_types`).
    """

    def __init__(self, thread_id: int, name: str) -> None:
        self.id = thread_id
        self.name = name
        self.added: list[int] = []
        self.sent: list[dict] = []
        self.locked = False
        self.archived = False
        self.deleted = False
        self.messages: dict[int, "_FakeMessage"] = {}
        self._next_id = 1000

    async def add_user(self, member) -> None:
        self.added.append(member.id)

    async def send(self, *, content=None, embed=None, view=None, allowed_mentions=None):
        message = _FakeMessage(self, self._next_id)
        self._next_id += 1
        self.messages[message.id] = message
        self.sent.append({"content": content, "embed": embed, "view": view})
        return message

    async def edit(self, **kwargs):
        self.locked = bool(kwargs.get("locked", self.locked))
        self.archived = bool(kwargs.get("archived", self.archived))
        if kwargs.get("name"):
            self.name = kwargs["name"]
        return self

    async def delete(self, **_kwargs) -> None:
        self.deleted = True

    async def fetch_message(self, message_id):
        return self.messages[int(message_id)]


class _FakeCategoryChannel:
    """Stands in for discord.CategoryChannel (the parent of channel tickets)."""

    def __init__(self, category_id: int, name: str = "Tickets") -> None:
        self.id = category_id
        self.name = name
        self.mention = f"<#{category_id}>"


class _FakeMessage:
    def __init__(self, channel, message_id: int) -> None:
        self.id = message_id
        self.channel = channel
        self.author = SimpleNamespace(id=BOT_ID)
        self.edits: list[dict] = []
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self

    async def delete(self) -> None:
        self.deleted = True


class _FakeTextChannel:
    """A text channel that records what the bot posts and creates."""

    def __init__(self, guild, channel_id: int, name: str = "open-a-ticket") -> None:
        self.id = channel_id
        self.guild = guild
        self.name = name
        self.category = None
        self.mention = f"<#{channel_id}>"
        self.sent: list[dict] = []
        self.messages: dict[int, _FakeMessage] = {}
        self._next_id = 500
        self.permission_changes: list[dict] = []

    def permissions_for(self, _member):
        return SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)

    async def send(self, *, content=None, embed=None, view=None, allowed_mentions=None):
        message = _FakeMessage(self, self._next_id)
        self._next_id += 1
        self.messages[message.id] = message
        self.sent.append({"content": content, "embed": embed, "view": view})
        return message

    async def fetch_message(self, message_id):
        message = self.messages.get(int(message_id))
        if message is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        return message

    async def create_thread(self, *, name, type, invitable, auto_archive_duration, reason=None):
        thread = _FakeThread(9000 + len(self.sent), name)
        # Register it with the guild so lookups by id resolve, exactly like a
        # real thread would.
        self.guild._channels[thread.id] = thread
        self.thread = thread
        self.created_thread = {
            "name": name,
            "type": type,
            "invitable": invitable,
            "auto_archive_duration": auto_archive_duration,
        }
        return thread

    async def set_permissions(self, target, **kwargs) -> None:
        self.permission_changes.append({"target": getattr(target, "id", target), **kwargs})

    async def edit(self, **kwargs):
        if kwargs.get("name"):
            self.name = kwargs["name"]
        return self


class _FakeGuild:
    def __init__(self, *, with_panel: bool = True) -> None:
        self.id = GUILD_ID
        self.name = "Test Guild"
        self.default_role = _FakeRole(GUILD_ID, "@everyone")
        self.me = _FakeMember(BOT_ID)
        self.owner_id = STAFF_ID
        self.text_channels = []
        self.categories = []
        self.created_channels: list[dict] = []
        self._channels: dict[int, object] = {}
        self._roles: dict[int, object] = {}
        panel = _FakeTextChannel(self, PANEL_CHANNEL_ID)
        log = _FakeTextChannel(self, LOG_CHANNEL_ID, name="staff")
        self.panel_channel = panel
        self.log_channel = log
        for channel in (panel, log):
            self._channels[channel.id] = channel
            self.text_channels.append(channel)

    def add_role(self, role_id: int, name: str, members=()) -> _FakeRole:
        role = _FakeRole(role_id, name, members)
        self._roles[role_id] = role
        return role

    def get_role(self, role_id):
        return self._roles.get(int(role_id))

    def get_channel(self, channel_id):
        return self._channels.get(int(channel_id))

    def get_member(self, member_id):
        return self._members.get(int(member_id)) if hasattr(self, "_members") else None

    def set_members(self, members) -> None:
        self._members = {member.id: member for member in members}

    async def create_text_channel(self, *, name, category=None, overwrites=None, topic=None, reason=None):
        channel = _FakeTextChannel(self, 12_000 + len(self.created_channels), name=name)
        channel.category = category
        channel.overwrites = overwrites
        self.created_channels.append(
            {"name": name, "category": category, "overwrites": overwrites, "topic": topic}
        )
        self._channels[channel.id] = channel
        return channel


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[dict] = []
        self.modals: list[object] = []

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, *, ephemeral=False) -> None:
        self.deferred = True

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False, allowed_mentions=None):
        self.messages.append(
            {"content": content, "embed": embed, "view": view, "ephemeral": ephemeral}
        )

    async def send_modal(self, modal) -> None:
        self.modals.append(modal)


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.embeds: list[object] = []

    async def send(self, content=None, *, embed=None, ephemeral=False, view=None, allowed_mentions=None):
        if content is not None:
            self.messages.append({"content": content, "ephemeral": ephemeral})
        if embed is not None:
            self.embeds.append(embed)


class _FakeInteraction:
    def __init__(self, *, user, guild, channel_id=PANEL_CHANNEL_ID, data=None) -> None:
        self.user = user
        self.guild = guild
        self.guild_id = guild.id
        self.channel_id = channel_id
        self.data = data or {}
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()

    def replies(self) -> list[str]:
        return [item["content"] for item in self.response.messages + self.followup.messages]


class _FakeBot:
    def __init__(self, config, guild=None) -> None:
        self.config = config
        self.user = SimpleNamespace(id=BOT_ID)
        self._guild = guild
        self._cogs: dict[str, object] = {}

    @property
    def cogs(self):
        return self._cogs

    def get_guild(self, guild_id):
        return self._guild if self._guild is not None and int(guild_id) == self._guild.id else None


def _isolated_config(guild_id: int = GUILD_ID, **settings) -> dict:
    return {"server_id": guild_id, "staff_role_id": STAFF_ROLE_ID, "tickets": settings}


class TicketConfigTests(_IsolatedStoreMixin, unittest.TestCase):
    """Reading/writing the per-guild ticket configuration."""

    def setUp(self) -> None:
        self._isolate()
        self.addCleanup(self._cleanup)

    def test_writing_settings_persists_them(self) -> None:
        config = {"tickets": {}}
        tickets.upsert_category(config, GUILD_ID, {"category_id": "a", "label": "Help"})
        # The config was handed to the writer, so it survives a restart.
        self.assertTrue(self.saved_configs)
        self.assertEqual(
            tickets.get_guild_tickets(self.saved_configs[-1], GUILD_ID)["categories"][0]["label"],
            "Help",
        )

    def test_missing_guild_reads_as_usable_defaults(self) -> None:
        settings = tickets.get_guild_tickets({}, GUILD_ID)
        self.assertEqual(settings["mode"], tickets.MODE_THREAD)
        self.assertEqual(settings["categories"], [])
        self.assertIsNone(settings["panel"])
        self.assertEqual(settings["number"], 0)

    def test_numbering_is_per_guild_and_monotonic(self) -> None:
        config = {"tickets": {}}
        self.assertEqual(tickets.take_next_number(config, GUILD_ID), 1)
        self.assertEqual(tickets.take_next_number(config, GUILD_ID), 2)
        # A different server has its own counter.
        self.assertEqual(tickets.take_next_number(config, 4242), 1)
        self.assertEqual(tickets.take_next_number(config, GUILD_ID), 3)

    def test_categories_round_trip_and_normalize(self) -> None:
        config = {"tickets": {}}
        stored = tickets.upsert_category(
            config,
            GUILD_ID,
            {
                "category_id": "abc12345",
                "label": "  General help  ",
                "emoji": "❓",
                "mode": "CHANNEL",          # normalized to lowercase
                "staff_role_id": str(STAFF_ROLE_ID),
                "ask_subject": False,
            },
        )
        self.assertEqual(stored["label"], "General help")
        self.assertEqual(stored["mode"], tickets.MODE_CHANNEL)
        self.assertEqual(stored["staff_role_id"], STAFF_ROLE_ID)
        self.assertFalse(stored["ask_subject"])

        reread = tickets.get_guild_tickets(config, GUILD_ID)
        self.assertEqual(len(reread["categories"]), 1)
        self.assertEqual(tickets.find_category(reread, "general help")["category_id"], "abc12345")

    def test_unknown_mode_falls_back_to_the_default(self) -> None:
        """A hand-edited config must not stop tickets from working."""
        config = {
            "tickets": {
                str(GUILD_ID): {
                    "mode": "carrier-pigeon",
                    "categories": [
                        {"category_id": "x", "label": "Help", "mode": "nonsense"}
                    ],
                }
            }
        }
        settings = tickets.get_guild_tickets(config, GUILD_ID)
        self.assertEqual(settings["mode"], tickets.MODE_THREAD)
        self.assertEqual(
            tickets.effective_mode(settings, settings["categories"][0]),
            tickets.MODE_THREAD,
        )

    def test_category_mode_overrides_the_server_default(self) -> None:
        settings = {"mode": tickets.MODE_THREAD, "categories": []}
        category = {"mode": tickets.MODE_CHANNEL}
        self.assertEqual(tickets.effective_mode(settings, category), tickets.MODE_CHANNEL)
        # No override: the server default applies.
        self.assertEqual(tickets.effective_mode(settings, {"mode": None}), tickets.MODE_THREAD)

    def test_removing_a_missing_category_is_an_error(self) -> None:
        with self.assertRaises(TicketError):
            tickets.remove_category({"tickets": {}}, GUILD_ID, "nope")

    def test_category_cap_is_enforced(self) -> None:
        config = {"tickets": {}}
        for index in range(tickets.MAX_CATEGORIES):
            tickets.upsert_category(
                config, GUILD_ID, {"category_id": f"c{index}", "label": f"Cat {index}"}
            )
        with self.assertRaises(TicketError):
            tickets.upsert_category(
                config, GUILD_ID, {"category_id": "overflow", "label": "Too many"}
            )


class TicketPermissionTests(unittest.TestCase):
    """Who counts as staff — the check every read and write path goes through."""

    def setUp(self) -> None:
        self.config = {"server_id": GUILD_ID, "staff_role_id": STAFF_ROLE_ID}

    def test_administrator_and_moderator_permissions_count_as_staff(self) -> None:
        self.assertTrue(
            tickets.is_ticket_staff(_member(1, administrator=True), self.config, GUILD_ID)
        )
        self.assertTrue(
            tickets.is_ticket_staff(_member(1, moderate_members=True), self.config, GUILD_ID)
        )
        self.assertTrue(
            tickets.is_ticket_staff(_member(1, manage_guild=True), self.config, GUILD_ID)
        )

    def test_the_configured_staff_role_counts_as_staff(self) -> None:
        self.assertTrue(
            tickets.is_ticket_staff(_member(1, roles=[STAFF_ROLE_ID]), self.config, GUILD_ID)
        )

    def test_a_category_role_counts_as_staff(self) -> None:
        category = {"staff_role_id": OTHER_ROLE_ID}
        self.assertTrue(
            tickets.is_ticket_staff(
                _member(1, roles=[OTHER_ROLE_ID]), self.config, GUILD_ID, category
            )
        )

    def test_a_plain_member_is_not_staff(self) -> None:
        member = _member(1, roles=[4242])
        self.assertFalse(tickets.is_ticket_staff(member, self.config, GUILD_ID))
        self.assertFalse(
            tickets.is_ticket_staff(member, self.config, GUILD_ID, {"staff_role_id": OTHER_ROLE_ID})
        )
        # A bot account is never staff, whatever it holds.
        self.assertFalse(
            tickets.is_ticket_staff(
                SimpleNamespace(
                    id=BOT_ID,
                    bot=True,
                    guild_permissions=_permissions(administrator=True),
                ),
                self.config,
                GUILD_ID,
            )
        )


class TicketFlowTests(_IsolatedStoreMixin, unittest.IsolatedAsyncioTestCase):
    """Opening, claiming and closing a ticket, against Discord-shaped fakes."""

    def setUp(self) -> None:
        self._isolate()
        self.guild = _FakeGuild()
        self.staff_role = self.guild.add_role(STAFF_ROLE_ID, "Staff")
        self.opener = _member(OPENER_ID, roles=[OTHER_ROLE_ID])
        self.staff = _member(STAFF_ID, roles=[STAFF_ROLE_ID])
        self.outsider = _member(OUTSIDER_ID)
        # A role knows its members: that is how the module decides who to add to
        # a private thread (a role cannot be granted access to one).
        self.staff_role.members = [self.staff]
        self.guild.set_members([self.opener, self.staff, self.outsider])
        self.config = {
            "server_id": GUILD_ID,
            "staff_role_id": STAFF_ROLE_ID,
            "tickets": {
                str(GUILD_ID): {
                    "mode": tickets.MODE_THREAD,
                    "log_channel_id": LOG_CHANNEL_ID,
                    "panel": {"channel_id": PANEL_CHANNEL_ID, "message_id": 42},
                    "categories": [
                        {"category_id": "help", "label": "General help", "ask_subject": True},
                        {"category_id": "report", "label": "Report", "mode": "channel"},
                    ],
                }
            },
        }
        self.bot = _FakeBot(self.config, self.guild)
        self.mixin = TicketMixin(self.bot, lambda _config: None)
        self.bot.cogs["SentinelCog"] = self.mixin

    def tearDown(self) -> None:
        self._cleanup()

    def _settings(self) -> dict:
        return tickets.get_guild_tickets(self.bot.config, GUILD_ID)

    def _category(self, category_id: str) -> dict:
        return tickets.find_panel_category(self._settings(), category_id)

    async def _open(self, category_id: str = "help", subject: str | None = "Help me"):
        return await tickets.open_ticket(
            self.bot,
            guild=self.guild,
            opener=self.opener,
            category=self._category(category_id),
            subject=subject,
        )

    async def test_thread_ticket_is_private_and_adds_the_opener(self) -> None:
        ticket = await self._open()
        thread = self.guild.get_channel(ticket["thread_id"])
        self.assertEqual(self.guild.panel_channel.created_thread["type"], discord.ChannelType.private_thread)
        self.assertFalse(self.guild.panel_channel.created_thread["invitable"])
        self.assertIn(OPENER_ID, thread.added)
        # Staff holding the category's staff role are added too (they must be,
        # or a private thread would be invisible to the team).
        self.assertIn(STAFF_ID, thread.added)
        self.assertEqual(thread.sent[0]["embed"].title, "Ticket #0001 — General help")

    async def test_channel_ticket_denies_everyone_and_allows_the_opener(self) -> None:
        ticket = await self._open("report")
        created = self.guild.created_channels[-1]
        self.assertEqual(ticket["mode"], tickets.MODE_CHANNEL)
        overwrites = created["overwrites"]
        self.assertFalse(overwrites[self.guild.default_role].view_channel)
        self.assertTrue(overwrites[self.opener].view_channel)
        self.assertTrue(overwrites[self.staff_role].view_channel)
        self.assertTrue(overwrites[self.guild.me].manage_channels)

    async def test_staff_are_notified_with_claim_and_close_buttons(self) -> None:
        ticket = await self._open()
        notice = self.guild.log_channel.sent[-1]
        self.assertIn("Ticket #0001 opened", notice["embed"].title)
        custom_ids = [child.custom_id for child in notice["view"].children]
        self.assertIn(f"sentinel:tk:claim:{ticket['id']}", custom_ids)
        self.assertIn(f"sentinel:tk:close:{ticket['id']}", custom_ids)

    async def test_a_second_ticket_in_the_same_category_is_refused(self) -> None:
        await self._open()
        with self.assertRaises(TicketError):
            await self._open()

    async def test_claiming_adds_the_claimer_and_records_it(self) -> None:
        ticket = await self._open()
        claimed = await tickets.claim_ticket(self.bot, self.guild, ticket, self.staff)
        self.assertEqual(claimed["status"], tickets.STATUS_CLAIMED)
        self.assertEqual(claimed["claimed_by"], STAFF_ID)
        self.assertIn(STAFF_ID, self.guild.get_channel(ticket["thread_id"]).added)
        # The in-ticket header was refreshed, so the buttons now show "Claimed".
        self.assertTrue(
            any("Ticket #0001" in (edit.get("embed").title or "") for edit in self.guild.get_channel(ticket["thread_id"]).messages[1000].edits)
        )

    async def test_closing_locks_the_ticket_and_reopening_unlocks_it(self) -> None:
        ticket = await self._open()
        closed = await tickets.close_ticket(
            self.bot, self.guild, ticket, self.staff, "Sorted out"
        )
        thread = self.guild.get_channel(ticket["thread_id"])
        self.assertEqual(closed["status"], tickets.STATUS_CLOSED)
        self.assertEqual(closed["closed_by"], STAFF_ID)
        self.assertEqual(closed["close_reason"], "Sorted out")
        self.assertTrue(thread.locked)
        self.assertTrue(thread.name.startswith("closed-"))

        reopened = await tickets.reopen_ticket(self.bot, self.guild, closed)
        self.assertEqual(reopened["status"], tickets.STATUS_OPEN)
        self.assertFalse(thread.locked)
        self.assertFalse(thread.name.startswith("closed-"))

    async def test_closing_a_closed_ticket_is_refused(self) -> None:
        ticket = await self._open()
        await tickets.close_ticket(self.bot, self.guild, ticket, self.staff, None)
        with self.assertRaises(TicketError):
            await tickets.close_ticket(self.bot, self.guild, ticket, self.staff, None)

    async def test_deleting_a_ticket_removes_the_channel_and_the_row(self) -> None:
        ticket = await self._open()
        await tickets.delete_ticket(self.bot, self.guild, ticket)
        self.assertTrue(self.guild.get_channel(ticket["thread_id"]).deleted)
        self.assertIsNone(tickets.get_ticket(ticket["id"]))

    async def test_ticket_ids_resolve_by_number_and_by_channel(self) -> None:
        ticket = await self._open()
        self.assertEqual(tickets.find_ticket(GUILD_ID, "#1")["id"], ticket["id"])
        self.assertEqual(tickets.find_ticket(GUILD_ID, ticket["id"])["id"], ticket["id"])
        self.assertEqual(
            tickets.get_ticket_by_thread(GUILD_ID, ticket["thread_id"])["id"], ticket["id"]
        )
        self.assertIsNone(tickets.find_ticket(GUILD_ID, "999"))


class TicketCommandTests(_IsolatedStoreMixin, unittest.IsolatedAsyncioTestCase):
    """The slash commands, including what a normal member is refused."""

    def setUp(self) -> None:
        self._isolate()
        self.guild = _FakeGuild()
        self.staff_role = self.guild.add_role(STAFF_ROLE_ID, "Staff")
        self.opener = _member(OPENER_ID)
        self.staff = _member(STAFF_ID, roles=[STAFF_ROLE_ID])
        self.guild.set_members([self.opener, self.staff])
        self.config = {
            "server_id": GUILD_ID,
            "staff_role_id": STAFF_ROLE_ID,
            "tickets": {
                str(GUILD_ID): {
                    "log_channel_id": LOG_CHANNEL_ID,
                    "panel": {"channel_id": PANEL_CHANNEL_ID, "message_id": 42},
                    "categories": [
                        {"category_id": "help", "label": "General help"}
                    ],
                }
            },
        }
        self.bot = _FakeBot(self.config, self.guild)
        self.mixin = TicketMixin(self.bot, lambda _config: None)
        self.bot.cogs["SentinelCog"] = self.mixin

    def tearDown(self) -> None:
        self.home.cleanup()

    async def _open_ticket(self):
        return await tickets.open_ticket(
            self.bot,
            guild=self.guild,
            opener=self.opener,
            category=tickets.find_panel_category(
                tickets.get_guild_tickets(self.bot.config, GUILD_ID), "help"
            ),
            subject="Please help",
        )

    async def test_a_member_cannot_list_or_view_tickets(self) -> None:
        await self._open_ticket()
        for handler, kwargs in (
            (self.mixin.tickets_list.callback, {}),
            (self.mixin.tickets_view.callback, {"ticket": "#1"}),
            (self.mixin.tickets_claim.callback, {"ticket": "#1"}),
            (self.mixin.tickets_reopen.callback, {"ticket": "#1"}),
        ):
            with self.subTest(handler=handler.__name__):
                interaction = _FakeInteraction(user=self.opener, guild=self.guild)
                await handler(self.mixin, interaction, **kwargs)
                self.assertTrue(
                    any(
                        "only staff" in reply.lower()
                        for reply in interaction.replies()
                    ),
                    interaction.replies(),
                )

    async def test_staff_can_list_and_view_tickets(self) -> None:
        ticket = await self._open_ticket()
        listing = _FakeInteraction(user=self.staff, guild=self.guild)
        await self.mixin.tickets_list.callback(self.mixin, listing)
        self.assertTrue(any("#0001" in reply for reply in listing.replies()), listing.replies())

        detail = _FakeInteraction(user=self.staff, guild=self.guild)
        await self.mixin.tickets_view.callback(self.mixin, detail, ticket="#0001")
        self.assertEqual(len(detail.followup.embeds), 1)
        self.assertIn("Ticket #0001", detail.followup.embeds[0].title)

    async def test_staff_can_claim_and_close_from_inside_the_ticket(self) -> None:
        ticket = await self._open_ticket()
        claim = _FakeInteraction(
            user=self.staff, guild=self.guild, channel_id=ticket["thread_id"]
        )
        await self.mixin.tickets_claim.callback(self.mixin, claim)
        self.assertEqual(tickets.get_ticket(ticket["id"])["status"], tickets.STATUS_CLAIMED)

        close = _FakeInteraction(
            user=self.staff, guild=self.guild, channel_id=ticket["thread_id"]
        )
        await self.mixin.tickets_close.callback(self.mixin, close, reason="Done")
        self.assertEqual(tickets.get_ticket(ticket["id"])["status"], tickets.STATUS_CLOSED)
        self.assertEqual(tickets.get_ticket(ticket["id"])["close_reason"], "Done")

    async def test_the_opener_can_withdraw_their_own_ticket_only(self) -> None:
        ticket = await self._open_ticket()
        own = _FakeInteraction(
            user=self.opener, guild=self.guild, channel_id=ticket["thread_id"]
        )
        await self.mixin.tickets_close.callback(self.mixin, own)
        self.assertEqual(tickets.get_ticket(ticket["id"])["status"], tickets.STATUS_CLOSED)

    async def test_a_member_cannot_close_someone_elses_ticket(self) -> None:
        ticket = await self._open_ticket()
        outsider = _member(OUTSIDER_ID)
        self.guild.set_members([self.opener, self.staff, outsider])
        other = _FakeInteraction(user=outsider, guild=self.guild, channel_id=ticket["thread_id"])
        await self.mixin.tickets_close.callback(self.mixin, other)
        self.assertEqual(tickets.get_ticket(ticket["id"])["status"], tickets.STATUS_OPEN)
        self.assertTrue(
            any("only staff" in reply.lower() for reply in other.replies()), other.replies()
        )

    async def test_categories_command_lists_modes(self) -> None:
        interaction = _FakeInteraction(user=self.staff, guild=self.guild)
        await self.mixin.tickets_categories.callback(self.mixin, interaction)
        reply = "\n".join(interaction.replies())
        self.assertIn("General help", reply)
        self.assertIn(tickets.MODE_THREAD, reply)


class TicketComponentTests(_IsolatedStoreMixin, unittest.IsolatedAsyncioTestCase):
    """Panel buttons and in-ticket buttons, routed by custom_id."""

    def setUp(self) -> None:
        self._isolate()
        self.guild = _FakeGuild()
        self.guild.add_role(STAFF_ROLE_ID, "Staff")
        self.opener = _member(OPENER_ID)
        self.staff = _member(STAFF_ID, roles=[STAFF_ROLE_ID])
        self.outsider = _member(OUTSIDER_ID)
        self.guild.set_members([self.opener, self.staff, self.outsider])
        self.config = {
            "server_id": GUILD_ID,
            "staff_role_id": STAFF_ROLE_ID,
            "tickets": {
                str(GUILD_ID): {
                    "log_channel_id": LOG_CHANNEL_ID,
                    "panel": {"channel_id": PANEL_CHANNEL_ID, "message_id": 42},
                    "categories": [
                        {"category_id": "help", "label": "General help", "ask_subject": True},
                        {"category_id": "quick", "label": "Quick question", "ask_subject": False},
                    ],
                }
            },
        }
        self.bot = _FakeBot(self.config, self.guild)
        self.mixin = TicketMixin(self.bot, lambda _config: None)
        self.bot.cogs["SentinelCog"] = self.mixin

    def tearDown(self) -> None:
        self.home.cleanup()

    async def test_ignores_other_components(self) -> None:
        interaction = _FakeInteraction(
            user=self.opener, guild=self.guild, data={"custom_id": "sentinel:ap:apply:x"}
        )
        self.assertFalse(await tickets.route_ticket_interaction(self.bot, interaction))
        self.assertEqual(interaction.replies(), [])

    async def test_panel_button_opens_the_subject_modal(self) -> None:
        interaction = _FakeInteraction(
            user=self.opener,
            guild=self.guild,
            data={"custom_id": "sentinel:tk:open:help"},
        )
        self.assertTrue(await tickets.route_ticket_interaction(self.bot, interaction))
        self.assertEqual(len(interaction.response.modals), 1)
        self.assertEqual(interaction.response.modals[0].custom_id, "sentinel:tk:create:help")

    async def test_category_without_subject_creates_the_ticket_immediately(self) -> None:
        interaction = _FakeInteraction(
            user=self.opener,
            guild=self.guild,
            data={"custom_id": "sentinel:tk:open:quick"},
        )
        await tickets.route_ticket_interaction(self.bot, interaction)
        self.assertEqual(interaction.response.modals, [])
        replies = " ".join(interaction.replies()).lower()
        self.assertIn("created", replies)
        self.assertEqual(tickets.count_open_tickets(GUILD_ID), 1)

    async def test_modal_submit_creates_the_ticket_with_its_subject(self) -> None:
        interaction = _FakeInteraction(
            user=self.opener,
            guild=self.guild,
            data={
                "custom_id": "sentinel:tk:create:help",
                "components": [{"components": [{"custom_id": "subject", "value": "Broken role"}]}],
            },
        )
        await tickets.route_ticket_interaction(self.bot, interaction)
        ticket = tickets.list_tickets(GUILD_ID, status="open")[0]
        self.assertEqual(ticket["subject"], "Broken role")
        self.assertIn("#0001", " ".join(interaction.replies()))

    async def test_a_stale_panel_explains_itself(self) -> None:
        interaction = _FakeInteraction(
            user=self.opener,
            guild=self.guild,
            data={"custom_id": "sentinel:tk:open:deleted-category"},
        )
        await tickets.route_ticket_interaction(self.bot, interaction)
        self.assertIn("no longer exists", " ".join(interaction.replies()))

    async def test_a_member_cannot_press_claim(self) -> None:
        ticket = await tickets.open_ticket(
            self.bot,
            guild=self.guild,
            opener=self.opener,
            category=tickets.find_panel_category(
                tickets.get_guild_tickets(self.bot.config, GUILD_ID), "quick"
            ),
        )
        interaction = _FakeInteraction(
            user=self.outsider,
            guild=self.guild,
            data={"custom_id": f"sentinel:tk:claim:{ticket['id']}"},
        )
        await tickets.route_ticket_interaction(self.bot, interaction)
        self.assertEqual(tickets.get_ticket(ticket["id"])["status"], tickets.STATUS_OPEN)
        self.assertIn("only staff", " ".join(interaction.replies()).lower())

    async def test_staff_can_claim_from_the_panel_notice(self) -> None:
        ticket = await tickets.open_ticket(
            self.bot,
            guild=self.guild,
            opener=self.opener,
            category=tickets.find_panel_category(
                tickets.get_guild_tickets(self.bot.config, GUILD_ID), "quick"
            ),
        )
        interaction = _FakeInteraction(
            user=self.staff,
            guild=self.guild,
            data={"custom_id": f"sentinel:tk:claim:{ticket['id']}"},
        )
        await tickets.route_ticket_interaction(self.bot, interaction)
        self.assertEqual(tickets.get_ticket(ticket["id"])["claimed_by"], STAFF_ID)


class PanelPublishTests(_IsolatedStoreMixin, unittest.IsolatedAsyncioTestCase):
    """Publishing the panel: one button per category, edits in place."""

    def setUp(self) -> None:
        self._isolate()
        self.guild = _FakeGuild()
        self.config = {
            "server_id": GUILD_ID,
            "tickets": {
                str(GUILD_ID): {
                    "categories": [
                        {"category_id": "help", "label": "General help", "emoji": "❓"},
                        {"category_id": "report", "label": "Report a member"},
                    ]
                }
            },
        }
        self.bot = _FakeBot(self.config, self.guild)

    def tearDown(self) -> None:
        self.home.cleanup()

    async def test_panel_has_one_button_per_category(self) -> None:
        panel = await tickets.publish_panel(
            self.bot, self.guild, self.guild.panel_channel, title="Support", description=None
        )
        posted = self.guild.panel_channel.sent[-1]
        self.assertEqual(panel["channel_id"], PANEL_CHANNEL_ID)
        custom_ids = [child.custom_id for child in posted["view"].children]
        self.assertEqual(
            custom_ids, ["sentinel:tk:open:help", "sentinel:tk:open:report"]
        )
        self.assertEqual(posted["embed"].title, "Support")
        self.assertIn("General help", posted["embed"].fields[0].value)

    async def test_republishing_edits_the_existing_message(self) -> None:
        first = await tickets.publish_panel(
            self.bot, self.guild, self.guild.panel_channel
        )
        second = await tickets.publish_panel(
            self.bot, self.guild, self.guild.panel_channel
        )
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(len(self.guild.panel_channel.sent), 1)
        self.assertTrue(self.guild.panel_channel.messages[first["message_id"]].edits)

    async def test_publishing_without_categories_is_refused(self) -> None:
        self.config["tickets"][str(GUILD_ID)]["categories"] = []
        with self.assertRaises(TicketError):
            await tickets.publish_panel(self.bot, self.guild, self.guild.panel_channel)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    sys.exit(main())
