import discord
from discord.ext import commands


class Sync(commands.Cog):
    """Push the slash command tree to Discord.

    Syncing is deliberately manual. A global sync is rate limited and this
    host pulls from git and restarts often, so doing it on every boot would
    burn the allowance for no benefit. Run it once after a deploy that
    changed a command name, description or argument.

    Guild syncs land immediately. Global syncs can take up to an hour to
    appear in every server, so use the guild variant while iterating.
    """

    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if await self.bot.is_owner(ctx.author):
            return True
        raise commands.NotOwner()

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.NotOwner):
            return
        await ctx.send(f"Sync failed: {type(error).__name__}: {error}")

    @commands.group(name="sync", invoke_without_command=True, hidden=True)
    async def sync(self, ctx):
        async with ctx.typing():
            synced = await self.bot.tree.sync()
        await ctx.send(
            f"Synced {len(synced)} command(s) globally. These can take up to "
            f"an hour to show up everywhere. Use `{ctx.prefix}sync here` for "
            "an instant test in this server."
        )

    @sync.command(name="here", aliases=["guild"])
    @commands.guild_only()
    async def sync_here(self, ctx):
        self.bot.tree.copy_global_to(guild=ctx.guild)
        async with ctx.typing():
            synced = await self.bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"Synced {len(synced)} command(s) to this server.")

    @sync.command(name="clear")
    async def sync_clear(self, ctx):
        self.bot.tree.clear_commands(guild=None)
        async with ctx.typing():
            await self.bot.tree.sync()
        await ctx.send(
            "Cleared every global command. Restart the bot and run "
            f"`{ctx.prefix}sync` to put them back."
        )

    @sync.command(name="clearhere")
    @commands.guild_only()
    async def sync_clear_here(self, ctx):
        self.bot.tree.clear_commands(guild=ctx.guild)
        async with ctx.typing():
            await self.bot.tree.sync(guild=ctx.guild)
        await ctx.send(
            "Cleared the copies registered to this server. Global commands "
            "are untouched."
        )

    @sync.command(name="list")
    async def sync_list(self, ctx):
        names = sorted(
            command.qualified_name for command in self.bot.tree.walk_commands()
        )
        if not names:
            await ctx.send("Nothing in the tree.")
            return

        listed = ", ".join(f"`/{name}`" for name in names)
        await ctx.send(f"{len(names)} in the tree: {listed}"[:2000])


async def setup(bot):
    await bot.add_cog(Sync(bot))