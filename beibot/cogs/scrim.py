import discord
from discord.ext import commands

MAX_TEAMS = 20


def can_manage(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_messages


class ScrimModal(discord.ui.Modal, title="Scrim Setup"):
    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    description = discord.ui.TextInput(
        label="Description",
        placeholder="e.g. cezu scrims!",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    team_count = discord.ui.TextInput(
        label="Team count",
        placeholder="e.g. 4",
        required=True,
        max_length=2,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            count = int(self.team_count.value)
        except ValueError:
            await interaction.response.send_message(
                "Team count must be a number.",
                ephemeral=True
            )
            return

        if count < 1 or count > MAX_TEAMS:
            await interaction.response.send_message(
                f"Team count must be between 1 and {MAX_TEAMS}.",
                ephemeral=True
            )
            return

        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You no longer have permission to do that.",
                ephemeral=True
            )
            return

        permissions = self.channel.permissions_for(interaction.guild.me)
        if not permissions.send_messages or not permissions.create_public_threads:
            await interaction.response.send_message(
                "I can't post or create threads in that channel anymore.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            await self.channel.send(
                self.description.value,
                allowed_mentions=discord.AllowedMentions.none(),
            )

            created = 0
            for i in range(1, count + 1):
                team_message = await self.channel.send(f"``team {i}``")
                await team_message.create_thread(name=f"team {i}")
                created += 1
        except discord.Forbidden:
            await interaction.followup.send(
                "I lost permission partway through. "
                f"Created {created} thread(s) before stopping.",
                ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Discord rejected a request after {created} thread(s): {exc}",
                ephemeral=True
            )
            return

        await interaction.followup.send(
            f"Created {created} scrim thread(s) in {self.channel.mention}.",
            ephemeral=True
        )


class ScrimView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.selected_channel = None
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Run `,scrim` yourself to use this panel.",
                ephemeral=True
            )
            return False

        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need Administrator or Manage Messages permission.",
                ephemeral=True
            )
            return False

        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Pick a channel for the scrim",
        min_values=1,
        max_values=1,
    )
    async def channel_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.ChannelSelect
    ):
        self.selected_channel = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="Create Scrim", style=discord.ButtonStyle.primary)
    async def create_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if self.selected_channel is None:
            await interaction.response.send_message(
                "Pick a channel first.",
                ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(self.selected_channel.id)

        if channel is None:
            await interaction.response.send_message(
                "That channel no longer exists.",
                ephemeral=True
            )
            return

        author_perms = channel.permissions_for(interaction.user)
        if not author_perms.send_messages:
            await interaction.response.send_message(
                "You can't post in that channel.",
                ephemeral=True
            )
            return

        permissions = channel.permissions_for(interaction.guild.me)

        if not permissions.send_messages:
            await interaction.response.send_message(
                "I can't send messages in that channel.",
                ephemeral=True
            )
            return

        if not permissions.create_public_threads:
            await interaction.response.send_message(
                "I don't have permission to create threads in that channel.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(ScrimModal(channel))


class Scrims(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if can_manage(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_messages"])

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                "You need Administrator or Manage Messages permission to use this."
            )
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command can only be used in a server.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Slow down, try again in {error.retry_after:.0f}s.")
        else:
            await ctx.send(f"Something went wrong: {error}")

    @commands.command()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def scrim(self, ctx):
        view = ScrimView(ctx.author.id)
        message = await ctx.send(
            "To create a scrim, pick a channel and click the button below",
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = message


async def setup(bot):
    await bot.add_cog(Scrims(bot))