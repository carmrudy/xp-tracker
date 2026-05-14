"""Reusable command checks for xp-tracker."""

from __future__ import annotations

import os

import discord
from discord.ext import commands


def dm_role_name() -> str:
    return os.environ.get("XP_DM_ROLE_NAME", "DM").strip() or "DM"


def require_dm_admin():
    """
    Guild-only: invoker must have Administrator permission and the configured DM role.
    """

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        author = ctx.author
        if not isinstance(author, discord.Member):
            return False
        if not author.guild_permissions.administrator:
            raise commands.MissingPermissions(["administrator"])
        name = dm_role_name()
        if not any(r.name == name for r in author.roles):
            raise commands.CheckFailure(
                f"This command requires the **{name}** role in addition to Administrator."
            )
        return True

    return commands.check(predicate)
