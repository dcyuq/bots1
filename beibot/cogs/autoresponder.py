import discord
from discord.ext import commands
from storage import Store

_store = Store("autoresponders.json")

MATCH_MODES = [
    ("exact", "Exact Match", "Message must match the trigger exactly"),
    ("contains", "Contains", "Trigger can appear anywhere in the message"),
    ("startswith", "Starts With", "Message must start with the trigger"),
    ("endswith", "Ends With", "Message must end with the trigger"),
]
MATCH_MODE_LABELS = {key: label for key, label, _ in MATCH_MODES}


def load_data():
    return _store.load()


def save_data(data):
    _store.save(data)


responders = load_data()


def get_guild_responders(guild_id):
    gid = str(guild_id)
    if gid not in responders:
        responders[gid] = {}
    return responders[gid]


def normalize_trigger(text: str) -> str:
    return text.strip().lower()


def can_manage(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_messages


def matches(content: str, trigger: str, mode: str) -> bool:
    content = content.lower()
    if mode == "exact":
        return content == trigger
    if mode == "contains":
        return trigger in content
    if mode == "startswith":
        return content.startswith(trigger)
    if mode == "endswith":
        return content.endswith(trigger)
    return False


class AutoResponderCreateModal(discord.ui.Modal, title="Create Autoresponder"):

    def __init__(self):
        super().__init__()

        self.trigger_input = discord.ui.TextInput(
            placeholder="Example: how do i join",
            required=True,
            max_length=200
        )
        self.trigger_label = discord.ui.Label(
            text="Trigger",
            description="What text should the bot watch for?",
            component=self.trigger_input
        )
        self.add_item(self.trigger_label)

        self.response_input = discord.ui.TextInput(
            placeholder="What should the bot respond with?",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000
        )
        self.response_label = discord.ui.Label(
            text="Response",
            component=self.response_input
        )
        self.add_item(self.response_label)

        self.matchmode_select = discord.ui.Select(
            options=[
                discord.SelectOption(label=label, value=key, description=desc)
                for key, label, desc in MATCH_MODES
            ]
        )
        self.matchmode_label = discord.ui.Label(
            text="Match Mode",
            description="How should the trigger be matched against messages?",
            component=self.matchmode_select
        )
        self.add_item(self.matchmode_label)

    async def on_submit(self, interaction: discord.Interaction):
        guild_responders = get_guild_responders(interaction.guild_id)
        trigger = normalize_trigger(self.trigger_input.value)

        if not trigger:
            await interaction.response.send_message(
                "That trigger isn't valid.", ephemeral=True
            )
            return

        if trigger in guild_responders:
            await interaction.response.send_message(
                f"An autoresponder for `{trigger}` already exists.",
                ephemeral=True
            )
            return

        mode = self.matchmode_select.values[0]

        guild_responders[trigger] = {
            "response": self.response_input.value,
            "matchmode": mode,
            "creator": interaction.user.mention
        }
        save_data(responders)

        await interaction.response.send_message(
            f"Autoresponder created for `{trigger}` "
            f"({MATCH_MODE_LABELS[mode]}).",
            ephemeral=True
        )


class AutoResponderEditModal(discord.ui.Modal):

    def __init__(self, trigger: str, current: dict):
        super().__init__(title=f"Edit '{trigger}'")
        self.original_trigger = trigger

        self.trigger_input = discord.ui.TextInput(
            default=trigger,
            required=True,
            max_length=200
        )
        self.trigger_label = discord.ui.Label(
            text="Trigger",
            description="What text should the bot watch for?",
            component=self.trigger_input
        )
        self.add_item(self.trigger_label)

        self.response_input = discord.ui.TextInput(
            default=current["response"],
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000
        )
        self.response_label = discord.ui.Label(
            text="Response",
            component=self.response_input
        )
        self.add_item(self.response_label)

        self.matchmode_select = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label=label,
                    value=key,
                    description=desc,
                    default=(key == current["matchmode"])
                )
                for key, label, desc in MATCH_MODES
            ]
        )
        self.matchmode_label = discord.ui.Label(
            text="Match Mode",
            description="How should the trigger be matched against messages?",
            component=self.matchmode_select
        )
        self.add_item(self.matchmode_label)

    async def on_submit(self, interaction: discord.Interaction):
        guild_responders = get_guild_responders(interaction.guild_id)

        if self.original_trigger not in guild_responders:
            await interaction.response.send_message(
                "That autoresponder no longer exists.", ephemeral=True
            )
            return

        new_trigger = normalize_trigger(self.trigger_input.value)
        if not new_trigger:
            await interaction.response.send_message(
                "That trigger isn't valid.", ephemeral=True
            )
            return

        if new_trigger != self.original_trigger and new_trigger in guild_responders:
            await interaction.response.send_message(
                f"An autoresponder for `{new_trigger}` already exists.",
                ephemeral=True
            )
            return

        mode = self.matchmode_select.values[0]

        data = guild_responders.pop(self.original_trigger)
        data["response"] = self.response_input.value
        data["matchmode"] = mode
        guild_responders[new_trigger] = data
        save_data(responders)

        await interaction.response.send_message(
            f"Updated `{self.original_trigger}` → `{new_trigger}` "
            f"({MATCH_MODE_LABELS[mode]}).",
            ephemeral=True
        )


class ResponderEditSelect(discord.ui.Select):

    def __init__(self, guild_id):
        self.guild_id = guild_id
        guild_responders = get_guild_responders(guild_id)

        options = [
            discord.SelectOption(label=trigger, value=trigger)
            for trigger in guild_responders
        ][:25]

        super().__init__(
            placeholder="Select an autoresponder to edit",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        guild_responders = get_guild_responders(self.guild_id)
        trigger = self.values[0]

        if trigger not in guild_responders:
            await interaction.response.edit_message(
                content="That autoresponder no longer exists.",
                embed=None, view=None
            )
            return

        modal = AutoResponderEditModal(trigger, guild_responders[trigger])
        await interaction.response.send_modal(modal)


class ResponderEditSelectView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=120)
        self.add_item(ResponderEditSelect(guild_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need Administrator or Manage Messages permission.",
                ephemeral=True
            )
            return False
        return True


class ResponderDeleteSelect(discord.ui.Select):

    def __init__(self, guild_id):
        self.guild_id = guild_id
        guild_responders = get_guild_responders(guild_id)

        options = [
            discord.SelectOption(label=trigger, value=trigger)
            for trigger in guild_responders
        ][:25]

        super().__init__(
            placeholder="Select an autoresponder to delete",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        guild_responders = get_guild_responders(self.guild_id)
        trigger = self.values[0]

        if trigger not in guild_responders:
            await interaction.response.edit_message(
                content="That autoresponder no longer exists.",
                embed=None, view=None
            )
            return

        del guild_responders[trigger]
        save_data(responders)

        await interaction.response.edit_message(
            content=f"Deleted `{trigger}`.", embed=None, view=None
        )


class ResponderDeleteSelectView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=120)
        self.add_item(ResponderDeleteSelect(guild_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need Administrator or Manage Messages permission.",
                ephemeral=True
            )
            return False
        return True


class AutoResponderPanelView(discord.ui.View):

    def __init__(self, guild_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need Administrator or Manage Messages permission.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Create", style=discord.ButtonStyle.success)
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AutoResponderCreateModal())

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_responders = get_guild_responders(self.guild_id)
        if not guild_responders:
            await interaction.response.send_message(
                "No autoresponders to edit.", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content="Select an autoresponder to edit:",
            embed=None,
            view=ResponderEditSelectView(self.guild_id)
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_responders = get_guild_responders(self.guild_id)
        if not guild_responders:
            await interaction.response.send_message(
                "No autoresponders to delete.", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content="Select an autoresponder to delete:",
            embed=None,
            view=ResponderDeleteSelectView(self.guild_id)
        )


class AutoResponder(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="autoresponder", aliases=["ar"])
    @commands.guild_only()
    async def autoresponder(self, ctx: commands.Context):
        if not can_manage(ctx.author):
            await ctx.send(
                "You need Administrator or Manage Messages permission to use this."
            )
            return

        guild_responders = get_guild_responders(ctx.guild.id)

        embed = discord.Embed(
            title="Autoresponders",
            description=(
                f"Total: **{len(guild_responders)}**\n\n"
                "**Create** — add a new autoresponder\n"
                "**Edit** — change a trigger, response, or match mode\n"
                "**Delete** — remove an autoresponder"
            ),
            color=discord.Color.dark_theme()
        )

        await ctx.send(embed=embed, view=AutoResponderPanelView(ctx.guild.id))


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        guild_responders = get_guild_responders(message.guild.id)
        if not guild_responders:
            return

        content = message.content
        if not content:
            return

        for trigger, entry in guild_responders.items():
            if matches(content, trigger, entry["matchmode"]):
                await message.channel.send(entry["response"])
                return


async def setup(bot):
    await bot.add_cog(AutoResponder(bot))