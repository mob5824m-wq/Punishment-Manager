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
from tickets import TicketCog, TicketError, TicketMixin  # noqa: E402


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
ADMIN_ID = 121212121212121212
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

    def add_category(self, category_id: int, name: str):
        category = _FakeCategoryChannel(category_id, name)
        self.categories.append(category)
        self._channels[category.id] = category
        return category

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
        self.edits: list[dict] = []

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, *, ephemeral=False) -> None:
        self.deferred = True

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False, allowed_mentions=None):
        self.messages.append(
            {"content": content, "embed": embed, "view": view, "ephemeral": ephemeral}
        )

    async def edit_message(self, *, embed=None, view=None, content=None) -> None:
        # The console/review selects rewrite their own ephemeral message.
        self.edits.append({"content": content, "embed": embed, "view": view})

    async def send_modal(self, modal) -> None:
        self.modals.append(modal)


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.embeds: list[object] = []
        self.views: list[object] = []

    async def send(self, content=None, *, embed=None, ephemeral=False, view=None, allowed_mentions=None):
        if content is not None:
            self.messages.append({"content": content, "ephemeral": ephemeral})
        if embed is not None:
            self.embeds.append(embed)
        if view is not None:
            self.views.append(view)


class _FakeComponentMessage:
    """Just enough of a message for ``interaction.message``.

    ``_refresh_card`` looks at ``flags.ephemeral`` to tell the console's card
    (ephemeral, worth redrawing) from a ticket's own public control message.
    """

    def __init__(self, *, ephemeral: bool = False) -> None:
        self.flags = SimpleNamespace(ephemeral=ephemeral)


class _FakeInteraction:
    def __init__(
        self, *, user, guild, channel_id=PANEL_CHANNEL_ID, data=None, message=None
    ) -> None:
        self.user = user
        self.guild = guild
        self.guild_id = guild.id
        self.channel_id = channel_id
        self.data = data or {}
        self.message = message
        self.edited: list[dict] = []
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()

    async def edit_original_response(self, *, embed=None, view=None) -> None:
        self.edited.append({"embed": embed, "view": view})

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
    """The three staff commands, and what a normal member is refused.

    The surface is deliberately small: ``panel`` configures and publishes,
    ``category`` lists/adds/edits/removes, ``console`` is where staff work the
    queue. Reading or acting on a ticket that is not yours is refused on every
    one of them.
    """

    def setUp(self) -> None:
        self._isolate()
        self.guild = _FakeGuild()
        self.staff_role = self.guild.add_role(STAFF_ROLE_ID, "Staff")
        self.category = self.guild.add_category(CATEGORY_CHANNEL_ID, "Tickets")
        self.opener = _member(OPENER_ID)
        self.staff = _member(STAFF_ID, roles=[STAFF_ROLE_ID])
        self.admin = _member(ADMIN_ID)
        self.admin.guild_permissions = SimpleNamespace(administrator=True)
        self.guild.set_members([self.opener, self.staff, self.admin])
        self.config = {
            "server_id": GUILD_ID,
            "staff_role_id": STAFF_ROLE_ID,
            "staff_channel_id": LOG_CHANNEL_ID,
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

    async def test_a_member_cannot_use_any_staff_ticket_command(self) -> None:
        await self._open_ticket()
        for handler, kwargs in (
            (self.mixin.tickets_console.callback, {}),
            (self.mixin.tickets_category.callback, {}),
            (self.mixin.tickets_panel.callback, {}),
        ):
            with self.subTest(handler=handler.__name__):
                interaction = _FakeInteraction(user=self.opener, guild=self.guild)
                await handler(self.mixin, interaction, **kwargs)
                reply = "\n".join(interaction.replies()).lower()
                self.assertTrue(
                    "only staff" in reply or "only server administrators" in reply,
                    interaction.replies(),
                )

    async def test_console_lists_and_details_tickets_for_staff(self) -> None:
        ticket = await self._open_ticket()
        console = _FakeInteraction(user=self.staff, guild=self.guild)
        await self.mixin.tickets_console.callback(self.mixin, console)
        self.assertEqual(len(console.followup.embeds), 1)
        embed = console.followup.embeds[0]
        self.assertEqual(embed.title, "Ticket console")
        self.assertIn("Open", [field.name for field in embed.fields])
        # The select offers the ticket by number and subject.
        view = console.followup.views[0]
        options = view.children[0].options
        self.assertEqual(options[0].value, str(ticket["id"]))
        self.assertIn("#0001", options[0].label)
        self.assertIn("Please help", options[0].description)

        # Picking it rewrites the same message into the ticket's card, with the
        # buttons that act on it.
        pick = _FakeInteraction(
            user=self.staff,
            guild=self.guild,
            data={"custom_id": f"sentinel:tk:console:{ticket['id']}", "values": [str(ticket["id"])]},
        )
        await tickets.route_ticket_interaction(self.bot, pick)
        self.assertEqual(len(pick.response.edits), 1)
        card = pick.response.edits[0]
        self.assertIn("Ticket #0001", card["embed"].title)
        labels = [child.label for child in card["view"].children]
        self.assertEqual(labels, ["Claim", "Close"])

    async def test_console_refuses_a_member_and_hides_closed_tickets_by_default(self) -> None:
        ticket = await self._open_ticket()
        await tickets.close_ticket(self.bot, self.guild, ticket, self.staff, "done")

        empty = _FakeInteraction(user=self.staff, guild=self.guild)
        await self.mixin.tickets_console.callback(self.mixin, empty)
        self.assertTrue(
            any("no tickets match" in reply.lower() for reply in empty.replies()),
            empty.replies(),
        )

        closed = _FakeInteraction(user=self.staff, guild=self.guild)
        await self.mixin.tickets_console.callback(
            self.mixin, closed, status=SimpleNamespace(value=tickets.STATUS_CLOSED)
        )
        self.assertEqual(len(closed.followup.embeds), 1)
        # A closed ticket's card offers Reopen, not Claim.
        pick = _FakeInteraction(
            user=self.staff,
            guild=self.guild,
            data={"custom_id": f"sentinel:tk:console:{ticket['id']}", "values": [str(ticket["id"])]},
        )
        await tickets.route_ticket_interaction(self.bot, pick)
        labels = [child.label for child in pick.response.edits[0]["view"].children]
        self.assertEqual(labels, ["Reopen"])

        refused = _FakeInteraction(user=self.opener, guild=self.guild)
        await self.mixin.tickets_console.callback(self.mixin, refused)
        self.assertTrue(
            any("only staff" in reply.lower() for reply in refused.replies()),
            refused.replies(),
        )

    async def test_claiming_from_the_console_redraws_its_card(self) -> None:
        """A console button must not leave its own card offering the old action.

        The card is an ephemeral message (not the ticket's control message), so
        ``_refresh_card`` redraws it with the state the action produced.
        """
        ticket = await self._open_ticket()
        press = _FakeInteraction(
            user=self.staff,
            guild=self.guild,
            data={"custom_id": f"sentinel:tk:claim:{ticket['id']}"},
            message=_FakeComponentMessage(ephemeral=True),
        )
        await tickets.route_ticket_interaction(self.bot, press)
        card = press.response.edits[0]
        self.assertEqual(
            tickets.get_ticket(ticket["id"])["status"], tickets.STATUS_CLAIMED
        )
        # Claim is gone (already claimed); Close is still there.
        self.assertEqual([child.label for child in card["view"].children], ["Close"])
        self.assertIn("is yours", card["content"])

        # A button inside the ticket keeps its existing behaviour: the public
        # control message is left to refresh_ticket_messages().
        inside = _FakeInteraction(
            user=self.staff,
            guild=self.guild,
            channel_id=ticket["thread_id"],
            data={"custom_id": f"sentinel:tk:close:{ticket['id']}"},
            message=_FakeComponentMessage(ephemeral=False),
        )
        await tickets.route_ticket_interaction(self.bot, inside)
        self.assertEqual(inside.response.edits, [])
        self.assertEqual(len(inside.response.modals), 1)

    async def test_category_command_lists_adds_edits_and_removes(self) -> None:
        listed = _FakeInteraction(user=self.admin, guild=self.guild)
        await self.mixin.tickets_category.callback(self.mixin, listed)
        reply = "\n".join(listed.replies())
        self.assertIn("General help", reply)
        self.assertIn(tickets.MODE_THREAD, reply)

        added = _FakeInteraction(user=self.admin, guild=self.guild)
        await self.mixin.tickets_category.callback(
            self.mixin, added, label="Billing", emoji="💳", description="Invoices"
        )
        stored = tickets.find_category(
            tickets.get_guild_tickets(self.bot.config, GUILD_ID), "Billing"
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored["description"], "Invoices")

        edited = _FakeInteraction(user=self.admin, guild=self.guild)
        await self.mixin.tickets_category.callback(
            self.mixin, edited, label="Billing", mode=SimpleNamespace(value=tickets.MODE_CHANNEL)
        )
        stored = tickets.find_category(
            tickets.get_guild_tickets(self.bot.config, GUILD_ID), "Billing"
        )
        # Editing by name keeps the options that were left out ...
        self.assertEqual(stored["description"], "Invoices")
        self.assertEqual(stored["emoji"], "💳")
        # ... and applies the one that was given.
        self.assertEqual(stored["mode"], tickets.MODE_CHANNEL)

        removed = _FakeInteraction(user=self.admin, guild=self.guild)
        await self.mixin.tickets_category.callback(
            self.mixin, removed, label="Billing", remove=True
        )
        self.assertIsNone(
            tickets.find_category(tickets.get_guild_tickets(self.bot.config, GUILD_ID), "Billing")
        )

    async def test_panel_command_saves_options_and_publishes(self) -> None:
        admin = _FakeInteraction(user=self.admin, guild=self.guild)
        await self.mixin.tickets_panel.callback(
            self.mixin,
            admin,
            channel=self.guild.get_channel(PANEL_CHANNEL_ID),
            mode=SimpleNamespace(value=tickets.MODE_CHANNEL),
            log=self.guild.get_channel(LOG_CHANNEL_ID),
            parent=self.category,
        )
        self.assertEqual(len(admin.followup.messages), 1)
        reply = admin.followup.messages[0]["content"]
        self.assertIn("Saved:", reply)
        self.assertIn("published", reply)
        settings = tickets.get_guild_tickets(self.bot.config, GUILD_ID)
        self.assertEqual(settings["mode"], tickets.MODE_CHANNEL)
        self.assertEqual(settings["log_channel_id"], LOG_CHANNEL_ID)
        self.assertEqual(settings["category_id"], CATEGORY_CHANNEL_ID)
        self.assertTrue(settings["panel"]["message_id"])

    async def test_panel_command_with_a_channel_only_refreshes(self) -> None:
        before = tickets.get_guild_tickets(self.bot.config, GUILD_ID)["mode"]
        admin = _FakeInteraction(user=self.admin, guild=self.guild)
        await self.mixin.tickets_panel.callback(
            self.mixin, admin, channel=self.guild.get_channel(PANEL_CHANNEL_ID)
        )
        self.assertIn("published", admin.followup.messages[0]["content"])
        # A refresh must not silently reset the server's mode.
        self.assertEqual(tickets.get_guild_tickets(self.bot.config, GUILD_ID)["mode"], before)

    async def test_panel_command_without_a_channel_just_saves(self) -> None:
        tickets.write_guild_tickets(
            self.bot.config, GUILD_ID, {"mode": tickets.MODE_THREAD, "categories": []}
        )
        admin = _FakeInteraction(user=self.admin, guild=self.guild)
        await self.mixin.tickets_panel.callback(
            self.mixin, admin, mode=SimpleNamespace(value=tickets.MODE_CHANNEL)
        )
        reply = admin.followup.messages[0]["content"]
        self.assertIn("Saved:", reply)
        self.assertIn("to publish the panel", reply)
        self.assertEqual(
            tickets.get_guild_tickets(self.bot.config, GUILD_ID)["mode"],
            tickets.MODE_CHANNEL,
        )
        # Nothing was published: there is no panel to publish into yet.
        self.assertFalse(tickets.get_guild_tickets(self.bot.config, GUILD_ID)["panel"])

    async def test_member_starts_a_ticket_with_the_ticket_command(self) -> None:
        member = self.guild.get_member(OPENER_ID)
        interaction = _FakeInteraction(user=member, guild=self.guild)
        self.bot.cogs["TicketCog"] = TicketCog(self.bot)
        await self.bot.cogs["TicketCog"].ticket.callback(self.bot.cogs["TicketCog"], interaction)
        # Two categories? No - one category, so it goes straight to the modal.
        self.assertEqual(len(interaction.response.modals), 1)
        self.assertEqual(interaction.response.modals[0].custom_id, "sentinel:tk:create:help")

    async def test_ticket_command_offers_a_picker_for_several_categories(self) -> None:
        tickets.upsert_category(
            self.bot.config, GUILD_ID, {"category_id": "bill", "label": "Billing"}
        )
        cog = TicketCog(self.bot)
        interaction = _FakeInteraction(user=self.opener, guild=self.guild)
        await cog.ticket.callback(cog, interaction)
        self.assertEqual(len(interaction.response.messages), 1)
        view = interaction.response.messages[0]["view"]
        labels = [option.label for option in view.children[0].options]
        self.assertEqual(labels, ["General help", "Billing"])

        # Picking one opens that category's modal, resolved against the config.
        pick = _FakeInteraction(
            user=self.opener,
            guild=self.guild,
            data={"custom_id": "sentinel:tk:pick", "values": ["bill"]},
        )
        await tickets.route_ticket_interaction(self.bot, pick)
        self.assertEqual(pick.response.modals[0].custom_id, "sentinel:tk:create:bill")

    async def test_ticket_command_explains_when_nothing_is_configured(self) -> None:
        tickets.write_guild_tickets(self.bot.config, GUILD_ID, {"categories": []})
        cog = TicketCog(self.bot)
        interaction = _FakeInteraction(user=self.opener, guild=self.guild)
        await cog.ticket.callback(cog, interaction)
        self.assertTrue(
            any("no ticket categories" in reply.lower() for reply in interaction.replies()),
            interaction.replies(),
        )


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
