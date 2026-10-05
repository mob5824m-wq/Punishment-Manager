"""Rules publication and reaction-role support for Punishment Manager.

An administrator can publish a rules embed to a channel. Members react with
:const:`RULES_ACCEPT_EMOJI` to receive the configured role; removing that
reaction removes the role. The active message and role are persisted in the
bot's config so the behavior survives restarts.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands


logger = logging.getLogger("punishment_manager.rules")
RULES_ACCEPT_EMOJI = "✅"
MAX_RULES_LENGTH = 4096

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


def get_guild_rules(config: dict, guild_id: int) -> Optional[dict]:
    """Get a guild's persisted rules/reaction settings, if configured."""
    rules_by_guild = config.get("rules", {})
    if not isinstance(rules_by_guild, dict):
        return None
    entry = rules_by_guild.get(str(int(guild_id)))
    if not isinstance(entry, dict):
        return None
    return entry


def is_active_rules_reaction(
    config: dict, guild_id: Optional[int], message_id: int, emoji: object
) -> bool:
    """Return whether a raw reaction event belongs to the active rules post."""
    if guild_id is None or str(emoji) != RULES_ACCEPT_EMOJI:
        return False
    settings = get_guild_rules(config, guild_id)
    if settings is None:
        return False
    try:
        return int(settings["message_id"]) == int(message_id)
    except (KeyError, TypeError, ValueError):
        return False


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
        description="Post server rules and enable the acceptance reaction role.",
    )
    @app_commands.describe(
        channel="Channel where the rules post should appear.",
        role="Non-staff role granted when a member accepts the rules.",
        rules_text="Rules shown in the embed (maximum 4,096 characters).",
    )
    async def publish_rules(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role,
        rules_text: str,
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
            title=f"{guild.name} Rules",
            description=text,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"React with {RULES_ACCEPT_EMOJI} to accept the rules")
        content = (
            f"React with {RULES_ACCEPT_EMOJI} below to accept these rules and "
            f"receive {role.mention}. Removing your reaction removes the role."
        )
        allowed_mentions = discord.AllowedMentions.none()

        posted_message: Optional[discord.Message] = None
        try:
            posted_message = await channel.send(
                content=content,
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

        previous = get_guild_rules(self.bot.config, guild.id)
        previous_config = self.bot.config
        updated = self._updated_config(
            guild.id,
            {
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

        await self._respond(
            interaction,
            f"Published rules in {channel.mention}. Members can react with "
            f"{RULES_ACCEPT_EMOJI} to receive {role.mention}; removing the "
            "reaction removes the role. The previous active post was replaced.",
        )

    @rules_group.command(
        name="disable",
        description="Disable the active rules acceptance reaction role.",
    )
    async def disable_rules(self, interaction: discord.Interaction) -> None:
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

        previous = get_guild_rules(self.bot.config, guild.id)
        if previous is None:
            await self._respond(
                interaction,
                "No rules reaction role is configured for this server.",
            )
            return

        updated = self._updated_config(guild.id, None)
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
        await self._respond(
            interaction,
            "Rules reactions are disabled. Existing role assignments are left unchanged; "
            "remove them manually if needed.",
        )

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
        if guild_id is None or not is_active_rules_reaction(
            self.bot.config, guild_id, payload.message_id, payload.emoji
        ):
            return

        bot_user = self.bot.user
        if bot_user is not None and payload.user_id == bot_user.id:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        settings = get_guild_rules(self.bot.config, guild_id)
        if settings is None:
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
        if role.is_default():
            return "The @everyone role cannot be used as an acceptance role."
        if role.managed:
            return "That role is managed by an integration and can't be assigned by the bot."

        elevated = [
            name
            for name in PRIVILEGED_ROLE_PERMISSIONS
            if getattr(role.permissions, name, False)
        ]
        if elevated:
            return (
                "Choose a non-staff role. The acceptance role cannot have "
                "moderation or server-management permissions."
            )

        bot_member = guild.me
        if bot_member is None:
            return "I couldn't verify my role permissions in this server."
        if not bot_member.guild_permissions.manage_roles:
            return "I need the Manage Roles permission to grant the acceptance role."
        if role >= bot_member.top_role:
            return "Move my bot role above the acceptance role in Server Settings → Roles."

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

    def _updated_config(self, guild_id: int, settings: Optional[dict]) -> dict:
        updated = dict(self.bot.config)
        existing = updated.get("rules", {})
        rules_by_guild = dict(existing) if isinstance(existing, dict) else {}
        key = str(int(guild_id))
        if settings is None:
            rules_by_guild.pop(key, None)
        else:
            rules_by_guild[key] = dict(settings)
        updated["rules"] = rules_by_guild
        return updated

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
                logger.info("Could not fetch rules channel %s: %s", channel_id, exc)
                return None

        if not hasattr(channel, "fetch_message"):
            return None
        try:
            return await channel.fetch_message(message_id)
        except discord.HTTPException as exc:
            logger.info("Could not fetch rules message %s: %s", message_id, exc)
            return None

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
