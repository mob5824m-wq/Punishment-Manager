"""Offline tests for the reaction-role menus (reaction_roles.py).

The rules-acceptance gate is covered by tests/test_rules.py; this file covers
the extra dashboard-published menus: the stored shape, the emoji parsing that
keeps a role name from being stored as a "reaction", and the reaction handler
that grants and removes roles.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reaction_roles import (  # noqa: E402
    DEFAULT_POST_MESSAGE,
    MAX_REACTION_ENTRIES,
    ReactionRolesCog,
    build_post_content,
    entry_for_emoji,
    emoji_key,
    find_reaction_post,
    find_reaction_post_by_message,
    get_guild_reaction_posts,
    message_limit,
    new_post_id,
    normalize_emoji,
    parse_entries,
    remove_reaction_post,
    upsert_reaction_post,
    validate_post_entries,
)


def make_config(**posts_by_guild):
    return {"reaction_roles": posts_by_guild}


def make_post(**overrides):
    post = {
        "post_id": "abc123",
        "channel_id": 45,
        "message_id": 67,
        "title": "Choose your roles",
        "message": "React below.",
        "use_embed": True,
        "remove_on_unreact": True,
        "entries": [{"emoji": "🎮", "role_id": 89}],
    }
    post.update(overrides)
    return post


class ConfigStorageTests(unittest.TestCase):
    def test_posts_are_stored_per_guild(self) -> None:
        config = make_config(**{"123": [make_post()]})
        self.assertEqual(len(get_guild_reaction_posts(config, 123)), 1)
        self.assertEqual(get_guild_reaction_posts(config, 456), [])

    def test_lookups_by_post_id_and_message_id(self) -> None:
        post = make_post()
        config = make_config(**{"123": [post]})
        self.assertIs(find_reaction_post(config, 123, "abc123"), post)
        self.assertIsNone(find_reaction_post(config, 123, "nope"))
        self.assertIs(find_reaction_post_by_message(config, 123, 67), post)
        self.assertIs(find_reaction_post_by_message(config, 123, "67"), post)
        self.assertIsNone(find_reaction_post_by_message(config, 123, 68))

    def test_broken_config_fails_closed(self) -> None:
        self.assertEqual(get_guild_reaction_posts({"reaction_roles": []}, 123), [])
        self.assertEqual(get_guild_reaction_posts({"reaction_roles": {"123": "x"}}, 123), [])
        self.assertEqual(get_guild_reaction_posts({}, 123), [])
        config = {"reaction_roles": {"123": [make_post(message_id="not-an-id")]}}
        self.assertIsNone(find_reaction_post_by_message(config, 123, 67))

    def test_upsert_adds_then_replaces_without_mutating_the_original(self) -> None:
        original = make_config(**{"123": [make_post()]})
        added = upsert_reaction_post(original, 123, make_post(post_id="def456", message_id=99))
        self.assertEqual(len(get_guild_reaction_posts(added, 123)), 2)
        self.assertEqual(len(get_guild_reaction_posts(original, 123)), 1, "config must be copied")

        replaced = upsert_reaction_post(added, 123, make_post(message="New text."))
        self.assertEqual(len(get_guild_reaction_posts(replaced, 123)), 2)
        self.assertEqual(find_reaction_post(replaced, 123, "abc123")["message"], "New text.")

    def test_removing_the_last_post_drops_the_guild_key(self) -> None:
        config = make_config(**{"123": [make_post()]})
        emptied = remove_reaction_post(config, 123, "abc123")
        self.assertEqual(emptied["reaction_roles"], {})

    def test_new_post_ids_are_short_and_unique(self) -> None:
        ids = {new_post_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(len(value) == 8 for value in ids), ids)


class EmojiParsingTests(unittest.TestCase):
    def test_unicode_emoji_pass_through(self) -> None:
        for value in ("🎮", "✅", "🛠️", "🎬"):
            with self.subTest(value=value):
                self.assertEqual(normalize_emoji(value), value)

    def test_custom_emoji_are_normalised(self) -> None:
        self.assertEqual(normalize_emoji("gaming:123456789"), "<:gaming:123456789>")
        self.assertEqual(normalize_emoji("<:gaming:123456789>"), "<:gaming:123456789>")
        self.assertEqual(normalize_emoji("<a:party:123456789>"), "<a:party:123456789>")

    def test_text_is_never_stored_as_a_reaction(self) -> None:
        for value in ("", "   ", "abc", ":smile:", "role name", "🎮 🎬", "x" * 120, None):
            with self.subTest(value=value):
                self.assertIsNone(normalize_emoji(value))

    def test_variation_selectors_still_match(self) -> None:
        self.assertEqual(emoji_key("✅\ufe0f"), emoji_key("✅"))
        post = make_post(entries=[{"emoji": "✅", "role_id": 89}])
        self.assertIsNotNone(entry_for_emoji(post, "✅\ufe0f"))
        self.assertIsNone(entry_for_emoji(post, "🎮"))

    def test_entry_lookup_ignores_broken_entries(self) -> None:
        post = make_post(entries=["nonsense", {"emoji": "🎮", "role_id": 89}])
        self.assertEqual(entry_for_emoji(post, "🎮")["role_id"], 89)
        self.assertIsNone(entry_for_emoji({"entries": "nope"}, "🎮"))


class EntryValidationTests(unittest.TestCase):
    def test_valid_pairs_are_normalised(self) -> None:
        entries = parse_entries(
            [
                {"emoji": " 🎮 ", "roleId": "89", "label": "Gaming"},
                {"emoji": "gaming:123456789", "roleId": 90},
            ]
        )
        self.assertEqual(entries, [
            {"emoji": "🎮", "role_id": 89, "label": "Gaming"},
            {"emoji": "<:gaming:123456789>", "role_id": 90},
        ])

    def test_duplicate_emoji_are_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_entries([{"emoji": "🎮", "roleId": 1}, {"emoji": "🎮", "roleId": 2}])
        self.assertIn("more than once", str(caught.exception))

    def test_empty_and_oversized_lists_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_entries([])
        with self.assertRaises(ValueError):
            parse_entries("nope")
        with self.assertRaises(ValueError) as caught:
            parse_entries([{"emoji": "🎮", "roleId": index} for index in range(MAX_REACTION_ENTRIES + 1)])
        self.assertIn(str(MAX_REACTION_ENTRIES), str(caught.exception))

    def test_each_pair_needs_an_emoji_and_a_role(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_entries([{"emoji": "hello", "roleId": 1}])
        self.assertIn("Pair 1", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            parse_entries([{"emoji": "🎮", "roleId": ""}])
        self.assertIn("choose a role", str(caught.exception))
        with self.assertRaises(ValueError):
            parse_entries([{"emoji": "🎮", "roleId": -5}])
        with self.assertRaises(ValueError):
            parse_entries([[ "🎮", 1 ]])


class _Permissions:
    """Answers False for every permission that was not explicitly granted."""

    def __init__(self, **granted) -> None:
        self.__dict__.update(granted)

    def __getattr__(self, _name):
        return False


class _Role:
    def __init__(self, role_id, *, name="Gaming", position=10, **attributes) -> None:
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = attributes.pop("managed", False)
        self.permissions = attributes.pop("permissions", _Permissions())
        self.__dict__.update(attributes)

    def is_default(self) -> bool:
        return False

    def __ge__(self, other): return self.position >= other.position

    def __gt__(self, other): return self.position > other.position

    def __lt__(self, other): return self.position < other.position

    def __le__(self, other): return self.position <= other.position


class _Guild:
    def __init__(self, roles, member=None) -> None:
        self.id = 123
        self._roles = {role.id: role for role in roles}
        self._member = member
        self._fetch_member = None
        self.me = SimpleNamespace(
            guild_permissions=_Permissions(manage_roles=True),
            top_role=_Role(1, name="bot", position=50),
        )
        self.fetches = 0

    def get_role(self, role_id):
        return self._roles.get(int(role_id))

    def get_member(self, user_id):
        return self._member if self._member and user_id == self._member.id else None

    async def fetch_member(self, user_id):
        self.fetches += 1
        return self._fetch_member


class _Member:
    def __init__(self, user_id, *, roles=None, bot=False) -> None:
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


class _Bot:
    def __init__(self, config, guild, bot_user_id=999) -> None:
        self.config = config
        self.user = SimpleNamespace(id=bot_user_id)
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild if guild_id == self._guild.id else None


class PostContentTests(unittest.TestCase):
    def test_embed_posts_carry_title_and_message(self) -> None:
        content, embed = build_post_content(make_post())
        self.assertIsNone(content)
        self.assertEqual(embed.title, "Choose your roles")
        self.assertEqual(embed.description, "React below.")

    def test_plain_posts_post_the_message_alone(self) -> None:
        content, embed = build_post_content(make_post(use_embed=False))
        self.assertEqual(content, "React below.")
        self.assertIsNone(embed)

    def test_empty_message_falls_back_to_the_default_prompt(self) -> None:
        content, embed = build_post_content(make_post(message="   ", use_embed=False))
        self.assertEqual(content, DEFAULT_POST_MESSAGE)
        _content, embed = build_post_content(make_post(message="", title=""))
        self.assertEqual(embed.description, DEFAULT_POST_MESSAGE)
        self.assertEqual(embed.title, None)

    def test_message_limit_depends_on_the_style(self) -> None:
        self.assertEqual(message_limit(True), 4096)
        self.assertEqual(message_limit(False), 2000)


class PostValidationTests(unittest.TestCase):
    def test_roles_must_exist_and_be_self_assignable(self) -> None:
        guild = _Guild([_Role(89), _Role(90, name="Staff", permissions=_Permissions(ban_members=True))])
        self.assertIsNone(validate_post_entries(guild, [{"emoji": "🎮", "role_id": 89}]))

        missing = validate_post_entries(guild, [{"emoji": "🎮", "role_id": 404}])
        self.assertIn("no longer exists", missing)

        privileged = validate_post_entries(guild, [{"emoji": "🎮", "role_id": 90}])
        self.assertIn("Staff", privileged)
        self.assertIn("non-staff", privileged)

    def test_roles_above_the_bot_are_rejected(self) -> None:
        guild = _Guild([_Role(89, position=60)])
        problem = validate_post_entries(guild, [{"emoji": "🎮", "role_id": 89}])
        self.assertIn("above", problem)


class ReactionHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.role = _Role(89)
        self.member = _Member(321)
        self.guild = _Guild([self.role], self.member)
        self.post = make_post(entries=[{"emoji": "🎮", "role_id": 89}])
        self.bot = _Bot(make_config(**{"123": [self.post]}), self.guild)
        self.cog = ReactionRolesCog(self.bot)

    def payload(self, *, message_id=67, emoji="🎮", user_id=321, member=None):
        return SimpleNamespace(
            guild_id=123,
            message_id=message_id,
            emoji=emoji,
            user_id=user_id,
            member=member,
        )

    async def test_reacting_grants_the_paired_role_once(self) -> None:
        payload = self.payload(member=self.member)
        await self.cog.on_raw_reaction_add(payload)
        await self.cog.on_raw_reaction_add(payload)
        self.assertEqual(self.member.added, [self.role])

    async def test_removing_the_reaction_removes_the_role(self) -> None:
        self.member.roles.append(self.role)
        await self.cog.on_raw_reaction_remove(self.payload(member=self.member))
        self.assertEqual(self.member.removed, [self.role])

    async def test_a_post_can_keep_roles_when_reactions_are_removed(self) -> None:
        self.post["remove_on_unreact"] = False
        self.member.roles.append(self.role)
        await self.cog.on_raw_reaction_remove(self.payload(member=self.member))
        self.assertEqual(self.member.removed, [])

    async def test_other_messages_emojis_bots_and_dms_are_ignored(self) -> None:
        await self.cog.on_raw_reaction_add(self.payload(message_id=68, member=self.member))
        await self.cog.on_raw_reaction_add(self.payload(emoji="🎬", member=self.member))
        await self.cog.on_raw_reaction_add(self.payload(user_id=999, member=_Member(999, bot=True)))
        await self.cog.on_raw_reaction_add(
            SimpleNamespace(guild_id=None, message_id=67, emoji="🎮", user_id=321, member=self.member)
        )
        self.assertEqual(self.member.added, [])

    async def test_cached_member_is_used_without_a_fetch(self) -> None:
        await self.cog.on_raw_reaction_add(self.payload())
        self.assertEqual(self.member.added, [self.role])
        self.assertEqual(self.guild.fetches, 0)

    async def test_uncached_member_is_fetched(self) -> None:
        self.guild._member = None
        fetched = _Member(321)
        self.guild._fetch_member = fetched
        await self.cog.on_raw_reaction_add(self.payload())
        self.assertEqual(fetched.added, [self.role])
        self.assertEqual(self.guild.fetches, 1)

    async def test_a_missing_role_is_skipped_without_error(self) -> None:
        post = make_post(entries=[{"emoji": "🎮", "role_id": 404}])
        self.bot.config = make_config(**{"123": [post]})
        await self.cog.on_raw_reaction_add(self.payload(member=self.member))
        self.assertEqual(self.member.added, [])

    async def test_variation_selector_reacts_match_the_stored_emoji(self) -> None:
        post = make_post(entries=[{"emoji": "✅", "role_id": 89}])
        self.bot.config = make_config(**{"123": [post]})
        await self.cog.on_raw_reaction_add(self.payload(emoji="✅\ufe0f", member=self.member))
        self.assertEqual(self.member.added, [self.role])

    async def test_rules_acceptance_posts_are_not_handled_here(self) -> None:
        # A guild with only a rules post configured: nothing in this cog fires.
        self.bot.config = {"rules": {"123": {"message_id": 67, "role_id": 89}}}
        await self.cog.on_raw_reaction_add(self.payload(member=self.member))
        self.assertEqual(self.member.added, [])


class CogWiringTests(unittest.TestCase):
    """The cog must be registered, and its config key must exist by default.

    A cog that is never added loads nothing and logs nothing, so a typo in
    bot.py would leave every menu post silently inert in Discord.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.bot_source = (REPO_ROOT / "bot.py").read_text(encoding="utf-8")

    def test_bot_registers_the_cog(self) -> None:
        self.assertIn("from reaction_roles import ReactionRolesCog", self.bot_source)
        self.assertIn('bot.get_cog("ReactionRolesCog")', self.bot_source)
        self.assertIn("await bot.add_cog(ReactionRolesCog(bot))", self.bot_source)

    def test_default_config_has_the_reaction_roles_key(self) -> None:
        self.assertIn('"reaction_roles": {}', self.bot_source)

    def test_cog_listens_for_both_reaction_events(self) -> None:
        listeners = {name for name, _method in ReactionRolesCog.__cog_listeners__}
        self.assertEqual(listeners, {"on_raw_reaction_add", "on_raw_reaction_remove"})


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
