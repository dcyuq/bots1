import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import embeds
from storage import Store

log = logging.getLogger(__name__)

ACCENT = embeds.ACCENT.value
SWEEP_MINUTES = 10
MIN_KEYWORD = 2

_store = Store("vanity.json")
config = _store.load()


def save():
    _store.save(config)


def get_cfg(guild_id):
    return config.get(str(guild_id))


def ensure(guild_id):
    return config.setdefault(
        str(guild_id),
        {
            "enabled": False,
            "keyword": None,
            "role_id": None,
            "channel_id": None,
            "status": True,
            "tag": True,
        },
    )


def can_manage(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_roles


def assignable(role, me):
    return (
        role is not None
        and me is not None
        and me.guild_permissions.manage_roles
        and not role.managed
        and role < me.top_role
    )


def custom_status(member: discord.Member) -> str:
    for activity in member.activities:
        if isinstance(activity, discord.CustomActivity):
            return activity.name or ""
    return ""


def tag_guild_id(obj):
    """The id of the guild whose server tag this user/member is displaying, or None."""
    primary = getattr(obj, "primary_guild", None)
    if primary is None or not getattr(primary, "identity_enabled", False):
        return None
    return getattr(primary, "id", None)


def wearing_tag(member: discord.Member) -> bool:
    """True when the member is publicly displaying *this* server's tag."""
    return tag_guild_id(member) == member.guild.id


def _row(*items):
    row = discord.ui.ActionRow()
    for item in items:
        row.add_item(item)
    return row


class RolePick(discord.ui.RoleSelect):
    def __init__(self, panel):
        self.panel = panel
        super().__init__(placeholder="Pick the role to award", max_values=1)

    async def callback(self, interaction):
        role = interaction.guild.get_role(self.values[0].id)
        if not assignable(role, interaction.guild.me):
            await interaction.response.send_message(
                embed=embeds.error(f"i can't manage **{role.name}** - move my role above it."),
                ephemeral=True,
            )
            return
        self.panel.cfg()["role_id"] = role.id
        await self.panel.refresh(interaction)


class ChannelPick(discord.ui.ChannelSelect):
    def __init__(self, panel):
        self.panel = panel
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder="Announce channel (optional)",
            min_values=0,
            max_values=1,
        )

    async def callback(self, interaction):
        self.panel.cfg()["channel_id"] = self.values[0].id if self.values else None
        await self.panel.refresh(interaction)


class KeywordModal(discord.ui.Modal, title="Vanity keyword"):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.field = discord.ui.TextInput(
            label="Keyword to watch for in a status",
            default=panel.cfg().get("keyword") or "",
            placeholder="/dainted",
            max_length=100,
            required=True,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction):
        value = self.field.value.strip()
        if len(value) < MIN_KEYWORD:
            await interaction.response.send_message(
                embed=embeds.error(f"use at least **{MIN_KEYWORD} characters** - short keywords match everything."),
                ephemeral=True,
            )
            return
        self.panel.cfg()["keyword"] = value
        await interaction.response.defer()
        await self.panel.refresh()


class DetectRow(discord.ui.ActionRow):
    """The two ways to earn the role, each independently toggleable."""

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        cfg = panel.cfg()
        status_on = cfg.get("status", True)
        tag_on = cfg.get("tag", True)
        self.status.label = f"Status: {'on' if status_on else 'off'}"
        self.status.style = discord.ButtonStyle.success if status_on else discord.ButtonStyle.secondary
        self.tag.label = f"Server tag: {'on' if tag_on else 'off'}"
        self.tag.style = discord.ButtonStyle.success if tag_on else discord.ButtonStyle.secondary

    @discord.ui.button(label="Keyword", style=discord.ButtonStyle.primary)
    async def keyword(self, interaction, button):
        await interaction.response.send_modal(KeywordModal(self.panel))

    @discord.ui.button(label="Status", style=discord.ButtonStyle.success)
    async def status(self, interaction, button):
        cfg = self.panel.cfg()
        cfg["status"] = not cfg.get("status", True)
        await self.panel.refresh(interaction)

    @discord.ui.button(label="Server tag", style=discord.ButtonStyle.success)
    async def tag(self, interaction, button):
        cfg = self.panel.cfg()
        cfg["tag"] = not cfg.get("tag", True)
        await self.panel.refresh(interaction)


class ControlRow(discord.ui.ActionRow):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        on = bool(panel.cfg().get("enabled"))
        self.toggle.label = "Turn off" if on else "Turn on"
        self.toggle.style = discord.ButtonStyle.danger if on else discord.ButtonStyle.success

    @discord.ui.button(label="Turn on", style=discord.ButtonStyle.success)
    async def toggle(self, interaction, button):
        cfg = self.panel.cfg()
        if not cfg.get("enabled"):
            problems = []
            if not cfg.get("role_id"):
                problems.append("pick a role")
            status_active = cfg.get("status", True) and bool(cfg.get("keyword"))
            tag_active = cfg.get("tag", True)
            if not (status_active or tag_active):
                if cfg.get("status", True) and not cfg.get("keyword"):
                    problems.append("set a keyword or turn on **Server tag**")
                else:
                    problems.append("turn on **Status** or **Server tag**")
            if problems:
                await interaction.response.send_message(
                    embed=embeds.error("still needs: " + ", ".join(problems)), ephemeral=True
                )
                return
        cfg["enabled"] = not cfg.get("enabled")
        await self.panel.refresh(interaction)

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.danger)
    async def reset(self, interaction, button):
        config.pop(str(self.panel.guild.id), None)
        save()
        await self.panel.refresh(interaction)


class VanityPanel(discord.ui.LayoutView):
    def __init__(self, guild, author_id, message=None):
        super().__init__(timeout=600)
        self.guild = guild
        self.author_id = author_id
        self.message = message
        self.build()

    def cfg(self):
        return ensure(self.guild.id)

    def build(self, interactive=True):
        self.clear_items()
        cfg = self.cfg()
        role = self.guild.get_role(cfg["role_id"]) if cfg.get("role_id") else None
        channel = self.guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None

        if cfg.get("status", True):
            keyword = cfg.get("keyword")
            status_line = f"on · `{keyword}`" if keyword else "on · keyword not set"
        else:
            status_line = "off"

        container = discord.ui.Container(accent_colour=ACCENT)
        container.add_item(discord.ui.TextDisplay("## Vanity role setup"))
        container.add_item(discord.ui.TextDisplay(
            f"**Enabled** — {'on' if cfg.get('enabled') else 'off'}\n"
            f"**Status keyword** — {status_line}\n"
            f"**Server tag** — {'on' if cfg.get('tag', True) else 'off'}\n"
            f"**Role** — {role.mention if role else 'not set'}\n"
            f"**Announce** — {channel.mention if channel else 'off'}"
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "-# members get the role while the keyword is in their **custom status** "
            "or they're wearing this **server's tag**, and lose it when neither is true"
        ))
        self.add_item(container)

        if not interactive:
            return

        self.add_item(_row(RolePick(self)))
        self.add_item(_row(ChannelPick(self)))
        self.add_item(DetectRow(self))
        self.add_item(ControlRow(self))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=embeds.error("this panel isn't yours - run the command yourself."),
                ephemeral=True,
            )
            return False
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                embed=embeds.error("you need **Manage Roles** for that."), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        self.build(interactive=False)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def refresh(self, interaction=None):
        save()
        self.build()
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(view=self)
        elif self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Vanity(commands.Cog):
    """Award a role for a keyword in someone's status or this server's tag."""

    def __init__(self, bot):
        self.bot = bot
        self.sweep.start()

    def cog_unload(self):
        self.sweep.cancel()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if can_manage(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_roles"])

    async def sync_member(self, member: discord.Member):
        if member.bot:
            return
        cfg = get_cfg(member.guild.id)
        if not cfg or not cfg.get("enabled") or not cfg.get("role_id"):
            return
        role = member.guild.get_role(cfg["role_id"])
        if not assignable(role, member.guild.me):
            return

        keyword = cfg.get("keyword")
        status_on = cfg.get("status", True) and bool(keyword)
        tag_on = cfg.get("tag", True)
        if not (status_on or tag_on):
            return

        wears_tag = tag_on and wearing_tag(member)

        if member.status is discord.Status.offline:
            if wears_tag:
                should = True
            elif not status_on:
                should = False
            else:
                return
        else:
            in_status = status_on and keyword.lower() in custom_status(member).lower()
            should = wears_tag or in_status

        has = role in member.roles
        try:
            if should and not has:
                await member.add_roles(role, reason="vanity: status keyword or server tag")
                await self._announce(member, cfg, tag=wears_tag)
            elif not should and has:
                await member.remove_roles(role, reason="vanity: no longer repping")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _announce(self, member, cfg, *, tag=False):
        channel = member.guild.get_channel(cfg.get("channel_id")) if cfg.get("channel_id") else None
        if channel is None:
            return
        if tag:
            text = f"{member.mention} is repping the **server tag** — role granted."
        else:
            text = f"{member.mention} is repping **{cfg['keyword']}** — role granted."
        try:
            await channel.send(
                text,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if after.guild is None:
            return
        if custom_status(before) == custom_status(after):
            return
        await self.sync_member(after)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        if tag_guild_id(before) == tag_guild_id(after):
            return
        for guild_id, cfg in list(config.items()):
            if not cfg.get("enabled") or not cfg.get("tag", True) or not cfg.get("role_id"):
                continue
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue
            member = guild.get_member(after.id)
            if member is not None:
                await self.sync_member(member)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.sync_member(member)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        cfg = get_cfg(role.guild.id)
        if cfg and cfg.get("role_id") == role.id:
            cfg["role_id"] = None
            cfg["enabled"] = False
            save()

    @tasks.loop(minutes=SWEEP_MINUTES)
    async def sweep(self):
        for guild_id, cfg in list(config.items()):
            if not cfg.get("enabled"):
                continue
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue
            role = guild.get_role(cfg.get("role_id"))
            if not assignable(role, guild.me):
                continue
            for member in guild.members:
                await self.sync_member(member)

    @sweep.before_loop
    async def before_sweep(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="vanity", aliases=["vr"],
                             description="Set up the vanity status role.")
    @app_commands.default_permissions(manage_roles=True)
    @commands.guild_only()
    async def vanity(self, ctx):
        panel = VanityPanel(ctx.guild, ctx.author.id)
        panel.message = await ctx.send(view=panel)


async def setup(bot):
    await bot.add_cog(Vanity(bot))