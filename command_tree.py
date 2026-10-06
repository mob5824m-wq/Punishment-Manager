"""The shared slash-command tree for Sentinel.

Everything the bot can do hangs off one top-level group, ``/manage``::

    /manage punish      apply a timed punishment role
    /manage pardon      end one early
    /manage warn        record a warning (no role change)
    /manage warnings    list / clear a member's warnings
    /manage status      server config, active punishments, member history
    /manage setup       configure this server (administrators)
    /manage fixcommands clean up duplicated slash commands (administrators)
    /manage rules …     publish rule sets and their acceptance role (admins)
    /manage tickets …   ticket panel, categories, claim/close (staff)
    /manage applications … application forms, panels and decisions (staff)
    /apply              fill in an application form (every member)

The group object lives in its own module because two places need it:

* :mod:`bot` owns the cog that Discord sees, so it owns registration of the
  group (only a command whose ``parent`` is ``None`` is registered by
  ``Cog``);
* :mod:`rules` attaches its ``/manage rules`` sub-group to it with
  ``parent=manage_group``, so the rules commands are defined next to the
  helpers they use instead of being moved into bot.py.

Sub-command callbacks are bound to whichever cog owns the group, so the rules,
tickets and applications commands are mixed into that same cog (see
:class:`rules.RulesMixin`, :class:`tickets.TicketMixin` and
:class:`applications.ApplicationsMixin`).

``/apply`` is deliberately *not* a child of this group: it is the one command a
normal member must be able to see, and all it does is open the application
modal. Reading or deciding an application always goes through the group (and
through a permission re-check), which is what keeps "members can apply but not
view or edit" true.

Discord only honours ``default_member_permissions`` on the *top-level*
command, so the group is the single permission gate: it is visible to members
with **Moderate Members**, and every administrative command re-checks for
**Administrator** when it runs.
"""

from __future__ import annotations

import discord
from discord import app_commands

__all__ = ["MANAGE_GROUP_NAME", "is_administrator", "manage_group"]


MANAGE_GROUP_NAME = "manage"

manage_group = app_commands.Group(
    name=MANAGE_GROUP_NAME,
    description="Server management: moderation, setup, rules and reaction roles.",
    # Every command targets a specific server, so never offer them in DMs.
    guild_only=True,
)


def is_administrator(interaction: discord.Interaction) -> bool:
    """Whether the caller has *Administrator* in this server.

    Sub-commands share their group's ``default_member_permissions``, and a
    server can relax that per integration, so the administrative commands
    re-check here instead of trusting what the client was told to hide.
    """
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is None:
        return False
    return bool(getattr(perms, "administrator", False))
