"""Offline tests for the rules acceptance and reaction-role module."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rules import (  # noqa: E402
    DEFAULT_RULESET_NAME,
    MAX_RULESETS_PER_GUILD,
    MAX_RULESET_NAME_LENGTH,
    RULES_ACCEPT_EMOJI,
    RULES_POST_CONTENT,
    RulesMixin,
    find_ruleset,
    find_ruleset_by_message,
    find_ruleset_by_name,
    get_guild_rulesets,
    is_active_rules_reaction,
    new_ruleset_id,
    remove_ruleset,
    rules_embed_title,
    ruleset_id,
    ruleset_name,
    upsert_ruleset,
)


class FakeMember:
    def __init__(self, user_id: int, roles=None, *, bot: bool = False) -> None:
        self.id = user_id
        self.roles = list(roles or [])
        self.bot = bot
        self.added = []
        self.removed = []

    async def add_roles(self, role, *, reason=None) -> None:
        self.added.append(role)
        self.roles.append(role)

    async def remove_roles(self, role, *, reason=None) -> None:
        self.removed.append(role)
        self.roles.remove(role)


class FakeGuild:
    def __init__(self, role, member) -> None:
        self.id = 123
        self.name = "Test Guild"
        self._role = role
        self.roles = [role] if role is not None else []
        self._member = member
        self.member_cached = True
        self.fetches = 0
        # Set by the publish tests; unused by the reaction tests.
        self.me = None
        self._channel = None

    def get_role(self, role_id):
        for role in self.roles:
            if role.id == int(role_id):
                return role
        return None

    def get_member(self, user_id):
        if not self.member_cached:
            return None
        return self._member if user_id == self._member.id else None

    def get_channel(self, channel_id):
        if self._channel is not None and channel_id == self._channel.id:
            return self._channel
        return None

    async def fetch_member(self, user_id):
        self.fetches += 1
        return self._member if user_id == self._member.id else None


class FakeBot:
    def __init__(self, config, guild, *, bot_user_id=999) -> None:
        self.config = config
        self.user = SimpleNamespace(id=bot_user_id)
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild if guild_id == self._guild.id else None


class RulesConfigTests(unittest.TestCase):
    """The stored shape: a list of named sets per guild.

    The single-object shape older versions wrote is still accepted; it is read
    as one set, so an existing install needs no config migration.
    """

    def setUp(self) -> None:
        self.legacy = {
            "rules": {
                "123": {
                    "channel_id": 45,
                    "message_id": 67,
                    "role_id": 89,
                    "rules_text": "Be kind.",
                }
            }
        }

    def test_legacy_single_object_is_read_as_one_set(self) -> None:
        rulesets = get_guild_rulesets(self.legacy, 123)
        self.assertEqual(len(rulesets), 1)
        self.assertEqual(rulesets[0]["role_id"], 89)
        self.assertEqual(ruleset_name(rulesets[0]), DEFAULT_RULESET_NAME)
        self.assertEqual(ruleset_id(rulesets[0]), "67")
        self.assertEqual(get_guild_rulesets(self.legacy, 456), [])

    def test_multiple_sets_are_per_guild(self) -> None:
        config = {"rules": {"123": [{"ruleset_id": "a", "message_id": 67},
                                    {"ruleset_id": "b", "message_id": 68}]}}
        self.assertEqual(len(get_guild_rulesets(config, 123)), 2)
        self.assertEqual(get_guild_rulesets(config, 456), [])

    def test_sets_are_found_by_id_name_and_message(self) -> None:
        config = {"rules": {"123": [
            {"ruleset_id": "a", "name": "Server rules", "message_id": 67},
            {"ruleset_id": "b", "name": "Event rules", "message_id": 68},
        ]}}
        self.assertEqual(find_ruleset(config, 123, "b")["message_id"], 68)
        self.assertIsNone(find_ruleset(config, 123, "zzz"))
        self.assertEqual(find_ruleset_by_message(config, 123, 68)["ruleset_id"], "b")
        self.assertEqual(find_ruleset_by_name(config, 123, "event RULES")["ruleset_id"], "b")
        self.assertIsNone(find_ruleset_by_name(config, 123, "nope"))

    def test_every_published_message_accepts_the_checkmark(self) -> None:
        config = {"rules": {"123": [{"message_id": 67}, {"message_id": 68}]}}
        self.assertTrue(is_active_rules_reaction(config, 123, 67, RULES_ACCEPT_EMOJI))
        self.assertTrue(is_active_rules_reaction(config, 123, 68, RULES_ACCEPT_EMOJI))
        self.assertFalse(is_active_rules_reaction(config, 123, 69, RULES_ACCEPT_EMOJI))
        self.assertFalse(is_active_rules_reaction(config, 123, 67, "👋"))
        self.assertFalse(is_active_rules_reaction(config, None, 67, RULES_ACCEPT_EMOJI))

    def test_bad_or_missing_settings_fail_closed(self) -> None:
        self.assertFalse(is_active_rules_reaction({}, 123, 67, RULES_ACCEPT_EMOJI))
        broken = {"rules": {"123": {"message_id": "not-an-id"}}}
        self.assertFalse(is_active_rules_reaction(broken, 123, 67, RULES_ACCEPT_EMOJI))
        self.assertEqual(get_guild_rulesets({"rules": []}, 123), [])
        self.assertEqual(get_guild_rulesets({"rules": {"123": "x"}}, 123), [])

    def test_upsert_replaces_by_id_and_never_mutates(self) -> None:
        original = {"rules": {"123": [{"ruleset_id": "a", "role_id": 1}]}}
        both = upsert_ruleset(
            original, 123, {"ruleset_id": "b", "role_id": 2}
        )
        self.assertEqual(len(get_guild_rulesets(both, 123)), 2)
        self.assertEqual(len(get_guild_rulesets(original, 123)), 1, "config must be copied")

        replaced = upsert_ruleset(both, 123, {"ruleset_id": "a", "role_id": 9})
        self.assertEqual(len(get_guild_rulesets(replaced, 123)), 2)
        self.assertEqual(find_ruleset(replaced, 123, "a")["role_id"], 9)

    def test_removing_the_last_set_drops_the_guild_key(self) -> None:
        config = {"rules": {"123": [{"ruleset_id": "a"}]}}
        self.assertEqual(remove_ruleset(config, 123, "a")["rules"], {})

    def test_writing_converts_the_legacy_shape_to_a_list(self) -> None:
        added = upsert_ruleset(self.legacy, 123, {"ruleset_id": "b", "message_id": 99})
        self.assertIsInstance(added["rules"]["123"], list)
        self.assertEqual(len(added["rules"]["123"]), 2)

    def test_ruleset_names_and_titles(self) -> None:
        # Only the *current* default name gets the short embed title.
        self.assertEqual(rules_embed_title("Guild", DEFAULT_RULESET_NAME), "Guild Rules")
        self.assertEqual(rules_embed_title("Guild", "zone RULES"), "Guild Rules")
        self.assertEqual(rules_embed_title("Guild", "Event rules"), "Guild — Event rules")
        self.assertEqual(ruleset_name({"name": "  "}), DEFAULT_RULESET_NAME)
        self.assertEqual(len(new_ruleset_id()), 8)

    def test_a_typed_name_is_never_auto_corrected(self) -> None:
        """"Server rules" is a name, not an alias for the default.

        It used to be the default; it is now an ordinary custom name, so it
        must keep its exact text and its own embed title instead of being
        auto-corrected to whatever the default is called today.
        """
        settings = {"name": "Server Rules"}
        self.assertEqual(ruleset_name(settings), "Server Rules")
        self.assertNotEqual(ruleset_name(settings), DEFAULT_RULESET_NAME)
        self.assertEqual(
            rules_embed_title("Guild", "Server Rules"), "Guild — Server Rules"
        )
        # Case is preserved too: this is the title the administrator typed.
        self.assertEqual(ruleset_name({"name": "server rules"}), "server rules")


class ReactionRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.role = SimpleNamespace(id=89)
        self.member = FakeMember(321)
        self.guild = FakeGuild(self.role, self.member)
        config = {
            "rules": {
                "123": {
                    "channel_id": 45,
                    "message_id": 67,
                    "role_id": 89,
                    "rules_text": "Be kind.",
                }
            }
        }
        self.bot = FakeBot(config, self.guild)
        self.cog = RulesMixin(self.bot, lambda _config: None)

    def payload(self, *, message_id=67, emoji=RULES_ACCEPT_EMOJI, user_id=321, member=None):
        return SimpleNamespace(
            guild_id=123,
            message_id=message_id,
            emoji=emoji,
            user_id=user_id,
            member=member,
        )

    async def test_acceptance_grants_role_once(self) -> None:
        payload = self.payload(member=self.member)
        await self.cog.on_raw_reaction_add(payload)
        await self.cog.on_raw_reaction_add(payload)
        self.assertEqual(self.member.added, [self.role])
        self.assertIn(self.role, self.member.roles)

    async def test_removing_acceptance_reaction_removes_role(self) -> None:
        self.member.roles.append(self.role)
        await self.cog.on_raw_reaction_remove(self.payload(member=self.member))
        self.assertEqual(self.member.removed, [self.role])
        self.assertNotIn(self.role, self.member.roles)

    async def test_other_messages_emojis_and_bot_reactions_are_ignored(self) -> None:
        await self.cog.on_raw_reaction_add(self.payload(message_id=68, member=self.member))
        await self.cog.on_raw_reaction_add(self.payload(emoji="👋", member=self.member))
        await self.cog.on_raw_reaction_add(
            self.payload(user_id=self.bot.user.id, member=FakeMember(self.bot.user.id, bot=True))
        )
        self.assertEqual(self.member.added, [])

    async def test_missing_payload_member_uses_guild_cache(self) -> None:
        await self.cog.on_raw_reaction_add(self.payload(member=None))
        self.assertEqual(self.member.added, [self.role])
        self.assertEqual(self.guild.fetches, 0)

    async def test_uncached_member_is_fetched(self) -> None:
        self.guild.member_cached = False
        await self.cog.on_raw_reaction_add(self.payload(member=None))
        self.assertEqual(self.member.added, [self.role])
        self.assertEqual(self.guild.fetches, 1)

    async def test_bots_do_not_receive_acceptance_role(self) -> None:
        bot_member = FakeMember(444, bot=True)
        self.guild._member = bot_member
        await self.cog.on_raw_reaction_add(self.payload(user_id=444, member=bot_member))
        self.assertEqual(bot_member.added, [])


class _Permissions:
    """Answers False for every permission that was not explicitly granted."""

    def __init__(self, **granted) -> None:
        self.__dict__.update(granted)

    def __getattr__(self, _name):
        return False


class _PositionedRole:
    """A role that can be compared by position, like discord.Role."""

    def __init__(self, **attributes) -> None:
        self.__dict__.update(attributes)

    def __ge__(self, other): return self.position >= other.position
    def __gt__(self, other): return self.position > other.position
    def __lt__(self, other): return self.position < other.position
    def __le__(self, other): return self.position <= other.position


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[str] = []

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self, *, ephemeral=False) -> None:
        self.deferred = True

    async def send_message(self, content, *, ephemeral=False, allowed_mentions=None) -> None:
        self.messages.append(content)


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, content, *, ephemeral=False, allowed_mentions=None) -> None:
        self.messages.append(content)


class RulesPublishTests(unittest.IsolatedAsyncioTestCase):
    """`/rules publish` posts the prompt members actually see."""

    async def test_publish_posts_the_prompt_embed_and_reaction(self) -> None:
        self.saved: list[dict] = []
        sent: list[dict] = []
        reactions: list[str] = []

        guild = FakeGuild(role=None, member=None)
        guild.me = SimpleNamespace(
            guild_permissions=_Permissions(manage_roles=True),
            top_role=_PositionedRole(position=50),
        )
        role = _PositionedRole(
            id=89,
            position=10,
            mention="<@&89>",
            managed=False,
            permissions=_Permissions(),
            guild=SimpleNamespace(id=guild.id),
        )
        role.is_default = lambda: False
        channel = SimpleNamespace(
            id=45,
            mention="#rules",
            guild=SimpleNamespace(id=guild.id),
            permissions_for=lambda _member: _Permissions(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                add_reactions=True,
            ),
        )

        class _Message:
            id = 67

            async def add_reaction(self, emoji):
                reactions.append(str(emoji))

        async def send(content, *, embed=None, allowed_mentions=None):
            sent.append({"content": content, "embed": embed})
            return _Message()

        channel.send = send
        guild._channel = channel

        bot = FakeBot({"rules": {}}, guild)
        cog = RulesMixin(bot, self.saved.append)
        interaction = SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=7, guild_permissions=_Permissions(administrator=True)),
            response=_FakeResponse(),
            followup=_FakeFollowup(),
        )

        # @rules_group.command() wraps the method in a Command; .callback is the
        # underlying (unbound) coroutine, so the cog is passed explicitly.
        await cog.publish_rules.callback(cog, interaction, channel, role, "  1. Be kind.  ")

        self.assertEqual(len(sent), 1)
        # The prompt is the fixed sentence, not a per-publish string.
        self.assertEqual(sent[0]["content"], RULES_POST_CONTENT)
        embed = sent[0]["embed"]
        self.assertEqual(embed.title, "Test Guild Rules")
        self.assertEqual(embed.description, "1. Be kind.")
        self.assertIn(RULES_ACCEPT_EMOJI, embed.footer.text)
        self.assertEqual(reactions, [RULES_ACCEPT_EMOJI])
        # The published set is persisted so reactions survive a restart.
        stored = self.saved[-1]["rules"][str(guild.id)]
        self.assertEqual(stored[0]["message_id"], 67)
        self.assertEqual(stored[0]["name"], DEFAULT_RULESET_NAME)
        self.assertTrue(stored[0]["ruleset_id"])
        self.assertIn(f"**{DEFAULT_RULESET_NAME}**", interaction.followup.messages[-1])


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.channel = None
        self.author = SimpleNamespace(id=999)
        self.deleted = False
        self.reactions: list[str] = []

    async def add_reaction(self, emoji) -> None:
        self.reactions.append(str(emoji))

    async def remove_reaction(self, emoji, member) -> None:
        self.reactions = [item for item in self.reactions if item != str(emoji)]

    async def delete(self) -> None:
        self.deleted = True

    async def edit(self, *, content=None, embed=None, allowed_mentions=None):
        self.edited = True
        self.content = content
        return self


class _RulesHarness:
    """A guild/channel/bot triple good enough to drive the rules commands."""

    BOT_USER_ID = 999

    def __init__(self, config: dict, *, message_ids=(67, 68, 69)) -> None:
        self.saved: list[dict] = []
        self.sent: list[dict] = []
        self.messages: dict[int, _FakeMessage] = {}
        self._message_ids = list(message_ids)

        self.guild = FakeGuild(role=None, member=None)
        self.guild.me = SimpleNamespace(
            guild_permissions=_Permissions(manage_roles=True),
            top_role=_PositionedRole(position=50),
        )
        self.role = _PositionedRole(
            id=89,
            name="Verified",
            position=10,
            mention="<@&89>",
            managed=False,
            permissions=_Permissions(),
            guild=SimpleNamespace(id=self.guild.id),
        )
        self.role.is_default = lambda: False
        self.second_role = _PositionedRole(
            id=90,
            name="Events",
            position=11,
            mention="<@&90>",
            managed=False,
            permissions=_Permissions(),
            guild=SimpleNamespace(id=self.guild.id),
        )
        self.second_role.is_default = lambda: False

        self.channel = SimpleNamespace(
            id=45,
            mention="#rules",
            guild=SimpleNamespace(id=self.guild.id),
            permissions_for=lambda _member: _Permissions(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                add_reactions=True,
            ),
        )
        self.channel.send = self._send
        self.channel.fetch_message = self._fetch_message
        self.guild._channel = self.channel

        self.guild.roles = [self.role, self.second_role]
        self.guild._channel = self.channel
        self.bot = FakeBot(config, self.guild, bot_user_id=self.BOT_USER_ID)
        self.cog = RulesMixin(self.bot, self.saved.append)
        self.interaction = SimpleNamespace(
            guild=self.guild,
            user=SimpleNamespace(id=7, guild_permissions=_Permissions(administrator=True)),
            response=_FakeResponse(),
            followup=_FakeFollowup(),
        )

    async def _send(self, content, *, embed=None, allowed_mentions=None):
        message_id = self._message_ids.pop(0)
        message = _FakeMessage(message_id)
        message.channel = self.channel
        self.messages[message_id] = message
        self.sent.append({"content": content, "embed": embed, "message": message})
        return message

    async def _fetch_message(self, message_id):
        return self.messages[int(message_id)]

    async def publish(self, text, name=None, role=None):
        await self.cog.publish_rules.callback(
            self.cog, self.interaction, self.channel, role or self.role, text, name
        )

    async def disable(self, name=None):
        await self.cog.disable_rules.callback(self.cog, self.interaction, name)

    async def list_sets(self):
        await self.cog.list_rules.callback(self.cog, self.interaction)

    @property
    def reply(self) -> str:
        return (
            self.interaction.followup.messages[-1]
            if self.interaction.followup.messages
            else self.interaction.response.messages[-1]
        )


class RulesMultiSetTests(unittest.IsolatedAsyncioTestCase):
    """A server can run several independent rule sets.

    Each set owns its post, role and text; publishing a second set must not
    replace the first, and a reaction is routed by the message it was added to.
    """

    async def test_two_named_sets_coexist(self) -> None:
        harness = _RulesHarness({"rules": {}})
        await harness.publish("1. Be kind.", name="Server rules")
        await harness.publish("2. No spam.", name="Event rules", role=harness.second_role)

        self.assertEqual(len(harness.sent), 2, "the first set must not be replaced")
        # "Server rules" is an ordinary custom name now (the old default), so
        # it is titled as typed rather than folded into the default's title.
        self.assertEqual(
            [entry["embed"].title for entry in harness.sent],
            ["Test Guild — Server rules", "Test Guild — Event rules"],
        )
        stored = harness.bot.config["rules"]["123"]
        self.assertEqual([item["name"] for item in stored], ["Server rules", "Event rules"])
        self.assertEqual([item["message_id"] for item in stored], [67, 68])
        self.assertEqual([item["role_id"] for item in stored], [89, 90])
        self.assertEqual(len({item["ruleset_id"] for item in stored}), 2, "ids must differ")

    async def test_the_default_name_is_only_a_default(self) -> None:
        """An unnamed set gets the default; a typed name is never changed."""
        harness = _RulesHarness({"rules": {}})
        await harness.publish("1. Be kind.")  # no name given
        await harness.publish(
            "2. No spoilers.", name="Server Rules", role=harness.second_role
        )

        self.assertEqual(
            [entry["embed"].title for entry in harness.sent],
            ["Test Guild Rules", "Test Guild — Server Rules"],
        )
        stored = harness.bot.config["rules"]["123"]
        self.assertEqual(
            [item["name"] for item in stored], [DEFAULT_RULESET_NAME, "Server Rules"]
        )
        # Nothing migrated the second set to the default name.
        self.assertNotIn(DEFAULT_RULESET_NAME, stored[1]["name"])

    async def test_republishing_the_old_default_name_keeps_that_name(self) -> None:
        """Republishing under "Server rules" replaces that set and stores it as typed."""
        harness = _RulesHarness({"rules": {}})
        await harness.publish("1. Be kind.", name="Server rules")
        await harness.publish("1. Be extra kind.", name="Server rules")

        stored = harness.bot.config["rules"]["123"]
        self.assertEqual(len(stored), 1, "the same name replaces its own set")
        self.assertEqual(stored[0]["name"], "Server rules")
        self.assertEqual(stored[0]["rules_text"], "1. Be extra kind.")

    async def test_republishing_a_name_replaces_only_that_set(self) -> None:
        harness = _RulesHarness({"rules": {}})
        await harness.publish("1. Be kind.", name="Server rules")
        await harness.publish("2. No spam.", name="Event rules", role=harness.second_role)
        await harness.publish("1. Be extra kind.", name="Server rules")

        stored = harness.bot.config["rules"]["123"]
        self.assertEqual(len(stored), 2)
        self.assertEqual(
            [item["rules_text"] for item in stored], ["1. Be extra kind.", "2. No spam."]
        )
        # Republishing replaces the old post rather than leaving it behind.
        self.assertEqual(len(harness.sent), 3)
        self.assertTrue(harness.messages[67].deleted)
        self.assertFalse(harness.messages[68].deleted)
        self.assertEqual(stored[0]["message_id"], 69)

    async def test_each_set_grants_its_own_role(self) -> None:
        harness = _RulesHarness({"rules": {}})
        await harness.publish("Server rules.", name="Server rules")
        await harness.publish("Event rules.", name="Event rules", role=harness.second_role)

        member = FakeMember(321)
        harness.guild._member = member
        payload = lambda message_id: SimpleNamespace(  # noqa: E731
            guild_id=123, message_id=message_id, emoji=RULES_ACCEPT_EMOJI, user_id=321, member=member
        )
        await harness.cog.on_raw_reaction_add(payload(67))
        self.assertEqual(member.added, [harness.role])
        await harness.cog.on_raw_reaction_add(payload(68))
        self.assertEqual(member.added, [harness.role, harness.second_role])
        # Unknown messages are ignored.
        await harness.cog.on_raw_reaction_add(payload(999))
        self.assertEqual(len(member.added), 2)

    async def test_disable_needs_a_name_when_several_are_published(self) -> None:
        harness = _RulesHarness({"rules": {}})
        await harness.publish("Server rules.", name="Server rules")
        await harness.publish("Event rules.", name="Event rules", role=harness.second_role)

        await harness.disable()
        self.assertIn("pass `name`", harness.reply)
        self.assertEqual(len(harness.bot.config["rules"]["123"]), 2, "nothing removed")

        await harness.disable("Event rules")
        stored = harness.bot.config["rules"]["123"]
        self.assertEqual([item["name"] for item in stored], ["Server rules"])
        # The dropped post is marked disabled (not deleted), and the surviving
        # set's post is untouched.
        self.assertTrue(getattr(harness.messages[68], "edited", False))
        self.assertFalse(getattr(harness.messages[67], "edited", False))
        self.assertIn("Event rules", harness.reply)

    async def test_disable_without_a_name_works_for_a_single_set(self) -> None:
        harness = _RulesHarness({"rules": {}})
        await harness.publish("Server rules.")
        await harness.disable()
        self.assertEqual(get_guild_rulesets(harness.bot.config, 123), [])
        self.assertIn("Disabled the rule set", harness.reply)

    async def test_unknown_name_and_too_many_sets_are_reported(self) -> None:
        harness = _RulesHarness(
            {"rules": {"123": [
                {"ruleset_id": f"id{index}", "name": f"Set {index}", "message_id": index}
                for index in range(MAX_RULESETS_PER_GUILD)
            ]}}
        )
        await harness.publish("More rules.", name="One too many")
        self.assertEqual(len(harness.sent), 0)
        self.assertIn(str(MAX_RULESETS_PER_GUILD), harness.reply)

        await harness.disable("Not a set")
        self.assertIn("No rule set is named", harness.reply)

    async def test_long_names_are_rejected(self) -> None:
        harness = _RulesHarness({"rules": {}})
        await harness.publish("Rules.", name="x" * (MAX_RULESET_NAME_LENGTH + 1))
        self.assertEqual(len(harness.sent), 0)
        self.assertIn(str(MAX_RULESET_NAME_LENGTH), harness.reply)

    async def test_list_shows_every_set(self) -> None:
        harness = _RulesHarness({"rules": {}})
        await harness.publish("Server rules.", name="Server rules")
        await harness.publish("Event rules.", name="Event rules", role=harness.second_role)
        await harness.list_sets()
        self.assertIn("2 rule set(s)", harness.reply)
        self.assertIn("Server rules", harness.reply)
        self.assertIn("Event rules", harness.reply)
        self.assertIn("#rules", harness.reply)
        self.assertIn("message `67`", harness.reply)

    async def test_legacy_config_keeps_working_end_to_end(self) -> None:
        """A config written before this feature is still reactive and editable."""
        config = {"rules": {"123": {
            "channel_id": 45, "message_id": 67, "role_id": 89, "rules_text": "Old rules.",
        }}}
        harness = _RulesHarness(config)

        member = FakeMember(321)
        harness.guild._member = member
        await harness.cog.on_raw_reaction_add(
            SimpleNamespace(
                guild_id=123, message_id=67, emoji=RULES_ACCEPT_EMOJI, user_id=321, member=member
            )
        )
        self.assertEqual(member.added, [harness.role])

        # The form posts a named set: the legacy entry is upgraded, not dropped.
        await harness.publish("New rules.", name="Event rules", role=harness.second_role)
        stored = harness.bot.config["rules"]["123"]
        self.assertIsInstance(stored, list)
        self.assertEqual(len(stored), 2)
        # The legacy entry keeps working (it has no stored name, so it is read
        # as the default set) and the new set was added next to it. The
        # explicitly named "Event rules" set is stored exactly as typed.
        self.assertEqual(
            [ruleset_name(item) for item in stored], [DEFAULT_RULESET_NAME, "Event rules"]
        )


class RulesPostContentTests(unittest.TestCase):
    """The prompt that accompanies the rules embed.

    The wording is fixed by the server owner, so it is pinned here; the same
    constant feeds /rules publish, the dashboard's publish endpoint and the
    dashboard preview (which is checked against this constant in
    tests/test_dashboard.py), so the three cannot drift apart.
    """

    EXPECTED = "By reacting to this you acknowledge the rules and will abide by them."

    def test_content_is_the_configured_sentence(self) -> None:
        self.assertEqual(RULES_POST_CONTENT, self.EXPECTED)

    def test_content_is_a_single_plain_sentence(self) -> None:
        # It is posted with mentions disabled, so it must not rely on pings and
        # must not wrap onto several lines.
        self.assertNotIn("\n", RULES_POST_CONTENT)
        self.assertNotIn("@", RULES_POST_CONTENT)
        self.assertTrue(RULES_POST_CONTENT.endswith("."))


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
