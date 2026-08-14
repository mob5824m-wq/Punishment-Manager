"""
Punishment Manager - a discord.py bot that temporarily swaps a user's role
sequence:  Normal -> Punished -> PostPunished
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks


# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "data" / "punishments.db"
LOG_PATH = BASE_DIR / "data" / "bot.log"

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logger = logging.getLogger("punishment_manager")
logger.setLevel(logging.INFO)
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
file_handler.setFormatter(formatter)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(stream_handler)


# --------------------------------------------------------------------------- #
# Persistence (SQLite)
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS punishments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    moderator_id    INTEGER NOT NULL,
    reason          TEXT,
    punish_role_id  INTEGER NOT NULL,
    normal_role_id  INTEGER NOT NULL,
    post_role_id    INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'punished'  -- 'punished' or 'post'
);
CREATE INDEX IF NOT EXISTS idx_punishments_expiry
    ON punishments (expires_at);
CREATE INDEX IF NOT EXISTS idx_punishments_user
    ON punishments (guild_id, user_id);

-- Permanent record of every punishment that's ever ended. Used by
-- /punish status <user> to show a user's history.
CREATE TABLE IF NOT EXISTS punishment_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    moderator_id     INTEGER NOT NULL,
    reason           TEXT,
    started_at       TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    ended_at         TEXT NOT NULL,
    ended_reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_punishment_history_user
    ON punishment_history (guild_id, user_id, started_at DESC);
"""


def init_db() -> None:
    """Create the SQLite database and tables if they don't exist."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
    logger.info("Database initialised at %s", DB_PATH)


def db_execute(query: str, params: tuple = ()) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(query, params)
        conn.commit()


def db_fetchall(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(query, params).fetchall())


def db_fetchone(query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchone()


def archive_punishment(record: dict, ended_reason: str) -> None:
    """Move a finished punishment into the history table. The original
    record dict is the row from the `punishments` table."""
    started = datetime.fromisoformat(record["started_at"])
    expires = datetime.fromisoformat(record["expires_at"])
    duration = max(0, int((expires - started).total_seconds()))
    try:
        db_execute(
            """
            INSERT INTO punishment_history
                (guild_id, user_id, moderator_id, reason,
                 started_at, duration_seconds, ended_at, ended_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["guild_id"],
                record["user_id"],
                record["moderator_id"],
                record.get("reason"),
                record["started_at"],
                duration,
                datetime.now(timezone.utc).isoformat(),
                ended_reason,
            ),
        )
    except sqlite3.Error as exc:
        logger.warning("Failed to archive punishment %s: %s", record.get("id"), exc)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "token": "",
    "guilds": {},
    "default_duration_minutes": 30,
    "log_channel_id": None,
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        logger.warning(
            "Created default config at %s - please fill in your token and roles.",
            CONFIG_PATH,
        )
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def get_guild_config(cfg: dict, guild_id: int) -> Optional[dict]:
    return cfg.get("guilds", {}).get(str(guild_id))


# --------------------------------------------------------------------------- #
# Duration parsing
# --------------------------------------------------------------------------- #
DURATION_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def parse_duration(text: str) -> Optional[int]:
    """
    Parse a duration like '30m', '2h', '1d12h', '90' (treated as minutes).
    Returns the duration in seconds, or None if invalid.
    """
    if not text:
        return None
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    if text.isdigit():
        return int(text) * 60
    total = 0
    i = 0
    n = len(text)
    while i < n:
        j = i
        while j < n and (text[j].isdigit() or text[j] == "."):
            j += 1
        if j == i:
            return None
        try:
            value = float(text[i:j])
        except ValueError:
            return None
        k = j
        while k < n and text[k].isalpha():
            k += 1
        unit = text[j:k] if k > j else "m"
        multiplier = DURATION_UNITS.get(unit)
        if multiplier is None:
            return None
        total += int(value * multiplier)
        i = k
    return total if total > 0 else None


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    parts = []
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not (days or hours):
        parts.append(f"{secs}s")
    return " ".join(parts) or "0m"


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
intents = discord.Intents.default()
intents.members = True       # required to read/modify members
intents.guilds = True


class PunishmentBot(commands.Bot):
    def __init__(self, config: dict) -> None:
        super().__init__(
            command_prefix="!",  # not used; we only expose slash commands
            intents=intents,
            help_command=None,
        )
        self.config = config
        self.scheduler_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        # Sync the global slash command tree.
        try:
            synced = await self.tree.sync()
            logger.info("Synced %d global command(s).", len(synced))
        except Exception:
            logger.exception("Failed to sync commands.")

        if self.scheduler_task is None or self.scheduler_task.done():
            self.scheduler_task = self.loop.create_task(self.scheduler_loop())
            logger.info("Scheduler loop started.")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id=%s)", self.user, self.user.id)
        logger.info("Connected to %d guild(s).", len(self.guilds))
        await self.process_due_punishments()

    # ------------------------------------------------------------------ #
    # Scheduler
    # ------------------------------------------------------------------ #
    async def scheduler_loop(self) -> None:
        try:
            while not self.is_closed():
                await self.process_due_punishments()
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler loop crashed.")
            raise

    async def process_due_punishments(self) -> None:
        now = datetime.now(timezone.utc)
        rows = db_fetchall(
            "SELECT * FROM punishments WHERE expires_at <= ?",
            (now.isoformat(),),
        )
        for row in rows:
            await self._advance_punishment(dict(row))

    async def _advance_punishment(self, record: dict) -> None:
        guild = self.get_guild(record["guild_id"])
        if guild is None:
            logger.warning(
                "Guild %s not found; deleting punishment record.",
                record["guild_id"],
            )
            db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
            return

        try:
            member = await guild.fetch_member(record["user_id"])
        except (discord.NotFound, discord.HTTPException):
            logger.info(
                "User %s no longer in guild %s; deleting record.",
                record["user_id"],
                guild.id,
            )
            db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
            return

        stage = record["stage"]
        punish_role = guild.get_role(record["punish_role_id"])
        post_role = guild.get_role(record["post_role_id"])

        try:
            if stage == "punished":
                if punish_role and punish_role in member.roles:
                    await member.remove_roles(
                        punish_role, reason="Punishment timer expired."
                    )
                if post_role and post_role not in member.roles:
                    await member.add_roles(
                        post_role, reason="Punishment timer expired."
                    )
                archive_punishment(record, ended_reason="timer_expired")
                db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
                await self._log_event(
                    guild,
                    f"[timer] {member.mention} moved from **punished** -> "
                    f"**post-punishment**.",
                )
                logger.info(
                    "Advanced punishment %s for user %s to post stage.",
                    record["id"],
                    record["user_id"],
                )
            else:
                db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
        except discord.Forbidden:
            logger.error(
                "Missing permissions in guild %s for punishment %s.",
                guild.id, record["id"],
            )
        except discord.HTTPException:
            logger.exception(
                "Discord error while processing punishment %s.", record["id"]
            )

    async def _log_event(self, guild: discord.Guild, message: str) -> None:
        channel_id = self.config.get("log_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.HTTPException:
                return
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(message)
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ #
    # Core punishment flow
    # ------------------------------------------------------------------ #
    async def start_punishment(
        self,
        guild: discord.Guild,
        member: discord.Member,
        moderator: discord.Member,
        duration_seconds: int,
        reason: str,
        cfg: dict,
    ) -> Optional[dict]:
        normal_role = guild.get_role(cfg["normal_role_id"])
        punish_role = guild.get_role(cfg["punish_role_id"])
        post_role = guild.get_role(cfg["post_role_id"])

        if not (normal_role and punish_role and post_role):
            return None

        if punish_role >= guild.me.top_role:
            return {"error": "The punish role is higher than (or equal to) my top role."}
        if post_role >= guild.me.top_role:
            return {"error": "The post-punish role is higher than (or equal to) my top role."}
        if normal_role >= guild.me.top_role:
            return {"error": "The normal role is higher than (or equal to) my top role."}

        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=duration_seconds)

        try:
            db_execute(
                """
                INSERT INTO punishments
                    (guild_id, user_id, moderator_id, reason,
                     punish_role_id, normal_role_id, post_role_id,
                     started_at, expires_at, stage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'punished')
                """,
                (
                    guild.id,
                    member.id,
                    moderator.id,
                    reason,
                    punish_role.id,
                    normal_role.id,
                    post_role.id,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
        except sqlite3.Error as exc:
            logger.exception("Failed to record punishment: %s", exc)
            return {"error": "Database error; punishment not recorded."}

        try:
            if normal_role in member.roles:
                await member.remove_roles(normal_role, reason=f"Punished: {reason}")
            if punish_role not in member.roles:
                await member.add_roles(punish_role, reason=f"Punished: {reason}")
        except discord.Forbidden:
            db_execute(
                "DELETE FROM punishments WHERE user_id = ? AND guild_id = ? AND stage='punished'",
                (member.id, guild.id),
            )
            return {"error": "I lack permission to change that user's roles."}
        except discord.HTTPException as exc:
            db_execute(
                "DELETE FROM punishments WHERE user_id = ? AND guild_id = ? AND stage='punished'",
                (member.id, guild.id),
            )
            return {"error": f"Discord error: {exc}"}

        return {
            "expires_at": expires,
            "duration_seconds": duration_seconds,
        }


# --------------------------------------------------------------------------- #
# Slash command setup
# --------------------------------------------------------------------------- #
# NOTE: import-time matters. The `bot` instance is created at module import
# so that `@bot.tree.command(...)` decorators below can register onto it.
# We also build the Cog and add it to the bot explicitly so that the
# command discovery happens in one well-defined place instead of being
# scattered across the module. This is the most reliable way to avoid
# the "app_commands breaks at sync time" footgun in discord.py 2.x.
# --------------------------------------------------------------------------- #

bot = PunishmentBot(load_config())


# ---- A single shared Cog holds all slash commands. --------------------- #
class PunishmentCog(commands.Cog):
    """Slash commands for the Punishment Manager."""

    def __init__(self, bot_: PunishmentBot) -> None:
        self.bot = bot_

    async def cog_load(self) -> None:
        """Set default_permissions on each slash command after the
        decorator machinery has run. ``app_commands.command()`` does not
        accept this kwarg, so we assign it here.
        """
        # The punish group itself: visible to everyone with Moderate Members.
        self.punish_group.default_permissions = discord.Permissions(
            moderate_members=True
        )
        # /setup: only admins should see/use it, and only in a guild.
        for cmd in self.__cog_app_commands__:
            if cmd.qualified_name == "setup":
                cmd.default_permissions = discord.Permissions(administrator=True)
                cmd.guild_only = True

    # ---- Group of helpers (use a slash command group) ------------------ #
    punish_group = app_commands.Group(
        name="punish",
        description="Temporarily swap a user's role.",
    )

    @punish_group.command(
        name="apply",
        description="Strip a user's normal role and give them the punish role for a duration.",
    )
    @app_commands.describe(
        user="The user to punish.",
        duration="How long. Examples: 30m, 2h, 1d, 1d12h. Bare numbers = minutes.",
        reason="Optional reason shown in logs and stored in the database.",
    )
    async def punish_apply(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: Optional[str] = None,
    ) -> None:
        await self._handle_punish(interaction, user, duration, reason)

    @punish_group.command(
        name="pardon",
        description="End an active punishment early and restore the user.",
    )
    @app_commands.describe(user="The user to pardon.")
    async def punish_pardon(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        await self._handle_pardon(interaction, user)

    @punish_group.command(
        name="status",
        description="Show this server's configuration and active punishments. "
                    "Pass a user to see their punishment history.",
    )
    @app_commands.describe(
        user="Optional: show this user's punishment history instead of all active punishments.",
    )
    async def punish_status(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
    ) -> None:
        await self._handle_status(interaction, user)

    # ---- /setup lives at the top level, admin-only --------------------- #
    @app_commands.command(
        name="setup",
        description="Configure this server's normal / punish / post-punish roles.",
    )
    @app_commands.describe(
        normal_role="The role users normally have.",
        punish_role="The role given during punishment.",
        post_role="The role given after the timer expires.",
        log_channel="Optional channel for punishment logs.",
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        normal_role: discord.Role,
        punish_role: discord.Role,
        post_role: discord.Role,
        log_channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await self._handle_setup(interaction, normal_role, punish_role, post_role, log_channel)

    # ------------------------------------------------------------------ #
    # Command implementations
    # ------------------------------------------------------------------ #
    async def _handle_punish(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: Optional[str],
    ) -> None:
        # All responses are ephemeral. Defer first so we have time to act.
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass
        except discord.HTTPException:
            return

        cfg = get_guild_config(self.bot.config, interaction.guild_id)
        if not cfg:
            await self._safe_followup(
                interaction,
                "This server is not configured yet. An admin should run "
                "`/setup` or edit `config.json`.",
            )
            return

        if user.bot:
            await self._safe_followup(interaction, "You can't punish a bot.")
            return
        if user.id == interaction.user.id:
            await self._safe_followup(interaction, "You can't punish yourself.")
            return
        if user.top_role >= interaction.guild.me.top_role:
            await self._safe_followup(
                interaction, "That user has a role equal to or higher than mine."
            )
            return
        if user.guild_permissions.administrator:
            await self._safe_followup(
                interaction, "Refusing to punish a server administrator."
            )
            return

        seconds = parse_duration(duration)
        if seconds is None or seconds < 5:
            await self._safe_followup(
                interaction,
                "Invalid duration. Try e.g. `30m`, `2h`, `1d12h`.",
            )
            return
        if seconds > 60 * 60 * 24 * 30:  # 30 days cap
            await self._safe_followup(
                interaction, "Duration too long (max 30 days)."
            )
            return

        result = await self.bot.start_punishment(
            guild=interaction.guild,
            member=user,
            moderator=interaction.user,  # type: ignore[arg-type]
            duration_seconds=seconds,
            reason=reason or "No reason provided.",
            cfg=cfg,
        )

        if result is None:
            await self._safe_followup(
                interaction,
                "Could not start punishment: one or more configured roles are missing.",
            )
            return
        if "error" in result:
            await self._safe_followup(interaction, result["error"])
            return

        pretty = format_duration(seconds)
        reason_text = reason or "No reason provided."
        await self._safe_followup(
            interaction,
            f"Punished {user.mention} for **{pretty}**.\nReason: {reason_text}",
        )
        await self.bot._log_event(
            interaction.guild,
            f"[mod] {user.mention} punished by {interaction.user.mention} for "
            f"**{pretty}**. Reason: {reason_text}",
        )

    async def _handle_pardon(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass
        except discord.HTTPException:
            return

        cfg = get_guild_config(self.bot.config, interaction.guild_id)
        if not cfg:
            await self._safe_followup(
                interaction,
                "This server is not configured yet. An admin should run "
                "`/setup` or edit `config.json`.",
            )
            return

        record = db_fetchone(
            "SELECT * FROM punishments WHERE guild_id = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (interaction.guild_id, user.id),
        )
        if not record:
            await self._safe_followup(
                interaction, "That user has no active punishment on record."
            )
            return

        punish_role = interaction.guild.get_role(record["punish_role_id"])
        post_role = interaction.guild.get_role(record["post_role_id"])
        normal_role = (
            interaction.guild.get_role(record["normal_role_id"])
            or interaction.guild.get_role(cfg["normal_role_id"])
        )

        try:
            if punish_role and punish_role in user.roles:
                await user.remove_roles(
                    punish_role, reason=f"Pardoned by {interaction.user}"
                )
            if post_role and post_role in user.roles:
                await user.remove_roles(
                    post_role, reason=f"Pardoned by {interaction.user}"
                )
            if normal_role and normal_role not in user.roles:
                await user.add_roles(
                    normal_role, reason=f"Pardoned by {interaction.user}"
                )
        except discord.Forbidden:
            await self._safe_followup(
                interaction, "I lack permission to change that user's roles."
            )
            return

        db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
        archive_punishment(dict(record), ended_reason="pardoned")
        await self._safe_followup(
            interaction,
            f"Pardoned {user.mention}. Their normal role has been restored.",
        )
        await self.bot._log_event(
            interaction.guild,
            f"[mod] {user.mention} pardoned by {interaction.user.mention}.",
        )

    async def _handle_setup(
        self,
        interaction: discord.Interaction,
        normal_role: discord.Role,
        punish_role: discord.Role,
        post_role: discord.Role,
        log_channel: Optional[discord.TextChannel],
    ) -> None:
        if len({normal_role.id, punish_role.id, post_role.id}) != 3:
            await interaction.response.send_message(
                "The three roles must all be different.", ephemeral=True
            )
            return
        self.bot.config.setdefault("guilds", {})[str(interaction.guild_id)] = {
            "normal_role_id": normal_role.id,
            "punish_role_id": punish_role.id,
            "post_role_id": post_role.id,
        }
        if log_channel is not None:
            self.bot.config["log_channel_id"] = log_channel.id
        save_config(self.bot.config)
        lines = [
            "Saved configuration for this server:",
            f"- Normal role: {normal_role.mention}",
            f"- Punish role: {punish_role.mention}",
            f"- Post-punish role: {post_role.mention}",
        ]
        if log_channel is not None:
            lines.append(f"- Log channel: {log_channel.mention}")
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    async def _handle_status(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
    ) -> None:
        cfg = get_guild_config(self.bot.config, interaction.guild_id)
        if not cfg:
            await interaction.response.send_message(
                "This server is not configured yet. An admin should run "
                "`/setup` or edit `config.json`.",
                ephemeral=True,
            )
            return
        normal = interaction.guild.get_role(cfg["normal_role_id"])
        punish = interaction.guild.get_role(cfg["punish_role_id"])
        post = interaction.guild.get_role(cfg["post_role_id"])

        if user is not None:
            await self._status_for_user(interaction, cfg, user)
            return

        # Server-wide summary.
        active = db_fetchall(
            "SELECT user_id, expires_at FROM punishments WHERE guild_id = ? "
            "ORDER BY expires_at",
            (interaction.guild_id,),
        )
        lines = [
            "**Server configuration**",
            f"- Normal role: {normal.mention if normal else '(missing)'}",
            f"- Punish role: {punish.mention if punish else '(missing)'}",
            f"- Post-punish role: {post.mention if post else '(missing)'}",
            "",
            f"**Active punishments:** {len(active)}",
        ]
        for row in active[:10]:
            member = interaction.guild.get_member(row["user_id"])
            exp = datetime.fromisoformat(row["expires_at"])
            delta = exp - datetime.now(timezone.utc)
            who = member.mention if member else f"<@{row['user_id']}>"
            lines.append(
                f"- {who} - ends in "
                f"{format_duration(max(0, int(delta.total_seconds())))}"
            )
        if len(active) > 10:
            lines.append(f"... and {len(active) - 10} more.")
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    async def _status_for_user(
        self,
        interaction: discord.Interaction,
        cfg: dict,
        user: discord.Member,
    ) -> None:
        """Show a single user's active + historical punishment state."""
        # Currently active?
        active = db_fetchone(
            "SELECT * FROM punishments WHERE guild_id = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (interaction.guild_id, user.id),
        )
        # Past punishments: try the history table first; if it doesn't exist
        # (old DBs / pre-migration), gracefully fall back to nothing.
        history_rows: list[sqlite3.Row] = []
        try:
            history_rows = db_fetchall(
                "SELECT reason, started_at, duration_seconds, moderator_id, "
                "       ended_reason "
                "FROM punishment_history "
                "WHERE guild_id = ? AND user_id = ? "
                "ORDER BY started_at DESC LIMIT 10",
                (interaction.guild_id, user.id),
            )
        except sqlite3.OperationalError:
            # Table doesn't exist yet. That's fine; just show no history.
            history_rows = []

        lines = [f"**Punishment status for {user.mention}**", ""]

        if active is not None:
            exp = datetime.fromisoformat(active["expires_at"])
            now = datetime.now(timezone.utc)
            remaining = int((exp - now).total_seconds())
            remaining_text = (
                format_duration(max(0, remaining)) if remaining > 0
                else "ending now"
            )
            mod_id = active["moderator_id"]
            lines.extend([
                "**Currently active:**",
                f"- Stage: `{active['stage']}`",
                f"- Reason: {active['reason'] or 'No reason provided.'}",
                f"- Started: <t:{int(datetime.fromisoformat(active['started_at']).timestamp())}:f>",
                f"- Ends in: {remaining_text}",
                f"- Moderator: <@{mod_id}>",
            ])
        else:
            lines.append("No active punishment.")

        lines.append("")
        lines.append(f"**Recent history:** {len(history_rows)}")
        if history_rows:
            for row in history_rows:
                started = datetime.fromisoformat(row["started_at"])
                duration_s = int(row["duration_seconds"])
                mod_id = row["moderator_id"]
                ended = row["ended_reason"] or "completed"
                lines.append(
                    f"- <t:{int(started.timestamp())}:d> "
                    f"for {format_duration(duration_s)} "
                    f"by <@{mod_id}> - ended: {ended} - "
                    f"reason: {row['reason'] or 'n/a'}"
                )
        else:
            lines.append("- (none recorded)")

        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )

    # ------------------------------------------------------------------ #
    # Robustly send a followup message; never raise.
    # ------------------------------------------------------------------ #
    async def _safe_followup(
        self, interaction: discord.Interaction, content: str
    ) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except (discord.InteractionResponded, discord.HTTPException) as exc:
            logger.warning("Failed to send interaction response: %s", exc)


# Add the cog to the bot. We do this BEFORE setup_hook runs (so the
# tree has the commands by the time it syncs).
async def _register_cog() -> None:
    await bot.add_cog(PunishmentCog(bot))


# --------------------------------------------------------------------------- #
# Slash command error handling - the part that almost always "breaks".
# --------------------------------------------------------------------------- #
# Two things matter here:
#   1) Every error path must produce SOME user-visible feedback, even if
#      interaction.response is already done. We use followup as fallback.
#   2) We must not double-respond (which raises InteractionResponded).
# --------------------------------------------------------------------------- #
@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    # Check failures: the predicate should have already sent the user-facing
    # message. If it didn't (e.g. it crashed), send a generic one.
    if isinstance(error, app_commands.CheckFailure):
        msg = "You don't have permission to run that command."
    elif isinstance(error, app_commands.CommandNotFound):
        return  # silently ignore
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"That command is on cooldown. Try again in {error.retry_after:.1f}s."
    elif isinstance(error, app_commands.MissingPermissions):
        msg = f"You're missing permissions: {', '.join(error.missing_permissions)}."
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = (
            f"I'm missing permissions: {', '.join(error.missing_permissions)}. "
            "Check the bot's role position and channel overrides."
        )
    elif isinstance(error, app_commands.TransformerError):
        msg = f"Invalid value for `{error.param.name}`."
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "This command can only be used in a server."
    else:
        logger.exception("Unhandled slash command error: %s", error)
        msg = "Something went wrong while running that command."

    # Send via followup if already responded, otherwise via response.
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except (discord.InteractionResponded, discord.HTTPException) as exc:
        logger.warning("Failed to deliver error message: %s", exc)


# --------------------------------------------------------------------------- #
# Override setup_hook to register the cog before syncing.
# --------------------------------------------------------------------------- #
_original_setup_hook = PunishmentBot.setup_hook


async def setup_hook(self: PunishmentBot) -> None:
    await _register_cog()
    await _original_setup_hook(self)


PunishmentBot.setup_hook = setup_hook  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def resolve_token(cfg: dict) -> Optional[str]:
    return os.environ.get("DISCORD_TOKEN") or cfg.get("token") or None


def main() -> int:
    init_db()
    token = resolve_token(bot.config)
    if not token:
        logger.error(
            "No Discord token found. Set the DISCORD_TOKEN environment variable "
            "or put it in config.json under 'token'."
        )
        return 1
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.error("Login failed - is the token valid?")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
