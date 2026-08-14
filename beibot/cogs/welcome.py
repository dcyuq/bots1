import discord
from discord.ext import commands
from storage import Store

_store = Store("welcome.json")
config = _store.load()

EVENTS = [
    ("join", "Welcome", "Sent when someone joins."),
    ("leave", "Goodbye", "Sent when someone leaves, is kicked or is banned."),
]

EVENT_LABELS = {key: label for key, label, _ in EVENTS}

MODES = [
    ("embed_title", "Embed with header", "Full embed with a title at the top."),
    ("embed_plain", "Embed without header", "Same embed, no title. Slimmer."),
    ("text", "Plain text", "No embed. A normal message."),
]

MODE_LABELS = {key: label for key, label, _ in MODES}

TIMING_LABELS = {
    "join": "As soon as they join",
    "screened": "After they accept the rules",
}

PLACEHOLDERS = [
    ("{mention}", "Pings the member"),
    ("{user}", "Their display name"),
    ("{name}", "Their username"),
    ("{tag}", "Their full username with discriminator"),
    ("{id}", "Their user ID"),
    ("{server}", "The server name"),
    ("{count}", "Member count after the change"),
]

URL_TOKENS = [
    ("{avatar}", "The member's avatar"),
    ("{server_icon}", "The server icon"),
]


def save():
    _store.save(config)


def default_event(kind):
    return {
        "enabled": False,
        "channel_id": None,
        "mode": "embed_title",
        "content": "",
        "title": "Welcome" if kind == "join" else "Goodbye",
        "description": (
            "{mention} joined. You're member number {count}."
            if kind == "join"
            else "{user} left the server."
        ),
        "color": 0x2B2D31,
        "image_url": None,
        "thumbnail_url": "{avatar}",
        "ping": kind == "join",
        "skip_bots": True,
        "timing": "join",
    }


def ensure_settings(guild_id):
    key = str(guild_id)
    if key not in config:
        config[key] = {}

    settings = config[key]
    for kind, _, _ in EVENTS:
        if kind not in settings:
            settings[kind] = default_event(kind)
        else:
            for field, value in default_event(kind).items():
                settings[kind].setdefault(field, value)
    return settings


def get_event(guild_id, kind):
    settings = config.get(str(guild_id))
    if settings is None:
        return None
    return settings.get(kind)


def can_manage(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild


def parse_color(text, fallback=0x2B2D31):
    if not text:
        return fallback
    text = text.strip().lstrip("#")
    try:
        value = int(text, 16)
    except ValueError:
        return fallback
    return value if 0 <= value <= 0xFFFFFF else fallback


def clean_url(text):
    """Accept a real URL or one of the avatar tokens, otherwise nothing."""
    if not text:
        return None
    text = text.strip()
    if text in {token for token, _ in URL_TOKENS}:
        return text
    if text.lower().startswith(("http://", "https://")):
        return text
    return None


def render(text, member, guild, count):
    if not text:
        return text
    return (
        text.replace("{mention}", member.mention)
        .replace("{user}", member.display_name)
        .replace("{name}", member.name)
        .replace("{tag}", str(member))
        .replace("{id}", str(member.id))
        .replace("{server}", guild.name)
        .replace("{count}", str(count))
    )


def render_url(value, member, guild):
    if not value:
        return None
    if value == "{avatar}":
        return member.display_avatar.url
    if value == "{server_icon}":
        return guild.icon.url if guild.icon else None
    return value


def build_payload(event, member, guild, count):
    """Return (content, embed) for one event, with placeholders filled in."""
    content = render(event.get("content", ""), member, guild, count)
    mode = event.get("mode", "embed_title")

    if mode == "text":
        body = render(event.get("description", ""), member, guild, count)
        combined = "\n".join(part for part in (content, body) if part)
        return combined[:2000], None

    embed = discord.Embed(
        description=render(event.get("description", ""), member, guild, count)[:4096],
        color=event.get("color", 0x2B2D31),
    )

    if mode == "embed_title" and event.get("title"):
        embed.title = render(event["title"], member, guild, count)[:256]

    image = render_url(event.get("image_url"), member, guild)
    if image:
        embed.set_image(url=image)

    thumb = render_url(event.get("thumbnail_url"), member, guild)
    if thumb:
        embed.set_thumbnail(url=thumb)

    return (content[:2000] if content else None), embed


def event_problems(event, guild):
    problems = []

    if not event.get("channel_id"):
        problems.append("a channel")
    else:
        channel = guild.get_channel(event["channel_id"])
        if channel is None:
            problems.append("a channel that still exists")
        else:
            perms = channel.permissions_for(guild.me)
            if not perms.send_messages:
                problems.append("permission for me to post there")
            elif event.get("mode") != "text" and not perms.embed_links:
                problems.append("permission for me to embed links there")

    has_body = bool(event.get("description", "").strip())
    has_content = bool(event.get("content", "").strip())

    if event.get("mode") == "text":
        if not has_body and not has_content:
            problems.append("some message text")
    elif not has_body:
        problems.append("an embed description")

    return problems


async def dispatch(member, guild, kind, count):
    event = get_event(guild.id, kind)
    if event is None or not event.get("enabled"):
        return

    if member.bot and event.get("skip_bots", True):
        return

    channel = guild.get_channel(event.get("channel_id"))
    if channel is None:
        return

    perms = channel.permissions_for(guild.me)
    if not perms.send_messages:
        return
    if event.get("mode") != "text" and not perms.embed_links:
        return

    content, embed = build_payload(event, member, guild, count)
    if not content and embed is None:
        return

    if event.get("ping"):
        mentions = discord.AllowedMentions(everyone=False, roles=False, users=True)
    else:
        mentions = discord.AllowedMentions.none()

    try:
        await channel.send(content=content, embed=embed, allowed_mentions=mentions)
    except (discord.Forbidden, discord.HTTPException):
        pass


class TextModal(discord.ui.Modal, title="Message Text"):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        event = panel.event

        self.f_body = discord.ui.TextInput(
            label="Message",
            default=event.get("description", ""),
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=True,
        )
        self.add_item(self.f_body)

    async def on_submit(self, interaction):
        await interaction.response.defer()
        self.panel.event["description"] = self.f_body.value
        save()
        await self.panel.refresh()


class EmbedModal(discord.ui.Modal, title="Embed Content"):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        event = panel.event

        self.f_content = discord.ui.TextInput(
            label="Text above the embed",
            default=event.get("content", ""),
            placeholder="Put {mention} here if you want the ping to notify",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=False,
        )
        self.f_title = discord.ui.TextInput(
            label="Title",
            default=event.get("title", ""),
            placeholder="Ignored if you picked the no header style",
            max_length=256,
            required=False,
        )
        self.f_desc = discord.ui.TextInput(
            label="Description",
            default=event.get("description", ""),
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )

        for item in (self.f_content, self.f_title, self.f_desc):
            self.add_item(item)

    async def on_submit(self, interaction):
        await interaction.response.defer()
        event = self.panel.event
        event["content"] = self.f_content.value
        event["title"] = self.f_title.value
        event["description"] = self.f_desc.value
        save()
        await self.panel.refresh()


class StyleModal(discord.ui.Modal, title="Colour & Images"):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        event = panel.event

        self.f_color = discord.ui.TextInput(
            label="Colour hex",
            default=f"{event.get('color', 0x2B2D31):06X}",
            placeholder="5865F2",
            max_length=7,
            required=False,
        )
        self.f_image = discord.ui.TextInput(
            label="Large image URL",
            default=event.get("image_url") or "",
            placeholder="https://... or {avatar} or {server_icon}",
            required=False,
        )
        self.f_thumb = discord.ui.TextInput(
            label="Thumbnail URL",
            default=event.get("thumbnail_url") or "",
            placeholder="https://... or {avatar} or {server_icon}",
            required=False,
        )

        for item in (self.f_color, self.f_image, self.f_thumb):
            self.add_item(item)

    async def on_submit(self, interaction):
        await interaction.response.defer()
        event = self.panel.event
        event["color"] = parse_color(self.f_color.value, event.get("color", 0x2B2D31))
        event["image_url"] = clean_url(self.f_image.value)
        event["thumbnail_url"] = clean_url(self.f_thumb.value)
        save()
        await self.panel.refresh()


class EventSelect(discord.ui.Select):
    def __init__(self, panel):
        self.panel = panel
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                description=blurb,
                default=(key == panel.kind),
            )
            for key, label, blurb in EVENTS
        ]
        super().__init__(placeholder="Which message?", options=options, row=0)

    async def callback(self, interaction):
        await interaction.response.defer()
        self.panel.switch(self.values[0])
        await self.panel.refresh()


class ModeSelect(discord.ui.Select):
    def __init__(self, panel):
        self.panel = panel
        current = panel.event.get("mode", "embed_title")
        options = [
            discord.SelectOption(
                label=label, value=key, description=blurb, default=(key == current)
            )
            for key, label, blurb in MODES
        ]
        super().__init__(placeholder="Message style", options=options, row=2)

    async def callback(self, interaction):
        await interaction.response.defer()
        self.panel.event["mode"] = self.values[0]
        save()
        await self.panel.refresh()


class OptionsView(discord.ui.View):
    """Ephemeral sub-panel for the switches that don't need the main view."""

    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel

    async def interaction_check(self, interaction):
        return interaction.user.id == self.panel.ctx.author.id

    def blurb(self):
        event = self.panel.event
        lines = [
            f"**{EVENT_LABELS[self.panel.kind]} options**",
            "",
            f"**Ping** - {'the mention notifies them' if event.get('ping') else 'silent'}",
            f"**Bots** - {'skipped' if event.get('skip_bots', True) else 'announced too'}",
        ]
        if self.panel.kind == "join":
            lines.append(
                f"**Timing** - {TIMING_LABELS[event.get('timing', 'join')]}"
            )
        return "\n".join(lines)

    async def redraw(self, interaction):
        save()
        await self.panel.refresh()
        await interaction.response.edit_message(content=self.blurb(), view=self)

    @discord.ui.button(label="Toggle Ping", style=discord.ButtonStyle.secondary)
    async def toggle_ping(self, interaction, button):
        self.panel.event["ping"] = not self.panel.event.get("ping")
        await self.redraw(interaction)

    @discord.ui.button(label="Toggle Bots", style=discord.ButtonStyle.secondary)
    async def toggle_bots(self, interaction, button):
        self.panel.event["skip_bots"] = not self.panel.event.get("skip_bots", True)
        await self.redraw(interaction)

    @discord.ui.button(label="Toggle Timing", style=discord.ButtonStyle.secondary)
    async def toggle_timing(self, interaction, button):
        if self.panel.kind != "join":
            await interaction.response.send_message(
                "Timing only applies to the welcome message.", ephemeral=True
            )
            return
        current = self.panel.event.get("timing", "join")
        self.panel.event["timing"] = "screened" if current == "join" else "join"
        await self.redraw(interaction)


class WelcomePanel(discord.ui.View):
    def __init__(self, ctx, settings, kind="join"):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.settings = settings
        self.kind = kind
        self.message = None
        self.add_item(EventSelect(self))
        self.add_item(ModeSelect(self))

    @property
    def event(self):
        return self.settings[self.kind]

    def switch(self, kind):
        self.kind = kind
        for item in list(self.children):
            if isinstance(item, (EventSelect, ModeSelect)):
                self.remove_item(item)
        self.add_item(EventSelect(self))
        self.add_item(ModeSelect(self))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This panel isn't yours.", ephemeral=True
            )
            return False
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need Administrator or Manage Server permission.", ephemeral=True
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
        event = self.event
        guild = self.ctx.guild
        channel = (
            guild.get_channel(event["channel_id"]) if event.get("channel_id") else None
        )

        body = event.get("description") or "nothing written yet"
        preview = body if len(body) <= 200 else body[:200] + "..."

        lines = [
            f"**Status** - {'on' if event.get('enabled') else 'off'}",
            f"**Channel** - {channel.mention if channel else 'not set'}",
            f"**Style** - {MODE_LABELS.get(event.get('mode'), 'Embed with header')}",
        ]

        if event.get("content"):
            lines.append("")
            lines.append("**Above the embed**")
            lines.append(event["content"][:200])

        lines.append("")
        lines.append("**Body**")
        lines.append(preview)

        problems = event_problems(event, guild)
        if problems:
            lines.append("")
            lines.append("Still needs: " + ", ".join(problems))

        return discord.Embed(
            title=f"{EVENT_LABELS[self.kind]} Message",
            description="\n".join(lines),
            color=event.get("color", 0x2B2D31),
        )

    def sync_labels(self):
        on = bool(self.event.get("enabled"))
        self.toggle_enabled.label = "Turn Off" if on else "Turn On"
        self.toggle_enabled.style = (
            discord.ButtonStyle.danger if on else discord.ButtonStyle.success
        )

    async def refresh(self):
        if self.message is None:
            return
        self.sync_labels()
        try:
            await self.message.edit(embed=self.status_embed(), view=self)
        except discord.HTTPException:
            pass

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Which channel?",
        row=1,
    )
    async def pick_channel(self, interaction, select):
        await interaction.response.defer()
        self.event["channel_id"] = select.values[0].id
        save()
        await self.refresh()

    @discord.ui.button(label="Write", style=discord.ButtonStyle.primary, row=3)
    async def write(self, interaction, button):
        if self.event.get("mode") == "text":
            await interaction.response.send_modal(TextModal(self))
        else:
            await interaction.response.send_modal(EmbedModal(self))

    @discord.ui.button(label="Colour & Images", style=discord.ButtonStyle.secondary, row=3)
    async def style(self, interaction, button):
        if self.event.get("mode") == "text":
            await interaction.response.send_message(
                "Plain text mode has no colour or images.", ephemeral=True
            )
            return
        await interaction.response.send_modal(StyleModal(self))

    @discord.ui.button(label="Preview", style=discord.ButtonStyle.secondary, row=3)
    async def preview(self, interaction, button):
        count = interaction.guild.member_count or len(interaction.guild.members)
        content, embed = build_payload(
            self.event, interaction.user, interaction.guild, count
        )

        if not content and embed is None:
            await interaction.response.send_message(
                "Nothing written yet.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            content=content,
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Turn On", style=discord.ButtonStyle.success, row=4)
    async def toggle_enabled(self, interaction, button):
        if self.event.get("enabled"):
            await interaction.response.defer()
            self.event["enabled"] = False
            save()
            await self.refresh()
            return

        problems = event_problems(self.event, interaction.guild)
        if problems:
            await interaction.response.send_message(
                "Still needs: " + ", ".join(problems), ephemeral=True
            )
            return

        await interaction.response.defer()
        self.event["enabled"] = True
        save()
        await self.refresh()

    @discord.ui.button(label="Options", style=discord.ButtonStyle.secondary, row=4)
    async def options(self, interaction, button):
        view = OptionsView(self)
        await interaction.response.send_message(
            view.blurb(), view=view, ephemeral=True
        )

    @discord.ui.button(label="Placeholders", style=discord.ButtonStyle.secondary, row=4)
    async def placeholders(self, interaction, button):
        embed = discord.Embed(
            title="Placeholders",
            description="\n".join(
                f"`{token}` - {meaning}" for token, meaning in PLACEHOLDERS
            ),
            color=discord.Color.dark_theme(),
        )
        embed.add_field(
            name="Image fields also accept",
            value="\n".join(
                f"`{token}` - {meaning}" for token, meaning in URL_TOKENS
            ),
            inline=False,
        )
        embed.set_footer(
            text="A mention inside an embed will not notify anyone. "
            "Put it in the text above the embed instead."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if can_manage(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_guild"])

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need Administrator or Manage Server permission.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command only works in a server.")
        else:
            await ctx.send(f"Something went wrong: {error}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        event = get_event(member.guild.id, "join")
        if event is None:
            return
        if event.get("timing") == "screened" and member.pending:
            return
        count = member.guild.member_count or len(member.guild.members)
        await dispatch(member, member.guild, "join", count)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if not before.pending or after.pending:
            return
        event = get_event(after.guild.id, "join")
        if event is None or event.get("timing") != "screened":
            return
        count = after.guild.member_count or len(after.guild.members)
        await dispatch(after, after.guild, "join", count)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        count = member.guild.member_count or len(member.guild.members)
        await dispatch(member, member.guild, "leave", count)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        settings = config.get(str(channel.guild.id))
        if settings is None:
            return
        changed = False
        for kind, _, _ in EVENTS:
            event = settings.get(kind)
            if event and event.get("channel_id") == channel.id:
                event["channel_id"] = None
                event["enabled"] = False
                changed = True
        if changed:
            save()

    async def open_panel(self, ctx, kind):
        settings = ensure_settings(ctx.guild.id)
        save()

        view = WelcomePanel(ctx, settings, kind)
        view.sync_labels()
        message = await ctx.send(
            embed=view.status_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = message

    @commands.command(name="welcome", aliases=["wel"])
    async def welcome(self, ctx: commands.Context):
        await self.open_panel(ctx, "join")

    @commands.command(name="goodbye", aliases=["leave"])
    async def goodbye(self, ctx: commands.Context):
        await self.open_panel(ctx, "leave")


async def setup(bot):
    await bot.add_cog(Welcome(bot))