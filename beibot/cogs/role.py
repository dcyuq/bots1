import discord
from discord.ext import commands
from roleutils import check_role_assignable, dangerous_perms


def can_manage(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_roles


class Roles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if can_manage(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_roles"])

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                "You need Administrator or Manage Roles permission to use this."
            )
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command only works in a server.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("Couldn't find that member.")
        elif isinstance(error, commands.RoleNotFound):
            await ctx.send(
                "Couldn't find that role. Mention it, use its ID, or type the "
                "name exactly as it appears."
            )
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `role <member> <role>`")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Slow down, try again in {error.retry_after:.0f}s.")
        else:
            await ctx.send(f"Something went wrong: {error}")

    @commands.command(name="role")
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def role(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        role: discord.Role,
    ):
        problem = check_role_assignable(role, ctx.author, allow_privileged=True)
        if problem:
            await ctx.send(problem)
            return

        has_it = role in member.roles
        granted = dangerous_perms(role)

        try:
            if has_it:
                await member.remove_roles(
                    role, reason=f"Role toggle by {ctx.author}"
                )
            else:
                await member.add_roles(role, reason=f"Role toggle by {ctx.author}")
        except discord.Forbidden:
            await ctx.send("I don't have permission to change that role.")
            return
        except discord.HTTPException as exc:
            await ctx.send(f"Discord rejected the request: {exc}")
            return

        if has_it:
            summary = f"Removed {role.mention} from {member.mention}."
        else:
            summary = f"Gave {role.mention} to {member.mention}."
            if granted:
                summary += (
                    f"\nHeads up, that role grants **{', '.join(granted)}**."
                )

        await ctx.send(summary, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Roles(bot))