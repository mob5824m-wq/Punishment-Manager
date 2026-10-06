"""Tickets for Sentinel: panel buttons, private threads or private channels.

Members open a ticket by pressing a button on a *panel* an administrator
published. Staff get notified, can **claim** the ticket, and close it; the
whole conversation stays between the opener and staff.

Two shapes, chosen per server (``mode``) and overridable per category
(``category["mode"]``) — "both" is a supported configuration, not a fallback:

``thread`` (the default)
    A **private thread** under the panel's channel. Private threads are
    invite-only, so the opener is added explicitly, and so is everyone holding
    the category's staff role (up to :const:`MAX_THREAD_STAFF`). Staff who were
    not added can still join: the notice posted in the log channel carries a
    *Claim* button that adds them to the thread.

``channel``
    A private text channel, created under the configured ticket category (or
    the panel channel's category), with ``@everyone`` denied and the opener,
    the staff role and the bot allowed. Discord's channel permission
    overwrites mean a role can be granted access without listing members one
    by one, which is why this mode is the better choice when a server has more
    staff than fit in a private thread.

Normal members can open a ticket and then *only* see their own ticket. They
cannot list, search, view or edit anybody else's, cannot claim, and cannot
reopen a closed one; there is no slash command or button that exposes a ticket
list to a non-staff member. Every management command lives under
``/manage tickets`` (hidden from members by the group's
``default_member_permissions``) and re-checks permission when it runs, and the
dashboard — which can list and act on every ticket — requires the dashboard
token.

Configuration lives per guild in ``config["tickets"]``::

    "tickets": {
        "123456789012345678": {
            "mode": "thread",             # default for new categories
            "log_channel_id": 444,        # staff notification / claim buttons
            "category_id": null,          # Discord category for channel tickets
            "number": 7,                  # last used ticket number
            "categories": [
                {
                    "category_id": "6f1c0b3a",
                    "label": "General help",
                    "emoji": "❓",
                    "staff_role_id": 333,
                    "description": "Questions about the server.",
                    "mode": null,             # null = inherit the default
                    "ask_subject": True,      # ask "what is this about?" first
                    "ping_staff": True        # mention the staff role on open
                }
            ],
            "panel": {"channel_id": 222, "message_id": 333,
                      "title": null, "description": null}
        }
    }

The rows themselves live in SQLite (``tickets``), because threads and channels
can be renamed, archived or deleted by hand while the record of who opened
what, who claimed it and how it ended has to survive.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

import store
from command_tree import is_administrator, manage_group
from settings import (
    get_staff_channel_id,
    get_staff_role_id,
    save_config,
    should_dm_user,
)

logger = logging.getLogger("sentinel.tickets")

TICKETS_KEY = "tickets"

#: How new tickets are created. ``thread`` = private thread under the panel
#: channel, ``channel`` = its own private text channel.
MODE_THREAD = "thread"
MODE_CHANNEL = "channel"
MODES = (MODE_THREAD, MODE_CHANNEL)
DEFAULT_MODE = MODE_THREAD

STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_CLOSED = "closed"
STATUSES = (STATUS_OPEN, STATUS_CLAIMED, STATUS_CLOSED)

# Discord limits: 5 buttons per action row and 5 rows on a message. The panel
# uses one button per category, so that is the hard ceiling.
MAX_CATEGORIES = 25
BUTTONS_PER_ROW = 5
#: Button/select labels are capped at 80 characters by Discord.
MAX_LABEL_LENGTH = 80
MAX_EMOJI_LENGTH = 100
MAX_PANEL_TITLE_LENGTH = 256
MAX_PANEL_DESCRIPTION_LENGTH = 4096
#: A modal has at most 5 inputs, and the panel's "what is this about?" prompt is
#: a single one of them.
MAX_SUBJECT_LENGTH = 200
#: Private threads cap their member list. Adding staff by role is a
#: convenience, so it is bounded: anyone beyond this joins by claiming.
MAX_THREAD_STAFF = 20
MAX_CLOSE_REASON_LENGTH = 400
MAX_LIST_ROWS = 25

DEFAULT_PANEL_TITLE = "Support tickets"
DEFAULT_PANEL_DESCRIPTION = (
    "Press a button below to open a private ticket. Only you and the staff "
    "team can see it."
)
DEFAULT_SUBJECT_QUESTION = "What is this about?"

# --------------------------------------------------------------------------- #
# Component routing
# --------------------------------------------------------------------------- #
# Buttons and modals published into Discord must keep working across restarts,
# with no state in memory, so every component carries its own intent in its
# custom_id and is handled centrally (see route_ticket_interaction). A
# registered dynamic View would need re-registering after every config change;
# a custom_id prefix needs nothing.
CUSTOM_ID_PREFIX = "sentinel:tk:"

ACTION_OPEN = "open"          # sentinel:tk:open:<category_id>      (panel button)
ACTION_CREATE = "create"      # sentinel:tk:create:<category_id>    (modal submit)
ACTION_CLAIM = "claim"        # sentinel:tk:claim:<ticket_id>       (button)
ACTION_CLOSE = "close"        # sentinel:tk:close:<ticket_id>       (button)
ACTION_CLOSED = "closed"      # sentinel:tk:closed:<ticket_id>      (modal submit)
ACTION_REOPEN = "reopen"      # sentinel:tk:reopen:<ticket_id>      (button)


class TicketError(Exception):
    """A ticket action that cannot proceed, with a member-facing reason."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def new_category_id() -> str:
    """Return a short unique id for a panel category (like ruleset ids)."""
    return secrets.token_hex(4)


def normalize_mode(value: object) -> Optional[str]:
    """Read a stored mode, or ``None`` when it is missing/unknown.

    An unknown value falls back to the server default rather than failing: a
    hand-edited config should not stop tickets from working.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned if cleaned in MODES else None


def effective_mode(settings: dict, category: Optional[dict] = None) -> str:
    """The mode a ticket opened for ``category`` will use."""
    if category is not None:
        category_mode = normalize_mode(category.get("mode"))
        if category_mode is not None:
            return category_mode
    return normalize_mode(settings.get("mode")) or DEFAULT_MODE


def _as_int(value: object) -> Optional[int]:
    """Best-effort int for ids that may arrive as str, int or None."""
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _truthy(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def slugify(text: str, *, fallback: str = "ticket", limit: int = 80) -> str:
    """A channel/thread-name-safe version of ``text``.

    Discord lowercases channel names, forbids spaces and a handful of
    punctuation characters, and caps names at 100 characters; this keeps a
    healthy margin.
    """
    cleaned = re.sub(r"[^a-z0-9-_]+", "-", (text or "").strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return (cleaned[:limit] or fallback)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment is not None else None


def _format_timestamp(value: object) -> str:
    """Render a stored ISO timestamp as a Discord timestamp (or ``—``)."""
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return f"<t:{int(moment.timestamp())}:R>"


def actor_id(actor: object) -> Optional[int]:
    """The id of whoever acted, from a Member or a dashboard actor."""
    return _as_int(getattr(actor, "id", None))


def actor_name(actor: object) -> str:
    """A readable name for whoever acted, whatever kind of object it is."""
    for attribute in ("display_name", "global_name", "name"):
        value = getattr(actor, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    return f"<@{actor_id(actor)}>" if actor_id(actor) else "unknown"


def actor_mention(actor: object) -> str:
    mention = getattr(actor, "mention", None)
    if isinstance(mention, str) and mention:
        return mention
    return f"<@{actor_id(actor)}>" if actor_id(actor) else actor_name(actor)


def custom_id(action: str, argument: object = None) -> str:
    """Build a component custom_id this module will recognise."""
    if argument is None:
        return f"{CUSTOM_ID_PREFIX}{action}"
    return f"{CUSTOM_ID_PREFIX}{action}:{argument}"


def parse_custom_id(value: object) -> tuple[Optional[str], Optional[str]]:
    """Split a custom_id into ``(action, argument)``, or ``(None, None)``.

    ``None`` means "not ours" — the caller must then leave the interaction
    alone for whatever else handles it.
    """
    if not isinstance(value, str) or not value.startswith(CUSTOM_ID_PREFIX):
        return None, None
    remainder = value[len(CUSTOM_ID_PREFIX):]
    action, _, argument = remainder.partition(":")
    return action, argument or None


# --------------------------------------------------------------------------- #
# Reading and writing the per-guild configuration
# --------------------------------------------------------------------------- #
def _guild_settings(config: dict, guild_id: int) -> dict:
    stored = config.get(TICKETS_KEY, {})
    if isinstance(stored, dict):
        entry = stored.get(str(int(guild_id)))
        if isinstance(entry, dict):
            return entry
    return {}


def _replace_guild_settings(config: dict, guild_id: int, settings: dict) -> dict:
    """Store ``settings`` for a guild and return the same dict."""
    stored = config.get(TICKETS_KEY, {})
    updated = dict(stored) if isinstance(stored, dict) else {}
    updated[str(int(guild_id))] = settings
    config[TICKETS_KEY] = updated
    return settings


def normalize_category(raw: object) -> Optional[dict]:
    """A stored category in its canonical shape (``None`` if unusable)."""
    if not isinstance(raw, dict):
        return None
    label = str(raw.get("label") or "").strip()
    category_id = str(raw.get("category_id") or "").strip()
    if not label or not category_id:
        return None
    emoji = raw.get("emoji")
    emoji = str(emoji).strip() if emoji else None
    if emoji and len(emoji) > MAX_EMOJI_LENGTH:
        emoji = None
    description = raw.get("description")
    description = str(description).strip() if description else None
    return {
        "category_id": category_id,
        "label": label[:MAX_LABEL_LENGTH],
        "emoji": emoji,
        "staff_role_id": _as_int(raw.get("staff_role_id")),
        "description": description,
        "mode": normalize_mode(raw.get("mode")),
        "ask_subject": _truthy(raw.get("ask_subject"), True),
        "ping_staff": _truthy(raw.get("ping_staff"), True),
    }


def get_guild_tickets(config: dict, guild_id: int) -> dict:
    """Normalized ticket settings for one guild.

    Always returns a usable dict, so callers never branch on ``None``: an
    unconfigured server simply has no categories and no panel.
    """
    entry = _guild_settings(config, guild_id)
    categories = [
        category
        for category in (
            normalize_category(raw) for raw in entry.get("categories", [])
        )
        if category is not None
    ]
    panel = entry.get("panel")
    panel = (
        {
            "channel_id": _as_int(panel.get("channel_id")),
            "message_id": _as_int(panel.get("message_id")),
            "title": (str(panel.get("title")).strip() or None)
            if panel.get("title")
            else None,
            "description": (str(panel.get("description")).strip() or None)
            if panel.get("description")
            else None,
        }
        if isinstance(panel, dict)
        else None
    )
    return {
        "mode": normalize_mode(entry.get("mode")) or DEFAULT_MODE,
        "log_channel_id": _as_int(entry.get("log_channel_id")),
        "category_id": _as_int(entry.get("category_id")),
        "number": _as_int(entry.get("number")) or 0,
        "categories": categories,
        "panel": panel,
    }


def write_guild_tickets(config: dict, guild_id: int, settings: dict) -> None:
    """Persist normalized ticket settings (panel + categories included)."""
    entry = _guild_settings(config, guild_id)
    entry.update(
        {
            "mode": normalize_mode(settings.get("mode")) or DEFAULT_MODE,
            "log_channel_id": _as_int(settings.get("log_channel_id")),
            "category_id": _as_int(settings.get("category_id")),
            "number": int(settings.get("number") or 0),
            "categories": [
                category
                for category in (
                    normalize_category(raw) for raw in settings.get("categories", [])
                )
                if category is not None
            ],
            "panel": settings.get("panel"),
        }
    )
    _replace_guild_settings(config, guild_id, entry)
    save_config(config)


def find_category(settings: dict, wanted: object) -> Optional[dict]:
    """Find a category by id, or by label (case-insensitive)."""
    if wanted is None:
        return None
    text = str(wanted).strip()
    if not text:
        return None
    for category in settings.get("categories", []):
        if category.get("category_id") == text:
            return category
    lowered = text.lower()
    for category in settings.get("categories", []):
        if str(category.get("label", "")).lower() == lowered:
            return category
    return None


def upsert_category(config: dict, guild_id: int, category: dict) -> dict:
    """Add or replace one category, keeping its panel position stable."""
    settings = get_guild_tickets(config, guild_id)
    normalized = normalize_category(category)
    if normalized is None:
        raise TicketError("A ticket category needs both a name and an id.")
    categories = settings["categories"]
    for index, existing in enumerate(categories):
        if existing["category_id"] == normalized["category_id"]:
            categories[index] = {**existing, **normalized}
            break
    else:
        if len(categories) >= MAX_CATEGORIES:
            raise TicketError(
                f"This server already has {MAX_CATEGORIES} ticket categories. "
                "Remove one before adding another."
            )
        categories.append(normalized)
    settings["categories"] = categories
    write_guild_tickets(config, guild_id, settings)
    return normalized


def remove_category(config: dict, guild_id: int, wanted: object) -> dict:
    """Remove a category, returning the one that was removed.

    Raising instead of silently succeeding keeps the slash command honest: an
    administrator who mistypes a name is told, and the panel is not republished
    over nothing.
    """
    settings = get_guild_tickets(config, guild_id)
    category = find_category(settings, wanted)
    if category is None:
        raise TicketError(
            "No ticket category matches that. Use `/manage tickets categories`."
        )
    settings["categories"] = [
        existing
        for existing in settings["categories"]
        if existing["category_id"] != category["category_id"]
    ]
    write_guild_tickets(config, guild_id, settings)
    return category


def take_next_number(config: dict, guild_id: int) -> int:
    """Reserve the next ticket number for a guild (stored in the config).

    Numbers are the human-facing handle (``#12``) used by ``/manage tickets
    view`` and the dashboard, so they never change once assigned.
    """
    settings = get_guild_tickets(config, guild_id)
    settings["number"] = int(settings.get("number") or 0) + 1
    write_guild_tickets(config, guild_id, settings)
    return settings["number"]


def find_panel_category(settings: dict, category_id: object) -> Optional[dict]:
    """Look a category up by its stored ``category_id`` only."""
    wanted = str(category_id or "").strip()
    if not wanted:
        return None
    for category in settings.get("categories", []):
        if category.get("category_id") == wanted:
            return category
    return None


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
def is_ticket_staff(
    member: object,
    config: dict,
    guild_id: int,
    category: Optional[dict] = None,
) -> bool:
    """Whether this member may see and act on every ticket in a guild.

    Staff is any of: an administrator, a member with *Manage Server* or
    *Moderate Members*, the server's configured staff role, or the role a
    category assigns to itself. The check is repeated at every entry point
    (command, button, dashboard) because a bot can be told to show a command to
    members it would refuse, and buttons outlive the configuration that
    produced them.
    """
    if member is None:
        return False
    if getattr(member, "bot", False):
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and (
        getattr(perms, "administrator", False)
        or getattr(perms, "manage_guild", False)
        or getattr(perms, "moderate_members", False)
        or getattr(perms, "manage_channels", False)
    ):
        return True

    role_ids = {
        _as_int(getattr(role, "id", None))
        for role in (getattr(member, "roles", None) or [])
    }
    role_ids.discard(None)
    staff_role = get_staff_role_id(config, guild_id)
    if staff_role is not None and staff_role in role_ids:
        return True
    if category is not None:
        category_role = _as_int(category.get("staff_role_id"))
        if category_role is not None and category_role in role_ids:
            return True
    return False


def category_staff_role(
    guild: discord.Guild, category: Optional[dict], config: dict, guild_id: int
) -> Optional[discord.Role]:
    """The role that should get access to a category's tickets.

    A category may name its own role; otherwise the server's configured staff
    role is used, which is what ``/manage setup`` already collects.
    """
    role_id = _as_int((category or {}).get("staff_role_id"))
    if role_id is None:
        role_id = get_staff_role_id(config, guild_id)
    if role_id is None:
        return None
    return guild.get_role(role_id)


# --------------------------------------------------------------------------- #
# Ticket records (SQLite)
# --------------------------------------------------------------------------- #
TICKET_COLUMNS = (
    "guild_id",
    "number",
    "user_id",
    "category_id",
    "category_label",
    "mode",
    "channel_id",
    "thread_id",
    "status",
    "claimed_by",
    "claimed_at",
    "subject",
    "log_channel_id",
    "log_message_id",
    "control_message_id",
    "created_at",
    "closed_at",
    "closed_by",
    "close_reason",
)

#: Columns the dashboard needs in a payload. Written out once so the JSON API
#: and the embeds cannot drift.
_PUBLIC_FIELDS = (
    "id",
    "guild_id",
    "number",
    "user_id",
    "category_id",
    "category_label",
    "mode",
    "channel_id",
    "thread_id",
    "status",
    "claimed_by",
    "claimed_at",
    "subject",
    "log_channel_id",
    "log_message_id",
    "control_message_id",
    "created_at",
    "closed_at",
    "closed_by",
    "close_reason",
)


def _fresh(ticket: dict) -> dict:
    """Re-read a ticket row before changing its state.

    Callers pass the dict they were handed (a button's arguments, a command's
    lookup, a dashboard route's fetch). Re-reading here means two clicks in a
    row — claim then close, close then close — act on the current status rather
    than on a copy taken before the first one finished.
    """
    fresh = get_ticket(ticket.get("id")) if isinstance(ticket, dict) else None
    return fresh or ticket


def _row_to_ticket(row: object) -> Optional[dict]:
    if row is None:
        return None
    return {key: row[key] for key in _PUBLIC_FIELDS if key in row.keys()}


def insert_ticket(**fields: object) -> dict:
    """Create a ticket row and return it as a dict."""
    record = {key: fields.get(key) for key in TICKET_COLUMNS}
    placeholders = ", ".join("?" for _ in TICKET_COLUMNS)
    ticket_id = store.db_insert(
        f"INSERT INTO tickets ({', '.join(TICKET_COLUMNS)}) VALUES ({placeholders})",
        tuple(record[key] for key in TICKET_COLUMNS),
    )
    ticket = get_ticket(ticket_id)
    if ticket is None:  # pragma: no cover - the row was just written
        raise TicketError("Could not store the new ticket.")
    return ticket


def get_ticket(ticket_id: object) -> Optional[dict]:
    """One ticket by its database id."""
    numeric = _as_int(ticket_id)
    if numeric is None:
        return None
    return _row_to_ticket(
        store.db_fetchone("SELECT * FROM tickets WHERE id = ?", (numeric,))
    )


def get_ticket_by_thread(guild_id: int, thread_id: object) -> Optional[dict]:
    """The ticket a channel/thread belongs to (used by in-channel buttons)."""
    numeric = _as_int(thread_id)
    if numeric is None:
        return None
    return _row_to_ticket(
        store.db_fetchone(
            "SELECT * FROM tickets WHERE guild_id = ? AND thread_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (int(guild_id), numeric),
        )
    )


def find_ticket(guild_id: int, wanted: object) -> Optional[dict]:
    """Resolve a ticket from ``12``, ``#12``, a database id, or a channel id."""
    numeric = _as_int(str(wanted).lstrip("#").strip()) if wanted is not None else None
    if numeric is None:
        return None
    row = store.db_fetchone(
        "SELECT * FROM tickets WHERE guild_id = ? AND number = ? LIMIT 1",
        (int(guild_id), numeric),
    )
    if row is not None:
        return _row_to_ticket(row)
    row = store.db_fetchone(
        "SELECT * FROM tickets WHERE guild_id = ? AND id = ? LIMIT 1",
        (int(guild_id), numeric),
    )
    if row is not None:
        return _row_to_ticket(row)
    return get_ticket_by_thread(guild_id, numeric)


def list_tickets(
    guild_id: int,
    *,
    status: Optional[str] = None,
    user_id: Optional[int] = None,
    limit: int = MAX_LIST_ROWS,
) -> list[dict]:
    """Tickets for a guild, newest first, optionally filtered.

    This is the staff-facing list: it is only ever reached from a command or
    route that already checked for staff permission.
    """
    clauses = ["guild_id = ?"]
    params: list[object] = [int(guild_id)]
    if status == "open":
        clauses.append("status IN ('open', 'claimed')")
    elif status in {STATUS_OPEN, STATUS_CLAIMED, STATUS_CLOSED}:
        clauses.append("status = ?")
        params.append(status)
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(int(user_id))
    params.append(max(1, min(int(limit), 100)))
    rows = store.db_fetchall(
        "SELECT * FROM tickets WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT ?",
        tuple(params),
    )
    return [ticket for ticket in (_row_to_ticket(row) for row in rows) if ticket]


def open_ticket_for_user(
    guild_id: int, user_id: int, category_id: str
) -> Optional[dict]:
    """An already-open ticket by this member in this category, if any.

    A member with an open ticket is told where it is instead of silently
    getting a second one; duplicate threads for the same question are the most
    common support-bot annoyance.
    """
    row = store.db_fetchone(
        "SELECT * FROM tickets WHERE guild_id = ? AND user_id = ? "
        "AND category_id = ? AND status IN ('open', 'claimed') "
        "ORDER BY id DESC LIMIT 1",
        (int(guild_id), int(user_id), str(category_id)),
    )
    return _row_to_ticket(row)


def update_ticket(ticket_id: object, **fields: object) -> Optional[dict]:
    """Update the given columns on one ticket and return the fresh row."""
    numeric = _as_int(ticket_id)
    if numeric is None:
        return None
    updates = {key: value for key, value in fields.items() if key in TICKET_COLUMNS}
    if updates:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        store.db_execute(
            f"UPDATE tickets SET {assignments} WHERE id = ?",
            tuple(updates.values()) + (numeric,),
        )
    return get_ticket(numeric)


def delete_ticket_row(ticket_id: object) -> None:
    numeric = _as_int(ticket_id)
    if numeric is not None:
        store.db_execute("DELETE FROM tickets WHERE id = ?", (numeric,))


def count_open_tickets(guild_id: int) -> int:
    row = store.db_fetchone(
        "SELECT COUNT(*) AS count FROM tickets WHERE guild_id = ? "
        "AND status IN ('open', 'claimed')",
        (int(guild_id),),
    )
    return int(row["count"]) if row else 0


# --------------------------------------------------------------------------- #
# Embeds and views
# --------------------------------------------------------------------------- #
def panel_embed(settings: dict, guild_name: str) -> discord.Embed:
    """The embed members see on the ticket panel."""
    panel = settings.get("panel") or {}
    embed = discord.Embed(
        title=panel.get("title") or DEFAULT_PANEL_TITLE,
        description=panel.get("description") or DEFAULT_PANEL_DESCRIPTION,
        color=discord.Color.from_rgb(240, 166, 60),
    )
    if settings.get("categories"):
        lines = []
        for category in settings["categories"]:
            label = category["label"]
            if category.get("description"):
                label = f"{label} — {category['description']}"
            lines.append(f"{category.get('emoji') or '•'} {label}")
        embed.add_field(name="Categories", value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text=f"{guild_name} • press a button to open a ticket")
    return embed


def panel_view(settings: dict) -> discord.ui.View:
    """One button per category, five to a row.

    The buttons carry ``sentinel:tk:open:<category_id>`` so a click is resolved
    against the *current* configuration, even if the panel message itself is
    old.
    """
    view = discord.ui.View(timeout=None)
    for index, category in enumerate(settings.get("categories", [])):
        button = discord.ui.Button(
            label=category["label"][:MAX_LABEL_LENGTH],
            custom_id=custom_id(ACTION_OPEN, category["category_id"]),
            style=discord.ButtonStyle.primary,
            row=index // BUTTONS_PER_ROW,
        )
        if category.get("emoji"):
            try:
                button.emoji = category["emoji"]
            except (TypeError, ValueError):
                # An emoji Discord rejects must not cost the whole panel.
                logger.info("Ignoring unusable emoji %r", category["emoji"])
        view.add_item(button)
    return view


def ticket_embed(guild: discord.Guild, ticket: dict) -> discord.Embed:
    """The header embed posted inside a ticket."""
    status = str(ticket.get("status") or STATUS_OPEN)
    color = {
        STATUS_OPEN: discord.Color.from_rgb(240, 166, 60),
        STATUS_CLAIMED: discord.Color.from_rgb(88, 165, 255),
        STATUS_CLOSED: discord.Color.from_rgb(120, 120, 128),
    }.get(status, discord.Color.from_rgb(240, 166, 60))
    embed = discord.Embed(
        title=f"Ticket #{int(ticket.get('number') or 0):04d} — {ticket.get('category_label')}",
        description=ticket.get("subject") or "No subject given.",
        color=color,
        timestamp=utc_now(),
    )
    embed.add_field(name="Opened by", value=f"<@{ticket.get('user_id')}>", inline=True)
    embed.add_field(name="Category", value=str(ticket.get("category_label")), inline=True)
    embed.add_field(name="Mode", value=str(ticket.get("mode")), inline=True)
    embed.add_field(name="Status", value=status.capitalize(), inline=True)
    if ticket.get("claimed_by"):
        embed.add_field(
            name="Claimed by",
            value=f"<@{ticket['claimed_by']}> · {_format_timestamp(ticket.get('claimed_at'))}",
            inline=True,
        )
    if status == STATUS_CLOSED:
        embed.add_field(
            name="Closed by",
            value=(
                f"<@{ticket.get('closed_by')}> · "
                f"{_format_timestamp(ticket.get('closed_at'))}"
                if ticket.get("closed_by")
                else "—"
            ),
            inline=True,
        )
        embed.add_field(
            name="Reason",
            value=str(ticket.get("close_reason") or "No reason given.")[:1024],
            inline=False,
        )
    return embed


def ticket_control_view(ticket: dict) -> discord.ui.View:
    """The Claim / Close (or Reopen) buttons inside a ticket.

    Both are also rendered for the opener, who would be refused on *Claim* and
    *Reopen*: a message's components are the same for everyone who can read it,
    and an explanatory refusal beats a button that silently does nothing.
    """
    view = discord.ui.View(timeout=None)
    status = str(ticket.get("status") or STATUS_OPEN)
    if status != STATUS_CLOSED:
        view.add_item(
            discord.ui.Button(
                label="Claim" if status == STATUS_OPEN else "Claimed",
                emoji="🙋",
                custom_id=custom_id(ACTION_CLAIM, ticket["id"]),
                style=discord.ButtonStyle.success,
                disabled=status == STATUS_CLAIMED,
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Close",
                emoji="🔒",
                custom_id=custom_id(ACTION_CLOSE, ticket["id"]),
                style=discord.ButtonStyle.danger,
            )
        )
    else:
        view.add_item(
            discord.ui.Button(
                label="Reopen",
                emoji="🔓",
                custom_id=custom_id(ACTION_REOPEN, ticket["id"]),
                style=discord.ButtonStyle.primary,
            )
        )
    return view


def log_view(ticket: dict) -> discord.ui.View:
    """Buttons attached to the staff-channel notice.

    The notice is how a staff member who is *not* in a private thread can act:
    claiming through it adds them to the thread.
    """
    view = discord.ui.View(timeout=None)
    status = str(ticket.get("status") or STATUS_OPEN)
    if status != STATUS_CLOSED:
        view.add_item(
            discord.ui.Button(
                label="Claim",
                emoji="🙋",
                custom_id=custom_id(ACTION_CLAIM, ticket["id"]),
                style=discord.ButtonStyle.success,
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Close",
                emoji="🔒",
                custom_id=custom_id(ACTION_CLOSE, ticket["id"]),
                style=discord.ButtonStyle.danger,
            )
        )
    return view


def log_embed(guild: discord.Guild, ticket: dict) -> discord.Embed:
    """The staff-channel notice for a ticket."""
    embed = discord.Embed(
        title=f"Ticket #{int(ticket.get('number') or 0):04d} opened",
        description=str(ticket.get("subject") or "No subject given.")[:4096],
        color=discord.Color.from_rgb(240, 166, 60),
        timestamp=utc_now(),
    )
    embed.add_field(name="Member", value=f"<@{ticket.get('user_id')}>", inline=True)
    embed.add_field(name="Category", value=str(ticket.get("category_label")), inline=True)
    embed.add_field(name="Ticket", value=_ticket_mention(guild, ticket), inline=True)
    embed.add_field(
        name="Status",
        value=str(ticket.get("status") or STATUS_OPEN).capitalize(),
        inline=True,
    )
    if ticket.get("claimed_by"):
        embed.add_field(name="Claimed by", value=f"<@{ticket['claimed_by']}>", inline=True)
    return embed


def _ticket_mention(guild: discord.Guild, ticket: dict) -> str:
    """A link/mention for the ticket, falling back to its number."""
    thread_id = _as_int(ticket.get("thread_id"))
    if thread_id is not None:
        channel = guild.get_channel(thread_id)
        if channel is not None and hasattr(channel, "mention"):
            return channel.mention
        return f"<#{thread_id}>"
    return f"#{int(ticket.get('number') or 0):04d}"


def ticket_location(guild: discord.Guild, ticket: dict) -> str:
    """A readable handle for a ticket: its channel name, else its number."""
    thread_id = _as_int(ticket.get("thread_id"))
    if thread_id is not None:
        channel = guild.get_channel(thread_id)
        name = getattr(channel, "name", None)
        if name:
            return f"#{name}"
    return f"#{int(ticket.get('number') or 0):04d}"


def ticket_jump_url(guild: discord.Guild, ticket: dict) -> Optional[str]:
    thread_id = _as_int(ticket.get("thread_id"))
    if thread_id is None:
        return None
    channel = guild.get_channel(thread_id)
    jump = getattr(channel, "jump_url", None)
    return jump if isinstance(jump, str) else None


def close_modal(ticket: dict) -> discord.ui.Modal:
    """The "why are you closing this?" modal.

    Asking for a reason is what makes the ticket log useful later; it is
    optional so closing never gets stuck on an empty box.
    """
    modal = discord.ui.Modal(
        title=f"Close ticket #{int(ticket.get('number') or 0):04d}",
        custom_id=custom_id(ACTION_CLOSED, ticket["id"]),
    )
    modal.add_item(
        discord.ui.TextInput(
            label="Reason (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_CLOSE_REASON_LENGTH,
            placeholder="Resolved, duplicate, no response…",
            custom_id="reason",
        )
    )
    return modal


def subject_modal(category: dict) -> discord.ui.Modal:
    """The "what is this about?" prompt shown before a ticket is created."""
    modal = discord.ui.Modal(
        title=f"New ticket — {category['label']}"[:45],
        custom_id=custom_id(ACTION_CREATE, category["category_id"]),
    )
    modal.add_item(
        discord.ui.TextInput(
            label=DEFAULT_SUBJECT_QUESTION,
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_SUBJECT_LENGTH,
            placeholder=category.get("description") or "A short summary",
            custom_id="subject",
        )
    )
    return modal


# --------------------------------------------------------------------------- #
# Publishing the panel
# --------------------------------------------------------------------------- #
async def publish_panel(
    bot: commands.Bot,
    guild: discord.Guild,
    channel: discord.TextChannel,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Post (or refresh) the ticket panel and remember where it is.

    Republishing edits the existing message when it is still there, so a server
    never accumulates half a dozen stale panels; the message is replaced only
    when the stored one is gone or lives in another channel.
    """
    settings = get_guild_tickets(bot.config, guild.id)
    if not settings["categories"]:
        raise TicketError(
            "Add at least one ticket category first "
            "(/manage tickets add-category)."
        )

    if title is not None and len(title) > MAX_PANEL_TITLE_LENGTH:
        raise TicketError(f"The panel title must be {MAX_PANEL_TITLE_LENGTH} characters or fewer.")
    if description is not None and len(description) > MAX_PANEL_DESCRIPTION_LENGTH:
        raise TicketError(
            f"The panel description must be {MAX_PANEL_DESCRIPTION_LENGTH} characters or fewer."
        )

    previous = settings.get("panel") or {}
    if title is not None:
        previous["title"] = title.strip() or None
    if description is not None:
        previous["description"] = description.strip() or None
    settings["panel"] = previous

    embed = panel_embed(settings, guild.name)
    view = panel_view(settings)

    message = None
    if previous.get("message_id") and previous.get("channel_id"):
        old_channel = guild.get_channel(int(previous["channel_id"]))
        if old_channel is not None and hasattr(old_channel, "fetch_message"):
            try:
                message = await old_channel.fetch_message(int(previous["message_id"]))
            except discord.HTTPException:
                message = None

    if message is not None and message.channel.id == channel.id:
        await message.edit(embed=embed, view=view)
        settings["panel"] = {
            **previous,
            "channel_id": channel.id,
            "message_id": message.id,
        }
    else:
        sent = await channel.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        settings["panel"] = {
            **previous,
            "channel_id": channel.id,
            "message_id": sent.id,
        }
        # Only remove the old panel if it was really the old panel: a hand-made
        # bot message that happens to be stored there is deleted too, which is
        # the point (it is a stale panel), but never a member's message.
        if message is not None and bot.user is not None and message.author.id == bot.user.id:
            try:
                await message.delete()
            except discord.HTTPException:
                logger.info("Could not delete the replaced ticket panel %s", message.id)

    write_guild_tickets(bot.config, guild.id, settings)
    return settings["panel"]


# --------------------------------------------------------------------------- #
# Opening, claiming, closing
# --------------------------------------------------------------------------- #
def _ticket_name(number: int, opener: object, mode: str) -> str:
    if mode == MODE_THREAD:
        return f"ticket-{number:04d}-{slugify(actor_name(opener), limit=60)}"
    return f"ticket-{number:04d}"


def _channel_overwrites(
    guild: discord.Guild,
    opener: discord.abc.User,
    staff_role: Optional[discord.Role],
) -> dict:
    """Who may see a channel-mode ticket: the opener, staff, and the bot."""
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    }
    if staff_role is not None:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            manage_messages=True,
        )
    me = guild.me
    if me is not None:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            manage_channels=True,
            manage_messages=True,
            manage_permissions=True,
        )
    return overwrites


async def _create_thread_ticket(
    guild: discord.Guild,
    panel_channel: discord.TextChannel,
    opener: discord.Member,
    name: str,
    staff_role: Optional[discord.Role],
) -> discord.Thread:
    """Create the private thread and add everyone who should be in it."""
    thread = await panel_channel.create_thread(
        name=name[:100],
        type=discord.ChannelType.private_thread,
        invitable=False,
        auto_archive_duration=1440,
        reason=f"Ticket opened by {actor_name(opener)} (#{opener.id})",
    )
    await thread.add_user(opener)
    if staff_role is not None:
        added = 0
        for member in list(staff_role.members):
            if member.id == opener.id or member.bot:
                continue
            if added >= MAX_THREAD_STAFF:
                logger.info(
                    "Ticket %s: %d staff hold %s; only the first %d were added, "
                    "the rest can join with Claim.",
                    thread.id, len(staff_role.members), staff_role.id, MAX_THREAD_STAFF,
                )
                break
            try:
                await thread.add_user(member)
            except discord.HTTPException as exc:
                logger.info("Could not add %s to ticket thread %s: %s", member.id, thread.id, exc)
                continue
            added += 1
    return thread


async def _create_channel_ticket(
    guild: discord.Guild,
    settings: dict,
    opener: discord.Member,
    name: str,
    staff_role: Optional[discord.Role],
) -> discord.TextChannel:
    """Create the private text channel for a channel-mode ticket."""
    parent = None
    if settings.get("category_id"):
        candidate = guild.get_channel(int(settings["category_id"]))
        if isinstance(candidate, discord.CategoryChannel):
            parent = candidate
    if parent is None:
        panel = settings.get("panel") or {}
        panel_channel = guild.get_channel(int(panel.get("channel_id") or 0))
        if isinstance(panel_channel, discord.TextChannel):
            parent = panel_channel.category
    return await guild.create_text_channel(
        name=name[:100],
        category=parent,
        overwrites=_channel_overwrites(guild, opener, staff_role),
        topic=f"Ticket with {actor_name(opener)} (#{opener.id}) — staff only.",
        reason=f"Ticket opened by {actor_name(opener)} (#{opener.id})",
    )


async def open_ticket(
    bot: commands.Bot,
    *,
    guild: discord.Guild,
    opener: discord.Member,
    category: dict,
    subject: Optional[str] = None,
) -> dict:
    """Create a ticket for ``opener`` in ``category``.

    Returns the stored ticket. Raises :class:`TicketError` with a message that
    is safe to show the member when Discord refuses the channel/thread or the
    server has no panel yet.
    """
    settings = get_guild_tickets(bot.config, guild.id)

    existing = open_ticket_for_user(guild.id, opener.id, category["category_id"])
    if existing is not None:
        raise TicketError(
            f"You already have ticket #{int(existing['number']):04d} open in "
            f"**{existing['category_label']}**."
        )

    mode = effective_mode(settings, category)
    staff_role = category_staff_role(guild, category, bot.config, guild.id)
    number = take_next_number(bot.config, guild.id)
    name = _ticket_name(number, opener, mode)

    # Re-read the settings: take_next_number() rewrote them (with the number).
    settings = get_guild_tickets(bot.config, guild.id)
    panel = settings.get("panel") or {}
    panel_channel = guild.get_channel(int(panel.get("channel_id") or 0))

    if mode == MODE_THREAD and not isinstance(panel_channel, discord.TextChannel):
        raise TicketError(
            "This server's ticket panel is missing, so a private thread cannot "
            "be created. Ask an administrator to publish the panel again."
        )

    thread_id: Optional[int] = None
    channel_id: Optional[int] = None
    if mode == MODE_THREAD:
        try:
            thread = await _create_thread_ticket(
                guild, panel_channel, opener, name, staff_role  # type: ignore[arg-type]
            )
            thread_id = thread.id
            channel_id = panel_channel.id  # type: ignore[union-attr]
        except discord.Forbidden:
            raise TicketError(
                "I don't have permission to create private threads in the panel "
                "channel (I need *Create Private Threads* and *Send Messages in "
                "Threads*)."
            )
        except discord.HTTPException as exc:
            # A server that cannot use private threads (or a channel that does
            # not support them) falls back to a private channel rather than
            # leaving the member with nothing.
            logger.warning("Private thread ticket failed (%s); using a channel.", exc)
            mode = MODE_CHANNEL

    if mode == MODE_CHANNEL:
        try:
            channel = await _create_channel_ticket(guild, settings, opener, name, staff_role)
        except discord.Forbidden:
            raise TicketError(
                "I don't have permission to create ticket channels "
                "(I need *Manage Channels*)."
            )
        channel_id = channel.id
        thread_id = channel.id

    ticket = insert_ticket(
        guild_id=guild.id,
        number=number,
        user_id=opener.id,
        category_id=category["category_id"],
        category_label=category["label"],
        mode=mode,
        channel_id=channel_id,
        thread_id=thread_id,
        status=STATUS_OPEN,
        subject=(subject or "").strip()[:MAX_SUBJECT_LENGTH] or None,
        created_at=_stamp(utc_now()),
    )

    await _post_ticket_intro(bot, guild, ticket, category, opener)
    await _post_log_notice(bot, guild, ticket, category, opener)
    return ticket


async def _post_ticket_intro(
    bot: commands.Bot,
    guild: discord.Guild,
    ticket: dict,
    category: dict,
    opener: discord.Member,
) -> None:
    """Post the header + control buttons inside the new ticket."""
    channel = guild.get_channel(int(ticket["thread_id"]))
    if channel is None:
        return
    content = None
    if category.get("ping_staff"):
        role = category_staff_role(guild, category, bot.config, guild.id)
        if role is not None:
            content = f"{opener.mention} {role.mention}"
        else:
            content = opener.mention
    try:
        message = await channel.send(
            content=content,
            embed=ticket_embed(guild, ticket),
            view=ticket_control_view(ticket),
            allowed_mentions=discord.AllowedMentions(
                roles=True, users=True, everyone=False
            ),
        )
    except discord.HTTPException as exc:
        logger.warning("Could not post the ticket header for %s: %s", ticket["id"], exc)
        return
    update_ticket(ticket["id"], control_message_id=message.id)


async def _log_channel_for(bot: commands.Bot, guild: discord.Guild, settings: dict):
    """Where staff notices go: the ticket log, else the configured staff channel."""
    log_id = settings.get("log_channel_id") or get_staff_channel_id(bot.config, guild.id)
    if not log_id:
        return None
    channel = guild.get_channel(int(log_id))
    if channel is None:
        return None
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return None
    return channel


async def _post_log_notice(
    bot: commands.Bot,
    guild: discord.Guild,
    ticket: dict,
    category: dict,
    opener: discord.Member,
) -> None:
    """Tell staff a ticket exists, with Claim/Close buttons."""
    settings = get_guild_tickets(bot.config, guild.id)
    channel = await _log_channel_for(bot, guild, settings)
    if channel is None:
        return
    try:
        message = await channel.send(
            embed=log_embed(guild, ticket),
            view=log_view(ticket),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as exc:
        logger.info("Could not post the ticket notice for %s: %s", ticket["id"], exc)
        return
    update_ticket(ticket["id"], log_channel_id=channel.id, log_message_id=message.id)


async def _edit_message(channel, message_id, **kwargs) -> None:
    """Edit a stored message, ignoring anything Discord refuses."""
    if channel is None or not message_id:
        return
    fetch = getattr(channel, "fetch_message", None)
    if fetch is None:
        return
    try:
        message = await fetch(int(message_id))
    except discord.HTTPException:
        return
    try:
        await message.edit(**kwargs)
    except discord.HTTPException as exc:
        logger.info("Could not update message %s: %s", message_id, exc)


async def refresh_ticket_messages(bot: commands.Bot, guild: discord.Guild, ticket: dict) -> None:
    """Re-render the in-ticket header and the staff notice after a change."""
    fresh = get_ticket(ticket["id"]) or ticket
    channel = guild.get_channel(int(fresh["thread_id"] or 0))
    await _edit_message(
        channel,
        fresh.get("control_message_id"),
        embed=ticket_embed(guild, fresh),
        view=ticket_control_view(fresh),
    )
    log_channel = guild.get_channel(int(fresh.get("log_channel_id") or 0))
    await _edit_message(
        log_channel,
        fresh.get("log_message_id"),
        embed=log_embed(guild, fresh),
        view=log_view(fresh),
    )


async def _add_to_thread(
    guild: discord.Guild, ticket: dict, member: discord.Member
) -> bool:
    """Add ``member`` to a thread-mode ticket (no-op for channel mode)."""
    if str(ticket.get("mode")) != MODE_THREAD:
        return True
    thread = guild.get_channel(int(ticket["thread_id"] or 0))
    if not isinstance(thread, discord.Thread):
        return False
    try:
        await thread.add_user(member)
        return True
    except discord.HTTPException as exc:
        logger.info("Could not add %s to ticket %s: %s", member.id, ticket["id"], exc)
        return False


async def claim_ticket(
    bot: commands.Bot, guild: discord.Guild, ticket: dict, member: discord.Member
) -> dict:
    """Claim a ticket for ``member`` and put them in it."""
    ticket = _fresh(ticket)
    if str(ticket.get("status")) == STATUS_CLOSED:
        raise TicketError("That ticket is closed. Reopen it before claiming.")
    await _add_to_thread(guild, ticket, member)
    claimed = update_ticket(
        ticket["id"],
        status=STATUS_CLAIMED,
        claimed_by=member.id,
        claimed_at=_stamp(utc_now()),
    ) or ticket
    await refresh_ticket_messages(bot, guild, claimed)
    channel = guild.get_channel(int(claimed["thread_id"] or 0))
    if channel is not None:
        try:
            await channel.send(
                content=f"{member.mention} claimed this ticket.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            pass
    return claimed


async def close_ticket(
    bot: commands.Bot,
    guild: discord.Guild,
    ticket: dict,
    actor: object,
    reason: Optional[str] = None,
) -> dict:
    """Close a ticket: lock it, record who ended it and why.

    Both staff and the opener may close (a member must be able to withdraw
    their own request); everything else — claiming, reopening, listing — is
    staff-only.
    """
    ticket = _fresh(ticket)
    if str(ticket.get("status")) == STATUS_CLOSED:
        raise TicketError("That ticket is already closed.")
    closed = update_ticket(
        ticket["id"],
        status=STATUS_CLOSED,
        closed_at=_stamp(utc_now()),
        closed_by=actor_id(actor),
        close_reason=(reason or "").strip()[:MAX_CLOSE_REASON_LENGTH] or None,
    ) or ticket

    channel = guild.get_channel(int(closed["thread_id"] or 0))
    if isinstance(channel, discord.Thread):
        try:
            await channel.edit(locked=True, name=f"closed-{channel.name}"[:100])
        except discord.HTTPException as exc:
            logger.info("Could not lock ticket thread %s: %s", channel.id, exc)
    elif isinstance(channel, discord.TextChannel):
        opener = guild.get_member(int(closed["user_id"]))
        if opener is not None:
            try:
                await channel.set_permissions(
                    opener,
                    send_messages=False,
                    add_reactions=False,
                    attach_files=False,
                    reason="Ticket closed",
                )
            except discord.HTTPException as exc:
                logger.info("Could not lock ticket channel %s: %s", channel.id, exc)
        try:
            await channel.edit(name=f"closed-{channel.name}"[:100], reason="Ticket closed")
        except discord.HTTPException:
            pass

    await refresh_ticket_messages(bot, guild, closed)
    if channel is not None:
        try:
            await channel.send(
                content=(
                    f"Ticket closed by {actor_mention(actor)}."
                    + (f"\nReason: {closed['close_reason']}" if closed.get("close_reason") else "")
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass

    # The opener is told, when DMs are enabled: closing is often the only
    # signal a member gets that their request was handled.
    if should_dm_user(bot.config, guild.id) and channel is not None:
        opener = guild.get_member(int(closed["user_id"]))
        if opener is not None:
            await _dm(
                opener,
                discord.Embed(
                    title=f"Your ticket in {guild.name} was closed",
                    description=str(closed.get("close_reason") or "No reason given."),
                    color=discord.Color.from_rgb(200, 120, 120),
                    timestamp=utc_now(),
                ),
            )
    return closed


async def reopen_ticket(bot: commands.Bot, guild: discord.Guild, ticket: dict) -> dict:
    """Reopen a closed ticket (staff only — enforced by the caller)."""
    ticket = _fresh(ticket)
    if str(ticket.get("status")) != STATUS_CLOSED:
        raise TicketError("That ticket is still open.")
    reopened = update_ticket(
        ticket["id"],
        status=STATUS_OPEN,
        closed_at=None,
        closed_by=None,
        close_reason=None,
    ) or ticket
    channel = guild.get_channel(int(reopened["thread_id"] or 0))
    if isinstance(channel, discord.Thread):
        name = channel.name
        if name.startswith("closed-"):
            name = name[len("closed-"):]
        try:
            await channel.edit(locked=False, archived=False, name=name[:100])
        except discord.HTTPException as exc:
            logger.info("Could not unlock ticket thread %s: %s", channel.id, exc)
    elif isinstance(channel, discord.TextChannel):
        opener = guild.get_member(int(reopened["user_id"]))
        if opener is not None:
            try:
                await channel.set_permissions(
                    opener,
                    send_messages=True,
                    add_reactions=True,
                    attach_files=True,
                    reason="Ticket reopened",
                )
            except discord.HTTPException as exc:
                logger.info("Could not unlock ticket channel %s: %s", channel.id, exc)
        name = channel.name
        if name.startswith("closed-"):
            name = name[len("closed-"):]
            try:
                await channel.edit(name=name[:100], reason="Ticket reopened")
            except discord.HTTPException:
                pass

    await refresh_ticket_messages(bot, guild, reopened)
    if channel is not None:
        try:
            await channel.send(
                content="Ticket reopened.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass
    return reopened


async def delete_ticket(bot: commands.Bot, guild: discord.Guild, ticket: dict) -> None:
    """Delete a ticket's Discord container and its record (staff only).

    Closing keeps the history; deleting is for spam and mistakes, so the
    thread/channel really is removed.
    """
    channel = guild.get_channel(int(ticket["thread_id"] or 0))
    if channel is not None:
        try:
            await channel.delete(reason=f"Ticket #{ticket.get('number')} deleted")
        except discord.HTTPException as exc:
            logger.info("Could not delete ticket container %s: %s", channel.id, exc)
    delete_ticket_row(ticket["id"])


async def _dm(member: discord.Member, embed: discord.Embed) -> bool:
    try:
        await member.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.info("Could not DM %s: %s", member.id, exc)
        return False


# --------------------------------------------------------------------------- #
# Interaction routing
# --------------------------------------------------------------------------- #
def _modal_values(interaction: discord.Interaction) -> dict[str, str]:
    """Flatten a modal submit payload into ``{custom_id: value}``."""
    values: dict[str, str] = {}
    data = interaction.data or {}
    for row in data.get("components", []) or []:
        for component in row.get("components", []) or []:
            identifier = component.get("custom_id")
            if identifier:
                values[str(identifier)] = str(component.get("value") or "")
    return values


async def _respond(
    interaction: discord.Interaction, content: str, *, ephemeral: bool = True
) -> None:
    """Answer an interaction without ever raising (buttons must not hang)."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                content,
                ephemeral=ephemeral,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                content,
                ephemeral=ephemeral,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    except (discord.InteractionResponded, discord.HTTPException) as exc:
        logger.warning("Could not answer a ticket interaction: %s", exc)


async def _send_modal(interaction: discord.Interaction, modal: discord.ui.Modal) -> None:
    """Show a modal, or explain why it could not be shown.

    A modal is the one response type that cannot be deferred, so the failure
    path has to be a normal message: Discord rejects a modal on an interaction
    that was already acknowledged (a double click, for instance) and the member
    must not be left staring at nothing.
    """
    try:
        await interaction.response.send_modal(modal)
    except (discord.InteractionResponded, discord.HTTPException) as exc:
        logger.warning("Could not open ticket modal %s: %s", modal.custom_id, exc)
        await _respond(
            interaction, "I couldn't open that form — give it another try."
        )


def _find_mixin(bot: commands.Bot) -> Optional["TicketMixin"]:
    """The cog that owns the ticket commands and helpers.

    ``/manage tickets`` hangs off the shared ``/manage`` group, so it is mixed
    into the bot's main cog (see rules.RulesMixin for the same arrangement).
    Routing finds it by type, exactly like the dashboard does.
    """
    for cog in getattr(bot, "cogs", {}).values():
        if isinstance(cog, TicketMixin):
            return cog
    return None


async def route_ticket_interaction(
    bot: commands.Bot, interaction: discord.Interaction
) -> bool:
    """Handle one of this module's buttons or modals.

    Returns ``True`` when the interaction was ours, so the caller can skip the
    next router. Custom ids are self-describing, which is what keeps published
    panels working across restarts without registering persistent views.
    """
    action, argument = parse_custom_id((interaction.data or {}).get("custom_id"))
    if action is None:
        return False

    mixin = _find_mixin(bot)
    if mixin is None:  # pragma: no cover - the cog is always loaded
        logger.warning("Ticket interaction arrived with no TicketMixin loaded.")
        return True

    try:
        await mixin.handle_ticket_component(interaction, action, argument)
    except TicketError as exc:
        await _respond(interaction, str(exc))
    except Exception:
        logger.exception("Unhandled error handling ticket component %s", action)
        await _respond(interaction, "Something went wrong handling that ticket action.")
    return True


# --------------------------------------------------------------------------- #
# The cog: /manage tickets ... plus the component handlers above
# --------------------------------------------------------------------------- #
class TicketMixin:
    """``/manage tickets`` — the staff half of the ticket system.

    Mixed into the cog that owns the shared ``/manage`` group, like
    :class:`rules.RulesMixin`; the button/modal handlers live here too because
    they need the same helpers (and the cog is what routing can find).

    Callbacks still take ``self``, so the class is usable on its own in tests:
    ``TicketMixin(bot, save_config)``.
    """

    def __init__(self, bot: commands.Bot, save_config_fn: Callable[[dict], None]) -> None:
        self.bot = bot
        self._save_config = save_config_fn

    tickets_group = app_commands.Group(
        name="tickets",
        parent=manage_group,
        description="Ticket panels, categories and staff controls.",
    )

    # ---- Panel / settings (administrators) ----------------------------- #
    @tickets_group.command(
        name="panel",
        description="Post or refresh the ticket panel in a channel. (administrators)",
    )
    @app_commands.describe(
        channel="Channel where members press a button to open a ticket.",
        title="Optional panel title (defaults to 'Support tickets').",
        description="Optional panel text shown above the buttons.",
    )
    async def tickets_panel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> None:
        await self._defer(interaction)
        if not is_administrator(interaction):
            await self._respond(interaction, "Only server administrators can publish the ticket panel.")
            return
        guild = interaction.guild
        if guild is None or channel.guild.id != guild.id:
            await self._respond(interaction, "Choose a channel from this server.")
            return
        try:
            panel = await publish_panel(
                self.bot, guild, channel, title=title, description=description
            )
        except TicketError as exc:
            await self._respond(interaction, str(exc))
            return
        except discord.Forbidden:
            await self._respond(
                interaction,
                "I need *View Channel*, *Send Messages* and *Embed Links* in that channel.",
            )
            return
        await self._respond(
            interaction,
            f"Ticket panel published in <#{panel['channel_id']}> with "
            f"{len(get_guild_tickets(self.bot.config, guild.id)['categories'])} "
            "categor(ies).",
        )

    @tickets_group.command(
        name="mode",
        description="Choose how new tickets are created by default. (administrators)",
    )
    @app_commands.describe(mode="'thread' = private thread, 'channel' = private channel.")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Private thread (default)", value=MODE_THREAD),
            app_commands.Choice(name="Private channel", value=MODE_CHANNEL),
        ]
    )
    async def tickets_mode(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str]
    ) -> None:
        await self._defer(interaction)
        if not is_administrator(interaction):
            await self._respond(interaction, "Only server administrators can change ticket settings.")
            return
        settings = get_guild_tickets(self.bot.config, interaction.guild_id)
        settings["mode"] = mode.value
        write_guild_tickets(self.bot.config, interaction.guild_id, settings)
        await self._respond(
            interaction,
            f"New tickets will be created as **{mode.value}** unless a category "
            "overrides it. Republish the panel to update its footer.",
        )

    # ---- Categories (administrators) ----------------------------------- #
    @tickets_group.command(
        name="add-category",
        description="Add or update a ticket category on the panel. (administrators)",
    )
    @app_commands.describe(
        label="Button label, e.g. 'General help'.",
        staff_role="Role that can see this category's tickets (defaults to the staff role).",
        emoji="Optional emoji for the button.",
        mode="Optional override: private thread or private channel.",
        description="Optional text shown in the panel's category list.",
        ask_subject="Ask 'what is this about?' before opening (default: yes).",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Private thread", value=MODE_THREAD),
            app_commands.Choice(name="Private channel", value=MODE_CHANNEL),
        ]
    )
    async def tickets_add_category(
        self,
        interaction: discord.Interaction,
        label: str,
        staff_role: Optional[discord.Role] = None,
        emoji: Optional[str] = None,
        mode: Optional[app_commands.Choice[str]] = None,
        description: Optional[str] = None,
        ask_subject: Optional[bool] = None,
    ) -> None:
        await self._defer(interaction)
        if not is_administrator(interaction):
            await self._respond(interaction, "Only server administrators can change ticket categories.")
            return
        guild = interaction.guild
        cleaned = (label or "").strip()
        if not cleaned:
            await self._respond(interaction, "Give the category a name.")
            return
        if len(cleaned) > MAX_LABEL_LENGTH:
            await self._respond(
                interaction, f"Keep the label to {MAX_LABEL_LENGTH} characters or fewer."
            )
            return
        if staff_role is not None and staff_role.guild.id != guild.id:
            await self._respond(interaction, "Choose a role from this server.")
            return
        existing = find_category(get_guild_tickets(self.bot.config, guild.id), cleaned)
        category = {
            "category_id": existing["category_id"] if existing else new_category_id(),
            "label": cleaned,
            "emoji": emoji or (existing or {}).get("emoji"),
            "staff_role_id": staff_role.id
            if staff_role is not None
            else (existing or {}).get("staff_role_id"),
            "description": description
            if description is not None
            else (existing or {}).get("description"),
            "mode": mode.value if mode is not None else (existing or {}).get("mode"),
            "ask_subject": ask_subject
            if ask_subject is not None
            else (existing or {}).get("ask_subject", True),
            "ping_staff": True,
        }
        try:
            upsert_category(self.bot.config, guild.id, category)
        except TicketError as exc:
            await self._respond(interaction, str(exc))
            return
        await self._respond(
            interaction,
            f"Category **{cleaned}** "
            + ("updated" if existing else "added")
            + ". Republish the panel with `/manage tickets panel` to show it.",
        )

    @tickets_group.command(
        name="remove-category",
        description="Remove a ticket category from the panel. (administrators)",
    )
    @app_commands.describe(label="The category's name (as shown on the panel).")
    async def tickets_remove_category(
        self, interaction: discord.Interaction, label: str
    ) -> None:
        await self._defer(interaction)
        if not is_administrator(interaction):
            await self._respond(interaction, "Only server administrators can change ticket categories.")
            return
        try:
            removed = remove_category(self.bot.config, interaction.guild_id, label)
        except TicketError as exc:
            await self._respond(interaction, str(exc))
            return
        await self._respond(
            interaction,
            f"Removed **{removed['label']}**. Existing tickets in it stay where they are; "
            "republish the panel to drop the button.",
        )

    @tickets_group.command(
        name="categories",
        description="List this server's ticket categories and how they open.",
    )
    async def tickets_categories(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        if not await self._require_ticket_staff(interaction):
            return
        settings = get_guild_tickets(self.bot.config, interaction.guild_id)
        if not settings["categories"]:
            await self._respond(
                interaction,
                "No ticket categories yet. An administrator can add one with "
                "`/manage tickets add-category`.",
            )
            return
        lines = [f"**Ticket categories** (default mode: `{settings['mode']}`)"]
        for category in settings["categories"]:
            role = category.get("staff_role_id")
            lines.append(
                f"- {category.get('emoji') or '•'} **{category['label']}** · "
                f"`{effective_mode(settings, category)}` · "
                f"staff role: {f'<@&{role}>' if role else 'configured staff role'}"
            )
        await self._respond(interaction, "\n".join(lines))

    # ---- Staff controls ------------------------------------------------ #
    @tickets_group.command(
        name="list",
        description="List tickets in this server. (staff)",
    )
    @app_commands.describe(
        status="Filter: open (open + claimed), closed, or all.",
        user="Only tickets opened by this member.",
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Open and claimed", value="open"),
            app_commands.Choice(name="Closed", value=STATUS_CLOSED),
            app_commands.Choice(name="All", value="all"),
        ]
    )
    async def tickets_list(
        self,
        interaction: discord.Interaction,
        status: Optional[app_commands.Choice[str]] = None,
        user: Optional[discord.Member] = None,
    ) -> None:
        await self._defer(interaction)
        if not await self._require_ticket_staff(interaction):
            return
        wanted = status.value if status is not None else "open"
        tickets = list_tickets(
            interaction.guild_id,
            status=None if wanted == "all" else wanted,
            user_id=user.id if user is not None else None,
        )
        if not tickets:
            await self._respond(interaction, "No tickets match that filter.")
            return
        lines = [f"**Tickets** ({len(tickets)} shown)"]
        for ticket in tickets:
            claimed = f" · claimed by <@{ticket['claimed_by']}>" if ticket.get("claimed_by") else ""
            lines.append(
                f"- #{int(ticket['number']):04d} · {_ticket_mention(interaction.guild, ticket)} · "
                f"{ticket['category_label']} · <@{ticket['user_id']}> · "
                f"`{ticket['status']}`{claimed} · {_format_timestamp(ticket['created_at'])}"
            )
        await self._respond(interaction, "\n".join(lines))

    @tickets_group.command(
        name="view",
        description="Show one ticket's details. (staff)",
    )
    @app_commands.describe(ticket="Ticket number (#12), database id, or channel id.")
    async def tickets_view(self, interaction: discord.Interaction, ticket: str) -> None:
        await self._defer(interaction)
        if not await self._require_ticket_staff(interaction):
            return
        record = find_ticket(interaction.guild_id, ticket)
        if record is None:
            await self._respond(interaction, "No ticket matches that number or id.")
            return
        embed = ticket_embed(interaction.guild, record)
        embed.add_field(name="Location", value=_ticket_mention(interaction.guild, record), inline=False)
        if record.get("log_message_id"):
            embed.add_field(
                name="Staff notice",
                value=f"<#{record.get('log_channel_id')}> · message `{record['log_message_id']}`",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tickets_group.command(
        name="claim",
        description="Claim a ticket (adds you to it). (staff)",
    )
    @app_commands.describe(ticket="Ticket number or id. Defaults to this channel's ticket.")
    async def tickets_claim(
        self, interaction: discord.Interaction, ticket: Optional[str] = None
    ) -> None:
        await self._defer(interaction)
        if not await self._require_ticket_staff(interaction):
            return
        record = await self._resolve_ticket(interaction, ticket)
        if record is None:
            return
        try:
            claimed = await claim_ticket(
                self.bot, interaction.guild, record, interaction.user  # type: ignore[arg-type]
            )
        except TicketError as exc:
            await self._respond(interaction, str(exc))
            return
        await self._respond(
            interaction,
            f"You claimed ticket #{int(claimed['number']):04d}"
            + (
                f" — {claimed['category_label']}."
                if claimed.get("category_label")
                else "."
            ),
        )

    @tickets_group.command(
        name="close",
        description="Close a ticket and lock it. (staff, or the member who opened it)",
    )
    @app_commands.describe(
        ticket="Ticket number or id. Defaults to this channel's ticket.",
        reason="Optional reason stored with the ticket and shown to staff.",
    )
    async def tickets_close(
        self,
        interaction: discord.Interaction,
        ticket: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        await self._defer(interaction)
        record = await self._resolve_ticket(interaction, ticket)
        if record is None:
            return
        member = interaction.user
        staff = is_ticket_staff(member, self.bot.config, interaction.guild_id)
        if not staff and actor_id(member) != int(record["user_id"]):
            await self._respond(
                interaction,
                "Only staff can close someone else's ticket.",
            )
            return
        try:
            closed = await close_ticket(
                self.bot, interaction.guild, record, member, reason
            )
        except TicketError as exc:
            await self._respond(interaction, str(exc))
            return
        await self._respond(
            interaction, f"Ticket #{int(closed['number']):04d} closed."
        )

    @tickets_group.command(
        name="reopen",
        description="Reopen a closed ticket. (staff)",
    )
    @app_commands.describe(ticket="Ticket number or id. Defaults to this channel's ticket.")
    async def tickets_reopen(
        self, interaction: discord.Interaction, ticket: Optional[str] = None
    ) -> None:
        await self._defer(interaction)
        if not await self._require_ticket_staff(interaction):
            return
        record = await self._resolve_ticket(interaction, ticket)
        if record is None:
            return
        try:
            reopened = await reopen_ticket(self.bot, interaction.guild, record)
        except TicketError as exc:
            await self._respond(interaction, str(exc))
            return
        await self._respond(
            interaction, f"Ticket #{int(reopened['number']):04d} reopened."
        )

    # ---- Component handling -------------------------------------------- #
    async def handle_ticket_component(
        self,
        interaction: discord.Interaction,
        action: str,
        argument: Optional[str],
    ) -> None:
        """Dispatch one of this module's buttons or modal submits.

        Named after the module on purpose: the shared cog mixes in the tickets
        *and* applications mixins (they both hang off ``/manage``), so two
        ``handle_component`` methods would shadow each other in the MRO.
        """
        if interaction.guild is None:
            await _respond(interaction, "Tickets only work inside a server.")
            return
        guild = interaction.guild

        if action == ACTION_OPEN:
            await self._handle_open_button(interaction, argument)
        elif action == ACTION_CREATE:
            await self._handle_create_modal(interaction, argument)
        elif action == ACTION_CLAIM:
            await self._handle_claim_button(interaction, argument)
        elif action == ACTION_CLOSE:
            await self._handle_close_button(interaction, argument)
        elif action == ACTION_CLOSED:
            await self._handle_close_modal(interaction, argument)
        elif action == ACTION_REOPEN:
            await self._handle_reopen_button(interaction, argument)
        else:
            logger.info("Ignoring unknown ticket component action %r", action)

    def _category_from(self, guild_id: int, category_id: Optional[str]) -> dict:
        settings = get_guild_tickets(self.bot.config, guild_id)
        category = find_panel_category(settings, category_id)
        if category is None:
            raise TicketError(
                "That ticket category no longer exists — an administrator can "
                "republish the panel with `/manage tickets panel`."
            )
        return category

    async def _handle_open_button(
        self, interaction: discord.Interaction, category_id: Optional[str]
    ) -> None:
        category = self._category_from(interaction.guild_id, category_id)
        if category.get("ask_subject", True):
            await _send_modal(interaction, subject_modal(category))
            return
        await self._create_ticket(interaction, category, subject=None)

    async def _handle_create_modal(
        self, interaction: discord.Interaction, category_id: Optional[str]
    ) -> None:
        category = self._category_from(interaction.guild_id, category_id)
        subject = (_modal_values(interaction).get("subject") or "").strip() or None
        await self._create_ticket(interaction, category, subject=subject)

    async def _create_ticket(
        self,
        interaction: discord.Interaction,
        category: dict,
        subject: Optional[str],
    ) -> None:
        # Answer immediately: the channel/thread is created afterwards and takes
        # a moment, and a modal submit (or a button) must be acknowledged
        # within three seconds.
        await _respond(interaction, "Setting up your ticket…")
        try:
            ticket = await open_ticket(
                self.bot,
                guild=interaction.guild,
                opener=interaction.user,  # type: ignore[arg-type]
                category=category,
                subject=subject,
            )
        except TicketError as exc:
            await _respond(interaction, str(exc))
            return
        except discord.HTTPException:
            logger.exception("Discord refused to create a ticket.")
            await _respond(interaction, "Discord refused to create the ticket. Please tell staff.")
            return
        jump = ticket_jump_url(interaction.guild, ticket)
        await _respond(
            interaction,
            f"Ticket **#{int(ticket['number']):04d}** created"
            + (f": {jump}" if jump else ".")
            + "\nOnly you and the staff team can see it — staff will reply there.",
        )

    async def _handle_claim_button(
        self, interaction: discord.Interaction, ticket_id: Optional[str]
    ) -> None:
        record = get_ticket(ticket_id)
        if record is None or int(record["guild_id"]) != interaction.guild_id:
            await _respond(interaction, "That ticket no longer exists.")
            return
        if not is_ticket_staff(
            interaction.user,
            self.bot.config,
            interaction.guild_id,
            self._category_for_record(interaction, record),
        ):
            await _respond(interaction, "Only staff can claim tickets.")
            return
        try:
            claimed = await claim_ticket(
                self.bot, interaction.guild, record, interaction.user  # type: ignore[arg-type]
            )
        except TicketError as exc:
            await _respond(interaction, str(exc))
            return
        await _respond(
            interaction, f"Ticket #{int(claimed['number']):04d} is yours."
        )

    async def _handle_close_button(
        self, interaction: discord.Interaction, ticket_id: Optional[str]
    ) -> None:
        record = get_ticket(ticket_id)
        if record is None or int(record["guild_id"]) != interaction.guild_id:
            await _respond(interaction, "That ticket no longer exists.")
            return
        staff = is_ticket_staff(
            interaction.user,
            self.bot.config,
            interaction.guild_id,
            self._category_for_record(interaction, record),
        )
        if not staff and actor_id(interaction.user) != int(record["user_id"]):
            await _respond(interaction, "Only staff can close someone else's ticket.")
            return
        if str(record.get("status")) == STATUS_CLOSED:
            await _respond(interaction, "That ticket is already closed.")
            return
        await _send_modal(interaction, close_modal(record))

    async def _handle_close_modal(
        self, interaction: discord.Interaction, ticket_id: Optional[str]
    ) -> None:
        record = get_ticket(ticket_id)
        if record is None or int(record["guild_id"]) != interaction.guild_id:
            await _respond(interaction, "That ticket no longer exists.")
            return
        member = interaction.user
        staff = is_ticket_staff(
            member, self.bot.config, interaction.guild_id,
            self._category_for_record(interaction, record),
        )
        if not staff and actor_id(member) != int(record["user_id"]):
            await _respond(interaction, "Only staff can close someone else's ticket.")
            return
        reason = (_modal_values(interaction).get("reason") or "").strip() or None
        await _respond(interaction, "Closing the ticket…")
        try:
            closed = await close_ticket(self.bot, interaction.guild, record, member, reason)
        except TicketError as exc:
            await _respond(interaction, str(exc))
            return
        await _respond(interaction, f"Ticket #{int(closed['number']):04d} closed.")

    async def _handle_reopen_button(
        self, interaction: discord.Interaction, ticket_id: Optional[str]
    ) -> None:
        record = get_ticket(ticket_id)
        if record is None or int(record["guild_id"]) != interaction.guild_id:
            await _respond(interaction, "That ticket no longer exists.")
            return
        if not is_ticket_staff(
            interaction.user,
            self.bot.config,
            interaction.guild_id,
            self._category_for_record(interaction, record),
        ):
            await _respond(interaction, "Only staff can reopen a closed ticket.")
            return
        try:
            reopened = await reopen_ticket(self.bot, interaction.guild, record)
        except TicketError as exc:
            await _respond(interaction, str(exc))
            return
        await _respond(interaction, f"Ticket #{int(reopened['number']):04d} reopened.")

    def _category_for_record(
        self, interaction: discord.Interaction, record: dict
    ) -> Optional[dict]:
        settings = get_guild_tickets(self.bot.config, interaction.guild_id)
        return find_panel_category(settings, record.get("category_id"))

    # ---- Shared command helpers ---------------------------------------- #
    async def _require_ticket_staff(self, interaction: discord.Interaction) -> bool:
        """Refuse non-staff callers of the staff-facing ticket commands.

        The commands live under ``/manage`` (hidden from members), but a server
        can relax that per integration, and this is the check that decides what
        a caller may see — so it runs for every ticket command that lists,
        views or changes someone else's ticket. The refusal is sent here so a
        caller cannot forget to answer.
        """
        if is_ticket_staff(interaction.user, self.bot.config, interaction.guild_id):
            return True
        await _respond(
            interaction, "Only staff can list or manage tickets."
        )
        return False

    async def _resolve_ticket(
        self, interaction: discord.Interaction, ticket: Optional[str]
    ) -> Optional[dict]:
        """Find the ticket a staff command refers to (or the current channel's).

        A ticket id can come from the option or from the channel the command was
        typed in, which is what makes ``/manage tickets claim`` work without
        arguments inside a ticket. Returns ``None`` after responding when it
        cannot be resolved — the callers then simply stop.
        """
        if ticket:
            record = find_ticket(interaction.guild_id, ticket)
            if record is None:
                await _respond(interaction, "No ticket matches that number or id.")
                return None
            return record
        record = get_ticket_by_thread(
            interaction.guild_id, getattr(interaction, "channel_id", None)
        )
        if record is None:
            await _respond(
                interaction,
                "Run this inside a ticket, or pass a ticket number "
                "(see `/manage tickets list`).",
            )
            return None
        return record

    # ---- Wiring helpers shared with RulesMixin-style callbacks --------- #
    @staticmethod
    async def _defer(interaction: discord.Interaction) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except (discord.InteractionResponded, discord.HTTPException):
            pass

    @staticmethod
    async def _respond(interaction: discord.Interaction, content: str) -> None:
        await _respond(interaction, content)


__all__ = [
    "MODE_CHANNEL",
    "MODE_THREAD",
    "TicketError",
    "TicketMixin",
    "close_ticket",
    "count_open_tickets",
    "delete_ticket",
    "effective_mode",
    "find_category",
    "find_ticket",
    "get_guild_tickets",
    "get_ticket",
    "get_ticket_by_thread",
    "is_ticket_staff",
    "list_tickets",
    "open_ticket",
    "STATUSES",
    "ticket_location",
    "publish_panel",
    "reopen_ticket",
    "route_ticket_interaction",
    "upsert_category",
    "write_guild_tickets",
]
