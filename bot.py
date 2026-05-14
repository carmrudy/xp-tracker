"""
Discord bot for xp-tracker: MongoDB XP; staff !addxp / !removexp / !addxpall; anyone !xp.
"""

from __future__ import annotations

import os
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

import checks
import leveling
import xp_db

load_dotenv()

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

# Max XP a single !addxp can grant (override via env).
_ADDXP_CAP = max(1, int(os.environ.get("ADDXP_MAX_AMOUNT", "100000")))

intents = discord.Intents.default()
intents.message_content = True

_ADDXP_FORMAT_HELP = (
    "`!addxp @member <amount>`\n"
    "• **@member** — someone **in this server** (ping them, e.g. `@Alex`)\n"
    "• **<amount>** — XP to add: a **whole number** ≥ **1** (e.g. `50`, `1000`)"
)

_REMOVEXP_FORMAT_HELP = (
    "`!removexp @member <amount>`\n"
    "• **@member** — someone **in this server**\n"
    "• **<amount>** — XP to remove: a **whole number** ≥ **1** "
    "(total XP **cannot go below 0**; extra amount is ignored)"
)

_ADDXPALL_FORMAT_HELP = (
    "`!addxpall <amount>`\n"
    "• **<amount>** — XP to add to **each** stored player: a **whole number** ≥ **1** "
    f"(capped at **{_ADDXP_CAP:,}** per use, same as `!addxp`)"
)


def _staff_xp_invalid_embed(
    *,
    command: str,
    format_help: str,
    what_wrong: str,
    ctx: commands.Context,
) -> discord.Embed:
    raw = (ctx.message.content or "").strip()
    if len(raw) > 1024:
        raw = raw[:1021] + "…"
    embed = discord.Embed(
        title=f"`!{command}` — message not formatted correctly",
        description="Fix the issue below and run the command again.",
        color=discord.Color.dark_red(),
    )
    embed.add_field(name="What went wrong", value=what_wrong[:1024], inline=False)
    embed.add_field(name="What you sent", value=raw or "*(empty)*", inline=False)
    embed.add_field(name="Correct format", value=format_help, inline=False)
    return embed


# Must match command_prefix passed to commands.Bot (only this prefix is pre-filtered).
_COMMAND_PREFIX = "!"


def _message_invokes_this_bot(message: discord.Message, bot: commands.Bot) -> bool:
    """
    True only if the message starts with our prefix and the first token is a real
    command on this bot — avoids parsing other bots' ``!foo`` messages.
    """
    content = (message.content or "").strip()
    if not content.startswith(_COMMAND_PREFIX):
        return False
    rest = content[len(_COMMAND_PREFIX) :].lstrip()
    if not rest:
        return False
    token = rest.split(None, 1)[0].casefold()
    allowed: set[str] = set()
    for cmd in bot.commands:
        allowed.add(cmd.name.casefold())
        allowed.update(a.casefold() for a in cmd.aliases)
    return token in allowed


class XPTrackerBot(commands.Bot):
    mongo: AsyncIOMotorClient | None
    xp_collection: AsyncIOMotorCollection | None

    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.mongo = None
        self.xp_collection = None

    async def setup_hook(self) -> None:
        uri = xp_db.mongo_uri()
        if not uri:
            raise RuntimeError(
                "Missing MONGODB_URI. Set it in .env (see .env.example)."
            )
        self.mongo = xp_db.create_mongo_client(uri)
        self.xp_collection = xp_db.xp_collection(self.mongo)
        await xp_db.ensure_xp_indexes(self.xp_collection)

    async def close(self) -> None:
        if self.mongo is not None:
            self.mongo.close()
            self.mongo = None
        await super().close()


bot = XPTrackerBot()


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (id: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if not _message_invokes_this_bot(message, bot):
        return
    await bot.process_commands(message)


@bot.command(name="addxp")
@commands.guild_only()
@checks.require_dm_admin()
async def addxp(
    ctx: commands.Context,
    member: discord.Member,
    amount: commands.Range[int, 1, None],
) -> None:
    """Grant XP to a member. Requires Administrator + DM role."""
    if bot.xp_collection is None or ctx.guild is None:
        return
    if member.bot:
        await ctx.send("Bots cannot receive XP.")
        return
    capped = min(amount, _ADDXP_CAP)
    if capped < amount:
        await ctx.send(
            f"Amount capped to **{capped}** per command (configured limit)."
        )
    p = await xp_db.grant_xp(
        bot.xp_collection,
        guild_id=ctx.guild.id,
        user_id=member.id,
        amount=capped,
    )
    max_note = " _(max level — chart stops at 20)_" if p.level >= leveling.MAX_LEVEL else ""
    await ctx.send(
        f"Added **{capped}** XP to {member.mention}.\n"
        f"**Level {p.level}** · **{p.total_xp:,}** total XP · "
        f"**{p.xp_to_next_level:,}** XP to next level{max_note}."
    )


@addxp.error
async def addxp_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("This command can only be used in a server.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Administrator** to use this command.")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send(str(error))
    elif isinstance(error, commands.MissingRequiredArgument):
        name = error.param.name
        if name == "member":
            wrong = (
                "You did not specify **who** should receive XP (missing **@member**).\n"
                "The first argument must be a member of this server."
            )
        elif name == "amount":
            wrong = (
                "You did not specify how much XP to add (missing **<amount>**).\n"
                "The second argument must be a whole number, e.g. `100`."
            )
        else:
            wrong = f"Missing required argument: **{name}**."
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="addxp",
                format_help=_ADDXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.TooManyArguments):
        wrong = (
            "You passed **too many** arguments. This command only accepts a member and an amount.\n"
            "Example: `!addxp @Player 100` — nothing after the number."
        )
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="addxp",
                format_help=_ADDXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.MemberNotFound):
        wrong = (
            f"I could not find a member matching **{error.argument!r}** in this server.\n"
            "Use an **@mention** of someone who is in this server, or a lookup the bot can resolve to a member here."
        )
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="addxp",
                format_help=_ADDXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.RangeError):
        wrong = (
            f"The amount **{error.value!r}** is not valid: it must be at least **{error.minimum!r}** "
            f"(whole number XP to add)."
        )
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="addxp",
                format_help=_ADDXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.ConversionError):
        orig = error.original
        if isinstance(orig, ValueError):
            wrong = (
                "The **<amount>** must be a **whole number** (digits only, e.g. `25` or `500`). "
                "Decimals, letters, or symbols are not accepted."
            )
        else:
            wrong = f"Could not convert a value: {orig!s}"[:500]
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="addxp",
                format_help=_ADDXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.BadArgument):
        msg = str(error)
        if "Converting to \"int\"" in msg or "Converting to 'int'" in msg:
            wrong = (
                "The **<amount>** must be a **whole number** (for example `50` or `1000`). "
                "You used something that is not a valid integer."
            )
        else:
            wrong = msg[:900]
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="addxp",
                format_help=_ADDXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    else:
        raise error


@bot.command(name="removexp")
@commands.guild_only()
@checks.require_dm_admin()
async def removexp(
    ctx: commands.Context,
    member: discord.Member,
    amount: commands.Range[int, 1, None],
) -> None:
    """Remove XP from a member. Total XP never goes below 0. Requires Administrator + DM role."""
    if bot.xp_collection is None or ctx.guild is None:
        return
    if member.bot:
        await ctx.send("Bots are not valid targets for XP changes.")
        return
    capped = min(amount, _ADDXP_CAP)
    if capped < amount:
        await ctx.send(
            f"Amount capped to **{capped}** per command (configured limit)."
        )
    p, removed = await xp_db.remove_xp(
        bot.xp_collection,
        guild_id=ctx.guild.id,
        user_id=member.id,
        amount=capped,
    )
    if removed == 0:
        await ctx.send(f"{member.mention} has **0** XP — nothing was removed.")
        return
    clamp_note = ""
    if removed < capped:
        clamp_note = (
            f" _(only **{removed:,}** XP removed of **{capped:,}** requested — "
            "totals cannot go below **0**)_"
        )
    max_note = " _(max level — chart stops at 20)_" if p.level >= leveling.MAX_LEVEL else ""
    await ctx.send(
        f"Removed **{removed:,}** XP from {member.mention}.\n"
        f"**Level {p.level}** · **{p.total_xp:,}** total XP · "
        f"**{p.xp_to_next_level:,}** XP to next level{max_note}{clamp_note}."
    )


@removexp.error
async def removexp_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("This command can only be used in a server.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Administrator** to use this command.")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send(str(error))
    elif isinstance(error, commands.MissingRequiredArgument):
        name = error.param.name
        if name == "member":
            wrong = (
                "You did not specify **who** should lose XP (missing **@member**).\n"
                "The first argument must be a member of this server."
            )
        elif name == "amount":
            wrong = (
                "You did not specify how much XP to remove (missing **<amount>**).\n"
                "The second argument must be a whole number, e.g. `100`."
            )
        else:
            wrong = f"Missing required argument: **{name}**."
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="removexp",
                format_help=_REMOVEXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.TooManyArguments):
        wrong = (
            "You passed **too many** arguments. This command only accepts a member and an amount.\n"
            "Example: `!removexp @Player 100` — nothing after the number."
        )
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="removexp",
                format_help=_REMOVEXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.MemberNotFound):
        wrong = (
            f"I could not find a member matching **{error.argument!r}** in this server.\n"
            "Use an **@mention** of someone who is in this server, or a lookup the bot can resolve to a member here."
        )
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="removexp",
                format_help=_REMOVEXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.RangeError):
        wrong = (
            f"The amount **{error.value!r}** is not valid: it must be at least **{error.minimum!r}** "
            f"(whole number XP to remove)."
        )
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="removexp",
                format_help=_REMOVEXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.ConversionError):
        orig = error.original
        if isinstance(orig, ValueError):
            wrong = (
                "The **<amount>** must be a **whole number** (digits only, e.g. `25` or `500`). "
                "Decimals, letters, or symbols are not accepted."
            )
        else:
            wrong = f"Could not convert a value: {orig!s}"[:500]
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="removexp",
                format_help=_REMOVEXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.BadArgument):
        msg = str(error)
        if "Converting to \"int\"" in msg or "Converting to 'int'" in msg:
            wrong = (
                "The **<amount>** must be a **whole number** (for example `50` or `1000`). "
                "You used something that is not a valid integer."
            )
        else:
            wrong = msg[:900]
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="removexp",
                format_help=_REMOVEXP_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    else:
        raise error


@bot.command(name="addxpall")
@commands.guild_only()
@checks.require_dm_admin()
async def addxpall(ctx: commands.Context, amount: commands.Range[int, 1, None]) -> None:
    """Add the same XP amount to every stored player in this server. Staff only."""
    if bot.xp_collection is None or ctx.guild is None:
        return
    capped = min(amount, _ADDXP_CAP)
    if capped < amount:
        await ctx.send(
            f"Amount capped to **{capped:,}** per command (configured limit)."
        )
    n = await xp_db.add_xp_to_all_in_guild(
        bot.xp_collection,
        guild_id=ctx.guild.id,
        amount=capped,
    )
    if n == 0:
        await ctx.send(
            "No XP records exist for this server yet — nothing was updated. "
            "Use `!addxp` first so players appear in the database."
        )
    else:
        await ctx.send(
            f"Added **{capped:,}** XP to **{n:,}** stored player(s) in this server."
        )


@addxpall.error
async def addxpall_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("This command can only be used in a server.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Administrator** to use this command.")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send(str(error))
    elif isinstance(error, commands.MissingRequiredArgument):
        wrong = (
            "You did not specify **<amount>** — how much XP to add to each stored player.\n"
            "Example: `!addxpall 10`"
        )
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="addxpall",
                format_help=_ADDXPALL_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.TooManyArguments):
        wrong = (
            "This command only takes **one** argument: the XP amount.\n"
            "Example: `!addxpall 25` — nothing after the number."
        )
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="addxpall",
                format_help=_ADDXPALL_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.RangeError):
        wrong = (
            f"The amount **{error.value!r}** is not valid: it must be at least **{error.minimum!r}** "
            f"(whole number XP to add to each person)."
        )
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="addxpall",
                format_help=_ADDXPALL_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.ConversionError):
        orig = error.original
        if isinstance(orig, ValueError):
            wrong = (
                "**<amount>** must be a **whole number** (digits only, e.g. `10` or `500`). "
                "Decimals, letters, or symbols are not accepted."
            )
        else:
            wrong = f"Could not convert a value: {orig!s}"[:500]
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="addxpall",
                format_help=_ADDXPALL_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    elif isinstance(error, commands.BadArgument):
        msg = str(error)
        if "Converting to \"int\"" in msg or "Converting to 'int'" in msg:
            wrong = (
                "**<amount>** must be a **whole number** (for example `5` or `1000`). "
                "You used something that is not a valid integer."
            )
        else:
            wrong = msg[:900]
        await ctx.send(
            embed=_staff_xp_invalid_embed(
                command="addxpall",
                format_help=_ADDXPALL_FORMAT_HELP,
                what_wrong=wrong,
                ctx=ctx,
            )
        )
    else:
        raise error


@bot.command(name="xp")
@commands.guild_only()
async def xp_cmd(ctx: commands.Context, user: Optional[discord.User] = None) -> None:
    """Show XP for you or another user (looked up by Discord user id in this server)."""
    if bot.xp_collection is None or ctx.guild is None:
        return
    target = user or ctx.author
    p = await xp_db.get_user_progress(
        bot.xp_collection,
        guild_id=ctx.guild.id,
        user_id=target.id,
    )
    max_note = " _(max level)_" if p.level >= leveling.MAX_LEVEL else ""
    await ctx.send(
        f"{target.mention}: **Level {p.level}** · **{p.total_xp:,}** total XP · "
        f"**{p.xp_to_next_level:,}** XP to next level{max_note}."
    )


@bot.command(name="help")
async def help_cmd(ctx: commands.Context) -> None:
    """List all bot commands and how to use them."""
    staff_role = checks.dm_role_name()
    embed = discord.Embed(
        title="XP Tracker — commands",
        description=(
            "All commands use the **`!`** prefix. "
            "XP is tracked **per Discord server** (guild), not globally."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="`!help`",
        value="Shows this overview.",
        inline=False,
    )
    embed.add_field(
        name="`!xp`",
        value=(
            "**Who can use it:** anyone in the server\n"
            "**Where:** server text channels only (not in DMs)\n"
            "**What it does:** shows **level** (D&D-style table, levels 1–20), **total XP**, "
            "and **XP remaining** to the next level **in this server**.\n"
            "**Examples:** `!xp` · `!xp @Player` · `!xp 123456789012345678`"
        ),
        inline=False,
    )
    embed.add_field(
        name="`!addxp`",
        value=(
            "**Who can use it:** members who have **Administrator** *and* the **"
            f"{staff_role}** role\n"
            "**Where:** server only\n"
            "**What it does:** adds XP to another member. Bots cannot receive XP. "
            f"Each command adds at most **{_ADDXP_CAP:,}** XP (configurable by the host).\n"
            "**Format:** `!addxp @member <amount>` — `@member` must be in this server; "
            "`<amount>` is a whole number **≥ 1** (e.g. `!addxp @Player 150`). "
            "If the format is wrong, the bot replies with an error embed explaining the fix."
        ),
        inline=False,
    )
    embed.add_field(
        name="`!removexp`",
        value=(
            "**Who can use it:** same as `!addxp` (**Administrator** + **"
            f"{staff_role}**)\n"
            "**Where:** server only\n"
            "**What it does:** subtracts XP from a member. **Total XP never goes below 0**; "
            f"if they have less than the amount, only their current XP is removed. "
            f"Same per-command cap as add (**{_ADDXP_CAP:,}**).\n"
            "**Format:** `!removexp @member <amount>`"
        ),
        inline=False,
    )
    embed.add_field(
        name="`!addxpall`",
        value=(
            "**Who can use it:** same as `!addxp` (**Administrator** + **"
            f"{staff_role}**)\n"
            "**Where:** server only\n"
            "**What it does:** adds the **same** `<amount>` of XP to **every** player "
            "already stored for **this server** in the database. "
            "Does **not** create rows for people who have never received XP. "
            f"Amount is capped at **{_ADDXP_CAP:,}** per command (same as `!addxp`).\n"
            "**Format:** `!addxpall <amount>` — e.g. `!addxpall 10`"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


@xp_cmd.error
async def xp_cmd_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("This command can only be used in a server.")
    elif isinstance(error, commands.UserNotFound | commands.MemberNotFound):
        await ctx.send("User not found. Try a mention or a numeric user id.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(
            "Usage: `!xp` (your XP) or `!xp @user` / `!xp <user_id>`"
        )
    else:
        raise error


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "Missing DISCORD_BOT_TOKEN. Copy .env.example to .env and set your bot token."
        )
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
