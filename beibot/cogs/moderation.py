import discord
from discord.ext import commands
from datetime import timedelta

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if ctx.author.guild_permissions.administrator:
            return True
        raise commands.MissingPermissions(["administrator"])

    @commands.command()
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        await member.kick(reason=reason)
        await ctx.send(f"Kicked {member.mention} | Reason: {reason}")

    @commands.command()
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        await member.ban(reason=reason)
        await ctx.send(f"Banned {member.mention} | Reason: {reason}")

    @commands.command()
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx, user: discord.User, *, reason: str = "No reason provided"):
        await ctx.guild.unban(user, reason=reason)
        await ctx.send(f"Unbanned {user.mention} | Reason: {reason}")

    @commands.command()
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(self, ctx, member: discord.Member, minutes: int, *, reason: str = "No reason provided"):
        duration = timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
        await ctx.send(f"Timed out {member.mention} for {minutes} minute(s) | Reason: {reason}")

    @commands.command()
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        await member.timeout(None, reason=reason)
        await ctx.send(f"Removed timeout from {member.mention} | Reason: {reason}")

    @kick.error
    @ban.error
    @unban.error
    @timeout.error
    @untimeout.error
    async def moderation_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the Administrator permission to use moderation commands.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command can only be used in a server.")
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send("I don't have the permissions needed to do that.")
        elif isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
            await ctx.send("Couldn't find that member/user.")
        else:
            await ctx.send(f"Something went wrong: {error}")

async def setup(bot):
    await bot.add_cog(Moderation(bot))