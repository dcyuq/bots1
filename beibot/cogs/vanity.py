import discord
from discord.ext import commands, tasks
from storage import Store

_store = Store("vanity.json")

SWEEP_MINUTES = 10


def load_data():
    return _store.load()


def save_data(data):
    _store.save(data)


config = load_data()


def get_guild_config(guild_id):
    return config.get(str(guild_id))


DANGEROUS_PERMS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "manage_messages",
    "ban_members",
    "kick_members",
    "moderate_members",
    "mention_everyone",
)


def can_manage(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_roles


def dangerous_perms(role: discord.Role):
    """Return the list of escalation-capable permissions a role grants."""
    perms = role.permissions
    return [name for name in DANGEROUS_PERMS if getattr(perms, name)]


def check_role_assignable(role: discord.Role, actor: discord.Member) -> str | None:
    """Validate a candidate vanity role. Returns an error string, or None."""
    guild = role.guild

    if role.is_default() or role.managed:
        return "That role can't be assigned by a bot."

    if not guild.me.guild_permissions.manage_roles:
        return "I don't have the Manage Roles permission."

    if role >= guild.me.top_role:
        return (
            "That role is above me in the hierarchy. Move my role higher in "
            "Server Settings first."
        )

    if actor.id != guild.owner_id and role >= actor.top_role:
        return (
            "That role is at or above your own highest role, so you can't "
            "configure it to be given out."
        )

    granted = dangerous_perms(role)
    if granted:
        listed = ", ".join(granted)
        return (
            f"That role grants **{listed}**. I won't hand out privileged roles "
            "automatically based on someone's status text."
        )

    return None


def get_custom_status(member: discord.Member) -> str:
    """Return the member's custom status text, or an empty string."""
    for activity in member.activities:
        if isinstance(activity, discord.CustomActivity):
            return activity.name or ""
    return ""


def has_keyword(member: discord.Member, keyword: str) -> bool:
    return keyword.lower() in get_custom_status(member).lower()


class VanityConfigModal(discord.ui.Modal, title="Vanity Role Setup"):

    keyword = discord.ui.TextInput(
        label="Keyword",
        placeholder="Example: /ione",
        required=True,
        max_length=100,
    )

    def __init__(self, role: discord.Role):
        super().__init__()
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You no longer have permission to do that.", ephemeral=True
            )
            return

        problem = check_role_assignable(self.role, interaction.user)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        keyword = self.keyword.value.strip()

        if len(keyword) < 2:
            await interaction.response.send_message(
                "Use a keyword of at least 2 characters. Very short keywords "
                "match almost every status by accident.",
                ephemeral=True,
            )
            return

        config[str(interaction.guild_id)] = {
            "keyword": keyword,
            "role_id": self.role.id,
            "configured_by": interaction.user.id,
        }
        save_data(config)

        await interaction.response.send_message(
            f"Vanity role set. Members with `{keyword}` in their custom "
            f"status will receive {self.role.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class VanityRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Pick the role to award", max_values=1)

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.values[0].id)

        if role is None:
            await interaction.response.send_message(
                "Couldn't resolve that role.", ephemeral=True
            )
            return

        problem = check_role_assignable(role, interaction.user)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        await interaction.response.send_modal(VanityConfigModal(role))


class VanitySetupView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.add_item(VanityRoleSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This panel isn't yours.", ephemeral=True
            )
            return False
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need Administrator or Manage Roles permission.",
                ephemeral=True,
            )
            return False
        return True


class Vanity(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.sweep.start()

    def cog_unload(self):
        self.sweep.cancel()


    async def sync_member(self, member: discord.Member):
        """Add or remove the vanity role based on the member's status."""
        if member.bot:
            return

        settings = get_guild_config(member.guild.id)
        if settings is None:
            return

        if member.status is discord.Status.offline:
            return

        role = member.guild.get_role(settings["role_id"])
        if role is None:
            return

        me = member.guild.me
        if not me.guild_permissions.manage_roles or role >= me.top_role:
            return

        if dangerous_perms(role):
            return

        should_have = has_keyword(member, settings["keyword"])
        currently_has = role in member.roles

        if should_have and not currently_has:
            try:
                await member.add_roles(role, reason="Vanity keyword in status")
            except (discord.Forbidden, discord.HTTPException):
                pass
        elif not should_have and currently_has:
            try:
                await member.remove_roles(role, reason="Vanity keyword removed")
            except (discord.Forbidden, discord.HTTPException):
                pass


    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        settings = get_guild_config(after.guild.id)
        if settings is None:
            return

        if get_custom_status(before) == get_custom_status(after):
            return

        await self.sync_member(after)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.sync_member(member)

    @tasks.loop(minutes=SWEEP_MINUTES)
    async def sweep(self):
        """Reconcile every member, catching anything the gateway missed."""
        for guild_id, settings in list(config.items()):
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue
            for member in guild.members:
                await self.sync_member(member)

    @sweep.before_loop
    async def before_sweep(self):
        await self.bot.wait_until_ready()


    async def cog_check(self, ctx):
        """Gate every command in this cog behind Administrator / Manage Roles."""
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
            await ctx.send("This command can only be used in a server.")
        else:
            await ctx.send(f"Something went wrong: {error}")

    @commands.command(name="vanity", aliases=["vr"])
    async def vanity(self, ctx: commands.Context):
        settings = get_guild_config(ctx.guild.id)

        if settings is None:
            description = (
                "No vanity role configured.\n\n"
                "Pick a role below, then enter the keyword to watch for."
            )
        else:
            role = ctx.guild.get_role(settings["role_id"])
            role_text = role.mention if role else "*deleted role*"
            description = (
                f"**Keyword** - `{settings['keyword']}`\n"
                f"**Role** - {role_text}\n\n"
                "Pick a role below to reconfigure."
            )

        embed = discord.Embed(
            title="Vanity Role",
            description=description,
            color=discord.Color.dark_theme(),
        )

        await ctx.send(
            embed=embed,
            view=VanitySetupView(ctx.author.id),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="unvanity")
    async def unvanity(self, ctx: commands.Context):
        if config.pop(str(ctx.guild.id), None) is None:
            await ctx.send("No vanity role is configured here.")
            return

        save_data(config)
        await ctx.send("Vanity role disabled. Existing roles were left in place.")


async def setup(bot):
    await bot.add_cog(Vanity(bot))