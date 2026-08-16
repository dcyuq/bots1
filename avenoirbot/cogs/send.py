import discord
from discord.ext import commands, tasks
import time
import uuid

from prefixes import prefix_of
from scheduling import (
    COMMON_ZONES,
    parse_date_only,
    parse_time_only,
    QUICK_OFFSETS,
    REPEAT_CHOICES,
    build_datetime,
    describe_when,
    local_now_text,
    next_occurrence,
    now_in,
    parse_when,
    repeat_label,
    set_timezone,
    tz_for,
    tz_name,
    upcoming_days,
    validate_when,
)
import datetime
from storage import Store

_schedule_store = Store("scheduled.json", default=list)
scheduled = _schedule_store.load()

CHECK_SECONDS = 20
LATE_GRACE_HOURS = 6
MAX_PER_GUILD = 25

SEND_MODES = [
    ("text", "Plain text", "A normal message. No embed."),
    ("embed_title", "Embed with header", "Embed with a title at the top."),
    ("embed_plain", "Embed without header", "Embed with no title. Slimmer."),
]

MODE_KEYS = {m[0] for m in SEND_MODES}


def can_send(member):
    perms = member.guild_permissions
    return perms.administrator or perms.manage_messages


def mode_label(mode):
    for key, label, _ in SEND_MODES:
        if key == mode:
            return label
    return "Plain text"


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


def new_draft():
    return {
        "channel_id": None,
        "mode": "text",
        "content": "",
        "title": "",
        "description": "",
        "color": 0x2B2D31,
        "image_url": None,
        "thumbnail_url": None,
        "pings": False,
        "when": None,
        "repeat": "none",
    }


def build_payload(draft):
    """Return (content, embed) for the message this draft describes."""
    if draft["mode"] == "text":
        return draft["content"][:2000], None

    embed = discord.Embed(
        description=draft["description"][:4096],
        color=draft["color"],
    )
    if draft["mode"] == "embed_title" and draft["title"]:
        embed.title = draft["title"][:256]
    if draft.get("image_url"):
        embed.set_image(url=draft["image_url"])
    if draft.get("thumbnail_url"):
        embed.set_thumbnail(url=draft["thumbnail_url"])

    return None, embed


def save_scheduled():
    _schedule_store.save(scheduled)


def guild_schedules(guild_id):
    return [job for job in scheduled if job["guild_id"] == guild_id]


def draft_problems(draft, guild, author):
    problems = []

    if not draft["channel_id"]:
        problems.append("a target channel")

    if draft["mode"] == "text":
        if not draft["content"].strip():
            problems.append("some message text")
    elif not draft["description"].strip():
        problems.append("an embed description")

    channel = guild.get_channel(draft["channel_id"]) if draft["channel_id"] else None
    if channel is not None:
        if not channel.permissions_for(author).send_messages:
            problems.append("permission for you to post there")
        if not channel.permissions_for(guild.me).send_messages:
            problems.append("permission for me to post there")

    return problems


class TextComposeModal(discord.ui.Modal, title="Message Text"):
    def __init__(self, builder):
        super().__init__()
        self.builder = builder
        self.f_content = discord.ui.TextInput(
            label="Message",
            default=builder.draft["content"],
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=True,
        )
        self.add_item(self.f_content)

    async def on_submit(self, interaction):
        await interaction.response.defer()
        self.builder.draft["content"] = self.f_content.value
        await self.builder.refresh()


class EmbedComposeModal(discord.ui.Modal, title="Embed Content"):
    def __init__(self, builder):
        super().__init__()
        self.builder = builder
        draft = builder.draft

        self.f_title = discord.ui.TextInput(
            label="Title",
            default=draft["title"],
            placeholder="Ignored if you picked the no header style",
            max_length=256,
            required=False,
        )
        self.f_desc = discord.ui.TextInput(
            label="Description",
            default=draft["description"],
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )
        self.f_color = discord.ui.TextInput(
            label="Colour hex",
            default=f"{draft['color']:06X}",
            placeholder="5865F2",
            max_length=7,
            required=False,
        )
        self.f_image = discord.ui.TextInput(
            label="Large image URL",
            default=draft.get("image_url") or "",
            placeholder="https://...",
            required=False,
        )
        self.f_thumb = discord.ui.TextInput(
            label="Thumbnail URL",
            default=draft.get("thumbnail_url") or "",
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
        draft = self.builder.draft
        draft["title"] = self.f_title.value
        draft["description"] = self.f_desc.value
        draft["color"] = parse_color(self.f_color.value, draft["color"])
        draft["image_url"] = clean_url(self.f_image.value)
        draft["thumbnail_url"] = clean_url(self.f_thumb.value)
        await self.builder.refresh()


class ModeSelect(discord.ui.Select):
    def __init__(self, builder):
        self.builder = builder
        current = builder.draft["mode"]
        options = [
            discord.SelectOption(
                label=label, value=key, description=blurb, default=(key == current)
            )
            for key, label, blurb in SEND_MODES
        ]
        super().__init__(placeholder="Message style", options=options, row=1)

    async def callback(self, interaction):
        await interaction.response.defer()
        self.builder.draft["mode"] = self.values[0]
        await self.builder.refresh()


TIME_FORMATS = [
    ("From now", "`30m` `2h` `2h30m` `3d` `1d6h`"),
    ("A clock time", "`9pm` `21:00` `9:30am` `07:45`"),
    ("Today or tomorrow", "`tomorrow 8am` `today 11pm` `tomorrow`"),
    ("A weekday", "`friday 18:00` `next tuesday` `mon 9am`"),
    ("A full date", "`2026-08-20 14:30` `20/08/2026 09:00`"),
]


def formats_text():
    return "\n".join(f"**{name}** - {examples}" for name, examples in TIME_FORMATS)


class ScheduleModal(discord.ui.Modal, title="Schedule Message"):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.f_when = discord.ui.TextInput(
            label="When should this send?",
            placeholder="2h30m  |  9pm  |  tomorrow 8am  |  2026-08-20 14:30",
            max_length=60,
            required=True,
        )
        self.add_item(self.f_when)

    async def on_submit(self, interaction):
        guild_id = interaction.guild.id

        target, error = parse_when(self.f_when.value, guild_id)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        problem = validate_when(target, guild_id)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        await interaction.response.defer()
        self.panel.builder.draft["when"] = target.timestamp()
        await self.panel.render(interaction)


class RepeatSelect(discord.ui.Select):
    def __init__(self, panel):
        self.panel = panel
        current = panel.builder.draft.get("repeat", "none")
        options = [
            discord.SelectOption(
                label=label, value=key, description=blurb, default=(key == current)
            )
            for key, label, blurb in REPEAT_CHOICES
        ]
        super().__init__(placeholder="Repeat this message?", options=options, row=0)

    async def callback(self, interaction):
        await interaction.response.defer()
        self.panel.builder.draft["repeat"] = self.values[0]
        await self.panel.render(interaction)


class SchedulePanel(discord.ui.View):
    def __init__(self, builder):
        super().__init__(timeout=300)
        self.builder = builder
        self.add_item(RepeatSelect(self))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.builder.ctx.author.id

    def embed(self):
        guild_id = self.builder.ctx.guild.id
        draft = self.builder.draft

        if draft.get("when"):
            heading = f"Sending {describe_when(draft['when'])}"
            if draft.get("repeat", "none") != "none":
                heading += f"\nRepeating {repeat_label(draft['repeat']).lower()}"
        else:
            heading = "No time set. This will send as soon as you press Send."

        embed = discord.Embed(
            title="Schedule",
            description=heading,
            color=discord.Color.dark_theme(),
        )
        embed.add_field(name="Ways to write the time", value=formats_text(), inline=False)
        embed.set_footer(
            text=(
                f"Timezone {tz_name(guild_id)} - it is "
                f"{local_now_text(guild_id)} there now"
            )
        )
        return embed

    async def render(self, interaction):
        await interaction.edit_original_response(embed=self.embed(), view=self)
        await self.builder.refresh()

    @discord.ui.button(label="Set Time", style=discord.ButtonStyle.primary, row=1)
    async def set_time(self, interaction, button):
        await interaction.response.send_modal(ScheduleModal(self))

    @discord.ui.button(label="Clear Time", style=discord.ButtonStyle.secondary, row=1)
    async def clear_time(self, interaction, button):
        await interaction.response.defer()
        self.builder.draft["when"] = None
        self.builder.draft["repeat"] = "none"
        await self.render(interaction)


class SendBuilderView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.draft = new_draft()
        self.message = None
        self.add_item(ModeSelect(self))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This composer isn't yours.", ephemeral=True
            )
            return False
        if not can_send(interaction.user):
            await interaction.response.send_message(
                "You need Administrator or Manage Messages permission.", ephemeral=True
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
        draft = self.draft
        channel = (
            self.ctx.guild.get_channel(draft["channel_id"])
            if draft["channel_id"]
            else None
        )

        if draft["mode"] == "text":
            body = draft["content"] or "nothing written yet"
        else:
            body = draft["description"] or "nothing written yet"

        preview = body if len(body) <= 200 else body[:200] + "..."

        if draft.get("when"):
            timing = describe_when(draft["when"])
            if draft.get("repeat", "none") != "none":
                timing += f", {repeat_label(draft['repeat']).lower()}"
        else:
            timing = "immediately"

        lines = [
            f"**Sending to** - {channel.mention if channel else 'not set'}",
            f"**Style** - {mode_label(draft['mode'])}",
            f"**Pings** - {'allowed' if draft['pings'] else 'blocked'}",
            f"**When** - {timing}",
            "",
            "**Content**",
            preview,
        ]

        embed = discord.Embed(
            title="Compose Message",
            description="\n".join(lines),
            color=draft["color"],
        )
        embed.add_field(
            name="Buttons",
            value=(
                "**Write** - set the text or embed content\n"
                "**Preview** - see it privately before anyone else does\n"
                "**Schedule** - send it later instead of now\n"
                "**Allow Pings** - let mentions actually notify people\n"
                "**Send** - post it, or save the schedule"
            ),
            inline=False,
        )
        return embed

    async def refresh(self):
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.status_embed(), view=self)
        except discord.HTTPException:
            pass

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Which channel?",
        row=0,
    )
    async def pick_channel(self, interaction, select):
        await interaction.response.defer()
        self.draft["channel_id"] = select.values[0].id
        await self.refresh()

    @discord.ui.button(label="Write", style=discord.ButtonStyle.primary, row=2)
    async def write(self, interaction, button):
        if self.draft["mode"] == "text":
            await interaction.response.send_modal(TextComposeModal(self))
        else:
            await interaction.response.send_modal(EmbedComposeModal(self))

    @discord.ui.button(label="Preview", style=discord.ButtonStyle.secondary, row=2)
    async def preview(self, interaction, button):
        content, embed = build_payload(self.draft)

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

    @discord.ui.button(label="Schedule", style=discord.ButtonStyle.secondary, row=2)
    async def open_schedule(self, interaction, button):
        panel = SchedulePanel(self)
        await interaction.response.send_message(
            embed=panel.embed(), view=panel, ephemeral=True
        )

    @discord.ui.button(label="Allow Pings", style=discord.ButtonStyle.secondary, row=3)
    async def toggle_pings(self, interaction, button):
        if not interaction.user.guild_permissions.mention_everyone:
            await interaction.response.send_message(
                "You need the Mention Everyone permission to enable pings.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        self.draft["pings"] = not self.draft["pings"]
        button.label = "Block Pings" if self.draft["pings"] else "Allow Pings"
        button.style = (
            discord.ButtonStyle.danger
            if self.draft["pings"]
            else discord.ButtonStyle.secondary
        )
        await self.refresh()

    @discord.ui.button(label="Send", style=discord.ButtonStyle.success, row=3)
    async def send(self, interaction, button):
        problems = draft_problems(self.draft, interaction.guild, interaction.user)
        if problems:
            await interaction.response.send_message(
                "Still need: " + ", ".join(problems), ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(self.draft["channel_id"])

        if self.draft.get("when"):
            if len(guild_schedules(interaction.guild.id)) >= MAX_PER_GUILD:
                await interaction.response.send_message(
                    f"This server already has {MAX_PER_GUILD} scheduled messages.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()

            job = {
                "id": uuid.uuid4().hex[:8],
                "guild_id": interaction.guild.id,
                "channel_id": channel.id,
                "author_id": interaction.user.id,
                "when": self.draft["when"],
                "repeat": self.draft.get("repeat", "none"),
                "draft": dict(self.draft),
            }
            scheduled.append(job)
            save_scheduled()

            for item in self.children:
                item.disabled = True

            if self.message:
                try:
                    await self.message.edit(
                        content=(
                            f"Scheduled for {channel.mention} at "
                            f"{describe_when(job['when'])}. "
                            f"Cancel it with `{prefix_of(interaction)}scheduled`."
                        ),
                        embed=self.status_embed(),
                        view=self,
                    )
                except discord.HTTPException:
                    pass

            self.stop()
            return

        content, embed = build_payload(self.draft)

        if self.draft["pings"]:
            mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
        else:
            mentions = discord.AllowedMentions.none()

        await interaction.response.defer()

        try:
            sent = await channel.send(
                content=content, embed=embed, allowed_mentions=mentions
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't post in that channel.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Discord rejected the message: {exc}", ephemeral=True
            )
            return

        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(
                    content=f"Sent to {channel.mention}: {sent.jump_url}",
                    embed=self.status_embed(),
                    view=self,
                )
            except discord.HTTPException:
                pass

        self.stop()


class CancelSelect(discord.ui.Select):
    def __init__(self, ctx, jobs):
        self.ctx = ctx
        options = []
        for job in jobs[:25]:
            channel = ctx.guild.get_channel(job["channel_id"])
            where = channel.name if channel else "missing channel"
            options.append(
                discord.SelectOption(
                    label=f"{job['id']} to {where}"[:100],
                    value=job["id"],
                    description=repeat_label(job.get("repeat", "none")),
                )
            )
        super().__init__(
            placeholder="Cancel which one?",
            options=options or [discord.SelectOption(label="none", value="none")],
            disabled=not options,
            max_values=min(len(options), 25) or 1,
        )

    async def callback(self, interaction):
        removed = 0
        for job_id in self.values:
            for job in list(scheduled):
                if job["id"] == job_id and job["guild_id"] == interaction.guild.id:
                    scheduled.remove(job)
                    removed += 1

        if removed:
            save_scheduled()

        for item in self.view.children:
            item.disabled = True

        await interaction.response.edit_message(
            content=f"Cancelled {removed} scheduled message(s).", view=self.view
        )


class CancelView(discord.ui.View):
    def __init__(self, ctx, jobs):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.add_item(CancelSelect(ctx, jobs))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.ctx.author.id


class TimezoneSelect(discord.ui.Select):
    def __init__(self, ctx):
        self.ctx = ctx
        current = tz_name(ctx.guild.id)
        options = [
            discord.SelectOption(
                label=nice, value=key, description=key, default=(key == current)
            )
            for key, nice in COMMON_ZONES
        ]
        super().__init__(placeholder="Pick your timezone", options=options)

    async def callback(self, interaction):
        ok, message = set_timezone(interaction.guild.id, self.values[0])
        for item in self.view.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"{message} It is {local_now_text(interaction.guild.id)} there now.",
            view=self.view,
        )


class TimezoneView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.add_item(TimezoneSelect(ctx))

    async def interaction_check(self, interaction):
        return interaction.user.id == self.ctx.author.id


class Send(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dispatch_due.start()

    def cog_unload(self):
        self.dispatch_due.cancel()

    @tasks.loop(seconds=CHECK_SECONDS)
    async def dispatch_due(self):
        now = time.time()
        due = [job for job in scheduled if job["when"] <= now]
        if not due:
            return

        changed = False

        for job in due:
            late_by = now - job["when"]
            guild = self.bot.get_guild(job["guild_id"])
            channel = guild.get_channel(job["channel_id"]) if guild else None

            skip = (
                guild is None
                or channel is None
                or late_by > LATE_GRACE_HOURS * 3600
            )

            if not skip:
                draft = job["draft"]
                content, embed = build_payload(draft)

                if draft.get("pings"):
                    mentions = discord.AllowedMentions(
                        everyone=True, roles=True, users=True
                    )
                else:
                    mentions = discord.AllowedMentions.none()

                try:
                    await channel.send(
                        content=content, embed=embed, allowed_mentions=mentions
                    )
                except (discord.Forbidden, discord.HTTPException):
                    skip = True

            following = next_occurrence(
                job["when"], job.get("repeat", "none"), job["guild_id"]
            )

            if following is None:
                if job in scheduled:
                    scheduled.remove(job)
            else:
                job["when"] = following

            changed = True

        if changed:
            save_scheduled()

    @dispatch_due.before_loop
    async def before_dispatch(self):
        await self.bot.wait_until_ready()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if can_send(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_messages"])

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need Administrator or Manage Messages permission.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command only works in a server.")
        else:
            await ctx.send(f"Something went wrong: {error}")

    @commands.command(name="send", aliases=["say"])
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def send(self, ctx, channel: discord.TextChannel = None, *, text: str = None):
        if channel is not None and text:
            if not channel.permissions_for(ctx.author).send_messages:
                await ctx.send("You can't post in that channel.")
                return
            if not channel.permissions_for(ctx.guild.me).send_messages:
                await ctx.send("I can't post in that channel.")
                return

            try:
                sent = await channel.send(
                    text[:2000], allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.Forbidden:
                await ctx.send("I can't post in that channel.")
                return

            await ctx.send(f"Sent to {channel.mention}: {sent.jump_url}")
            return

        view = SendBuilderView(ctx)
        if channel is not None:
            view.draft["channel_id"] = channel.id

        message = await ctx.send(
            embed=view.status_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = message


    @commands.command(name="scheduled", aliases=["schedules"])
    async def scheduled_list(self, ctx):
        jobs = sorted(guild_schedules(ctx.guild.id), key=lambda j: j["when"])

        if not jobs:
            await ctx.send(
                f"Nothing scheduled. Use `{prefix_of(ctx)}send` and press Schedule."
            )
            return

        lines = []
        for job in jobs:
            channel = ctx.guild.get_channel(job["channel_id"])
            where = channel.mention if channel else "missing channel"
            repeat = job.get("repeat", "none")
            suffix = f" - {repeat_label(repeat).lower()}" if repeat != "none" else ""
            lines.append(
                f"`{job['id']}` to {where} at {describe_when(job['when'])}{suffix}"
            )

        embed = discord.Embed(
            title="Scheduled Messages",
            description="\n".join(lines),
            color=discord.Color.dark_theme(),
        )
        embed.set_footer(text=f"Server timezone: {tz_name(ctx.guild.id)}")

        await ctx.send(
            embed=embed,
            view=CancelView(ctx, jobs),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="timezone", aliases=["tz"])
    async def timezone_command(self, ctx, *, name: str = None):
        if name:
            ok, message = set_timezone(ctx.guild.id, name.strip())
            await ctx.send(message)
            return

        await ctx.send(
            f"Timezone is `{tz_name(ctx.guild.id)}`, currently "
            f"{local_now_text(ctx.guild.id)}. Pick another below, or type "
            f"`{prefix_of(ctx)}timezone Area/City` for one not listed.",
            view=TimezoneView(ctx),
        )


async def setup(bot):
    await bot.add_cog(Send(bot))