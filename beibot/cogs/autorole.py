import discord
from discord.ext import commands
from storage import Store
from roleutils import assignable_now, check_role_assignable

_store = Store("autorole.json")
config = _store.load()

MAX_ROLES = 10

TIMING_LABELS = {
    "join": "As soon as they join",
    "screened": "After they accept the rules",
}


def save():
    _store.save(config)


def get_settings(guild_id):
    return config.get(str(guild_id))


def ensure_settings(guild_id):
    key = str(guild_id)
    if key not in config:
        config[key] = {}

    settings = config[key]
    settings.setdefault("role_ids", [])
    settings.setdefault("include_bots", False)
    settings.setdefault("timing", "join")
    return settings


def can_manage(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_roles


async def apply_roles(member: discord.Member, settings, reason="Autorole"):
    """Give a member every configured role they don't already have.

    Returns the roles actually handed out, which callers use for counting.
    Reading member.roles straight after add_roles would be stale, since the
    cache only updates when the gateway echoes the change back.
    """
    if member.bot and not settings.get("include_bots"):
        return []

    guild = member.guild
    me = guild.me

    if not me.guild_permissions.manage_roles:
        return []

    wanted = []
    for role_id in settings.get("role_ids", []):
        role = guild.get_role(role_id)
        if role is None or role in member.roles:
            continue
        if not assignable_now(role, me):
            continue
        wanted.append(role)

    if not wanted:
        return []

    try:
        await member.add_roles(*wanted, reason=reason)
    except (discord.Forbidden, discord.HTTPException):
        return []

    return wanted


class AutoroleSelect(discord.ui.RoleSelect):
    def __init__(self, panel):
        super().__init__(
            placeholder="Pick the roles new members should get",
            min_values=0,
            max_values=MAX_ROLES,
            row=0,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        accepted = []
        rejected = []

        for role in self.values:
            problem = check_role_assignable(role, interaction.user)
            if problem:
                rejected.append(f"**{role.name}** - {problem}")
            else:
                accepted.append(role.id)

        await interaction.response.defer()

        self.panel.settings["role_ids"] = accepted
        save()
        await self.panel.refresh()

        if rejected:
            await interaction.followup.send(
                "These were skipped:\n" + "\n".join(rejected),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class AutorolePanel(discord.ui.View):
    def __init__(self, ctx, settings):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.settings = settings
        self.message = None
        self.add_item(AutoroleSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This panel isn't yours.", ephemeral=True
            )
            return False
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need Administrator or Manage Roles permission.", ephemeral=True
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

    def status_embed(self):
        guild = self.ctx.guild
        settings = self.settings

        roles = []
        missing = 0
        for role_id in settings.get("role_ids", []):
            role = guild.get_role(role_id)
            if role is None:
                missing += 1
            else:
                roles.append(role)

        if roles:
            listed = " ".join(r.mention for r in roles)
        else:
            listed = "none set, nothing will be given out"

        lines = [
            f"**Roles** - {listed}",
            f"**Timing** - {TIMING_LABELS[settings.get('timing', 'join')]}",
            f"**Bots** - {'included' if settings.get('include_bots') else 'skipped'}",
        ]

        if missing:
            lines.append("")
            lines.append(f"{missing} configured role(s) no longer exist.")

        if settings.get("timing") == "screened":
            lines.append("")
            lines.append(
                "Screened timing only fires if membership screening is on. "
                "With it off, nobody is ever marked as accepting the rules "
                "and roles are never given."
            )

        return discord.Embed(
            title="Autorole",
            description="\n".join(lines),
            color=discord.Color.dark_theme(),
        )

    async def refresh(self):
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.status_embed(), view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Change Timing", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_timing(self, interaction, button):
        await interaction.response.defer()
        current = self.settings.get("timing", "join")
        self.settings["timing"] = "screened" if current == "join" else "join"
        save()
        await self.refresh()

    @discord.ui.button(label="Toggle Bots", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_bots(self, interaction, button):
        await interaction.response.defer()
        self.settings["include_bots"] = not self.settings.get("include_bots")
        save()
        await self.refresh()

    @discord.ui.button(label="Apply To Everyone", style=discord.ButtonStyle.primary, row=2)
    async def backfill(self, interaction, button):
        if not self.settings.get("role_ids"):
            await interaction.response.send_message(
                "Pick at least one role first.", ephemeral=True
            )
            return

        await interaction.response.defer()

        touched = 0
        for member in list(interaction.guild.members):
            added = await apply_roles(
                member, self.settings, reason="Autorole backfill"
            )
            if added:
                touched += 1

        await interaction.followup.send(
            f"Applied to {touched} member(s) who were missing a role.",
            ephemeral=True,
        )

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.danger, row=2)
    async def disable(self, interaction, button):
        await interaction.response.defer()
        config.pop(str(interaction.guild.id), None)
        save()

        self.settings["role_ids"] = []

        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(
                    content="Autorole disabled. Existing roles were left in place.",
                    embed=self.status_embed(),
                    view=self,
                )
            except discord.HTTPException:
                pass
        self.stop()


class Autorole(commands.Cog):
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
        else:
            await ctx.send(f"Something went wrong: {error}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        settings = get_settings(member.guild.id)
        if settings is None:
            return

        if settings.get("timing") == "screened" and member.pending:
            return

        await apply_roles(member, settings)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Catch the moment someone finishes membership screening."""
        if not before.pending or after.pending:
            return

        settings = get_settings(after.guild.id)
        if settings is None or settings.get("timing") != "screened":
            return

        await apply_roles(after, settings, reason="Autorole after screening")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        settings = get_settings(role.guild.id)
        if settings is None:
            return

        if role.id in settings.get("role_ids", []):
            settings["role_ids"].remove(role.id)
            save()

    @commands.command(name="autorole", aliases=["arole"])
    async def autorole(self, ctx: commands.Context):
        settings = ensure_settings(ctx.guild.id)

        view = AutorolePanel(ctx, settings)
        message = await ctx.send(
            embed=view.status_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = message


async def setup(bot):
    await bot.add_cog(Autorole(bot))