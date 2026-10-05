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
    RULES_ACCEPT_EMOJI,
    RulesCog,
    get_guild_rules,
    is_active_rules_reaction,
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
        self._role = role
        self._member = member
        self.member_cached = True
        self.fetches = 0

    def get_role(self, role_id):
        return self._role if role_id == self._role.id else None

    def get_member(self, user_id):
        if not self.member_cached:
            return None
        return self._member if user_id == self._member.id else None

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
    def setUp(self) -> None:
        self.config = {
            "rules": {
                "123": {
                    "channel_id": 45,
                    "message_id": 67,
                    "role_id": 89,
                    "rules_text": "Be kind.",
                }
            }
        }

    def test_settings_are_stored_per_guild(self) -> None:
        self.assertEqual(get_guild_rules(self.config, 123)["role_id"], 89)
        self.assertIsNone(get_guild_rules(self.config, 456))

    def test_only_the_active_checkmark_message_matches(self) -> None:
        self.assertTrue(is_active_rules_reaction(self.config, 123, 67, RULES_ACCEPT_EMOJI))
        self.assertFalse(is_active_rules_reaction(self.config, 123, 68, RULES_ACCEPT_EMOJI))
        self.assertFalse(is_active_rules_reaction(self.config, 123, 67, "👋"))
        self.assertFalse(is_active_rules_reaction(self.config, None, 67, RULES_ACCEPT_EMOJI))

    def test_bad_or_missing_settings_fail_closed(self) -> None:
        self.assertFalse(is_active_rules_reaction({}, 123, 67, RULES_ACCEPT_EMOJI))
        broken = {"rules": {"123": {"message_id": "not-an-id"}}}
        self.assertFalse(is_active_rules_reaction(broken, 123, 67, RULES_ACCEPT_EMOJI))
        self.assertIsNone(get_guild_rules({"rules": []}, 123))


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
        self.cog = RulesCog(self.bot, lambda _config: None)

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


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
