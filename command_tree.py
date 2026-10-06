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
    /manage tickets panel      set the options and publish the panel (admins)
    /manage tickets category   list / add / edit / remove a panel button (admins)
    /manage tickets console    work the ticket queue: claim, close, reopen (staff)
    /manage applications form    list / create / edit / delete a form (admins)
    /manage applications panel   publish an Apply panel (admins)
    /manage applications review  read submissions and approve or deny (staff)
    /manage applications decide  decide one by id, without the queue (staff)
    /apply              fill in an application form (every member)
    /ticket             open a ticket (every member)

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

The ticket and application surfaces are deliberately *small* — three commands
each for tickets (``panel``, ``category``, ``console``) and for applications
(``form``, ``panel``, ``review`` plus ``decide`` by id). Each of them covers
list/act cases through optional options and a select, so the everyday path is
one command instead of a family of near-synonyms: the ticket console and the
application review queue are single ephemeral messages whose selects show the
records and whose buttons do the work.

``/apply`` and ``/ticket`` are deliberately *not* children of this group: they
are the commands a normal member must be able to see, and all they do is open a
modal (or a category picker). Reading or deciding anything always goes through
the group (and through a permission re-check), which is what keeps "members can
apply but not view or edit" true.

Discord only honours ``default_member_permissions`` on the *top-level*
command, and Sentinel leaves it unset (the group is visible to everyone in the
server). A role is not expressible as a permission, and hiding ``/manage``
behind *Moderate Members* would hide it from staff-role holders who lack that
permission — so each command decides for itself when it runs: moderation needs
the configured staff role (or Administrator, see
:func:`settings.moderation_denial`), ``setup``/``fixcommands``/``rules`` need
Administrator (:func:`is_administrator`), and the ticket and application
commands re-check their own staff rules.
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
