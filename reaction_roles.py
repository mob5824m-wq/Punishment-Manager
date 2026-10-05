"""Reaction-role menus for Punishment Manager.

The rules-acceptance gate in :mod:`rules` grants exactly one role for one ✅.
This module covers *additional* self-service role menus: an administrator can
publish any number of bot posts from the dashboard, each with its own message
(or embed) and up to :const:`MAX_REACTION_ENTRIES` emoji → role pairs. Reacting
grants the paired role; removing the reaction takes it back, unless the post
was saved with ``remove_on_unreact`` turned off.

Configuration is stored per guild in the bot's config under the
``reaction_roles`` key, alongside ``rules``::

    "reaction_roles": {
        "123456789012345678": [
            {
                "post_id": "6f1c0b3a",
                "channel_id": 111,
                "message_id": 222,
                "title": "Choose your roles",     # embed only, optional
                "message": "React below to pick your pings.",
                "use_embed": true,
                "remove_on_unreact": true,
                "entries": [
                    {"emoji": "🎮", "role_id": 333, "label": "Gaming"},
                ],
            }
        ]
    }

``post_id`` is a short random id: it is what the dashboard uses to address a
post, so re-publishing never has to guess message ids, and message ids stay
free to change when a post is moved to another channel.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Optional

import discord
from discord.ext import commands

from rules import validate_self_assignable_role


logger = logging.getLogger("punishment_manager.reaction_roles")

REACTION_ROLES_KEY = "reaction_roles"

# Discord's per-message limits: 20 distinct reactions, 2000 characters of
# message content, 256/4096 for an embed title/description.
MAX_REACTION_ENTRIES = 20
MAX_EMOJI_LENGTH = 100
MAX_TITLE_LENGTH = 256
MAX_PLAIN_MESSAGE_LENGTH = 2000
MAX_EMBED_MESSAGE_LENGTH = 4096

# Posted when the administrator leaves the message box empty. It is a module
# constant so the dashboard's preview and the published post cannot drift.
DEFAULT_POST_MESSAGE = "React with an emoji below to add or remove a role."

_POST_ID_BYTES = 4

_CUSTOM_EMOJI_MENTION = re.compile(r"^<a?:([A-Za-z0-9_]{2,32}):(\d{5,25})>$")
_CUSTOM_EMOJI_NAME = re.compile(r"^([A-Za-z0-9_]{2,32}):(\d{5,25})$")


def new_post_id() -> str:
    """Return a short unique id for a reaction-role post."""
    return secrets.token_hex(_POST_ID_BYTES)


def message_limit(use_embed: bool) -> int:
    """Characters allowed in a post's message for the chosen style."""
    return MAX_EMBED_MESSAGE_LENGTH if use_embed else MAX_PLAIN_MESSAGE_LENGTH


# --------------------------------------------------------------------------- #
# Reading the configuration
# --------------------------------------------------------------------------- #
def get_guild_reaction_posts(config: dict, guild_id: int) -> list[dict]:
    """Return every reaction-role post configured for a guild."""
    stored = config.get(REACTION_ROLES_KEY, {})
    if not isinstance(stored, dict):
        return []
    posts = stored.get(str(int(guild_id)))
    if not isinstance(posts, list):
        return []
    return [post for post in posts if isinstance(post, dict)]


def find_reaction_post(config: dict, guild_id: int, post_id: object) -> Optional[dict]:
    """Find one post by its dashboard id."""
    wanted = str(post_id)
    for post in get_guild_reaction_posts(config, guild_id):
        if str(post.get("post_id")) == wanted:
            return post
    return None


def find_reaction_post_by_message(
    config: dict, guild_id: int, message_id: object
) -> Optional[dict]:
    """Find the post a raw reaction event belongs to, if any."""
    try:
        wanted = int(message_id)
    except (TypeError, ValueError):
        return None
    for post in get_guild_reaction_posts(config, guild_id):
        try:
            if int(post.get("message_id")) == wanted:
                return post
        except (TypeError, ValueError):
            continue
    return None


def emoji_key(value: str) -> str:
    """Normalise an emoji for comparison.

    Discord stores "✅" and "✅\ufe0f" as the same reaction, so the variation
    selector must not decide whether a mapping matches.
    """
    return value.strip().replace("\ufe0f", "")


def normalize_emoji(raw: object) -> Optional[str]:
    """Return the string Discord accepts as a reaction, or ``None``.

    Unicode emoji pass through; ``name:id`` and ``<:name:id>`` are accepted so
    custom emoji can be typed (or pasted) in the dashboard, and anything with
    whitespace, ASCII letters, digits or a colon is rejected so ``:smile:`` or
    a role name can never be stored as a "reaction".
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or len(text) > MAX_EMOJI_LENGTH:
        return None
    if re.search(r"\s", text):
        return None
    mention = _CUSTOM_EMOJI_MENTION.match(text)
    if mention:
        animated = "<a:" if text.startswith("<a:") else "<:"
        return f"{animated}{mention.group(1)}:{mention.group(2)}>"
    named = _CUSTOM_EMOJI_NAME.match(text)
    if named:
        return f"<:{named.group(1)}:{named.group(2)}>"
    if any(char.isascii() and (char.isalnum() or char == ":") for char in text):
        return None
    return text


def entry_for_emoji(post: dict, emoji: object) -> Optional[dict]:
    """Return the emoji → role entry a reaction belongs to, if any."""
    wanted = emoji_key(str(emoji))
    if not wanted:
        return None
    entries = post.get("entries")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if emoji_key(str(entry.get("emoji", ""))) == wanted:
            return entry
    return None


def parse_entries(raw: object) -> list[dict]:
    """Validate the emoji → role pairs sent by the dashboard.

    Raises :class:`ValueError` with a sentence that can be shown to the
    administrator as-is.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("Add at least one emoji and role pair.")
    if len(raw) > MAX_REACTION_ENTRIES:
        raise ValueError(
            f"A post can hold at most {MAX_REACTION_ENTRIES} reaction roles."
        )

    entries: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Pair {index} is not a valid emoji and role pair.")
        emoji = normalize_emoji(item.get("emoji"))
        if emoji is None:
            raise ValueError(
                f"Pair {index}: enter one emoji — a unicode emoji, or a custom "
                "emoji as name:id."
            )
        key = emoji_key(emoji)
        if key in seen:
            raise ValueError(f"Pair {index}: {emoji} is used more than once.")
        seen.add(key)

        role_id = _positive_int(item.get("roleId", item.get("role_id")))
        if role_id is None:
            raise ValueError(f"Pair {index}: choose a role.")

        entry = {"emoji": emoji, "role_id": role_id}
        label = str(item.get("label") or "").strip()
        if label:
            entry["label"] = label[:100]
        entries.append(entry)
    return entries


def _positive_int(value: object) -> Optional[int]:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def validate_post_entries(
    guild: discord.Guild, entries: list[dict]
) -> Optional[str]:
    """Check every role can be self-assigned, returning the first problem."""
    for entry in entries:
        role = guild.get_role(int(entry["role_id"]))
        if role is None:
            return "One of the selected roles no longer exists in this server."
        problem = validate_self_assignable_role(guild, role)
        if problem:
            return f"{role.name}: {problem}"
    return None


def build_post_content(post: dict) -> tuple[Optional[str], Optional[discord.Embed]]:
    """Build the ``content``/``embed`` pair for a post's message."""
    message = str(post.get("message") or "").strip() or DEFAULT_POST_MESSAGE
    if not post.get("use_embed", True):
        return message, None

    embed = discord.Embed(description=message, color=discord.Color.blurple())
    title = str(post.get("title") or "").strip()
    if title:
        embed.title = title
    return None, embed


# --------------------------------------------------------------------------- #
# Writing the configuration
# --------------------------------------------------------------------------- #
def _replace_guild_posts(
    config: dict, guild_id: int, posts: Optional[list[dict]]
) -> dict:
    """Copy ``config`` with a guild's post list replaced (never mutated)."""
    updated = dict(config)
    existing = updated.get(REACTION_ROLES_KEY, {})
    by_guild = dict(existing) if isinstance(existing, dict) else {}
    key = str(int(guild_id))
    if posts:
        by_guild[key] = [dict(post) for post in posts]
    else:
        # Keep the config tidy: a guild with no posts has no key at all.
        by_guild.pop(key, None)
    updated[REACTION_ROLES_KEY] = by_guild
    return updated


def upsert_reaction_post(config: dict, guild_id: int, post: dict) -> dict:
    """Add a post, or replace the existing one with the same ``post_id``."""
    wanted = str(post.get("post_id"))
    posts = get_guild_reaction_posts(config, guild_id)
    merged: list[dict] = []
    replaced = False
    for existing in posts:
        if str(existing.get("post_id")) == wanted:
            merged.append(dict(post))
            replaced = True
        else:
            merged.append(existing)
    if not replaced:
        merged.append(dict(post))
    return _replace_guild_posts(config, guild_id, merged)


def remove_reaction_post(config: dict, guild_id: int, post_id: object) -> dict:
    """Drop one post from the configuration."""
    wanted = str(post_id)
    remaining = [
        post
        for post in get_guild_reaction_posts(config, guild_id)
        if str(post.get("post_id")) != wanted
    ]
    return _replace_guild_posts(config, guild_id, remaining)


# --------------------------------------------------------------------------- #
# Reaction handling
# --------------------------------------------------------------------------- #
class ReactionRolesCog(commands.Cog):
    """Grants and removes roles for every configured reaction-role post.

    The dashboard owns configuration writes (``dashboard.py``), so this cog
    only reads the stored mappings and answers reaction events.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload, add_role=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        await self._handle_reaction(payload, add_role=False)

    async def _handle_reaction(
        self,
        payload: discord.RawReactionActionEvent,
        *,
        add_role: bool,
    ) -> None:
        guild_id = payload.guild_id
        if guild_id is None:
            return

        post = find_reaction_post_by_message(
            self.bot.config, guild_id, payload.message_id
        )
        if post is None:
            return

        bot_user = self.bot.user
        if bot_user is not None and payload.user_id == bot_user.id:
            return
        if not add_role and not post.get("remove_on_unreact", True):
            return

        entry = entry_for_emoji(post, payload.emoji)
        if entry is None:
            return
        role_id = _positive_int(entry.get("role_id"))
        if role_id is None:
            logger.warning(
                "Reaction role entry %r in guild %s has no valid role", entry, guild_id
            )
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        role = guild.get_role(role_id)
        if role is None:
            logger.warning(
                "Configured reaction role %s is missing in guild %s", role_id, guild_id
            )
            return

        member = getattr(payload, "member", None) or guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return
            except discord.HTTPException as exc:
                logger.warning(
                    "Could not fetch reaction user %s: %s", payload.user_id, exc
                )
                return
        if member.bot:
            return

        has_role = role in member.roles
        if add_role == has_role:
            return

        emoji = str(entry.get("emoji", ""))
        try:
            if add_role:
                await member.add_roles(role, reason=f"Reaction role {emoji}")
                logger.info(
                    "Granted reaction role %s (%s) to user %s in guild %s",
                    role_id,
                    emoji,
                    member.id,
                    guild_id,
                )
            else:
                await member.remove_roles(
                    role, reason=f"Removed reaction role {emoji}"
                )
                logger.info(
                    "Removed reaction role %s (%s) from user %s in guild %s",
                    role_id,
                    emoji,
                    member.id,
                    guild_id,
                )
        except discord.Forbidden:
            logger.warning(
                "Cannot manage reaction role %s in guild %s; check bot role order "
                "and Manage Roles",
                role_id,
                guild_id,
            )
        except discord.HTTPException as exc:
            logger.warning(
                "Could not update reaction role %s for user %s in guild %s: %s",
                role_id,
                member.id,
                guild_id,
                exc,
            )
