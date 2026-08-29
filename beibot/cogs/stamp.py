import os
import time

import discord
from discord import app_commands
from discord.ext import commands

from storage import Store

PUBLIC_COMMANDS = ("stamps",)
PUBLIC_SUBCOMMANDS = ("stamp profile",)

MAX_STAGES = 21
ALLOWED_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp")

NEUTRAL = 0x99AAB5
COMPLETE = 0x57F287

BLOCK_WORDS = ("off", "none", "no", "nothing", "remove")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE_ROOT = os.path.join(ROOT, "data", "stamp")


def guild_dir(guild_id):
    return os.path.join(IMAGE_ROOT, str(guild_id))


def stage_path(guild_id, filename):
    return os.path.join(guild_dir(guild_id), filename)


def default_config():
    return {"stages": {}, "milestones": {}, "version": 0}


def milestones_for(config):
    marks = {}
    for key, value in config.get("milestones", {}).items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if value:
            marks[index] = value
    return marks


def next_milestone(config, count, maximum):
    marks = milestones_for(config)
    for index in range(count + 1, maximum + 1):
        if index in marks:
            return index, marks[index]
    return 0, ""


def blank_record():
    return {"count": 0, "completed": 0, "first_seen": 0.0, "last": 0.0, "active": False}


def card_active(record):
    """Whether this member is currently holding a card.

    Records written before cards had to be issued carry no active flag, so
    a missing key means yes. blank_record sets it to False explicitly, which
    is what makes an unknown member read as having no card.
    """
    return bool(record.get("active", True))


def suffix_for(attachment):
    name = (attachment.filename or "").lower()
    for suffix in ALLOWED_SUFFIX:
        if name.endswith(suffix):
            return suffix
    return ""


def remaining_text(count, maximum):
    """The single subtext line shown under a card: stamps left to fill it."""
    remaining = max(0, maximum - count)
    word = "stamp" if remaining == 1 else "stamps"
    return "-# " + str(remaining) + " " + word + " remaining"


class OverrideModal(discord.ui.Modal, title="Override Stamps"):
    def __init__(self, cog, member, maximum, record):
        super().__init__(timeout=300)
        self.cog = cog
        self.member = member
        self.maximum = maximum
        self.count = discord.ui.TextInput(
            label="Stamps on the current card",
            placeholder="0 to " + str(maximum),
            default=str(record["count"]),
            max_length=3,
            required=True,
        )
        self.completed = discord.ui.TextInput(
            label="Full cards finished before now",
            placeholder="Leave as is to keep the current total",
            default=str(record["completed"]),
            max_length=4,
            required=False,
        )
        self.add_item(self.count)
        self.add_item(self.completed)

    async def on_submit(self, interaction):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "You need the Manage Messages permission to do that.", ephemeral=True
            )
            return

        try:
            count = int(self.count.value.strip())
        except ValueError:
            await interaction.response.send_message("Stamps must be a whole number.", ephemeral=True)
            return

        if count < 0 or count > self.maximum:
            await interaction.response.send_message(
                "Stamps must be between 0 and " + str(self.maximum) + ".", ephemeral=True
            )
            return

        completed = None
        raw = self.completed.value.strip()
        if raw:
            try:
                completed = max(0, int(raw))
            except ValueError:
                await interaction.response.send_message(
                    "Full cards must be a whole number.", ephemeral=True
                )
                return

        record = self.cog.write_record(interaction.guild.id, self.member.id, count, completed)
        await self.cog.refresh_card(
            interaction, self.member, count, record["completed"], "Card was set manually.", False
        )


class ConfirmReset(discord.ui.View):
    def __init__(self, cog, member, parent):
        super().__init__(timeout=60)
        self.cog = cog
        self.member = member
        self.parent = parent

    async def interaction_check(self, interaction):
        if interaction.user.guild_permissions.manage_messages:
            return True
        await interaction.response.send_message(
            "You need the Manage Messages permission to do that.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Reset stamps", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        record = self.cog.write_record(interaction.guild.id, self.member.id, 0, None)
        await interaction.response.edit_message(
            content="Reset the current card for " + self.member.display_name + ".", view=None
        )
        await self.cog.refresh_card(
            None, self.member, 0, record["completed"], "Card was reset.", False, message=self.parent
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.edit_message(content="Nothing was changed.", view=None)
        self.stop()


class StaffView(discord.ui.View):
    def __init__(self, cog, member):
        super().__init__(timeout=900)
        self.cog = cog
        self.member = member

    async def interaction_check(self, interaction):
        if interaction.user.guild_permissions.manage_messages:
            return True
        await interaction.response.send_message(
            "You need the Manage Messages permission to do that.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Stamp", style=discord.ButtonStyle.success)
    async def add_one(self, interaction, button):
        status, record, notes, celebrate, display = self.cog.push_stamp(
            interaction.guild.id, self.member.id, 1
        )
        if status == "nosetup":
            await interaction.response.send_message("Stamp cards are not set up.", ephemeral=True)
            return
        if status == "nocard":
            await interaction.response.send_message(
                self.member.display_name + " has no active card. Give them one first.",
                ephemeral=True,
            )
            return
        await self.cog.refresh_card(
            interaction, self.member, display, record["completed"], notes, celebrate
        )

    @discord.ui.button(label="Override", style=discord.ButtonStyle.secondary)
    async def override(self, interaction, button):
        config = self.cog.config_for(interaction.guild.id)
        maximum = self.cog.maximum_for(interaction.guild.id, config)
        record = self.cog.record_for(interaction.guild.id, self.member.id)
        await interaction.response.send_modal(
            OverrideModal(self.cog, self.member, maximum, record)
        )

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.danger)
    async def reset(self, interaction, button):
        await interaction.response.send_message(
            "Reset the current card for " + self.member.display_name + "? Finished cards are kept.",
            view=ConfirmReset(self.cog, self.member, interaction.message),
            ephemeral=True,
        )


class IssueView(discord.ui.View):
    def __init__(self, cog, member):
        super().__init__(timeout=900)
        self.cog = cog
        self.member = member

    async def interaction_check(self, interaction):
        if interaction.user.guild_permissions.manage_messages:
            return True
        await interaction.response.send_message(
            "You need the Manage Messages permission to do that.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Give a card", style=discord.ButtonStyle.success)
    async def give(self, interaction, button):
        record = self.cog.issue_card(interaction.guild.id, self.member.id)
        if record is None:
            await interaction.response.send_message(
                self.member.display_name + " already has a card.", ephemeral=True
            )
            return
        await self.cog.refresh_card(
            interaction, self.member, 0, record["completed"], "New card issued.", False
        )


class Stamp(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = Store("stamps.json")

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        qualified = ctx.command.qualified_name
        if qualified in PUBLIC_SUBCOMMANDS:
            return True
        root = qualified.split()[0]
        if root in PUBLIC_COMMANDS:
            return True
        if ctx.author.guild_permissions.manage_messages:
            return True
        raise commands.MissingPermissions(["manage_messages"])

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the Manage Messages permission to use that.")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send("Slow down, try again in " + str(round(error.retry_after, 1)) + " seconds.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("I could not find that member.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Stamp cards only work inside a server.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.send("Bad arguments, Run " + ctx.prefix + "stamp setup to see the usage.")
        else:
            raise error

    def guild_block(self, data, guild_id):
        key = str(guild_id)
        block = data.get(key)
        if block is None:
            block = {"config": default_config(), "users": {}}
            data[key] = block
        block.setdefault("config", default_config())
        block.setdefault("users", {})
        for field, value in default_config().items():
            block["config"].setdefault(field, value)
        return block

    def config_for(self, guild_id):
        return self.guild_block(self.store.load(), guild_id)["config"]

    def stage_count(self, guild_id, config):
        stages = config.get("stages", {})
        total = 0
        while str(total) in stages and os.path.isfile(
            stage_path(guild_id, stages[str(total)])
        ):
            total += 1
        return total

    def maximum_for(self, guild_id, config):
        return max(0, self.stage_count(guild_id, config) - 1)

    def missing_stages(self, guild_id, config):
        stages = config.get("stages", {})
        highest = -1
        for key in stages:
            try:
                highest = max(highest, int(key))
            except ValueError:
                continue
        gaps = []
        for index in range(highest + 1):
            filename = stages.get(str(index))
            if not filename or not os.path.isfile(stage_path(guild_id, filename)):
                gaps.append(index)
        return gaps, highest

    def stage_file(self, guild_id, config, count):
        filename = config.get("stages", {}).get(str(count))
        if not filename:
            return None
        path = stage_path(guild_id, filename)
        if not os.path.isfile(path):
            return None
        return path

    def record_for(self, guild_id, user_id):
        block = self.guild_block(self.store.load(), guild_id)
        return block["users"].get(str(user_id), blank_record())

    def write_record(self, guild_id, user_id, count, completed, active=True):
        data = self.store.load()
        block = self.guild_block(data, guild_id)
        record = block["users"].get(str(user_id), blank_record())
        now = time.time()
        if not record["first_seen"]:
            record["first_seen"] = now
        record["count"] = count
        if completed is not None:
            record["completed"] = max(0, completed)
        record["active"] = bool(active)
        record["last"] = now
        block["users"][str(user_id)] = record
        self.store.save(data)
        return record

    def issue_card(self, guild_id, user_id):
        """Hand a member a fresh blank card.

        Returns None when they are already holding one, so the caller can
        say so rather than quietly wiping progress.
        """
        data = self.store.load()
        block = self.guild_block(data, guild_id)
        record = block["users"].get(str(user_id), blank_record())
        if str(user_id) in block["users"] and card_active(record):
            return None

        now = time.time()
        if not record["first_seen"]:
            record["first_seen"] = now
        record["count"] = 0
        record["active"] = True
        record["last"] = now
        block["users"][str(user_id)] = record
        self.store.save(data)
        return record

    def push_stamp(self, guild_id, user_id, delta):
        """Move a member's count and report what happened.

        Returns a status first so callers can tell the three failure modes
        apart. A card stops at its last stamp rather than rolling over, and
        anything left in the batch is reported instead of silently starting
        the next card.
        """
        config = self.config_for(guild_id)
        if self.stage_count(guild_id, config) < 2:
            return "nosetup", None, "", False, 0

        maximum = self.maximum_for(guild_id, config)
        marks = milestones_for(config)

        data = self.store.load()
        block = self.guild_block(data, guild_id)
        record = block["users"].get(str(user_id), blank_record())

        if not card_active(record):
            return "nocard", record, "", False, 0

        now = time.time()
        if not record["first_seen"]:
            record["first_seen"] = now

        total = record["count"]
        finished = False
        reached = []
        leftover = 0

        if delta > 0:
            for step in range(delta):
                if finished:
                    leftover = delta - step
                    break
                total += 1
                if total in marks:
                    reached.append((total, marks[total]))
                if total >= maximum:
                    finished = True
        else:
            total = max(0, total + delta)

        display = total

        if finished:
            record["completed"] = max(0, record["completed"] + 1)
            record["active"] = False
            record["count"] = 0
        else:
            record["count"] = total

        record["last"] = now
        block["users"][str(user_id)] = record
        self.store.save(data)

        notes = []
        for index, prize in reached:
            notes.append("Reward unlocked at " + str(index) + ": " + prize)
        if finished:
            notes.append("Card complete. Ask staff for a new one to keep going.")
        if leftover:
            word = "stamp" if leftover == 1 else "stamps"
            notes.append(
                str(leftover) + " " + word + " were not applied, the card filled up first."
            )

        return "ok", record, "\n".join(notes), bool(finished or reached), display

    def build_card(self, guild_id, member, count, completed, staff):
        """The card is now just the stamp image plus a single subtext line.

        Returns (file, content, view). note/complete no longer change the
        display, so callers may still pass them but they are ignored here.
        """
        config = self.config_for(guild_id)
        maximum = self.maximum_for(guild_id, config)
        path = self.stage_file(guild_id, config, count)
        if path is None:
            return None, None, None

        filename = "stamp-card" + os.path.splitext(path)[1]
        file = discord.File(path, filename=filename)
        content = remaining_text(count, maximum)
        view = StaffView(self, member) if staff else None
        return file, content, view

    async def send_card(self, ctx, member, config, count, completed, note="", complete=False):
        staff = ctx.author.guild_permissions.manage_messages
        file, content, view = self.build_card(ctx.guild.id, member, count, completed, staff)
        if file is None:
            await ctx.send(
                "No image is set for " + str(count) + " stamps. Run " + ctx.prefix + "stamp setup."
            )
            return
        await ctx.send(
            content,
            file=file,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def refresh_card(
        self, interaction, member, count, completed, note="", complete=False, message=None
    ):
        guild_id = interaction.guild.id if interaction is not None else message.guild.id
        file, content, view = self.build_card(guild_id, member, count, completed, True)
        if file is None:
            if interaction is not None:
                await interaction.response.send_message(
                    "No image is set for " + str(count) + " stamps.", ephemeral=True
                )
            return

        if interaction is not None:
            await interaction.response.edit_message(
                content=content, attachments=[file], view=view
            )
        else:
            await message.edit(content=content, attachments=[file], view=view)

    def collect_images(self, ctx):
        attachments = list(ctx.message.attachments)
        reference = ctx.message.reference
        if not attachments and reference is not None:
            resolved = reference.resolved
            if isinstance(resolved, discord.Message):
                attachments = list(resolved.attachments)
        return attachments

    async def store_stage(self, ctx, attachment, index):
        suffix = suffix_for(attachment)
        if not suffix:
            return "Stage " + str(index) + ": unsupported file type, use png, jpg, gif or webp."
        if attachment.size > ctx.guild.filesize_limit:
            return "Stage " + str(index) + ": file is too large for this server."

        folder = guild_dir(ctx.guild.id)
        os.makedirs(folder, exist_ok=True)

        for old in ALLOWED_SUFFIX:
            stale = os.path.join(folder, "stage_" + str(index) + old)
            if os.path.isfile(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass

        filename = "stage_" + str(index) + suffix
        await attachment.save(os.path.join(folder, filename))
        return filename

    @commands.hybrid_command(
        name="stamps",
        aliases=["mycard", "stampcard"],
        description="Show a stamp card.",
    )
    @app_commands.describe(member="Whose card to show. Defaults to your own.")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def stamps(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        config = self.config_for(ctx.guild.id)
        if self.stage_count(ctx.guild.id, config) < 2:
            ctx.command.reset_cooldown(ctx)
            await ctx.send("Stamp cards are not set up here yet.")
            return
        record = self.guild_block(self.store.load(), ctx.guild.id)["users"].get(
            str(member.id), blank_record()
        )
        if not card_active(record):
            ctx.command.reset_cooldown(ctx)
            await self.send_no_card(ctx, member)
            return
        await self.send_card(ctx, member, config, record["count"], record["completed"], "")

    @commands.hybrid_command(
        name="givestampcard",
        aliases=["givecard", "issuecard", "newcard"],
        description="Hand a member a fresh blank stamp card.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(member="Who to give a card to")
    @commands.guild_only()
    async def give_stamp_card(self, ctx, member: discord.Member):
        if member.bot:
            await ctx.send("Bots do not collect stamps.")
            return

        config = self.config_for(ctx.guild.id)
        if self.stage_count(ctx.guild.id, config) < 2:
            await ctx.send("Run " + ctx.prefix + "stamp setup first.")
            return

        record = self.issue_card(ctx.guild.id, member.id)
        if record is None:
            current = self.record_for(ctx.guild.id, member.id)
            await ctx.send(
                member.display_name + " already has a card with "
                + str(current["count"]) + " stamp(s) on it. Use "
                + ctx.prefix + "stamp clear to wipe them first."
            )
            return

        note = "New card issued."
        if record["completed"]:
            word = "card" if record["completed"] == 1 else "cards"
            note += " That is " + str(record["completed"] + 1) + " overall, with "
            note += str(record["completed"]) + " full " + word + " already done."
        await self.send_card(ctx, member, config, 0, record["completed"], note)

    @commands.hybrid_group(name="stamp", invoke_without_command=True, fallback="give")
    @commands.guild_only()
    async def stamp(self, ctx, member: discord.Member = None):
        if member is None:
            await self.show_setup(ctx)
            return
        await self.apply(ctx, member, 1)

    @stamp.command(name="profile", description="Check stamp card progress.")
    @app_commands.describe(member="Whose progress to check. Defaults to your own.")
    async def stamp_profile(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        config = self.config_for(ctx.guild.id)
        if self.stage_count(ctx.guild.id, config) < 2:
            await ctx.send("Stamp cards are not set up here yet.")
            return

        maximum = self.maximum_for(ctx.guild.id, config)
        record = self.record_for(ctx.guild.id, member.id)
        count = record["count"]
        completed = record.get("completed", 0)

        who = "Your" if member.id == ctx.author.id else member.display_name + "'s"
        lines = ["**" + who + " stamp progress**"]

        if card_active(record):
            remaining = max(0, maximum - count)
            word = "stamp" if remaining == 1 else "stamps"
            lines.append(str(count) + " of " + str(maximum) + " stamps on the current card.")
            lines.append(str(remaining) + " " + word + " to go until it is complete.")

            upcoming, prize = next_milestone(config, count, maximum)
            if prize:
                left = upcoming - count
                word = "stamp" if left == 1 else "stamps"
                lines.append(
                    "Next reward at " + str(upcoming)
                    + " (" + str(left) + " " + word + " away): " + prize
                )
        else:
            lines.append("No active card right now. Ask a staff member for one.")

        if completed:
            word = "card" if completed == 1 else "cards"
            lines.append(str(completed) + " full " + word + " finished so far.")

        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @stamp.command(name="add", description="Give a member one or more stamps.")
    @app_commands.describe(member="Who to stamp", amount="How many to add")
    async def stamp_add(self, ctx, member: discord.Member, amount: int = 1):
        if amount < 1:
            await ctx.send("Amount must be at least 1.")
            return
        await self.apply(ctx, member, amount)

    @stamp.command(
        name="remove",
        aliases=["undo"],
        description="Take stamps back off a member's card.",
    )
    @app_commands.describe(member="Whose card to correct", amount="How many to remove")
    async def stamp_remove(self, ctx, member: discord.Member, amount: int = 1):
        if amount < 1:
            await ctx.send("Amount must be at least 1.")
            return
        await self.apply(ctx, member, -amount)

    @stamp.command(
        name="set",
        aliases=["override"],
        description="Set a member's card to an exact count.",
    )
    @app_commands.describe(
        member="Whose card to set",
        count="Stamps on the current card",
        completed="Full cards finished before now",
    )
    async def stamp_set(self, ctx, member: discord.Member, count: int, completed: int = None):
        config = self.config_for(ctx.guild.id)
        if self.stage_count(ctx.guild.id, config) < 2:
            await ctx.send("Run " + ctx.prefix + "stamp setup first.")
            return
        maximum = self.maximum_for(ctx.guild.id, config)
        if count < 0 or count > maximum:
            await ctx.send("Count must be between 0 and " + str(maximum) + ".")
            return

        record = self.write_record(ctx.guild.id, member.id, count, completed)
        await self.send_card(
            ctx, member, config, count, record["completed"], "Card was set manually."
        )

    @stamp.command(name="clear", description="Delete a member's stamp record.")
    @app_commands.describe(member="Whose record to delete")
    async def stamp_clear(self, ctx, member: discord.Member):
        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        if block["users"].pop(str(member.id), None) is None:
            await ctx.send(member.display_name + " has no stamp record.")
            return
        self.store.save(data)
        await ctx.send("Cleared the stamp record for " + member.display_name + ".")

    async def apply(self, ctx, member, delta):
        if member.bot:
            await ctx.send("Bots do not collect stamps.")
            return

        status, record, notes, celebrate, display = self.push_stamp(
            ctx.guild.id, member.id, delta
        )
        if status == "nosetup":
            await ctx.send("Run " + ctx.prefix + "stamp setup first.")
            return
        if status == "nocard":
            await ctx.send(
                member.display_name + " has no active card. Issue one with "
                + ctx.prefix + "givestampcard " + member.display_name + "."
            )
            return

        config = self.config_for(ctx.guild.id)
        await self.send_card(
            ctx, member, config, display, record["completed"], notes, complete=celebrate
        )

    async def send_no_card(self, ctx, member):
        """No active card means there is no image to show, so this stays a
        short line, plus a button for staff to hand one over."""
        staff = ctx.author.guild_permissions.manage_messages
        mine = member.id == ctx.author.id

        if mine:
            content = "You do not have a card yet. Ask a staff member for one."
        else:
            content = member.display_name + " does not have a card right now."

        view = IssueView(self, member) if staff else None
        await ctx.send(content, view=view, allowed_mentions=discord.AllowedMentions.none())

    @stamp.group(
        name="setup",
        aliases=["config"],
        invoke_without_command=True,
        fallback="show",
        description="Show the stamp card setup and what is still missing.",
    )
    async def setup(self, ctx):
        await self.show_setup(ctx)

    async def show_setup(self, ctx):
        config = self.config_for(ctx.guild.id)
        total = self.stage_count(ctx.guild.id, config)
        gaps, highest = self.missing_stages(ctx.guild.id, config)
        prefix = ctx.prefix

        if total >= 2:
            status = (
                "Ready. "
                + str(total)
                + " images loaded, covering 0 through "
                + str(total - 1)
                + " stamps."
            )
        elif highest < 0:
            status = "Nothing uploaded yet."
        else:
            status = "Incomplete. Missing images for " + ", ".join(str(g) for g in gaps) + "."

        body = (
            "**Stamp Card Setup**\n\n"
            + status
            + "\n\nUpload one image per stamp count, starting with the blank card. "
            "For an eight stamp card that is nine images, blank through full.\n\n"
            + prefix
            + "`stamp setup upload` with the images attached in order, blank card first\n"
            + prefix
            + "`stamp setup image 3` with one image attached to replace a single stage\n"
            + prefix
            + "`stamp setup reward 8 free milk tea` to set a reward at any stamp number\n"
            + prefix
            + "`stamp preview 3`\n\n"
            "Discord allows **10 attachments per message**, so longer cards need a __second__ upload. "
            "Just run " + prefix + "`stamp setup upload` again with the next batch and it "
            "carries on from where the last one stopped. "
            "The number of images sets the card length, so nine images means eight stamps then a reset."
        )

        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_colour=NEUTRAL)
        container.add_item(discord.ui.TextDisplay(body))
        view.add_item(container)
        await ctx.send(view=view)

    @setup.command(name="upload", aliases=["images"], with_app_command=False)
    async def setup_upload(self, ctx, *, options: str = ""):
        """Save attached images to consecutive stages.

        With no options the batch is appended after the last stage already
        uploaded, so a card longer than ten images needs no arithmetic. A
        bare number overrides that and starts there instead. The word
        reverse flips the batch, which is the fix when a desktop file
        picker has handed the images over newest first.
        """
        attachments = self.collect_images(ctx)
        if not attachments:
            await ctx.send("Attach your card images in order, or reply to a message holding them.")
            return

        start = None
        reverse = False
        for token in options.split():
            lowered = token.lower()
            if lowered in ("reverse", "rev", "desc", "backwards"):
                reverse = True
                continue
            try:
                start = int(token)
            except ValueError:
                await ctx.send("I did not understand `" + token + "`. Give a stage number, or reverse.")
                return

        config = self.config_for(ctx.guild.id)
        if start is None:
            start = self.stage_count(ctx.guild.id, config)

        if start < 0 or start + len(attachments) > MAX_STAGES:
            await ctx.send("A card can hold at most " + str(MAX_STAGES) + " stages.")
            return

        if reverse:
            attachments = list(reversed(attachments))

        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        stages = block["config"]["stages"]

        saved = []
        problems = []
        for offset, attachment in enumerate(attachments):
            index = start + offset
            result = await self.store_stage(ctx, attachment, index)
            if result.startswith("Stage "):
                problems.append(result)
                continue
            stages[str(index)] = result
            saved.append((index, attachment.filename))

        block["config"]["version"] = int(block["config"].get("version", 0)) + 1
        self.store.save(data)

        total = self.stage_count(ctx.guild.id, block["config"])
        lines = ["Saved " + str(len(saved)) + " image(s)."]
        for index, filename in saved:
            label = "blank card" if index == 0 else "stamp " + str(index)
            lines.append("Stage " + str(index) + " (" + label + ") is " + filename)
        if problems:
            lines.extend(problems)

        lines.append("")
        if total >= 2:
            lines.append("Card is ready with " + str(total - 1) + " stamps before a reset.")
        else:
            lines.append("Upload at least a blank card and one stamped version to go live.")

        gaps, highest = self.missing_stages(ctx.guild.id, block["config"])
        if gaps:
            listed = ", ".join(str(index) for index in gaps)
            lines.append(
                "Stage " + listed + " is still empty, so the card stops short at "
                + str(total - 1) + ". Images above the gap are ignored until it is filled."
            )
        elif len(saved) < MAX_STAGES - start:
            lines.append(
                "Attach the next batch and run " + ctx.prefix
                + "`stamp setup upload` again to carry on from stage " + str(total) + "."
            )

        if saved:
            lines.append(
                "Wrong way round? " + ctx.prefix + "`stamp setup upload "
                + str(start) + " reverse` redoes this batch backwards."
            )

        await ctx.send("\n".join(lines)[:2000])

    @setup.command(
        name="image",
        aliases=["stage"],
        description="Set the image for one stage of the card.",
    )
    @app_commands.describe(
        index="Which stage, where 0 is the blank card",
        image="The image to use. Prefix users can attach or reply instead.",
    )
    async def setup_image(self, ctx, index: int, image: discord.Attachment = None):
        if index < 0 or index >= MAX_STAGES:
            await ctx.send("Stage must be between 0 and " + str(MAX_STAGES - 1) + ".")
            return
        attachments = [image] if image is not None else self.collect_images(ctx)
        if not attachments:
            await ctx.send("Attach one image, or reply to a message holding it.")
            return

        result = await self.store_stage(ctx, attachments[0], index)
        if result.startswith("Stage "):
            await ctx.send(result)
            return

        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        block["config"]["stages"][str(index)] = result
        block["config"]["version"] = int(block["config"].get("version", 0)) + 1
        self.store.save(data)
        await ctx.send("Stage " + str(index) + " updated.")

    @setup.command(
        name="reward",
        aliases=["rewards"],
        description="Set, clear or list the reward attached to a stamp number.",
    )
    @app_commands.describe(
        target="A stamp number, or list, or clear",
        text="What they get. Leave blank to remove the reward.",
    )
    async def setup_reward(self, ctx, target: str = None, *, text: str = ""):
        target = (target or "").strip()
        text = text.strip()

        if not target or target.lower() in ("list", "show"):
            await self.show_rewards(ctx)
            return

        if target.lower() in ("clear", "unset", "remove"):
            await self.clear_reward(ctx, text)
            return

        try:
            count = int(target)
        except ValueError:
            await ctx.send("Give a stamp number, or list, or clear.")
            return

        config = self.config_for(ctx.guild.id)
        maximum = self.maximum_for(ctx.guild.id, config)
        if count < 1 or count >= MAX_STAGES:
            await ctx.send("Pick a stamp number between 1 and " + str(MAX_STAGES - 1) + ".")
            return

        text = text[:200]
        if not text or text.lower() in BLOCK_WORDS:
            await self.drop_milestone(ctx, count)
            return

        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        block["config"].setdefault("milestones", {})[str(count)] = text
        self.store.save(data)

        reply = "Reward at stamp " + str(count) + " set to: " + text
        if maximum and count > maximum:
            reply += "\nHeads up, this card only goes to " + str(maximum) + " so nobody will reach it yet."
        await ctx.send(reply)

    async def drop_milestone(self, ctx, count):
        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        marks = block["config"].setdefault("milestones", {})
        if marks.pop(str(count), None) is None:
            await ctx.send("There is no reward set at stamp " + str(count) + ".")
            return
        self.store.save(data)
        await ctx.send("Removed the reward at stamp " + str(count) + ".")

    async def clear_reward(self, ctx, target):
        """Remove one reward, or every reward when given all."""
        target = (target or "").strip().lower()

        if target == "all":
            data = self.store.load()
            block = self.guild_block(data, ctx.guild.id)
            block["config"]["milestones"] = {}
            self.store.save(data)
            await ctx.send("Every reward was removed.")
            return

        try:
            count = int(target)
        except ValueError:
            await ctx.send("Give a stamp number, or all.")
            return
        await self.drop_milestone(ctx, count)

    async def show_rewards(self, ctx):
        config = self.config_for(ctx.guild.id)
        maximum = self.maximum_for(ctx.guild.id, config)
        marks = milestones_for(config)

        lines = ["**Rewards**", ""]
        if not marks:
            lines.append("No rewards are set.")
        else:
            for index in sorted(marks):
                label = "Stamp " + str(index) + ": " + marks[index]
                if maximum and index == maximum:
                    label += " (card complete)"
                elif maximum and index > maximum:
                    label += " (beyond this card)"
                lines.append(label)

        lines.append("")
        lines.append(
            "Set one with `" + ctx.prefix + "stamp setup reward 4 free cookie`, "
            "remove it with `" + ctx.prefix + "stamp setup reward clear 4`."
        )

        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_colour=NEUTRAL)
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        view.add_item(container)
        await ctx.send(view=view)

    @setup.command(
        name="reset",
        description="Delete every uploaded card image. Collected stamps are kept.",
    )
    async def setup_reset(self, ctx):
        data = self.store.load()
        block = self.guild_block(data, ctx.guild.id)
        stages = block["config"].get("stages", {})
        for filename in list(stages.values()):
            try:
                os.remove(stage_path(ctx.guild.id, filename))
            except OSError:
                pass
        block["config"] = default_config()
        self.store.save(data)
        await ctx.send("Stamp card images cleared. Collected stamps were kept.")

    @stamp.command(name="preview", description="Preview one stage of the card.")
    @app_commands.describe(count="Which stage to render. Defaults to the last one.")
    async def stamp_preview(self, ctx, count: int = None):
        config = self.config_for(ctx.guild.id)
        total = self.stage_count(ctx.guild.id, config)
        if total < 1:
            await ctx.send("Nothing uploaded yet.")
            return
        maximum = total - 1
        if count is None:
            count = maximum
        count = max(0, min(count, maximum))
        path = self.stage_file(ctx.guild.id, config, count)
        if path is None:
            await ctx.send("No image is set for that stage.")
            return
        filename = "stamp-preview" + os.path.splitext(path)[1]
        await ctx.send(
            "Stage " + str(count) + " of " + str(maximum) + ".",
            file=discord.File(path, filename=filename),
        )


async def setup(bot):
    await bot.add_cog(Stamp(bot))