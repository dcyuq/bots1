import discord
from discord.ext import commands
from storage import Store, IntKeyStore
import time
import uuid

_config_store = Store("tickets_config.json")
_ticket_store = IntKeyStore("tickets.json")
_log_store = IntKeyStore("ticket_logs.json")

config = _config_store.load()
tickets = _ticket_store.load()
logs = _log_store.load()

CATEGORY_LIMIT = 50
MAX_BUTTONS = 10
MAX_QUESTIONS = 5
MAX_STAFF_ROLES = 10

STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}

STYLE_ALIASES = {
    "blurple": "primary",
    "grey": "secondary",
    "gray": "secondary",
    "green": "success",
    "red": "danger",
}

STYLE_CHOICES = [
    ("primary", "Blurple", "Discord's brand colour. Good for the main action."),
    ("secondary", "Grey", "Understated. Good for secondary options."),
    ("success", "Green", "Reads as positive or helpful."),
    ("danger", "Red", "Reads as serious. Good for reports or appeals."),
]


def canonical_style(value):
    value = (value or "primary").strip().lower()
    value = STYLE_ALIASES.get(value, value)
    return value if value in STYLES else "primary"


def style_label(value):
    value = canonical_style(value)
    for key, label, _ in STYLE_CHOICES:
        if key == value:
            return label
    return "Blurple"

PANEL_MODES = [
    (
        "embed_title",
        "Embed with header",
        "Full embed with the big title text at the top.",
    ),
    (
        "embed_plain",
        "Embed without header",
        "Same embed, no title. Slimmer.",
    ),
    (
        "text",
        "Plain text",
        "No embed. A normal message with buttons under it.",
    ),
    (
        "bare",
        "Buttons only",
        "No text and no embed. Nothing but the buttons.",
    ),
]

DEFAULT_PANEL = {
    "channel_id": None,
    "message_id": None,
    "mode": "embed_title",
    "title": "Support Tickets",
    "description": "Click a button below to open a private ticket.",
    "color": 0x2B2D31,
    "image_url": None,
    "thumbnail_url": None,
}


def panel_mode(panel):
    mode = (panel or {}).get("mode", "embed_title")
    return mode if mode in {m[0] for m in PANEL_MODES} else "embed_title"


def panel_mode_label(panel):
    current = panel_mode(panel)
    for key, label, _ in PANEL_MODES:
        if key == current:
            return label
    return "Embed with header"

DEFAULT_BUTTON = {
    "label": "Create Ticket",
    "style": "primary",
    "category_id": None,
    "welcome": "Describe your issue and someone will be with you shortly.",
    "questions": [],
}


def save_config():
    _config_store.save(config)


def save_tickets():
    _ticket_store.save(tickets)


def save_logs():
    _log_store.save(logs)


def get_config(guild_id):
    return config.get(str(guild_id))


def ensure_config(guild_id):
    key = str(guild_id)
    if key not in config:
        config[key] = {
            "staff_role_ids": [],
            "log_channel_id": None,
            "category_id": None,
            "counter": 0,
            "panel": dict(DEFAULT_PANEL),
            "buttons": [],
        }

    settings = config[key]
    settings.setdefault("panel", dict(DEFAULT_PANEL))
    settings.setdefault("buttons", [])
    settings.setdefault("counter", 0)
    settings.setdefault("staff_role_ids", [])
    settings["panel"].setdefault("mode", "embed_title")

    legacy = settings.pop("staff_role_id", None)
    if legacy and legacy not in settings["staff_role_ids"]:
        settings["staff_role_ids"].append(legacy)

    return settings


def staff_role_ids(settings):
    if not settings:
        return []
    ids = settings.get("staff_role_ids")
    if ids:
        return ids
    legacy = settings.get("staff_role_id")
    return [legacy] if legacy else []


def staff_roles(guild, settings):
    found = []
    for role_id in staff_role_ids(settings):
        role = guild.get_role(role_id)
        if role is not None:
            found.append(role)
    return found


def is_configured(settings):
    return bool(
        settings
        and settings.get("category_id")
        and staff_role_ids(settings)
        and settings.get("log_channel_id")
    )


def can_manage(member):
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild


def is_staff(member, settings):
    if member.guild_permissions.administrator:
        return True
    allowed = set(staff_role_ids(settings))
    return any(r.id in allowed for r in member.roles)


def find_button(settings, key):
    for entry in settings.get("buttons", []):
        if entry["key"] == key:
            return entry
    return None


def open_ticket_for(guild_id, user_id):
    for channel_id, data in tickets.items():
        if data["guild_id"] == guild_id and data["opener_id"] == user_id:
            return channel_id
    return None


def duration_text(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


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
    if not text:
        return None
    text = text.strip()
    if text.lower().startswith(("http://", "https://")):
        return text
    return None


def build_panel_view(guild_id, settings):
    """Buttons-only panels need a LayoutView, everything else a normal View."""
    if panel_mode(settings["panel"]) == "bare":
        return BarePanelView(guild_id, settings["buttons"])
    return PanelView(guild_id, settings["buttons"])


def build_panel_payload(settings):
    """Return (content, embed) for the panel message based on its mode."""
    panel = settings["panel"]
    mode = panel_mode(panel)

    if mode == "bare":
        return None, None

    if mode == "text":
        return panel["description"][:2000], None

    embed = discord.Embed(
        description=panel["description"][:4096],
        color=panel["color"],
    )
    if mode == "embed_title":
        embed.title = panel["title"][:256]
    if panel.get("image_url"):
        embed.set_image(url=panel["image_url"])
    if panel.get("thumbnail_url"):
        embed.set_thumbnail(url=panel["thumbnail_url"])

    return None, embed


async def send_log(guild, embed):
    settings = get_config(guild.id)
    if not settings:
        return
    channel = guild.get_channel(settings.get("log_channel_id"))
    if channel is None:
        return
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        pass


async def create_ticket(interaction, button_data, answers):
    guild = interaction.guild
    settings = get_config(guild.id)

    existing = open_ticket_for(guild.id, interaction.user.id)
    if existing is not None:
        channel = guild.get_channel(existing)
        if channel is not None:
            await interaction.followup.send(
                f"You already have an open ticket: {channel.mention}",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        tickets.pop(existing, None)
        save_tickets()

    category_id = button_data.get("category_id") or settings["category_id"]
    category = guild.get_channel(category_id)
    if not isinstance(category, discord.CategoryChannel):
        await interaction.followup.send(
            "The ticket category is missing. Ask an admin to run setup again.",
            ephemeral=True,
        )
        return

    if len(category.channels) >= CATEGORY_LIMIT:
        await interaction.followup.send(
            "That ticket category is full. Ask staff to close some tickets.",
            ephemeral=True,
        )
        return

    roles = staff_roles(guild, settings)

    number = settings.get("counter", 0) + 1
    settings["counter"] = number
    save_config()

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            read_message_history=True,
        ),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            attach_files=True,
            read_message_history=True,
        ),
    }
    for role in roles:
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )

    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{number:04d}",
            category=category,
            overwrites=overwrites,
            reason=f"Ticket opened by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to create channels there.", ephemeral=True
        )
        return
    except discord.HTTPException as exc:
        await interaction.followup.send(
            f"Discord rejected the request: {exc}", ephemeral=True
        )
        return

    tickets[channel.id] = {
        "guild_id": guild.id,
        "opener_id": interaction.user.id,
        "number": number,
        "opened_at": time.time(),
        "claimed_by": None,
        "kind": button_data["label"],
        "answers": answers,
    }
    save_tickets()

    embed = discord.Embed(
        title=f"Ticket {number:04d} - {button_data['label']}",
        description=button_data.get("welcome") or DEFAULT_BUTTON["welcome"],
        color=settings["panel"]["color"],
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Opened By", value=interaction.user.mention, inline=False)
    for question, answer in answers:
        embed.add_field(name=question[:256], value=(answer or "-")[:1024], inline=False)

    mentions = " ".join(r.mention for r in roles)
    await channel.send(
        content=f"{interaction.user.mention} {mentions}".strip(),
        embed=embed,
        view=TicketControlView(),
        allowed_mentions=discord.AllowedMentions(users=True, roles=roles or False),
    )

    await interaction.followup.send(
        f"Ticket created: {channel.mention}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    log_embed = discord.Embed(
        title="Ticket Opened",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )
    log_embed.add_field(name="Ticket ID", value=str(number), inline=True)
    log_embed.add_field(name="Opened By", value=interaction.user.mention, inline=True)
    log_embed.add_field(name="Type", value=button_data["label"], inline=True)
    log_embed.add_field(name="Channel", value=channel.mention, inline=False)
    for question, answer in answers:
        log_embed.add_field(
            name=question[:256], value=(answer or "-")[:1024], inline=False
        )
    await send_log(guild, log_embed)


def build_close_embed(guild, entry):
    opener = guild.get_member(entry["opener_id"])
    closer = guild.get_member(entry["closer_id"])
    claimer = guild.get_member(entry.get("claimed_by") or 0)

    embed = discord.Embed(
        title="Ticket Closed",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )

    embed.add_field(name="Ticket ID", value=str(entry["number"]), inline=True)
    embed.add_field(
        name="Opened By",
        value=opener.mention if opener else f"<@{entry['opener_id']}>",
        inline=True,
    )
    embed.add_field(
        name="Closed By",
        value=closer.mention if closer else f"<@{entry['closer_id']}>",
        inline=True,
    )
    embed.add_field(
        name="Open Time", value=f"<t:{int(entry['opened_at'])}:f>", inline=True
    )
    embed.add_field(
        name="Claimed By",
        value=claimer.mention if claimer else "Not claimed",
        inline=True,
    )
    embed.add_field(
        name="Open For",
        value=duration_text(entry["closed_at"] - entry["opened_at"]),
        inline=True,
    )
    embed.add_field(
        name="Reason", value=entry.get("reason") or "No reason given", inline=False
    )

    for question, answer in entry.get("answers", []):
        embed.add_field(name=question[:256], value=(answer or "-")[:1024], inline=False)

    return embed


class EditReasonModal(discord.ui.Modal, title="Edit Close Reason"):
    def __init__(self, entry):
        super().__init__()
        self.entry = entry
        self.f_reason = discord.ui.TextInput(
            label="Reason",
            default=entry.get("reason") or "",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.add_item(self.f_reason)

    async def on_submit(self, interaction):
        self.entry["reason"] = self.f_reason.value
        save_logs()

        embed = build_close_embed(interaction.guild, self.entry)
        embed.set_footer(text=f"Reason last edited by {interaction.user.display_name}")
        await interaction.response.edit_message(embed=embed, view=LogControlView())


class LogControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Edit Reason",
        style=discord.ButtonStyle.secondary,
        custom_id="ticket:editreason",
    )
    async def edit_reason(self, interaction, button):
        entry = logs.get(interaction.message.id)
        if entry is None:
            await interaction.response.send_message(
                "I no longer have this ticket on record.", ephemeral=True
            )
            return

        settings = get_config(interaction.guild.id)
        if not settings or not is_staff(interaction.user, settings):
            await interaction.response.send_message(
                "Only staff can edit the close reason.", ephemeral=True
            )
            return

        await interaction.response.send_modal(EditReasonModal(entry))


class CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    def __init__(self, data, channel):
        super().__init__()
        self.data = data
        self.channel = channel
        self.f_reason = discord.ui.TextInput(
            label="Reason for closing",
            placeholder="Shown to staff in the log",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.add_item(self.f_reason)

    async def on_submit(self, interaction):
        await interaction.response.send_message("Closing this ticket.", ephemeral=True)

        entry = {
            "guild_id": self.data["guild_id"],
            "number": self.data["number"],
            "opener_id": self.data["opener_id"],
            "closer_id": interaction.user.id,
            "claimed_by": self.data.get("claimed_by"),
            "opened_at": self.data["opened_at"],
            "closed_at": time.time(),
            "kind": self.data.get("kind", "Ticket"),
            "answers": self.data.get("answers", []),
            "reason": self.f_reason.value,
        }

        settings = get_config(interaction.guild.id)
        log_channel = interaction.guild.get_channel(settings.get("log_channel_id"))

        if log_channel is not None:
            try:
                sent = await log_channel.send(
                    embed=build_close_embed(interaction.guild, entry),
                    view=LogControlView(),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                logs[sent.id] = entry
                save_logs()
            except (discord.Forbidden, discord.HTTPException):
                pass

        tickets.pop(self.channel.id, None)
        save_tickets()

        try:
            await self.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            pass


class TicketQuestionModal(discord.ui.Modal):
    def __init__(self, button_data):
        super().__init__(title=button_data["label"][:45])
        self.button_data = button_data
        self.inputs = []

        for question in button_data["questions"][:MAX_QUESTIONS]:
            field = discord.ui.TextInput(
                label=question["label"][:45],
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=1000,
            )
            self.inputs.append((question["label"], field))
            self.add_item(field)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        answers = [(label, field.value) for label, field in self.inputs]
        await create_ticket(interaction, self.button_data, answers)


class TicketOpenButton(discord.ui.Button):
    def __init__(self, guild_id, button_data):
        super().__init__(
            label=button_data["label"][:80],
            style=STYLES[canonical_style(button_data.get("style"))],
            custom_id=f"ticket:open:{guild_id}:{button_data['key']}",
        )
        self.button_key = button_data["key"]

    async def callback(self, interaction):
        settings = get_config(interaction.guild.id)
        if not is_configured(settings):
            await interaction.response.send_message(
                "The ticket system isn't finished being set up.", ephemeral=True
            )
            return

        button_data = find_button(settings, self.button_key)
        if button_data is None:
            await interaction.response.send_message(
                "This button is no longer configured.", ephemeral=True
            )
            return

        if button_data.get("questions"):
            await interaction.response.send_modal(TicketQuestionModal(button_data))
            return

        await interaction.response.defer(ephemeral=True)
        await create_ticket(interaction, button_data, [])


class PanelView(discord.ui.View):
    def __init__(self, guild_id, buttons):
        super().__init__(timeout=None)
        for button_data in buttons[:MAX_BUTTONS]:
            self.add_item(TicketOpenButton(guild_id, button_data))


class BarePanelView(discord.ui.LayoutView):
    """Components V2 view. Lets the panel be buttons with no message body."""

    def __init__(self, guild_id, buttons):
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        for button_data in buttons[:5]:
            row.add_item(TicketOpenButton(guild_id, button_data))
        self.add_item(row)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Claim", style=discord.ButtonStyle.secondary, custom_id="ticket:claim"
    )
    async def claim(self, interaction, button):
        data = tickets.get(interaction.channel.id)
        if data is None:
            await interaction.response.send_message(
                "This isn't a tracked ticket.", ephemeral=True
            )
            return

        settings = get_config(interaction.guild.id)
        if not settings or not is_staff(interaction.user, settings):
            await interaction.response.send_message(
                "Only staff can claim tickets.", ephemeral=True
            )
            return

        if data.get("claimed_by"):
            claimer = interaction.guild.get_member(data["claimed_by"])
            await interaction.response.send_message(
                f"Already claimed by {claimer.mention if claimer else 'someone'}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        data["claimed_by"] = interaction.user.id
        save_tickets()

        await interaction.response.send_message(
            f"{interaction.user.mention} claimed this ticket.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        embed = discord.Embed(
            title="Ticket Claimed",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Ticket ID", value=str(data["number"]), inline=True)
        embed.add_field(name="Claimed By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
        await send_log(interaction.guild, embed)

    @discord.ui.button(
        label="Close", style=discord.ButtonStyle.danger, custom_id="ticket:close"
    )
    async def close(self, interaction, button):
        data = tickets.get(interaction.channel.id)
        if data is None:
            await interaction.response.send_message(
                "This isn't a tracked ticket.", ephemeral=True
            )
            return

        settings = get_config(interaction.guild.id)
        if not settings:
            await interaction.response.send_message(
                "Ticket system is not configured.", ephemeral=True
            )
            return

        if (
            not is_staff(interaction.user, settings)
            and interaction.user.id != data["opener_id"]
        ):
            await interaction.response.send_message(
                "Only staff or the ticket opener can close this.", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            CloseReasonModal(data, interaction.channel)
        )


class EmbedEditModal(discord.ui.Modal, title="Panel Appearance"):
    def __init__(self, settings, builder):
        super().__init__()
        self.settings = settings
        self.builder = builder
        panel = settings["panel"]

        self.f_title = discord.ui.TextInput(
            label="Title", default=panel["title"], max_length=256, required=True
        )
        self.f_desc = discord.ui.TextInput(
            label="Description",
            default=panel["description"],
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )
        self.f_color = discord.ui.TextInput(
            label="Colour hex",
            default=f"{panel['color']:06X}",
            placeholder="5865F2",
            max_length=7,
            required=False,
        )
        self.f_image = discord.ui.TextInput(
            label="Large image URL",
            default=panel.get("image_url") or "",
            placeholder="https://...",
            required=False,
        )
        self.f_thumb = discord.ui.TextInput(
            label="Thumbnail URL",
            default=panel.get("thumbnail_url") or "",
            placeholder="https://...",
            required=False,
        )

        for item in (
            self.f_title,
            self.f_desc,
            self.f_color,
            self.f_image,
            self.f_thumb,
        ):
            self.add_item(item)

    async def on_submit(self, interaction):
        await interaction.response.defer()

        panel = self.settings["panel"]
        panel["title"] = self.f_title.value
        panel["description"] = self.f_desc.value
        panel["color"] = parse_color(self.f_color.value, panel["color"])
        panel["image_url"] = clean_url(self.f_image.value)
        panel["thumbnail_url"] = clean_url(self.f_thumb.value)
        save_config()

        await self.builder.refresh()


class ButtonEditModal(discord.ui.Modal, title="Ticket Button"):
    def __init__(self, settings, builder, existing=None):
        super().__init__()
        self.settings = settings
        self.builder = builder
        self.existing = existing
        base = existing or DEFAULT_BUTTON

        self.f_label = discord.ui.TextInput(
            label="Button label", default=base["label"], max_length=80, required=True
        )
        self.f_welcome = discord.ui.TextInput(
            label="Opening message",
            default=base.get("welcome", ""),
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=False,
        )
        self.f_category = discord.ui.TextInput(
            label="Category ID override",
            default=str(base.get("category_id") or ""),
            placeholder="Blank uses the default category",
            required=False,
            max_length=25,
        )

        for item in (self.f_label, self.f_welcome, self.f_category):
            self.add_item(item)

    async def on_submit(self, interaction):
        category_id = None
        raw = self.f_category.value.strip()
        if raw:
            if not raw.isdigit():
                await interaction.response.send_message(
                    "Category ID must be numbers only.", ephemeral=True
                )
                return
            candidate = interaction.guild.get_channel(int(raw))
            if not isinstance(candidate, discord.CategoryChannel):
                await interaction.response.send_message(
                    "That ID isn't a category in this server.", ephemeral=True
                )
                return
            category_id = candidate.id

        await interaction.response.defer()

        if self.existing is None:
            self.settings["buttons"].append(
                {
                    "key": uuid.uuid4().hex[:8],
                    "label": self.f_label.value,
                    "style": "primary",
                    "category_id": category_id,
                    "welcome": self.f_welcome.value or DEFAULT_BUTTON["welcome"],
                    "questions": [],
                }
            )
        else:
            self.existing["label"] = self.f_label.value
            self.existing["category_id"] = category_id
            self.existing["welcome"] = self.f_welcome.value or DEFAULT_BUTTON["welcome"]

        save_config()
        await self.builder.refresh()


class QuestionsModal(discord.ui.Modal, title="Ticket Questions"):
    def __init__(self, builder, button_data):
        super().__init__()
        self.builder = builder
        self.button_data = button_data

        existing = button_data.get("questions", [])
        self.fields = []
        for i in range(MAX_QUESTIONS):
            current = existing[i]["label"] if i < len(existing) else ""
            field = discord.ui.TextInput(
                label=f"Question {i + 1}",
                default=current,
                placeholder="Leave blank to skip",
                required=False,
                max_length=45,
            )
            self.fields.append(field)
            self.add_item(field)

    async def on_submit(self, interaction):
        await interaction.response.defer()

        questions = []
        for field in self.fields:
            text = field.value.strip()
            if text:
                questions.append({"label": text})

        self.button_data["questions"] = questions
        save_config()
        await self.builder.refresh()


class PanelModeSelect(discord.ui.Select):
    def __init__(self, builder):
        self.builder = builder
        current = panel_mode(builder.settings["panel"])
        options = [
            discord.SelectOption(
                label=label, value=key, description=blurb, default=(key == current)
            )
            for key, label, blurb in PANEL_MODES
        ]
        super().__init__(placeholder="Panel style", options=options, row=0)

    async def callback(self, interaction):
        self.builder.settings["panel"]["mode"] = self.values[0]
        save_config()

        refreshed = AppearanceView(self.builder)
        await interaction.response.edit_message(
            content=refreshed.blurb(), view=refreshed
        )
        await self.builder.refresh()


class AppearanceView(discord.ui.View):
    def __init__(self, builder):
        super().__init__(timeout=300)
        self.builder = builder
        self.add_item(PanelModeSelect(builder))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    def blurb(self):
        mode = panel_mode(self.builder.settings["panel"])
        notes = {
            "embed_title": "Title, description, images and colour all apply.",
            "embed_plain": "Title is hidden. Everything else applies.",
            "text": "Only the description is used, as plain message text.",
            "bare": "Nothing but buttons. Text, colour and images are ignored.",
        }
        return f"**{panel_mode_label(self.builder.settings['panel'])}** - {notes[mode]}"

    @discord.ui.button(label="Edit Text & Images", style=discord.ButtonStyle.secondary, row=1)
    async def edit_text(self, interaction, button):
        await interaction.response.send_modal(
            EmbedEditModal(self.builder.settings, self.builder)
        )


class SettingsView(discord.ui.View):
    def __init__(self, builder):
        super().__init__(timeout=300)
        self.builder = builder

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.category],
        placeholder="Ticket category",
        row=0,
    )
    async def pick_category(self, interaction, select):
        await interaction.response.defer()
        self.builder.settings["category_id"] = select.values[0].id
        save_config()
        await self.builder.refresh()

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Staff roles (pick as many as you like)",
        min_values=0,
        max_values=MAX_STAFF_ROLES,
        row=1,
    )
    async def pick_roles(self, interaction, select):
        await interaction.response.defer()
        self.builder.settings["staff_role_ids"] = [r.id for r in select.values]
        save_config()
        await self.builder.refresh()

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Log channel",
        row=2,
    )
    async def pick_log(self, interaction, select):
        await interaction.response.defer()
        self.builder.settings["log_channel_id"] = select.values[0].id
        save_config()
        await self.builder.refresh()

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Where the panel gets posted",
        row=3,
    )
    async def pick_panel_channel(self, interaction, select):
        await interaction.response.defer()
        self.builder.settings["panel"]["channel_id"] = select.values[0].id
        save_config()
        await self.builder.refresh()


class StyleSelect(discord.ui.Select):
    def __init__(self, builder, button_data):
        self.builder = builder
        self.button_data = button_data
        current = canonical_style(button_data.get("style"))

        options = [
            discord.SelectOption(
                label=label,
                value=key,
                description=blurb,
                default=(key == current),
            )
            for key, label, blurb in STYLE_CHOICES
        ]

        super().__init__(placeholder="Button colour", options=options, row=0)

    async def callback(self, interaction):
        self.button_data["style"] = self.values[0]
        save_config()

        refreshed = ButtonManageView(self.builder, self.button_data)
        await interaction.response.edit_message(
            embed=refreshed.summary(), view=refreshed
        )
        await self.builder.refresh()


class ButtonManageView(discord.ui.View):
    def __init__(self, builder, button_data):
        super().__init__(timeout=300)
        self.builder = builder
        self.button_data = button_data
        self.add_item(StyleSelect(builder, button_data))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    def summary(self):
        count = len(self.button_data.get("questions", []))
        if count:
            mode = f"Asks {count} question(s) before opening"
            listed = "\n".join(
                f"{i + 1}. {q['label']}"
                for i, q in enumerate(self.button_data["questions"])
            )
        else:
            mode = "Opens a ticket immediately"
            listed = "No questions set."

        return discord.Embed(
            title=f"Button: {self.button_data['label']}",
            description=(
                f"**Colour** - {style_label(self.button_data.get('style'))}\n"
                f"**Behaviour** - {mode}\n\n"
                f"{listed}"
            ),
            color=discord.Color.dark_theme(),
        )

    @discord.ui.button(label="Edit Details", style=discord.ButtonStyle.secondary, row=1)
    async def edit_details(self, interaction, button):
        await interaction.response.send_modal(
            ButtonEditModal(self.builder.settings, self.builder, self.button_data)
        )

    @discord.ui.button(label="Set Questions", style=discord.ButtonStyle.secondary, row=1)
    async def set_questions(self, interaction, button):
        await interaction.response.send_modal(
            QuestionsModal(self.builder, self.button_data)
        )

    @discord.ui.button(label="Open Instantly", style=discord.ButtonStyle.secondary, row=1)
    async def clear_questions(self, interaction, button):
        await interaction.response.defer()
        self.button_data["questions"] = []
        save_config()
        await self.builder.refresh()
        await interaction.followup.send(
            f"`{self.button_data['label']}` now opens a ticket immediately.",
            ephemeral=True,
        )

    @discord.ui.button(label="Delete Button", style=discord.ButtonStyle.danger, row=2)
    async def delete_button(self, interaction, button):
        await interaction.response.defer()
        if self.button_data in self.builder.settings["buttons"]:
            self.builder.settings["buttons"].remove(self.button_data)
            save_config()
        await self.builder.refresh()
        await interaction.followup.send(
            f"Removed `{self.button_data['label']}`.", ephemeral=True
        )
        self.stop()


class ButtonPickSelect(discord.ui.Select):
    def __init__(self, builder):
        self.builder = builder
        options = [
            discord.SelectOption(
                label=b["label"][:100],
                value=b["key"],
                description=(
                    f"{len(b.get('questions', []))} question(s)"
                    if b.get("questions")
                    else "Opens instantly"
                ),
            )
            for b in builder.settings["buttons"]
        ]
        super().__init__(
            placeholder="Pick a button to manage",
            options=options or [discord.SelectOption(label="none", value="none")],
            disabled=not options,
        )

    async def callback(self, interaction):
        entry = find_button(self.builder.settings, self.values[0])
        if entry is None:
            await interaction.response.send_message(
                "That button is gone.", ephemeral=True
            )
            return

        manage = ButtonManageView(self.builder, entry)
        await interaction.response.edit_message(embed=manage.summary(), view=manage)


class ButtonsView(discord.ui.View):
    def __init__(self, builder):
        super().__init__(timeout=300)
        self.builder = builder
        self.add_item(ButtonPickSelect(builder))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    @discord.ui.button(label="Add New Button", style=discord.ButtonStyle.success, row=1)
    async def add_button(self, interaction, button):
        if len(self.builder.settings["buttons"]) >= MAX_BUTTONS:
            await interaction.response.send_message(
                f"You already have {MAX_BUTTONS} buttons.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ButtonEditModal(self.builder.settings, self.builder)
        )


class BuilderView(discord.ui.View):
    def __init__(self, ctx, settings):
        super().__init__(timeout=900)
        self.ctx = ctx
        self.settings = settings
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This builder isn't yours.", ephemeral=True
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
        guild = self.ctx.guild
        settings = self.settings
        panel = settings["panel"]

        category = guild.get_channel(settings.get("category_id"))
        log = guild.get_channel(settings.get("log_channel_id"))
        target = guild.get_channel(panel.get("channel_id"))
        roles = staff_roles(guild, settings)

        lines = [
            f"**Category** - {category.name if category else 'not set'}",
            f"**Staff** - {' '.join(r.mention for r in roles) if roles else 'not set'}",
            f"**Logs** - {log.mention if log else 'not set'}",
            f"**Panel** - {target.mention if target else 'not set'}",
            "",
            f"**Style** - {panel_mode_label(panel)}",
        ]

        if settings["buttons"]:
            lines.append("")
            lines.append("**Buttons**")
            for entry in settings["buttons"]:
                count = len(entry.get("questions", []))
                mode = f"asks {count}" if count else "instant"
                colour = style_label(entry.get("style"))
                lines.append(f"- {entry['label']} ({colour}, {mode})")
        else:
            lines.append("")
            lines.append("**Buttons** - none yet, add one before publishing")

        return discord.Embed(
            title="Ticket Builder",
            description="\n".join(lines),
            color=panel["color"],
        )

    async def refresh(self):
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.status_embed(), view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Channels & Roles", style=discord.ButtonStyle.secondary)
    async def open_settings(self, interaction, button):
        await interaction.response.send_message(
            "Pick your category, staff roles, log channel and panel channel.",
            view=SettingsView(self),
            ephemeral=True,
        )

    @discord.ui.button(label="Appearance", style=discord.ButtonStyle.secondary)
    async def open_appearance(self, interaction, button):
        view = AppearanceView(self)
        await interaction.response.send_message(
            view.blurb(), view=view, ephemeral=True
        )

    @discord.ui.button(label="Buttons", style=discord.ButtonStyle.secondary)
    async def open_buttons(self, interaction, button):
        view = ButtonsView(self)
        await interaction.response.send_message(
            "Manage the buttons that appear on your panel.",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.success)
    async def publish(self, interaction, button):
        settings = self.settings
        problems = []

        if not settings.get("category_id"):
            problems.append("ticket category")
        if not staff_role_ids(settings):
            problems.append("staff role")
        if not settings.get("log_channel_id"):
            problems.append("log channel")
        if not settings["panel"].get("channel_id"):
            problems.append("panel channel")
        if not settings["buttons"]:
            problems.append("at least one button")
        if panel_mode(settings["panel"]) == "text" and not settings["panel"].get(
            "description"
        ):
            problems.append("some text for plain text mode")

        if problems:
            await interaction.response.send_message(
                "Still missing: " + ", ".join(problems), ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(settings["panel"]["channel_id"])
        if channel is None:
            await interaction.response.send_message(
                "The panel channel no longer exists.", ephemeral=True
            )
            return

        await interaction.response.defer()

        old_id = settings["panel"].get("message_id")
        if old_id:
            try:
                old = await channel.fetch_message(old_id)
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        content, embed = build_panel_payload(settings)
        view = build_panel_view(interaction.guild.id, settings)

        try:
            if embed is not None:
                sent = await channel.send(embed=embed, view=view)
            elif content:
                sent = await channel.send(content=content, view=view)
            else:
                sent = await channel.send(view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't post in that channel.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Discord rejected the panel: {exc}", ephemeral=True
            )
            return

        settings["panel"]["message_id"] = sent.id
        save_config()

        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(
                    content=f"Panel published in {channel.mention}.",
                    embed=self.status_embed(),
                    view=self,
                )
            except discord.HTTPException:
                pass
        self.stop()


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._views_added = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._views_added:
            return
        self._views_added = True

        self.bot.add_view(TicketControlView())
        self.bot.add_view(LogControlView())

        for guild_key, settings in config.items():
            panel = settings.get("panel") or {}
            message_id = panel.get("message_id")
            if not message_id or not settings.get("buttons"):
                continue
            try:
                self.bot.add_view(
                    build_panel_view(int(guild_key), settings),
                    message_id=message_id,
                )
            except (ValueError, TypeError):
                continue

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if channel.id in tickets:
            tickets.pop(channel.id, None)
            save_tickets()

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

    async def open_builder(self, ctx, settings):
        view = BuilderView(ctx, settings)
        message = await ctx.send(
            embed=view.status_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = message

    @commands.group(name="ticketsetup", aliases=["tsetup"], invoke_without_command=True)
    async def ticketsetup(self, ctx):
        settings = get_config(ctx.guild.id)
        state = "configured" if is_configured(settings) else "not set up yet"

        embed = discord.Embed(
            title="Ticket Setup",
            description=(
                f"This server is **{state}**.\n\n"
                "`,ticketsetup fast` - creates one default button and opens the "
                "builder so you can pick your channels and publish.\n\n"
                "`,ticketsetup custom` - same builder, starting empty.\n\n"
                "`,ticketsetup edit` - reopen the builder on an existing setup."
            ),
            color=discord.Color.dark_theme(),
        )
        await ctx.send(embed=embed)

    @ticketsetup.command(name="fast")
    async def setup_fast(self, ctx):
        settings = ensure_config(ctx.guild.id)

        if not settings["buttons"]:
            entry = dict(DEFAULT_BUTTON)
            entry["key"] = uuid.uuid4().hex[:8]
            entry["questions"] = []
            settings["buttons"].append(entry)

        if not settings["panel"].get("channel_id"):
            settings["panel"]["channel_id"] = ctx.channel.id

        save_config()

        await ctx.send(
            "Fast setup. Open Channels & Roles, pick your three settings, "
            "then Publish. The panel goes in this channel unless you change it."
        )
        await self.open_builder(ctx, settings)

    @ticketsetup.command(name="custom")
    async def setup_custom(self, ctx):
        settings = ensure_config(ctx.guild.id)
        save_config()
        await self.open_builder(ctx, settings)

    @ticketsetup.command(name="edit")
    async def setup_edit(self, ctx):
        if get_config(ctx.guild.id) is None:
            await ctx.send(
                "Nothing to edit yet. Run `,ticketsetup fast` or `,ticketsetup custom` first."
            )
            return
        settings = ensure_config(ctx.guild.id)
        await self.open_builder(ctx, settings)

    @commands.command(name="ticketstats", aliases=["tstats"])
    async def ticket_stats(self, ctx):
        settings = get_config(ctx.guild.id)
        if settings is None:
            await ctx.send("Run `,ticketsetup` first.")
            return

        open_here = [d for d in tickets.values() if d["guild_id"] == ctx.guild.id]
        unclaimed = sum(1 for d in open_here if not d.get("claimed_by"))

        embed = discord.Embed(title="Ticket Stats", color=discord.Color.dark_theme())
        embed.add_field(name="Currently open", value=str(len(open_here)))
        embed.add_field(name="Unclaimed", value=str(unclaimed))
        embed.add_field(name="Total ever opened", value=str(settings.get("counter", 0)))
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Tickets(bot))