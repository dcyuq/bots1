import discord
from discord import app_commands
from discord.ext import commands, tasks

from storage import Store
from roleutils import assignable_now, check_role_assignable

_store = Store("vanity.json")

SWEEP_MINUTES = 10
MIN_KEYWORD_LENGTH = 2

MODES = {
    "status": "Status keyword only",
    "tag": "Server tag only",
    "either": "Keyword or server tag",
}


def load_data():
    return _store.load()


def save_data(data):
    _store.save(data)


config = load_data()


def get_guild_config(guild_id):
    return config.get(str(guild_id))


def save_settings(guild_id, role, mode, keyword, user_id):
    config[str(guild_id)] = {
        "mode": mode,
        "keyword": keyword,
        "role_id": role.id,
        "configured_by": user_id,
    }
    save_data(config)


def can_manage(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_roles


def get_custom_status(member: discord.Member) -> str:
    for activity in member.activities:
        if isinstance(activity, discord.CustomActivity):
            return activity.name or ""
    return ""


def has_keyword(member: discord.Member, keyword: str) -> bool:
    return bool(keyword) and keyword.lower() in get_custom_status(member).lower()


def tag_key(user):
    pg = getattr(user, "primary_guild", None)
    return (pg.id, pg.identity_enabled) if pg else None


def has_tag(member: discord.Member) -> bool:
    pg = getattr(member, "primary_guild", None)
    return bool(pg and pg.identity_enabled and pg.id == member.guild.id)


def describe(settings, mode):
    text = f"**Mode** - {MODES[mode]}\n"
    if mode != "tag":
        text += f"**Keyword** - `{settings.get('keyword', '')}`\n"
    return text


class VanityConfigModal(discord.ui.Modal, title="Vanity Role Setup"):

    keyword = discord.ui.TextInput(
        label="Keyword",
        placeholder="Example: /ione",
        required=True,
        max_length=100,
    )

    def __init__(self, role: discord.Role, mode: str):
        super().__init__()
        self.role = role
        self.mode = mode

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

        if len(keyword) < MIN_KEYWORD_LENGTH:
            await interaction.response.send_message(
                f"Use a keyword of at least {MIN_KEYWORD_LENGTH} characters. "
                "Very short keywords match almost every status by accident.",
                ephemeral=True,
            )
            return

        save_settings(interaction.guild_id, self.role, self.mode, keyword, interaction.user.id)

        extra = " or wearing this server's tag" if self.mode == "either" else ""
        await interaction.response.send_message(
            f"Vanity role set. Members with `{keyword}` in their custom "
            f"status{extra} will receive {self.role.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class VanityModeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Pick what members need",
            options=[
                discord.SelectOption(label=label, value=value, default=value == "either")
                for value, label in MODES.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.mode = self.values[0]
        for option in self.options:
            option.default = option.value == self.view.mode
        await interaction.response.edit_message(view=self.view)


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

        mode = self.view.mode

        if mode == "tag":
            save_settings(interaction.guild_id, role, mode, "", interaction.user.id)
            await interaction.response.send_message(
                f"Vanity role set. Members wearing this server's tag "
                f"will receive {role.mention}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.send_modal(VanityConfigModal(role, mode))


class VanitySetupView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.message = None
        self.mode = "either"
        self.add_item(VanityModeSelect())
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

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Vanity(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.sweep.start()

    def cog_unload(self):
        self.sweep.cancel()

    async def sync_member(self, member: discord.Member):
        if member.bot:
            return

        settings = get_guild_config(member.guild.id)
        if settings is None:
            return

        role = member.guild.get_role(settings["role_id"])
        if not assignable_now(role, member.guild.me):
            return

        mode = settings.get("mode", "status")
        online = member.status is not discord.Status.offline

        by_status = mode != "tag" and online and has_keyword(member, settings.get("keyword", ""))
        by_tag = mode != "status" and has_tag(member)
        should_have = by_status or by_tag

        if not should_have and not online and mode != "tag":
            return

        currently_has = role in member.roles

        if should_have and not currently_has:
            try:
                await member.add_roles(role, reason="Vanity requirement met")
            except (discord.Forbidden, discord.HTTPException):
                pass
        elif not should_have and currently_has:
            try:
                await member.remove_roles(role, reason="Vanity requirement lost")
            except (discord.Forbidden, discord.HTTPException):
                pass

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if get_guild_config(after.guild.id) is None:
            return

        if (
            get_custom_status(before) == get_custom_status(after)
            and before.status == after.status
            and tag_key(before) == tag_key(after)
        ):
            return

        await self.sync_member(after)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if tag_key(before) != tag_key(after):
            await self.sync_member(after)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        if tag_key(before) == tag_key(after):
            return

        for guild_id in list(config):
            guild = self.bot.get_guild(int(guild_id))
            member = guild.get_member(after.id) if guild else None
            if member:
                await self.sync_member(member)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.sync_member(member)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        settings = get_guild_config(role.guild.id)
        if settings is None or settings["role_id"] != role.id:
            return

        del config[str(role.guild.id)]
        save_data(config)

    @tasks.loop(minutes=SWEEP_MINUTES)
    async def sweep(self):
        for guild_id, settings in list(config.items()):
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue

            role = guild.get_role(settings["role_id"])
            if not assignable_now(role, guild.me):
                continue

            for member in guild.members:
                await self.sync_member(member)

    @sweep.before_loop
    async def before_sweep(self):
        await self.bot.wait_until_ready()

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
            await ctx.send("This command can only be used in a server.")
        else:
            await ctx.send(f"Something went wrong: {error}")

    @commands.hybrid_command(
        name="vanity",
        aliases=["vr"],
        description="Configure the role awarded for a status keyword or server tag.",
    )
    @app_commands.default_permissions(manage_roles=True)
    @commands.guild_only()
    async def vanity(self, ctx: commands.Context):
        settings = get_guild_config(ctx.guild.id)

        if settings is None:
            description = (
                "No vanity role configured.\n\n"
                "Pick a mode, then pick a role."
            )
        else:
            mode = settings.get("mode", "status")
            role = ctx.guild.get_role(settings["role_id"])
            role_text = role.mention if role else "*deleted role*"
            description = (
                describe(settings, mode)
                + f"**Role** - {role_text}\n\n"
                "Pick a mode and role below to reconfigure."
            )

        embed = discord.Embed(
            title="Vanity Role",
            description=description,
            color=discord.Color.dark_theme(),
        )

        view = VanitySetupView(ctx.author.id)
        view.message = await ctx.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(
        name="unvanity",
        description="Turn the vanity role off.",
    )
    @app_commands.default_permissions(manage_roles=True)
    @commands.guild_only()
    async def unvanity(self, ctx: commands.Context):
        if config.pop(str(ctx.guild.id), None) is None:
            await ctx.send("No vanity role is configured here.")
            return

        save_data(config)
        await ctx.send("Vanity role disabled. Existing roles were left in place.")


async def setup(bot):
    await bot.add_cog(Vanity(bot))