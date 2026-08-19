import discord
from discord import app_commands
from discord.ext import commands
import datetime
import re

MAX_AMOUNT = 500
CONFIRM_ABOVE = 50
SCAN_LIMIT = 2000
BULK_AGE_DAYS = 14
RESULT_LINGER = 8

FILTERS = {
    "bots": lambda m: m.author.bot,
    "humans": lambda m: not m.author.bot,
    "embeds": lambda m: bool(m.embeds),
    "images": lambda m: any(
        a.content_type and a.content_type.startswith("image") for a in m.attachments
    ),
    "files": lambda m: bool(m.attachments),
    "links": lambda m: "http://" in m.content or "https://" in m.content,
    "pinned": lambda m: m.pinned,
}

ID_PATTERN = re.compile(r"<@!?(\d+)>|^(\d{15,})$")


def can_purge(member):
    perms = member.guild_permissions
    return perms.administrator or perms.manage_messages


def cutoff_time():
    return discord.utils.utcnow() - datetime.timedelta(days=BULK_AGE_DAYS)


async def resolve_member(ctx, token):
    match = ID_PATTERN.match(token)
    if not match:
        return None

    raw = match.group(1) or match.group(2)
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        return None

    member = ctx.guild.get_member(user_id)
    if member is not None:
        return member

    try:
        return await ctx.guild.fetch_member(user_id)
    except (discord.NotFound, discord.HTTPException):
        return None


class ConfirmPurgeView(discord.ui.View):
    def __init__(self, author_id, amount, described):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed = False
        self.amount = amount
        self.described = described

    async def interaction_check(self, interaction):
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Delete Them", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Working...", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)
        self.stop()

    async def on_timeout(self):
        self.stop()


def amount_problem(amount):
    """Return a reason the count is unusable, or None."""
    if amount < 1:
        return "That has to be at least 1."
    if amount > MAX_AMOUNT:
        return f"I'll do at most {MAX_AMOUNT} at a time."
    return None


def describe_scope(amount, target, filter_name):
    if target is not None and filter_name:
        return f"{amount} {filter_name} message(s) from {target.display_name}"
    if target is not None:
        return f"{amount} message(s) from {target.display_name}"
    if filter_name:
        return f"{amount} {filter_name} message(s)"
    return f"{amount} message(s)"


class Purge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if can_purge(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_messages"])

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need Administrator or Manage Messages permission.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command only works in a server.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Slow down, try again in {error.retry_after:.0f}s.")
        else:
            await ctx.send(f"Something went wrong: {error}")

    async def run_purge(self, channel, author, amount, target, filter_name):
        """Delete the matching messages and return a summary line.

        Shared by the prefix command and the slash command so the two can
        never drift apart. Discord errors are left to the caller, which
        knows how to reply on its own surface.
        """
        limit = cutoff_time()
        matched = 0
        skipped_old = 0

        extra = FILTERS.get(filter_name)

        def check(message):
            nonlocal matched, skipped_old

            if matched >= amount:
                return False
            if target is not None and message.author.id != target.id:
                return False
            if extra is not None and not extra(message):
                return False
            if filter_name != "pinned" and message.pinned:
                return False
            if message.created_at < limit:
                skipped_old += 1
                return False

            matched += 1
            return True

        scan = amount if (target is None and extra is None) else SCAN_LIMIT

        deleted = await channel.purge(
            limit=scan,
            check=check,
            reason=f"Purge by {author}",
        )

        parts = [f"Deleted {len(deleted)} message(s)"]
        if target is not None:
            parts.append(f"from {target.display_name}")
        if filter_name:
            parts.append(f"matching {filter_name}")

        summary = " ".join(parts) + "."

        if skipped_old:
            summary += (
                f" Skipped {skipped_old} older than {BULK_AGE_DAYS} days, "
                "which Discord won't bulk delete."
            )

        if len(deleted) < amount and not skipped_old:
            summary += " That was everything I could find."

        return summary

    @commands.command(name="purge", aliases=["clear", "clean"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def purge(self, ctx, *args):
        if not ctx.channel.permissions_for(ctx.guild.me).manage_messages:
            await ctx.send("I need the Manage Messages permission here.")
            return

        amount = None
        target = None
        filter_name = None

        for token in args:
            lowered = token.lower()
            if lowered in FILTERS and filter_name is None:
                filter_name = lowered
                continue
            if token.isdigit() and amount is None and len(token) < 15:
                amount = int(token)
                continue
            if target is None:
                found = await resolve_member(ctx, token)
                if found is not None:
                    target = found

        if amount is None:
            await ctx.send(
                f"Give me a number. For example `{ctx.prefix}purge 10`, "
                f"`{ctx.prefix}purge @someone 10`, or `{ctx.prefix}purge bots 25`."
            )
            return

        problem = amount_problem(amount)
        if problem:
            await ctx.send(problem)
            return

        described = describe_scope(amount, target, filter_name)

        if amount > CONFIRM_ABOVE:
            confirm = ConfirmPurgeView(ctx.author.id, amount, described)
            prompt = await ctx.send(
                f"About to delete {described} in this channel. "
                "This can't be undone.",
                view=confirm,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await confirm.wait()

            try:
                await prompt.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

            if not confirm.confirmed:
                return

        try:
            await ctx.message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

        try:
            summary = await self.run_purge(
                ctx.channel, ctx.author, amount, target, filter_name
            )
        except discord.Forbidden:
            await ctx.send("I don't have permission to delete messages here.")
            return
        except discord.HTTPException as exc:
            await ctx.send(f"Discord rejected the request: {exc}")
            return

        await ctx.send(
            summary,
            delete_after=RESULT_LINGER,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="purge",
        description="Bulk delete messages in this channel.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    @app_commands.describe(
        amount="How many messages to delete",
        member="Only delete messages from this member",
        only="Only delete messages of this kind",
    )
    @app_commands.choices(
        only=[app_commands.Choice(name=name, value=name) for name in sorted(FILTERS)]
    )
    async def purge_slash(
        self,
        interaction: discord.Interaction,
        amount: int,
        member: discord.Member = None,
        only: str = None,
    ):
        """Slash version of purge.

        The prefix command reads its arguments in any order, which an app
        command cannot express, so the two share run_purge rather than one
        wrapping the other. Replies stay ephemeral because the invocation
        itself leaves no message to clean up.
        """
        if not can_purge(interaction.user):
            await interaction.response.send_message(
                "You need Administrator or Manage Messages permission.",
                ephemeral=True,
            )
            return

        if not interaction.channel.permissions_for(interaction.guild.me).manage_messages:
            await interaction.response.send_message(
                "I need the Manage Messages permission here.", ephemeral=True
            )
            return

        problem = amount_problem(amount)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        described = describe_scope(amount, member, only)

        if amount > CONFIRM_ABOVE:
            confirm = ConfirmPurgeView(interaction.user.id, amount, described)
            await interaction.response.send_message(
                f"About to delete {described} in this channel. This can't be undone.",
                view=confirm,
                ephemeral=True,
            )
            await confirm.wait()
            if not confirm.confirmed:
                return
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            summary = await self.run_purge(
                interaction.channel, interaction.user, amount, member, only
            )
        except discord.Forbidden:
            summary = "I don't have permission to delete messages here."
        except discord.HTTPException as exc:
            summary = f"Discord rejected the request: {exc}"

        if amount > CONFIRM_ABOVE:
            await interaction.edit_original_response(content=summary, view=None)
        else:
            await interaction.followup.send(summary, ephemeral=True)

    @commands.hybrid_command(
        name="purgehelp",
        aliases=["ph"],
        description="Show the purge filters and some examples.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @commands.guild_only()
    async def purge_help(self, ctx):
        embed = discord.Embed(
            title="Purge",
            description=(
                f"`{ctx.prefix}purge 10` - the last 10 messages here\n"
                f"`{ctx.prefix}purge @someone 10` - their last 10 messages\n"
                f"`{ctx.prefix}purge bots 25` - the last 25 from bots\n"
                f"`{ctx.prefix}purge links 15` - the last 15 containing a link\n\n"
                "**Filters** - "
                + ", ".join(f"`{name}`" for name in sorted(FILTERS))
                + "\n\n"
                "Prefix arguments can be given in any order. The slash command "
                "takes the same three as named options.\n\n"
                "Pinned messages are never deleted unless you use the "
                "`pinned` filter. Messages older than "
                f"{BULK_AGE_DAYS} days can't be bulk deleted by Discord, "
                "so they get skipped."
            ),
            color=discord.Color.dark_theme(),
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Purge(bot))