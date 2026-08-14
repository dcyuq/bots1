import discord
from discord.ext import commands
from prefixes import (
    DEFAULT_PREFIX,
    clear_prefix,
    is_custom,
    prefix_for,
    set_prefix,
    validate,
)


def can_manage(member: discord.Member) -> bool:
    return (
        member.id == member.guild.owner_id
        or member.guild_permissions.administrator
    )


def owner_or_admin():
    async def predicate(ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if can_manage(ctx.author):
            return True
        raise commands.MissingPermissions(["administrator"])

    return commands.check(predicate)


class Prefix(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                "Only the server owner or an administrator can change the prefix."
            )
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command only works in a server.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Slow down, try again in {error.retry_after:.0f}s.")
        else:
            await ctx.send(f"Something went wrong: {error}")

    @commands.group(name="prefix", invoke_without_command=True)
    @commands.guild_only()
    async def prefix(self, ctx):
        current = prefix_for(ctx.guild.id)
        origin = "custom" if is_custom(ctx.guild.id) else "the default"

        description = [
            f"The prefix here is `{current}` ({origin}).",
            "",
            f"`{current}prefix set <new>` - change it",
            f"`{current}prefix reset` - go back to `{DEFAULT_PREFIX}`",
            "",
            f"{self.bot.user.mention} also works as a prefix at any time, "
            "so you can always get back if a prefix stops working.",
        ]

        if not can_manage(ctx.author):
            description.append("")
            description.append(
                "Changing it needs the server owner or an administrator."
            )

        embed = discord.Embed(
            title="Command Prefix",
            description="\n".join(description),
            color=discord.Color.dark_theme(),
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @prefix.command(name="set")
    @owner_or_admin()
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def prefix_set(self, ctx, *, new: str = None):
        value, problem = validate(new)

        if problem:
            await ctx.send(problem)
            return

        current = prefix_for(ctx.guild.id)
        if value == current:
            await ctx.send(f"The prefix is already `{value}`.")
            return

        set_prefix(ctx.guild.id, value)

        await ctx.send(
            f"Prefix changed to `{value}`. Try `{value}help`."
        )

    @prefix.command(name="reset", aliases=["default"])
    @owner_or_admin()
    async def prefix_reset(self, ctx):
        removed = clear_prefix(ctx.guild.id)

        if removed is None:
            await ctx.send(f"The prefix is already the default `{DEFAULT_PREFIX}`.")
            return

        await ctx.send(f"Prefix reset to `{DEFAULT_PREFIX}`.")


async def setup(bot):
    await bot.add_cog(Prefix(bot))