import asyncio

import discord
from discord import app_commands
from discord.ext import commands, tasks

from storage import Store, IntKeyStore
from prefixes import prefix_for

_store = Store("stickynotes.json")
_active_store = IntKeyStore("active_stickies.json")

FLUSH_SECONDS = 20
REPOST_DELAY = 2.0
HISTORY_SCAN = 30
MAX_OPTIONS = 25


def load_data():
    return _store.load()


def save_data(data):
    _store.save(data)


def load_active():
    """Which channels currently display a sticky, keyed by channel id."""
    return _active_store.load()


def save_active():
    _active_store.save(active_stickies)


sticky_notes = load_data()
active_stickies = load_active()

_active_dirty = False


def mark_dirty():
    global _active_dirty
    _active_dirty = True


def get_guild_notes(guild_id):
    gid = str(guild_id)
    if gid not in sticky_notes:
        sticky_notes[gid] = {}
    return sticky_notes[gid]


def normalize_name(name: str) -> str:
    """Fold a trigger to its stored form.

    Only case and surrounding whitespace are normalised. The trigger is
    matched against whatever follows the guild prefix, so it must not be
    tied to any particular prefix character.
    """
    return " ".join(name.lower().split())


def can_manage(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_messages


def note_options(guild_id):
    notes = get_guild_notes(guild_id)
    return [
        discord.SelectOption(label=note, value=note)
        for note in list(notes)[:MAX_OPTIONS]
    ]


class StickyCreateModal(discord.ui.Modal, title="Create Sticky Note"):

    call = discord.ui.TextInput(
        label="Call Prefix",
        placeholder="Example: rules",
        required=True,
        max_length=50
    )

    message = discord.ui.TextInput(
        label="Bot Message",
        placeholder="What should the bot say?",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000
    )

    async def on_submit(self, interaction: discord.Interaction):
        notes = get_guild_notes(interaction.guild_id)
        name = normalize_name(self.call.value)

        if not name:
            await interaction.response.send_message(
                "That trigger isn't valid.", ephemeral=True
            )
            return

        if name in notes:
            await interaction.response.send_message(
                f"A sticky note called `{name}` already exists.",
                ephemeral=True
            )
            return

        notes[name] = {
            "message": self.message.value,
            "creator": interaction.user.mention
        }
        save_data(sticky_notes)

        prefix = prefix_for(interaction.guild_id)
        await interaction.response.send_message(
            f"Sticky note `{name}` created. Post it with `{prefix}{name}`.",
            ephemeral=True
        )


class StickyEditModal(discord.ui.Modal):

    def __init__(self, name: str):
        super().__init__(title=f"Edit '{name}'")
        self.original_name = name

        self.call = discord.ui.TextInput(
            label="Call Prefix",
            default=name,
            required=True,
            max_length=50
        )
        self.add_item(self.call)

        self.message = discord.ui.TextInput(
            label="Bot Message",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000
        )
        self.add_item(self.message)

    async def on_submit(self, interaction: discord.Interaction):
        notes = get_guild_notes(interaction.guild_id)

        if self.original_name not in notes:
            await interaction.response.send_message(
                "That sticky note no longer exists.", ephemeral=True
            )
            return

        new_name = normalize_name(self.call.value)
        if not new_name:
            await interaction.response.send_message(
                "That trigger isn't valid.", ephemeral=True
            )
            return

        if new_name != self.original_name and new_name in notes:
            await interaction.response.send_message(
                f"A sticky note called `{new_name}` already exists.",
                ephemeral=True
            )
            return

        data = notes.pop(self.original_name)
        data["message"] = self.message.value
        notes[new_name] = data
        save_data(sticky_notes)

        renamed = 0
        if new_name != self.original_name:
            for active in active_stickies.values():
                if (
                    active.get("guild_id") == interaction.guild_id
                    and active.get("trigger") == self.original_name
                ):
                    active["trigger"] = new_name
                    renamed += 1
            if renamed:
                save_active()

        await interaction.response.send_message(
            f"Updated `{self.original_name}` to `{new_name}`.",
            ephemeral=True
        )


class StickyViewSelect(discord.ui.Select):

    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(
            placeholder="Select a sticky note to view",
            options=note_options(guild_id)
        )

    async def callback(self, interaction: discord.Interaction):
        notes = get_guild_notes(self.guild_id)
        note = notes.get(self.values[0])

        if note is None:
            await interaction.response.edit_message(
                content="That sticky note no longer exists.",
                embed=None, view=None
            )
            return

        embed = discord.Embed(
            title=f"{self.values[0]}",
            description=note["message"],
            color=discord.Color.dark_theme()
        )
        embed.set_footer(text=f"Created by {note['creator']}")

        await interaction.response.edit_message(
            embed=embed,
            view=None,
            allowed_mentions=discord.AllowedMentions.none()
        )


class StickyViewSelectView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=120)
        self.add_item(StickyViewSelect(guild_id))


class StickyEditSelect(discord.ui.Select):

    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(
            placeholder="Select a sticky note to edit",
            options=note_options(guild_id)
        )

    async def callback(self, interaction: discord.Interaction):
        notes = get_guild_notes(self.guild_id)
        name = self.values[0]

        if name not in notes:
            await interaction.response.edit_message(
                content="That sticky note no longer exists.",
                embed=None, view=None
            )
            return

        modal = StickyEditModal(name)
        modal.message.default = notes[name]["message"]
        await interaction.response.send_modal(modal)


class StickyEditSelectView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=120)
        self.add_item(StickyEditSelect(guild_id))


class StickyDeleteSelect(discord.ui.Select):

    def __init__(self, guild_id):
        self.guild_id = guild_id
        super().__init__(
            placeholder="Select a sticky note to delete",
            options=note_options(guild_id)
        )

    async def callback(self, interaction: discord.Interaction):
        notes = get_guild_notes(self.guild_id)
        name = self.values[0]

        if name not in notes:
            await interaction.response.edit_message(
                content="That sticky note no longer exists.",
                embed=None, view=None
            )
            return

        del notes[name]
        save_data(sticky_notes)

        stale = [
            channel_id
            for channel_id, active in active_stickies.items()
            if active.get("guild_id") == self.guild_id
            and active.get("trigger") == name
        ]
        for channel_id in stale:
            del active_stickies[channel_id]
        if stale:
            save_active()

        await interaction.response.edit_message(
            content=f"Deleted `{name}`.", embed=None, view=None
        )


class StickyDeleteSelectView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=120)
        self.add_item(StickyDeleteSelect(guild_id))


class StickyPanelView(discord.ui.View):

    def __init__(self, guild_id, author_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.author_id = author_id
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            prefix = prefix_for(self.guild_id)
            await interaction.response.send_message(
                f"Run `{prefix}stickynote` yourself to use this panel.",
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

    @discord.ui.button(label="View", style=discord.ButtonStyle.secondary)
    async def view_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        notes = get_guild_notes(self.guild_id)

        if not notes:
            await interaction.response.send_message(
                "No sticky notes created yet.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Sticky Notes",
            description=f"Total Notes: **{len(notes)}**\n\nSelect one below.",
            color=discord.Color.dark_theme()
        )
        await interaction.response.edit_message(
            embed=embed, view=StickyViewSelectView(self.guild_id)
        )

    @discord.ui.button(label="Create", style=discord.ButtonStyle.success)
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StickyCreateModal())

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        notes = get_guild_notes(self.guild_id)
        if not notes:
            await interaction.response.send_message(
                "No sticky notes to edit.", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content="Select a sticky note to edit:",
            embed=None,
            view=StickyEditSelectView(self.guild_id)
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        notes = get_guild_notes(self.guild_id)
        if not notes:
            await interaction.response.send_message(
                "No sticky notes to delete.", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content="Select a sticky note to delete:",
            embed=None,
            view=StickyDeleteSelectView(self.guild_id)
        )


class StickyNotes(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self._restored = False
        self._locks = {}
        self._pending = {}
        self.flush_active.start()

    def cog_unload(self):
        self.flush_active.cancel()
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()
        save_active()

    def lock_for(self, channel_id):
        lock = self._locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[channel_id] = lock
        return lock

    @tasks.loop(seconds=FLUSH_SECONDS)
    async def flush_active(self):
        global _active_dirty
        if _active_dirty:
            _active_dirty = False
            save_active()

    @flush_active.before_loop
    async def before_flush(self):
        await self.bot.wait_until_ready()

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
            await ctx.send("This command only works in a server.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Slow down, try again in {error.retry_after:.0f}s.")
        else:
            await ctx.send(f"Something went wrong: {error}")

    @commands.Cog.listener()
    async def on_ready(self):
        """Rehydrate stickies after a restart. Runs once, not on reconnects."""
        if self._restored:
            return
        self._restored = True

        changed = False

        for channel_id, active in list(active_stickies.items()):
            channel = self.bot.get_channel(channel_id)

            if channel is None or not isinstance(channel, discord.TextChannel):
                del active_stickies[channel_id]
                changed = True
                continue

            note = get_guild_notes(channel.guild.id).get(active["trigger"])
            if note is None:
                del active_stickies[channel_id]
                changed = True
                continue

            try:
                async for old in channel.history(limit=HISTORY_SCAN):
                    if (
                        old.author.id == self.bot.user.id
                        and old.content == note["message"]
                    ):
                        try:
                            await old.delete()
                        except (discord.Forbidden, discord.NotFound):
                            pass
            except (discord.Forbidden, discord.HTTPException):
                pass

            sent = await self.safe_send(channel, note["message"])
            if sent is None:
                del active_stickies[channel_id]
                changed = True
                continue

            active_stickies[channel_id]["message_id"] = sent.id
            changed = True

        if changed:
            save_active()

    async def safe_send(self, channel, content):
        """Send a sticky, returning None if the bot can no longer post."""
        try:
            return await channel.send(
                content, allowed_mentions=discord.AllowedMentions.none()
            )
        except (discord.Forbidden, discord.HTTPException):
            return None

    @commands.hybrid_command(
        name="stickynote",
        aliases=["sn"],
        description="Open the sticky note panel.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @commands.guild_only()
    async def stickynote(self, ctx: commands.Context):
        notes = get_guild_notes(ctx.guild.id)

        embed = discord.Embed(
            title="Sticky Notes",
            description=(
                f"Total Notes: **{len(notes)}**\n\n"
                "**View** - browse existing sticky notes\n"
                "**Create** - add a new sticky note\n"
                "**Edit** - change a note's trigger or message\n"
                "**Delete** - remove a sticky note"
            ),
            color=discord.Color.dark_theme()
        )

        view = StickyPanelView(ctx.guild.id, ctx.author.id)
        view.message = await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(
        name="unsticky",
        aliases=["unsn"],
        description="Remove the active sticky note in this channel.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @commands.guild_only()
    async def unsticky(self, ctx: commands.Context):
        if ctx.channel.id not in active_stickies:
            await ctx.send("There's no active sticky note in this channel.")
            return

        await self._clear_active(ctx.channel)
        await ctx.send("Sticky note removed from this channel.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        notes = get_guild_notes(message.guild.id)
        channel_id = message.channel.id
        prefix = prefix_for(message.guild.id)

        if message.content.startswith(prefix):
            trigger = normalize_name(message.content[len(prefix):])

            if trigger in notes:
                if not can_manage(message.author):
                    return

                try:
                    await message.delete()
                except (discord.Forbidden, discord.NotFound):
                    pass

                await self._clear_active(message.channel)

                sent = await self.safe_send(
                    message.channel, notes[trigger]["message"]
                )
                if sent is None:
                    return

                active_stickies[channel_id] = {
                    "guild_id": message.guild.id,
                    "trigger": trigger,
                    "message_id": sent.id
                }
                save_active()
                return

        if channel_id not in active_stickies:
            return

        self.schedule_repost(message.channel)

    def schedule_repost(self, channel):
        """Queue a repost, replacing any repost already waiting.

        Bursts of chat collapse into one delete plus one send instead of a
        pair per message, which is what keeps a busy channel from burning
        through the rate limit.
        """
        existing = self._pending.get(channel.id)
        if existing is not None and not existing.done():
            existing.cancel()

        self._pending[channel.id] = asyncio.create_task(
            self._repost_after_delay(channel)
        )

    async def _repost_after_delay(self, channel):
        try:
            await asyncio.sleep(REPOST_DELAY)
        except asyncio.CancelledError:
            return

        async with self.lock_for(channel.id):
            active = active_stickies.get(channel.id)
            if active is None:
                return

            note = get_guild_notes(channel.guild.id).get(active["trigger"])
            if note is None:
                del active_stickies[channel.id]
                save_active()
                return

            try:
                old = await channel.fetch_message(active["message_id"])
                await old.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

            sent = await self.safe_send(channel, note["message"])
            if sent is None:
                del active_stickies[channel.id]
                save_active()
                return

            active_stickies[channel.id]["message_id"] = sent.id
            mark_dirty()

        self._pending.pop(channel.id, None)

    async def _clear_active(self, channel: discord.TextChannel):
        pending = self._pending.pop(channel.id, None)
        if pending is not None and not pending.done():
            pending.cancel()

        active = active_stickies.pop(channel.id, None)
        if active is None:
            return

        save_active()

        try:
            old_message = await channel.fetch_message(active["message_id"])
            await old_message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass


async def setup(bot):
    await bot.add_cog(StickyNotes(bot))