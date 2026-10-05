"""Authenticated server-side web dashboard for Punishment Manager.

The dashboard is deliberately bound to loopback by default. Its token is a
high-privilege, bot-wide credential; remote deployments should put it behind
HTTPS and a firewall (or reach it through an SSH tunnel).
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Any, Callable, Optional

import discord
from aiohttp import web

import paths
from rules import (
    MAX_RULES_LENGTH,
    PRIVILEGED_ROLE_PERMISSIONS,
    RULES_ACCEPT_EMOJI,
    get_guild_rules,
)


logger = logging.getLogger("punishment_manager.dashboard")
SESSION_COOKIE = "pm_dashboard_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 5 * 60
MAX_REASON_LENGTH = 900


def _snowflake(value: Any) -> Optional[str]:
    """Serialise a Discord id as a string for JSON transport.

    Snowflakes are 64-bit values that exceed JavaScript's
    ``Number.MAX_SAFE_INTEGER`` (2**53 - 1), so sending one as a JSON number
    makes the browser silently round it — ``...789`` comes back as ``...800``.
    The rounded id is then sent back on the next request and matches no guild,
    member, role, or channel, which surfaced as "The bot is not connected to
    that server." even though it is. Strings survive the round trip intact.
    """
    if value is None or value == "":
        return None
    return str(value)


def ensure_dashboard_token(
    config: dict,
    save_config: Callable[[dict], None],
) -> str:
    """Return the dashboard credential, creating a private config value once.

    ``PUNISHMENT_MANAGER_DASHBOARD_TOKEN`` overrides the stored value. A
    generated token is written through the same private config writer as the
    Discord bot token, and is never printed by the dashboard server itself.
    """
    env_token = os.environ.get("PUNISHMENT_MANAGER_DASHBOARD_TOKEN", "").strip()
    configured = env_token or config.get("dashboard_token")
    if isinstance(configured, str) and configured:
        if len(configured) < 32:
            raise ValueError("dashboard_token must contain at least 32 characters")
        return configured

    token = secrets.token_urlsafe(36)
    updated = dict(config)
    updated["dashboard_token"] = token
    save_config(updated)
    config.clear()
    config.update(updated)
    return token


class _DashboardActor:
    """Audit identity used by dashboard actions where no Discord user logs in."""

    def __init__(self, user_id: int) -> None:
        self.id = int(user_id)
        self.mention = f"<@{self.id}>"


class DashboardServer:
    """aiohttp dashboard that shares the bot's event loop and domain logic."""

    def __init__(
        self,
        bot: Any,
        *,
        save_config: Callable[[dict], None],
        db_fetchall: Callable[..., list],
        db_fetchone: Callable[..., Any],
        db_execute: Callable[..., None],
        archive_punishment: Callable[..., None],
        get_guild_config: Callable[..., Optional[dict]],
        get_staff_channel_id: Callable[..., Optional[int]],
        should_dm_user: Callable[..., bool],
        is_protected_member: Callable[..., Optional[str]],
        parse_duration: Callable[[str], Optional[int]],
        format_duration: Callable[[int], str],
    ) -> None:
        self.bot = bot
        self._save_config = save_config
        self._db_fetchall = db_fetchall
        self._db_fetchone = db_fetchone
        self._db_execute = db_execute
        self._archive_punishment = archive_punishment
        self._get_guild_config = get_guild_config
        self._get_staff_channel_id = get_staff_channel_id
        self._should_dm_user = should_dm_user
        self._is_protected_member = is_protected_member
        self._parse_duration = parse_duration
        self._format_duration = format_duration
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._sessions: dict[str, dict[str, Any]] = {}
        self._login_failures: dict[str, tuple[int, float]] = {}
        self._token: Optional[str] = None
        self._host = "127.0.0.1"
        self._port = 8765
        self._secure_cookie = False

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def start(self) -> bool:
        """Start the web server if enabled. Returns True if it is listening."""
        if self.running:
            return True
        config = self.bot.config
        if not config.get("dashboard_enabled", True):
            logger.info("Web dashboard disabled by config.")
            return False

        self._host = os.environ.get(
            "PUNISHMENT_MANAGER_DASHBOARD_HOST",
            str(config.get("dashboard_host") or "127.0.0.1"),
        )
        raw_port = os.environ.get(
            "PUNISHMENT_MANAGER_DASHBOARD_PORT",
            str(config.get("dashboard_port", 8765)),
        )
        try:
            self._port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("dashboard_port must be an integer") from exc
        if not 0 < self._port < 65536:
            raise ValueError("dashboard_port must be between 1 and 65535")
        self._secure_cookie = bool(config.get("dashboard_secure_cookie", False))
        self._token = ensure_dashboard_token(config, self._save_config)

        app = self._build_app()
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
        await runner.setup()
        site = web.TCPSite(runner, host=self._host, port=self._port)
        try:
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self._runner = runner
        self._site = site
        logger.info(
            "Admin dashboard listening at http://%s:%d/.",
            self._host,
            self._port,
        )
        logger.info(
            "Dashboard login key: run 'punishment-manager --dashboard-token' on the bot host "
            "(from source: 'python3 bot.py --dashboard-token'); it is also saved as "
            "'dashboard_token' in the bot's config.json."
        )
        return True

    async def close(self) -> None:
        if self._runner is not None:
            runner, self._runner = self._runner, None
            self._site = None
            await runner.cleanup()
        self._sessions.clear()

    def _build_app(self) -> web.Application:
        server = self

        @web.middleware
        async def auth_and_security(request: web.Request, handler):
            if not server._allowed_host(request.host):
                response = web.json_response(
                    {"error": "Unrecognized Host header."}, status=400
                )
                return server._security_headers(response)
            if request.path.startswith("/api/") and request.path != "/api/login":
                session = server._session_for_request(request)
                if session is None:
                    response = web.json_response(
                        {"error": "Authentication required."}, status=401
                    )
                    return server._security_headers(response)
                if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                    csrf = request.headers.get("X-CSRF-Token", "")
                    if not hmac.compare_digest(csrf, session["csrf"]):
                        response = web.json_response(
                            {"error": "Invalid CSRF token."}, status=403
                        )
                        return server._security_headers(response)

            try:
                response = await handler(request)
            except web.HTTPException as exc:
                if request.path.startswith("/api/"):
                    response = web.json_response(
                        {"error": exc.text or exc.reason}, status=exc.status
                    )
                else:
                    response = exc
            except Exception:
                logger.exception("Unhandled dashboard request %s %s", request.method, request.path)
                if request.path.startswith("/api/"):
                    response = web.json_response(
                        {"error": "The dashboard could not complete that request."},
                        status=500,
                    )
                else:
                    response = web.Response(text="Dashboard error", status=500)
            return server._security_headers(response)

        app = web.Application(
            middlewares=[auth_and_security],
            client_max_size=64 * 1024,
        )
        app.add_routes(
            [
                web.get("/", self.index),
                web.get("/api/session", self.session_status),
                web.post("/api/login", self.login),
                web.post("/api/logout", self.logout),
                web.get("/api/overview", self.overview),
                web.get("/api/guilds/{guild_id}", self.guild_detail),
                web.get("/api/guilds/{guild_id}/members", self.search_members),
                web.post("/api/guilds/{guild_id}/settings", self.save_guild_settings),
                web.get("/api/guilds/{guild_id}/punishments", self.list_punishments),
                web.post("/api/guilds/{guild_id}/punishments", self.apply_punishment),
                web.post(
                    "/api/guilds/{guild_id}/punishments/{user_id}/pardon",
                    self.pardon_member,
                ),
                web.get("/api/guilds/{guild_id}/warnings", self.list_warnings),
                web.post("/api/guilds/{guild_id}/warnings", self.warn_member),
                web.delete(
                    "/api/guilds/{guild_id}/warnings/{user_id}",
                    self.clear_warnings,
                ),
                web.get("/api/guilds/{guild_id}/history", self.history),
                web.get("/api/guilds/{guild_id}/rules", self.rules_status),
                web.put("/api/guilds/{guild_id}/rules", self.publish_rules),
                web.delete("/api/guilds/{guild_id}/rules", self.disable_rules),
                web.post("/api/guilds/{guild_id}/sync", self.sync_commands),
            ]
        )
        return app

    @staticmethod
    def _security_headers(response: web.StreamResponse) -> web.StreamResponse:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data: https://cdn.discordapp.com; "
            "connect-src 'self'; base-uri 'none'"
        )
        if response.headers.get("Content-Type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def _allowed_host(self, raw_host: str) -> bool:
        """Reject DNS-rebinding Host headers; remote names need an explicit allowlist."""
        host = self._normalize_host(raw_host)
        configured = self.bot.config.get("dashboard_allowed_hosts", [])
        if isinstance(configured, list) and configured:
            return host in {self._normalize_host(str(item)) for item in configured}

        loopback = {"localhost", "127.0.0.1", "::1"}
        if host in loopback:
            return True
        if self._host not in {"0.0.0.0", "::", ""}:
            return host == self._normalize_host(self._host)
        return False

    @staticmethod
    def _normalize_host(raw_host: str) -> str:
        value = str(raw_host or "").strip().lower()
        if value.startswith("["):
            return (urlsplit("//" + value).hostname or "").rstrip(".")
        if value.count(":") > 1:
            return value.rstrip(".")  # bare IPv6 literal
        if ":" in value:
            value = value.split(":", 1)[0]
        return value.rstrip(".")

    def _session_for_request(self, request: web.Request) -> Optional[dict[str, Any]]:
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        now = time.time()
        if session["expires_at"] <= now:
            self._sessions.pop(session_id, None)
            return None
        session["expires_at"] = now + SESSION_TTL_SECONDS
        return session

    async def index(self, _request: web.Request) -> web.Response:
        resource = paths.resource_path("dashboard.html")
        if resource is None:
            return web.Response(text="Dashboard UI is missing.", status=500)
        try:
            html = resource.read_text(encoding="utf-8")
        except OSError:
            logger.exception("Could not read dashboard.html from %s", resource)
            return web.Response(text="Dashboard UI is unavailable.", status=500)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def login(self, request: web.Request) -> web.Response:
        peer = request.remote or "unknown"
        now = time.monotonic()
        count, reset_at = self._login_failures.get(peer, (0, now + LOGIN_WINDOW_SECONDS))
        if reset_at <= now:
            count, reset_at = 0, now + LOGIN_WINDOW_SECONDS
        if count >= MAX_LOGIN_FAILURES:
            return web.json_response(
                {"error": "Too many failed attempts. Wait a few minutes and try again."},
                status=429,
            )

        try:
            data = await request.json()
        except (ValueError, TypeError):
            data = {}
        submitted = data.get("token", "") if isinstance(data, dict) else ""
        expected = self._token or ""
        if not isinstance(submitted, str) or not expected or not hmac.compare_digest(submitted, expected):
            self._login_failures[peer] = (count + 1, reset_at)
            logger.warning("Rejected dashboard login from %s", peer)
            return web.json_response({"error": "Invalid dashboard key."}, status=401)

        self._login_failures.pop(peer, None)
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self._sessions[session_id] = {
            "csrf": csrf,
            "expires_at": time.time() + SESSION_TTL_SECONDS,
        }
        response = web.json_response({"authenticated": True, "csrfToken": csrf})
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=self._secure_cookie,
            samesite="Strict",
            path="/",
        )
        logger.info("Dashboard login accepted from %s", peer)
        return response

    async def session_status(self, request: web.Request) -> web.Response:
        session = self._session_for_request(request)
        if session is None:  # normally intercepted by the auth middleware
            raise web.HTTPUnauthorized(text="Authentication required.")
        return web.json_response({"authenticated": True, "csrfToken": session["csrf"]})

    async def logout(self, request: web.Request) -> web.Response:
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            self._sessions.pop(session_id, None)
        response = web.json_response({"authenticated": False})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    async def overview(self, _request: web.Request) -> web.Response:
        guilds = list(self.bot.guilds)
        counts = {
            int(row["guild_id"]): int(row["count"])
            for row in self._db_fetchall(
                "SELECT guild_id, COUNT(*) AS count FROM punishments GROUP BY guild_id"
            )
        }
        payload = [
            {
                "id": _snowflake(guild.id),
                "name": guild.name,
                "icon": guild.icon.url if guild.icon else None,
                "memberCount": guild.member_count,
                "activePunishments": counts.get(guild.id, 0),
            }
            for guild in guilds
        ]
        active_total = sum(counts.values())
        return web.json_response(
            {
                "bot": {
                    "ready": self.bot.is_ready(),
                    "name": str(self.bot.user) if self.bot.user else "Connecting",
                    "avatar": self.bot.user.display_avatar.url if self.bot.user else None,
                    "guildCount": len(guilds),
                    "activePunishments": active_total,
                },
                "guilds": payload,
            }
        )

    async def guild_detail(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        config = self._get_guild_config(self.bot.config, guild.id) or {}
        rules = get_guild_rules(self.bot.config, guild.id) or {}
        roles = [
            {
                "id": _snowflake(role.id),
                "name": role.name,
                "position": role.position,
                "color": role.color.value,
                "managed": role.managed,
            }
            for role in sorted(guild.roles, key=lambda item: item.position, reverse=True)
            if not role.is_default()
        ]
        channels = [
            {
                "id": _snowflake(channel.id),
                "name": channel.name,
                "category": channel.category.name if channel.category else None,
            }
            for channel in guild.text_channels
        ]
        active_count = self._db_fetchone(
            "SELECT COUNT(*) AS count FROM punishments WHERE guild_id = ?",
            (guild.id,),
        )
        warning_count = self._db_fetchone(
            "SELECT COUNT(*) AS count FROM warnings WHERE guild_id = ?",
            (guild.id,),
        )
        return web.json_response(
            {
                "guild": {
                    "id": _snowflake(guild.id),
                    "name": guild.name,
                    "icon": guild.icon.url if guild.icon else None,
                    "ownerId": _snowflake(guild.owner_id),
                    "memberCount": guild.member_count,
                },
                "settings": {
                    "punishRoleId": _snowflake(config.get("punish_role_id")),
                    "postRoleId": _snowflake(config.get("post_role_id")),
                    "staffRoleId": _snowflake(config.get("staff_role_id")),
                    "staffChannelId": _snowflake(
                        self._get_staff_channel_id(self.bot.config, guild.id)
                    ),
                    "dmUser": self._should_dm_user(self.bot.config, guild.id),
                },
                "rules": {
                    "enabled": bool(rules),
                    "channelId": _snowflake(rules.get("channel_id")),
                    "messageId": _snowflake(rules.get("message_id")),
                    "roleId": _snowflake(rules.get("role_id")),
                    "text": rules.get("rules_text", ""),
                },
                "roles": roles,
                "channels": channels,
                "activePunishments": int(active_count["count"]) if active_count else 0,
                "warningCount": int(warning_count["count"]) if warning_count else 0,
            }
        )

    async def search_members(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        query = request.query.get("q", "").strip()
        if len(query) < 2:
            raise web.HTTPBadRequest(text="Enter at least two characters to search members.")
        try:
            if query.isdigit():
                member = await self._resolve_member(guild, int(query))
                members = [member] if member is not None else []
            else:
                members = await guild.query_members(query, limit=25, cache=False)
        except discord.DiscordException as exc:
            logger.warning("Member search failed for guild %s: %s", guild.id, exc)
            members = [
                member
                for member in guild.members
                if query.casefold() in member.display_name.casefold()
                or query.casefold() in member.name.casefold()
            ][:25]
        return web.json_response(
            {
                "members": [
                    {
                        "id": _snowflake(member.id),
                        "name": member.display_name,
                        "username": str(member),
                        "avatar": member.display_avatar.url,
                    }
                    for member in members
                ]
            }
        )

    async def list_punishments(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        rows = self._db_fetchall(
            "SELECT id, guild_id, user_id, moderator_id, reason, started_at, expires_at, stage "
            "FROM punishments WHERE guild_id = ? ORDER BY expires_at",
            (guild.id,),
        )
        return web.json_response(
            {
                "punishments": [
                    _json_safe_ids(self._punishment_payload(guild, dict(row)))
                    for row in rows
                ]
            }
        )

    async def history(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        rows = self._db_fetchall(
            "SELECT id, guild_id, user_id, moderator_id, reason, started_at, "
            "duration_seconds, ended_at, ended_reason FROM punishment_history "
            "WHERE guild_id = ? ORDER BY started_at DESC LIMIT 100",
            (guild.id,),
        )
        history = []
        for row in rows:
            item = dict(row)
            member = guild.get_member(item["user_id"])
            item["memberName"] = member.display_name if member else f"User {item['user_id']}"
            item["durationLabel"] = self._format_duration(item["duration_seconds"])
            history.append(_json_safe_ids(item))
        return web.json_response({"history": history})

    async def save_guild_settings(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        data = await self._json_body(request)
        punish_role = self._role_from_body(guild, data.get("punishRoleId"), "punish role")
        post_role = self._role_from_body(guild, data.get("postRoleId"), "post-punishment role")
        staff_role = self._role_from_body(
            guild, data.get("staffRoleId"), "staff role", optional=True
        )
        if punish_role.id == post_role.id:
            raise web.HTTPBadRequest(text="Punish and post-punishment roles must be different.")
        if staff_role and staff_role.id in {punish_role.id, post_role.id}:
            raise web.HTTPBadRequest(text="The staff role must be different from punishment roles.")

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            raise web.HTTPForbidden(text="The bot needs Manage Roles in this server.")
        for role, label in ((punish_role, "Punish"), (post_role, "Post-punishment")):
            if role.managed or role >= bot_member.top_role:
                raise web.HTTPBadRequest(text=f"Move the bot role above the {label.lower()} role.")
            if any(getattr(role.permissions, name, False) for name in PRIVILEGED_ROLE_PERMISSIONS):
                raise web.HTTPBadRequest(
                    text=f"The {label.lower()} role cannot grant moderation or server-management permissions."
                )

        staff_channel_id = self._optional_int(data.get("staffChannelId"), "staff channel")
        staff_channel = guild.get_channel(staff_channel_id) if staff_channel_id else None
        if staff_channel_id and not isinstance(staff_channel, discord.TextChannel):
            raise web.HTTPBadRequest(text="Choose a text channel from this server.")
        if staff_channel is not None:
            permissions = staff_channel.permissions_for(bot_member)
            if not permissions.view_channel or not permissions.send_messages or not permissions.embed_links:
                raise web.HTTPBadRequest(
                    text="The bot needs View Channel, Send Messages, and Embed Links in the staff channel."
                )

        dm_user = data.get("dmUser")
        if not isinstance(dm_user, bool):
            raise web.HTTPBadRequest(text="dmUser must be true or false.")

        guilds = self.bot.config.get("guilds", {})
        guilds = dict(guilds) if isinstance(guilds, dict) else {}
        entry = guilds.get(str(guild.id), {})
        entry = {
            key: value
            for key, value in entry.items()
            if key != "normal_role_id"
        } if isinstance(entry, dict) else {}
        entry.update(
            {
                "punish_role_id": punish_role.id,
                "post_role_id": post_role.id,
                "staff_role_id": staff_role.id if staff_role else None,
                "staff_channel_id": staff_channel_id,
                "dm_user": dm_user,
            }
        )
        guilds[str(guild.id)] = entry
        updated = dict(self.bot.config)
        updated["guilds"] = guilds
        if self._is_configured_primary_guild(guild.id):
            # Keep installer-managed punish role fields in sync. Optional
            # staff/notification values stay per-guild so this form doesn't
            # unexpectedly alter another connected server.
            updated["punish_role_id"] = punish_role.id
            updated["post_role_id"] = post_role.id
        self._persist_config(updated)
        logger.info("Dashboard saved server settings for guild %s", guild.id)
        return web.json_response({"saved": True})

    async def apply_punishment(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        data = await self._json_body(request)
        user_id = self._required_int(data.get("userId"), "user ID")
        member = await self._resolve_member(guild, user_id)
        if member is None:
            raise web.HTTPNotFound(text="That user is not in this server.")
        reason = str(data.get("reason", "")).strip() or "No reason provided."
        if len(reason) > MAX_REASON_LENGTH:
            raise web.HTTPBadRequest(text=f"Reason must be {MAX_REASON_LENGTH} characters or fewer.")
        raw_duration = str(data.get("duration", "")).strip()
        seconds = self._parse_duration(raw_duration)
        if seconds is None or seconds < 5:
            raise web.HTTPBadRequest(text="Invalid duration. Use a value such as 30m, 2h, or 1d.")
        if seconds > 30 * 24 * 60 * 60:
            raise web.HTTPBadRequest(text="Punishment duration cannot exceed 30 days.")

        config = self._get_guild_config(self.bot.config, guild.id)
        if not config or not config.get("punish_role_id") or not config.get("post_role_id"):
            raise web.HTTPBadRequest(text="Configure this server's punishment roles first.")
        protected = self._is_protected_member(member, self.bot.config, guild=guild)
        if protected:
            raise web.HTTPForbidden(text=protected)
        duplicate = self._db_fetchone(
            "SELECT id FROM punishments WHERE guild_id = ? AND user_id = ? LIMIT 1",
            (guild.id, member.id),
        )
        if duplicate:
            raise web.HTTPConflict(text="That member already has an active punishment.")

        # A local dashboard credential is not a Discord identity. Attribute its
        # moderation record to the server owner and mark the reason as dashboard-originated.
        actor = _DashboardActor(guild.owner_id)
        full_reason = f"[Dashboard] {reason}"
        result = await self.bot.start_punishment(
            guild=guild,
            member=member,
            moderator=actor,
            duration_seconds=seconds,
            reason=full_reason,
            cfg=config,
        )
        if result is None:
            raise web.HTTPBadRequest(text="Configured punishment roles are missing from Discord.")
        if "error" in result:
            raise web.HTTPBadRequest(text=result["error"])

        started_at = result["started_at"]
        expires_at = result["expires_at"]
        staff_embed = self.bot._build_punish_staff_embed(
            member=member,
            moderator=actor,
            duration_seconds=seconds,
            reason=full_reason,
            started_at=started_at,
            expires_at=expires_at,
        )
        await self.bot._send_staff_embed(guild, staff_embed)
        if self._should_dm_user(self.bot.config, guild.id):
            dm = self.bot._build_punish_dm_embed(
                guild_name=guild.name,
                moderator_name="Punishment Manager dashboard",
                duration_seconds=seconds,
                reason=full_reason,
                started_at=started_at,
                expires_at=expires_at,
            )
            await self.bot._dm_embed(member, dm)
        logger.warning(
            "Dashboard applied punishment guild=%s user=%s peer=%s",
            guild.id,
            member.id,
            request.remote or "unknown",
        )
        return web.json_response(
            {
                "applied": True,
                "userId": _snowflake(member.id),
                "expiresAt": expires_at.isoformat(),
            },
            status=201,
        )

    async def pardon_member(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        user_id = self._required_int(request.match_info.get("user_id"), "user ID")
        member = await self._resolve_member(guild, user_id)
        if member is None:
            raise web.HTTPNotFound(text="That user is no longer in this server.")
        record = self._db_fetchone(
            "SELECT * FROM punishments WHERE guild_id = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (guild.id, member.id),
        )
        if record is None:
            raise web.HTTPNotFound(text="No active punishment was found for that member.")

        punish_role = guild.get_role(record["punish_role_id"])
        post_role = guild.get_role(record["post_role_id"])
        try:
            if punish_role and punish_role in member.roles:
                await member.remove_roles(punish_role, reason="Pardoned from the admin dashboard")
            if post_role and post_role in member.roles:
                await member.remove_roles(post_role, reason="Pardoned from the admin dashboard")
        except discord.Forbidden:
            raise web.HTTPForbidden(text="The bot cannot change that member's roles.")
        except discord.HTTPException as exc:
            logger.warning("Dashboard pardon failed in guild %s: %s", guild.id, exc)
            raise web.HTTPBadGateway(text="Discord could not update the member's roles.")

        actor = _DashboardActor(guild.owner_id)
        self._db_execute("DELETE FROM punishments WHERE id = ?", (record["id"],))
        self._archive_punishment(dict(record), ended_reason="pardoned via dashboard")
        staff_embed = self.bot._build_pardon_staff_embed(
            member=member,
            moderator=actor,
            reason="Pardoned via admin dashboard",
            was_active=True,
        )
        await self.bot._send_staff_embed(guild, staff_embed)
        if self._should_dm_user(self.bot.config, guild.id):
            dm = self.bot._build_pardon_dm_embed(
                guild_name=guild.name,
                moderator_name="Punishment Manager dashboard",
            )
            await self.bot._dm_embed(member, dm)
        logger.warning(
            "Dashboard pardoned member guild=%s user=%s peer=%s",
            guild.id,
            member.id,
            request.remote or "unknown",
        )
        return web.json_response({"pardoned": True, "userId": _snowflake(member.id)})

    # ---- Warnings ------------------------------------------------------- #
    # Warnings never change roles and never expire; these endpoints mirror
    # /punish warn and /punish warnings so both surfaces share one record.
    async def list_warnings(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        rows = self._db_fetchall(
            "SELECT id, guild_id, user_id, moderator_id, reason, created_at "
            "FROM warnings WHERE guild_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 100",
            (guild.id,),
        )
        totals: dict[int, int] = {}
        for row in self._db_fetchall(
            "SELECT user_id, COUNT(*) AS count FROM warnings "
            "WHERE guild_id = ? GROUP BY user_id",
            (guild.id,),
        ):
            totals[int(row["user_id"])] = int(row["count"])

        warnings = []
        for row in rows:
            item = dict(row)
            member = guild.get_member(item["user_id"])
            item["memberName"] = (
                member.display_name if member else f"User {item['user_id']}"
            )
            item["memberAvatar"] = member.display_avatar.url if member else None
            item["totalForMember"] = totals.get(int(item["user_id"]), 0)
            warnings.append(_json_safe_ids(item))
        return web.json_response(
            {"warnings": warnings, "total": sum(totals.values())}
        )

    async def warn_member(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        data = await self._json_body(request)
        user_id = self._required_int(data.get("userId"), "user ID")
        member = await self._resolve_member(guild, user_id)
        if member is None:
            raise web.HTTPNotFound(text="That user is not in this server.")
        reason = str(data.get("reason", "")).strip()
        if not reason:
            raise web.HTTPBadRequest(text="A reason is required for a warning.")
        if len(reason) > MAX_REASON_LENGTH:
            raise web.HTTPBadRequest(
                text=f"Reason must be {MAX_REASON_LENGTH} characters or fewer."
            )
        protected = self._is_protected_member(member, self.bot.config, guild=guild)
        if protected:
            raise web.HTTPForbidden(text=protected)

        # Same audit convention as the punishment endpoints: the dashboard has
        # no Discord identity, so the action is attributed to the owner and
        # marked as dashboard-originated in the stored reason.
        actor = _DashboardActor(guild.owner_id)
        created_at = datetime.now(timezone.utc)
        full_reason = f"[Dashboard] {reason}"
        try:
            self._db_execute(
                "INSERT INTO warnings "
                "(guild_id, user_id, moderator_id, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild.id, member.id, actor.id, full_reason, created_at.isoformat()),
            )
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.exception("Dashboard could not record a warning: %s", exc)
            raise web.HTTPInternalServerError(
                text="The warning could not be saved."
            ) from exc

        row = self._db_fetchone(
            "SELECT COUNT(*) AS count FROM warnings "
            "WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        total = int(row["count"]) if row is not None else 1

        staff_embed = self.bot._build_warn_staff_embed(
            member=member,
            moderator=actor,
            reason=full_reason,
            created_at=created_at,
            total=total,
        )
        await self.bot._send_staff_embed(guild, staff_embed)
        if self._should_dm_user(self.bot.config, guild.id):
            dm_embed = self.bot._build_warn_dm_embed(
                guild_name=guild.name,
                moderator_name="Punishment Manager dashboard",
                reason=full_reason,
                total=total,
                created_at=created_at,
            )
            await self.bot._dm_embed(member, dm_embed)
        logger.warning(
            "Dashboard warned member guild=%s user=%s peer=%s",
            guild.id,
            member.id,
            request.remote or "unknown",
        )
        return web.json_response(
            {
                "warned": True,
                "userId": _snowflake(member.id),
                "totalForMember": total,
            },
            status=201,
        )

    async def clear_warnings(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        user_id = self._required_int(request.match_info.get("user_id"), "user ID")
        member = await self._resolve_member(guild, user_id)
        if member is None:
            raise web.HTTPNotFound(text="That user is no longer in this server.")
        row = self._db_fetchone(
            "SELECT COUNT(*) AS count FROM warnings "
            "WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        removed = int(row["count"]) if row is not None else 0
        if removed == 0:
            raise web.HTTPNotFound(text="That member has no warnings on record.")
        self._db_execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        actor = _DashboardActor(guild.owner_id)
        staff_embed = self.bot._build_warn_clear_staff_embed(
            member=member,
            moderator=actor,
            removed=removed,
        )
        await self.bot._send_staff_embed(guild, staff_embed)
        logger.warning(
            "Dashboard cleared %s warning(s) guild=%s user=%s peer=%s",
            removed,
            guild.id,
            member.id,
            request.remote or "unknown",
        )
        return web.json_response(
            {"cleared": removed, "userId": _snowflake(member.id)}
        )

    async def rules_status(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        settings = get_guild_rules(self.bot.config, guild.id) or {}
        return web.json_response(
            {
                "enabled": bool(settings),
                "channelId": _snowflake(settings.get("channel_id")),
                "messageId": _snowflake(settings.get("message_id")),
                "roleId": _snowflake(settings.get("role_id")),
                "text": settings.get("rules_text", ""),
            }
        )

    async def publish_rules(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        data = await self._json_body(request)
        text = str(data.get("text", "")).strip()
        if not text:
            raise web.HTTPBadRequest(text="Rules text cannot be empty.")
        if len(text) > MAX_RULES_LENGTH:
            raise web.HTTPBadRequest(text=f"Rules are limited to {MAX_RULES_LENGTH} characters.")
        channel_id = self._required_int(data.get("channelId"), "channel ID")
        role_id = self._required_int(data.get("roleId"), "role ID")
        channel = guild.get_channel(channel_id)
        role = guild.get_role(role_id)
        if not isinstance(channel, discord.TextChannel) or role is None:
            raise web.HTTPBadRequest(text="Choose a valid text channel and role from this server.")
        rules_cog = self.bot.get_cog("RulesCog")
        if rules_cog is None:
            raise web.HTTPServiceUnavailable(text="Rules module is not loaded.")
        validation = rules_cog._validate_role(guild, channel, role)
        if validation:
            raise web.HTTPBadRequest(text=validation)

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
        try:
            message = await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            raise web.HTTPForbidden(text="The bot cannot send embeds in that channel.")
        except discord.HTTPException as exc:
            logger.warning("Dashboard rules publish failed for guild %s: %s", guild.id, exc)
            raise web.HTTPBadGateway(text="Discord could not publish the rules message.")

        old_settings = get_guild_rules(self.bot.config, guild.id)
        old_config = self.bot.config
        updated = rules_cog._updated_config(
            guild.id,
            {
                "channel_id": channel.id,
                "message_id": message.id,
                "role_id": role.id,
                "rules_text": text,
            },
        )
        self.bot.config = updated
        try:
            await message.add_reaction(RULES_ACCEPT_EMOJI)
        except discord.HTTPException as exc:
            self.bot.config = old_config
            await self._delete_message(message)
            logger.warning("Dashboard could not add rules reaction for guild %s: %s", guild.id, exc)
            raise web.HTTPBadGateway(text="The rules post was sent, but the bot could not add its reaction.")
        try:
            self._save_config(updated)
        except Exception as exc:
            self.bot.config = old_config
            await self._delete_message(message)
            logger.exception("Could not save rules settings from dashboard")
            raise web.HTTPInternalServerError(text="Rules post could not be saved to config.") from exc

        if old_settings:
            await rules_cog._retire_previous_post(guild, old_settings, message.id)
        logger.info("Dashboard published rules for guild %s", guild.id)
        return web.json_response(
            {"published": True, "messageId": _snowflake(message.id)}, status=201
        )

    async def disable_rules(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        settings = get_guild_rules(self.bot.config, guild.id)
        if settings is None:
            return web.json_response({"disabled": True, "alreadyDisabled": True})
        rules_cog = self.bot.get_cog("RulesCog")
        if rules_cog is None:
            raise web.HTTPServiceUnavailable(text="Rules module is not loaded.")
        updated = rules_cog._updated_config(guild.id, None)
        try:
            self._save_config(updated)
        except Exception as exc:
            logger.exception("Could not disable rules from dashboard")
            raise web.HTTPInternalServerError(text="Could not save the rules settings.") from exc
        self.bot.config = updated
        await rules_cog._mark_post_disabled(guild, settings)
        logger.info("Dashboard disabled rules reactions for guild %s", guild.id)
        return web.json_response({"disabled": True})

    async def sync_commands(self, request: web.Request) -> web.Response:
        guild = self._guild_from_request(request)
        try:
            await self.bot._clear_global_commands(
                reason=f"requested from dashboard for guild {guild.id}"
            )
            synced = await self.bot._sync_guild_commands(guild.id, force=True)
        except discord.DiscordException as exc:
            logger.warning("Dashboard command sync failed for guild %s: %s", guild.id, exc)
            raise web.HTTPBadGateway(text="Discord could not refresh application commands.")
        return web.json_response({"synced": bool(synced)})

    async def _delete_message(self, message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.HTTPException as exc:
            logger.info("Could not clean up dashboard rules message %s: %s", message.id, exc)

    def _persist_config(self, updated: dict) -> None:
        try:
            self._save_config(updated)
        except OSError as exc:
            logger.exception("Dashboard could not save bot config")
            raise web.HTTPInternalServerError(text="Could not save config.json.") from exc
        self.bot.config = updated

    def _guild_from_request(self, request: web.Request) -> discord.Guild:
        guild_id = self._required_int(request.match_info.get("guild_id"), "server ID")
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise web.HTTPNotFound(text="The bot is not connected to that server.")
        return guild

    async def _resolve_member(
        self,
        guild: discord.Guild,
        user_id: int,
    ) -> Optional[discord.Member]:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            logger.warning("Could not fetch member %s in guild %s: %s", user_id, guild.id, exc)
            raise web.HTTPBadGateway(text="Discord could not look up that member.")

    @staticmethod
    async def _json_body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text="Send a valid JSON request body.") from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="JSON body must be an object.")
        return data

    @staticmethod
    def _required_int(value: Any, label: str) -> int:
        parsed = DashboardServer._optional_int(value, label)
        if parsed is None:
            raise web.HTTPBadRequest(text=f"{label.capitalize()} is required.")
        return parsed

    @staticmethod
    def _optional_int(value: Any, label: str) -> Optional[int]:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            raise web.HTTPBadRequest(text=f"{label.capitalize()} must be a number.")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text=f"{label.capitalize()} must be a number.") from exc
        if parsed <= 0:
            raise web.HTTPBadRequest(text=f"{label.capitalize()} must be positive.")
        return parsed

    @staticmethod
    def _role_from_body(
        guild: discord.Guild,
        raw_id: Any,
        label: str,
        *,
        optional: bool = False,
    ) -> Optional[discord.Role]:
        role_id = DashboardServer._optional_int(raw_id, label)
        if role_id is None and optional:
            return None
        if role_id is None:
            raise web.HTTPBadRequest(text=f"{label.capitalize()} is required.")
        role = guild.get_role(role_id)
        if role is None or role.is_default():
            raise web.HTTPBadRequest(text=f"Choose a valid {label} from this server.")
        return role

    def _is_configured_primary_guild(self, guild_id: int) -> bool:
        try:
            return int(self.bot.config.get("server_id") or 0) == int(guild_id)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _punishment_payload(guild: discord.Guild, record: dict) -> dict:
        member = guild.get_member(record["user_id"])
        expires = datetime.fromisoformat(record["expires_at"])
        remaining = max(0, int((expires - datetime.now(timezone.utc)).total_seconds()))
        return {
            **record,
            "memberName": member.display_name if member else f"User {record['user_id']}",
            "memberAvatar": member.display_avatar.url if member else None,
            "remainingSeconds": remaining,
            "remainingLabel": _human_duration(remaining),
        }


_ID_FIELD_SUFFIXES = ("_id", "Id")
_ID_FIELD_EXCEPTIONS = {"id"}  # database row keys


def _json_safe_ids(payload: dict) -> dict:
    """Stringify every snowflake-looking field of a database row.

    Rows are echoed to the browser as-is, so ids such as ``user_id`` and
    ``moderator_id`` must be strings too — otherwise JavaScript rounds them
    and any id shown to a moderator (or sent back to the server) is wrong.
    """
    safe = {}
    for key, value in payload.items():
        if (
            key not in _ID_FIELD_EXCEPTIONS
            and key.endswith(_ID_FIELD_SUFFIXES)
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            safe[key] = str(value)
        else:
            safe[key] = value
    return safe


def _human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "under a minute"
