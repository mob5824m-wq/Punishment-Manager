"""Rules publication and reaction-role support for Punishment Manager.

An administrator can publish any number of *rule sets* per server — a "Server
rules" post, an "Event rules" post, a "Contest rules" post and so on. Members
react with :const:`RULES_ACCEPT_EMOJI` on any of those posts to receive that
set's role; removing the reaction removes the role.

Each set stores its own channel, message, role and text in the bot's config,
so the behavior survives restarts::

    "rules": {
        "123456789012345678": [
            {
                "ruleset_id": "6f1c0b3a",
                "name": "Server rules",
                "channel_id": 111,
                "message_id": 222,
                "role_id": 333,
                "rules_text": "1. Be respectful."
            }
        ]
    }

Older configs stored a single set as a plain object rather than a list
(``"rules": {"123…": {"channel_id": …}}``). Those are still read — the object
is treated as one set named :const:`DEFAULT_RULESET_NAME` — and the first write
rewrites the guild's entry in the list shape.
"""

from __future__ import annotations

import logging
import secrets
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands


logger = logging.getLogger("punishment_manager.rules")
RULES_ACCEPT_EMOJI = "✅"
MAX_RULES_LENGTH = 4096

# Every set gets a name: it labels the post in the dashboard, titles the embed
# and is how /rules publish and /rules disable address a set.
DEFAULT_RULESET_NAME = "Server rules"
MAX_RULESET_NAME_LENGTH = 80
MAX_RULESETS_PER_GUILD = 25

# The text that accompanies the rules embed. It is a module constant so the
# /rules publish command and the dashboard's publish endpoint cannot drift
# apart, and so the dashboard preview can show the same wording members see.
RULES_POST_CONTENT = (
    "By reacting to this you acknowledge the rules and will abide by them."
)

# A self-service reaction must never grant moderation or server-management
# capabilities. Ordinary access/verified/member roles are fine.
PRIVILEGED_ROLE_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_messages",
    "manage_webhooks",
    "manage_threads",
    "view_audit_log",
    "kick_members",
    "ban_members",
    "moderate_members",
    "manage_nicknames",
    "manage_emojis_and_stickers",
    "manage_events",
)


def new_ruleset_id() -> str:
    """Return a short id for a new rule set (what the dashboard addresses it by)."""
    return secrets.token_hex(4)


def ruleset_id(settings: dict) -> str:
    """The id of a stored rule set, synthesised for pre-multi-set configs."""
    explicit = str(settings.get("ruleset_id") or "").strip()
    if explicit:
        return explicit
    try:
        return str(int(settings.get("message_id")))
    except (TypeError, ValueError):
        return "default"


def ruleset_name(settings: dict) -> str:
    """The display name of a stored rule set (legacy sets get the default)."""
    name = str(settings.get("name") or "").strip()
    return name or DEFAULT_RULESET_NAME


def rules_embed_title(guild_name: str, name: str) -> str:
    """The embed title for a set: "Guild Rules" for the default set."""
    label = (name or "").strip() or DEFAULT_RULESET_NAME
    if label.casefold() == DEFAULT_RULESET_NAME.casefold():
        return f"{guild_name} Rules"
    return f"{guild_name} — {label}"


def get_guild_rulesets(config: dict, guild_id: int) -> list[dict]:
    """Every rule set configured for a guild, in publish order.

    Accepts both config shapes: a list (current) or a single object (the shape
    older versions wrote), so an existing install keeps working untouched.
    """
    rules_by_guild = config.get("rules", {})
    if not isinstance(rules_by_guild, dict):
        return []
    entry = rules_by_guild.get(str(int(guild_id)))
    if isinstance(entry, dict):
        return [entry]
    if isinstance(entry, list):
        return [item for item in entry if isinstance(item, dict)]
    return []


def find_ruleset(config: dict, guild_id: int, wanted_id: object) -> Optional[dict]:
    """Find one rule set by its id."""
    target = str(wanted_id)
    for settings in get_guild_rulesets(config, guild_id):
        if ruleset_id(settings) == target:
            return settings
    return None


def find_ruleset_by_message(
    config: dict, guild_id: int, message_id: object
) -> Optional[dict]:
    """Find the rule set a raw reaction event belongs to, if any."""
    try:
        wanted = int(message_id)
    except (TypeError, ValueError):
        return None
    for settings in get_guild_rulesets(config, guild_id):
        try:
            if int(settings.get("message_id")) == wanted:
                return settings
        except (TypeError, ValueError):
            continue
    return None


def find_ruleset_by_name(
    config: dict, guild_id: int, wanted_name: object
) -> Optional[dict]:
    """Find one rule set by name, case-insensitively."""
    target = str(wanted_name or "").strip().casefold()
    if not target:
        return None
    for settings in get_guild_rulesets(config, guild_id):
        if ruleset_name(settings).casefold() == target:
            return settings
    return None


def _replace_guild_rulesets(
    config: dict, guild_id: int, rulesets: Optional[list[dict]]
) -> dict:
    """Copy ``config`` with a guild's rule sets replaced (never mutated)."""
    updated = dict(config)
    existing = updated.get("rules", {})
    rules_by_guild = dict(existing) if isinstance(existing, dict) else {}
    key = str(int(guild_id))
    if rulesets:
        rules_by_guild[key] = [dict(settings) for settings in rulesets]
    else:
        # Keep the config tidy: a server with no sets has no key at all.
        rules_by_guild.pop(key, None)
    updated["rules"] = rules_by_guild
    return updated


def upsert_ruleset(config: dict, guild_id: int, settings: dict) -> dict:
    """Add a rule set, or replace the existing one with the same id."""
    target = ruleset_id(settings)
    merged: list[dict] = []
    replaced = False
    for existing in get_guild_rulesets(config, guild_id):
        if ruleset_id(existing) == target:
            merged.append(dict(settings))
            replaced = True
        else:
            merged.append(existing)
    if not replaced:
        merged.append(dict(settings))
    return _replace_guild_rulesets(config, guild_id, merged)


def remove_ruleset(config: dict, guild_id: int, wanted_id: object) -> dict:
    """Drop one rule set from the configuration."""
    target = str(wanted_id)
    remaining = [
        settings
        for settings in get_guild_rulesets(config, guild_id)
        if ruleset_id(settings) != target
    ]
    return _replace_guild_rulesets(config, guild_id, remaining)


def validate_self_assignable_role(
    guild: discord.Guild, role: discord.Role
) -> Optional[str]:
    """Check a role a member can grant themselves, returning a problem or None.

    Shared by the rules-acceptance gate and by the reaction-role menus in
    :mod:`reaction_roles`, so neither path can hand out a moderation role and
    neither can drift from the other's hierarchy checks.
    """
    if role.is_default():
        return "The @everyone role cannot be used as a self-assignable role."
    if role.managed:
        return "That role is managed by an integration and can't be assigned by the bot."

    elevated = [
        name
        for name in PRIVILEGED_ROLE_PERMISSIONS
        if getattr(role.permissions, name, False)
    ]
    if elevated:
        return (
            "Choose a non-staff role. A self-assignable role cannot have "
            "moderation or server-management permissions."
        )

    bot_member = guild.me
    if bot_member is None:
        return "I couldn't verify my role permissions in this server."
    if not bot_member.guild_permissions.manage_roles:
        return "I need the Manage Roles permission to grant that role."
    if role >= bot_member.top_role:
        return "Move my bot role above that role in Server Settings → Roles."
    return None


def validate_post_channel(guild: discord.Guild, channel) -> Optional[str]:
    """Check the bot may post (and react) in a channel, returning a problem."""
    bot_member = guild.me
    if bot_member is None:
        return "I couldn't verify my channel permissions in this server."
    channel_permissions = channel.permissions_for(bot_member)
    required_channel_permissions = (
        ("view_channel", "view the channel"),
        ("send_messages", "send messages"),
        ("embed_links", "embed links"),
        ("add_reactions", "add reactions"),
    )
    missing = [
        label
        for permission, label in required_channel_permissions
        if not getattr(channel_permissions, permission, False)
    ]
    if missing:
        return "I need permission to " + ", ".join(missing) + " in that channel."
    return None


async def fetch_configured_message(
    guild: discord.Guild, settings: dict
) -> Optional[discord.Message]:
    """Fetch the message a stored ``channel_id``/``message_id`` pair points at.

    Used by the rules post and by reaction-role posts, which only differ in the
    config key they are stored under.
    """
    try:
        channel_id = int(settings["channel_id"])
        message_id = int(settings["message_id"])
    except (KeyError, TypeError, ValueError):
        return None

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            logger.info("Could not fetch configured channel %s: %s", channel_id, exc)
            return None

    if not hasattr(channel, "fetch_message"):
        return None
    try:
        return await channel.fetch_message(message_id)
    except discord.HTTPException as exc:
        logger.info("Could not fetch configured message %s: %s", message_id, exc)
        return None


def is_active_rules_reaction(
    config: dict, guild_id: Optional[int], message_id: int, emoji: object
) -> bool:
    """Return whether a raw reaction event belongs to a published rules post."""
    if guild_id is None or str(emoji) != RULES_ACCEPT_EMOJI:
        return False
    return find_ruleset_by_message(config, guild_id, message_id) is not None


class RulesCog(commands.Cog):
    """Admin commands and event handlers for rules acceptance reactions."""

    def __init__(
        self,
        bot: commands.Bot,
        save_config: Callable[[dict], None],
    ) -> None:
        self.bot = bot
        self._save_config = save_config

    async def cog_load(self) -> None:
        # The whole group is administrative. The same restriction is checked
        # again in each handler, since application-command permissions can be
        # changed by a server admin after the command is registered.
        self.rules_group.default_permissions = discord.Permissions(
            administrator=True
        )
        self.rules_group.guild_only = True

    rules_group = app_commands.Group(
        name="rules",
        description="Publish rules and configure the rules-acceptance role.",
    )

    @rules_group.command(
        name="publish",
        description="Post a rule set and enable its acceptance reaction role.",
    )
    @app_commands.describe(
        channel="Channel where the rules post should appear.",
        role="Non-staff role granted when a member accepts these rules.",
        rules_text="Rules shown in the embed (maximum 4,096 characters).",
        name="Rule set name. Publishing again with the same name replaces its post.",
    )
    async def publish_rules(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role,
        rules_text: str,
        name: Optional[str] = None,
    ) -> None:
        await self._defer(interaction)

        guild = interaction.guild
        if guild is None:
            await self._respond(interaction, "This command can only be used in a server.")
            return
        if not self._is_admin(interaction):
            await self._respond(interaction, "Only server administrators can publish rules.")
            return
        if channel.guild.id != guild.id or role.guild.id != guild.id:
            await self._respond(interaction, "Choose a channel and role from this server.")
            return

        label = (name or "").strip() or DEFAULT_RULESET_NAME
        if len(label) > MAX_RULESET_NAME_LENGTH:
            await self._respond(
                interaction,
                f"That rule set name is {len(label)} characters; the limit is "
                f"{MAX_RULESET_NAME_LENGTH}.",
            )
            return

        previous = find_ruleset_by_name(self.bot.config, guild.id, label)
        if previous is None and len(get_guild_rulesets(self.bot.config, guild.id)) >= (
            MAX_RULESETS_PER_GUILD
        ):
            await self._respond(
                interaction,
                f"This server already has {MAX_RULESETS_PER_GUILD} rule sets. "
                "Disable one with /rules disable before adding another.",
            )
            return

        text = rules_text.strip()
        if not text:
            await self._respond(interaction, "Rules text cannot be empty.")
            return
        if len(text) > MAX_RULES_LENGTH:
            await self._respond(
                interaction,
                f"Rules text is {len(text)} characters; the limit is {MAX_RULES_LENGTH}.",
            )
            return

        role_error = self._validate_role(guild, channel, role)
        if role_error is not None:
            await self._respond(interaction, role_error)
            return

        embed = discord.Embed(
            title=rules_embed_title(guild.name, label),
            description=text,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"React with {RULES_ACCEPT_EMOJI} to accept the rules")
        allowed_mentions = discord.AllowedMentions.none()

        posted_message: Optional[discord.Message] = None
        try:
            posted_message = await channel.send(
                content=RULES_POST_CONTENT,
                embed=embed,
                allowed_mentions=allowed_mentions,
            )
        except discord.Forbidden:
            await self._respond(
                interaction,
                "I can't publish there. Check that I can view the channel, "
                "send messages, embed links, and add reactions.",
            )
            return
        except discord.HTTPException as exc:
            logger.warning("Could not publish rules in guild %s: %s", guild.id, exc)
            await self._respond(
                interaction,
                "Discord couldn't publish the rules post. Check the channel "
                "permissions and try again.",
            )
            return

        previous_config = self.bot.config
        updated = upsert_ruleset(
            self.bot.config,
            guild.id,
            {
                "ruleset_id": ruleset_id(previous) if previous else new_ruleset_id(),
                "name": label,
                "channel_id": channel.id,
                "message_id": posted_message.id,
                "role_id": role.id,
                "rules_text": text,
            },
        )
        # Make the new message active before awaiting the reaction request so
        # an eager member reaction cannot be missed during publication.
        self.bot.config = updated
        try:
            await posted_message.add_reaction(RULES_ACCEPT_EMOJI)
        except discord.Forbidden:
            self.bot.config = previous_config
            await self._try_delete(posted_message)
            await self._respond(
                interaction,
                "I couldn't add the acceptance reaction. Check that I have "
                "Add Reactions permission in that channel.",
            )
            return
        except discord.HTTPException as exc:
            logger.warning("Could not add rules reaction in guild %s: %s", guild.id, exc)
            self.bot.config = previous_config
            await self._try_delete(posted_message)
            await self._respond(
                interaction,
                "Discord couldn't add the acceptance reaction. Check the "
                "channel permissions and try again.",
            )
            return

        try:
            self._save_config(updated)
        except Exception:
            logger.exception("Could not save rules configuration for guild %s", guild.id)
            self.bot.config = previous_config
            await self._try_delete(posted_message)
            await self._respond(
                interaction,
                "The rules post was created, but I couldn't save its configuration. "
                "Please check the bot's config-file permissions and try again.",
            )
            return

        if previous is not None:
            await self._retire_previous_post(guild, previous, posted_message.id)

        replaced = " Its previous post was replaced." if previous is not None else ""
        await self._respond(
            interaction,
            f"Published the rule set **{label}** in {channel.mention}. Members "
            f"can react with {RULES_ACCEPT_EMOJI} to receive {role.mention}; "
            f"removing the reaction removes the role.{replaced}",
        )

    @rules_group.command(
        name="disable",
        description="Disable a rule set's acceptance reaction role.",
    )
    @app_commands.describe(
        name="Rule set to disable. Only needed when the server has several.",
    )
    async def disable_rules(
        self, interaction: discord.Interaction, name: Optional[str] = None
    ) -> None:
        await self._defer(interaction)

        guild = interaction.guild
        if guild is None:
            await self._respond(interaction, "This command can only be used in a server.")
            return
        if not self._is_admin(interaction):
            await self._respond(
                interaction,
                "Only server administrators can disable rules reactions.",
            )
            return

        rulesets = get_guild_rulesets(self.bot.config, guild.id)
        if not rulesets:
            await self._respond(
                interaction,
                "No rules reaction role is configured for this server.",
            )
            return

        label = (name or "").strip()
        if label:
            previous = find_ruleset_by_name(self.bot.config, guild.id, label)
            if previous is None:
                await self._respond(
                    interaction,
                    f"No rule set is named **{label}**. Published: "
                    + self._names(rulesets)
                    + ".",
                )
                return
        elif len(rulesets) == 1:
            previous = rulesets[0]
            label = ruleset_name(previous)
        else:
            await self._respond(
                interaction,
                "This server has several rule sets — pass `name` to choose one: "
                + self._names(rulesets)
                + ".",
            )
            return

        updated = remove_ruleset(self.bot.config, guild.id, ruleset_id(previous))
        try:
            self._save_config(updated)
        except Exception:
            logger.exception("Could not disable rules configuration for guild %s", guild.id)
            await self._respond(
                interaction,
                "I couldn't save the updated config, so the rules reaction role is still active.",
            )
            return

        self.bot.config = updated
        await self._mark_post_disabled(guild, previous)
        remaining = len(get_guild_rulesets(self.bot.config, guild.id))
        await self._respond(
            interaction,
            f"Disabled the rule set **{ruleset_name(previous)}**; "
            f"{remaining} rule set(s) still active. Existing role assignments "
            "are left unchanged — remove them manually if needed.",
        )

    @rules_group.command(
        name="list",
        description="List the published rule sets and the roles they grant.",
    )
    async def list_rules(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)

        guild = interaction.guild
        if guild is None:
            await self._respond(interaction, "This command can only be used in a server.")
            return
        if not self._is_admin(interaction):
            await self._respond(
                interaction,
                "Only server administrators can list rule sets.",
            )
            return

        rulesets = get_guild_rulesets(self.bot.config, guild.id)
        if not rulesets:
            await self._respond(
                interaction,
                "No rule sets are published yet. Use `/rules publish` to add one.",
            )
            return

        lines = []
        for settings in rulesets:
            channel = guild.get_channel(int(settings.get("channel_id") or 0))
            role = guild.get_role(int(settings.get("role_id") or 0))
            lines.append(
                f"• **{ruleset_name(settings)}** — "
                f"{channel.mention if channel else 'channel missing'} → "
                f"{role.mention if role else 'role missing'} "
                f"(message `{settings.get('message_id')}`)"
            )
        await self._respond(
            interaction,
            f"{len(rulesets)} rule set(s) in this server:\n" + "\n".join(lines),
        )

    @staticmethod
    def _names(rulesets: list[dict]) -> str:
        """A comma-separated list of set names for an error message."""
        return ", ".join(f"**{ruleset_name(settings)}**" for settings in rulesets)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_rules_reaction(payload, add_role=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_rules_reaction(payload, add_role=False)

    async def _handle_rules_reaction(
        self,
        payload: discord.RawReactionActionEvent,
        *,
        add_role: bool,
    ) -> None:
        guild_id = payload.guild_id
        if guild_id is None or str(payload.emoji) != RULES_ACCEPT_EMOJI:
            return

        # Every published set owns its message, so the message id decides which
        # set's role (and text) the reaction belongs to.
        settings = find_ruleset_by_message(
            self.bot.config, guild_id, payload.message_id
        )
        if settings is None:
            return

        bot_user = self.bot.user
        if bot_user is not None and payload.user_id == bot_user.id:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        try:
            role_id = int(settings["role_id"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Invalid rules role id for guild %s", guild_id)
            return

        role = guild.get_role(role_id)
        if role is None:
            logger.warning("Configured rules role %s is missing in guild %s", role_id, guild_id)
            return

        member = getattr(payload, "member", None) or guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return
            except discord.HTTPException as exc:
                logger.warning("Could not fetch reaction user %s: %s", payload.user_id, exc)
                return

        if member.bot:
            return

        has_role = role in member.roles
        if add_role == has_role:
            return

        try:
            if add_role:
                await member.add_roles(role, reason="Accepted the server rules")
                logger.info(
                    "Granted rules role %s to user %s in guild %s",
                    role_id,
                    member.id,
                    guild_id,
                )
            else:
                await member.remove_roles(role, reason="Removed rules acceptance reaction")
                logger.info(
                    "Removed rules role %s from user %s in guild %s",
                    role_id,
                    member.id,
                    guild_id,
                )
        except discord.Forbidden:
            logger.warning(
                "Cannot manage rules role %s in guild %s; check bot role order and Manage Roles",
                role_id,
                guild_id,
            )
        except discord.HTTPException as exc:
            logger.warning(
                "Could not update rules role %s for user %s in guild %s: %s",
                role_id,
                member.id,
                guild_id,
                exc,
            )

    @staticmethod
    def _is_admin(interaction: discord.Interaction) -> bool:
        user = interaction.user
        permissions = getattr(user, "guild_permissions", None)
        return bool(permissions and permissions.administrator)

    @staticmethod
    def _validate_role(
        guild: discord.Guild,
        channel: discord.TextChannel,
        role: discord.Role,
    ) -> Optional[str]:
        role_error = validate_self_assignable_role(guild, role)
        if role_error is not None:
            return role_error
        return validate_post_channel(guild, channel)

    async def _retire_previous_post(
        self,
        guild: discord.Guild,
        settings: dict,
        replacement_message_id: int,
    ) -> None:
        """Delete the old bot-owned post after its replacement is active."""
        message = await self._get_configured_message(guild, settings)
        if message is None or message.id == replacement_message_id:
            return
        if self.bot.user is None or message.author.id != self.bot.user.id:
            return
        try:
            await message.delete()
        except discord.HTTPException as exc:
            logger.info("Could not delete replaced rules message %s: %s", message.id, exc)

    async def _mark_post_disabled(self, guild: discord.Guild, settings: dict) -> None:
        message = await self._get_configured_message(guild, settings)
        if message is None or self.bot.user is None or message.author.id != self.bot.user.id:
            return
        try:
            await message.edit(
                content=(
                    "The rules acceptance reaction role has been disabled by an "
                    "administrator. The rules below are retained for reference."
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            logger.info("Could not mark rules message %s disabled: %s", message.id, exc)
        try:
            await message.remove_reaction(RULES_ACCEPT_EMOJI, self.bot.user)
        except discord.HTTPException:
            # The message may have no bot reaction, or the bot may lack access.
            pass

    async def _get_configured_message(
        self,
        guild: discord.Guild,
        settings: dict,
    ) -> Optional[discord.Message]:
        return await fetch_configured_message(guild, settings)

    @staticmethod
    async def _try_delete(message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.HTTPException as exc:
            logger.info("Could not clean up incomplete rules message %s: %s", message.id, exc)

    @staticmethod
    async def _defer(interaction: discord.Interaction) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except (discord.InteractionResponded, discord.HTTPException):
            pass

    @staticmethod
    async def _respond(interaction: discord.Interaction, content: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (discord.InteractionResponded, discord.HTTPException) as exc:
            logger.warning("Could not respond to rules command: %s", exc)
