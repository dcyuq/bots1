import discord
from discord import app_commands
from discord.ext import commands
from datetime import timedelta

MAX_TIMEOUT_MINUTES = 40320


def hierarchy_problem(ctx, member):
    """Return a reason the action can't proceed, or None.

    Discord rejects moderating anyone at or above the actor's top role or
    the bot's own, so both are checked before the API call rather than
    letting it raise Forbidden.
    """
    if member.id == ctx.guild.owner_id:
        return "That's the server owner."
    if member.id == ctx.me.id:
        return "I can't do that to myself."
    if member.top_role >= ctx.me.top_role:
        return (
            "That member's highest role is at or above mine. Move my role "
            "higher in Server Settings first."
        )
    if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
        return "That member's highest role is at or above your own."
    return None


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if ctx.author.guild_permissions.administrator:
            return True
        raise commands.MissingPermissions(["administrator"])

    @commands.hybrid_command(description="Remove a member from the server.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="The member to kick", reason="Recorded in the audit log")
    @commands.guild_only()
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        problem = hierarchy_problem(ctx, member)
        if problem:
            await ctx.send(problem)
            return

        await member.kick(reason=f"{ctx.author}: {reason}")
        await ctx.send(
            f"Kicked {member.mention} | Reason: {reason}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(description="Ban a user from the server, even if they are not in it.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(user="The user or ID to ban", reason="Recorded in the audit log")
    @commands.guild_only()
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx, user: discord.User, *, reason: str = "No reason provided"):
        member = ctx.guild.get_member(user.id)

        if member is not None:
            problem = hierarchy_problem(ctx, member)
            if problem:
                await ctx.send(problem)
                return

        try:
            await ctx.guild.ban(user, reason=f"{ctx.author}: {reason}")
        except discord.NotFound:
            await ctx.send("Couldn't find that user.")
            return

        await ctx.send(
            f"Banned {user.mention} | Reason: {reason}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(description="Lift a ban on a user.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(user="The user or ID to unban", reason="Recorded in the audit log")
    @commands.guild_only()
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx, user: discord.User, *, reason: str = "No reason provided"):
        try:
            await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
        except discord.NotFound:
            await ctx.send("That user isn't banned.")
            return

        await ctx.send(
            f"Unbanned {user.mention} | Reason: {reason}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(description="Mute a member for a number of minutes.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="The member to time out", minutes="1 to 40320 (28 days)", reason="Recorded in the audit log")
    @commands.guild_only()
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(self, ctx, member: discord.Member, minutes: int, *, reason: str = "No reason provided"):
        if minutes < 1 or minutes > MAX_TIMEOUT_MINUTES:
            await ctx.send(
                f"Pick between 1 and {MAX_TIMEOUT_MINUTES} minutes (28 days)."
            )
            return

        problem = hierarchy_problem(ctx, member)
        if problem:
            await ctx.send(problem)
            return

        await member.timeout(timedelta(minutes=minutes), reason=f"{ctx.author}: {reason}")
        await ctx.send(
            f"Timed out {member.mention} for {minutes} minute(s) | Reason: {reason}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(description="End a member's timeout early.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="The member to release", reason="Recorded in the audit log")
    @commands.guild_only()
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if member.timed_out_until is None:
            await ctx.send("That member isn't timed out.")
            return

        problem = hierarchy_problem(ctx, member)
        if problem:
            await ctx.send(problem)
            return

        await member.timeout(None, reason=f"{ctx.author}: {reason}")
        await ctx.send(
            f"Removed timeout from {member.mention} | Reason: {reason}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the Administrator permission to use moderation commands.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command can only be used in a server.")
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send("I don't have the permissions needed to do that.")
        elif isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
            await ctx.send("Couldn't find that member/user.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("That argument wasn't valid.")
        elif isinstance(error, discord.Forbidden):
            await ctx.send("Discord refused that action.")
        else:
            await ctx.send(f"Something went wrong: {error}")


async def setup(bot):
    await bot.add_cog(Moderation(bot))