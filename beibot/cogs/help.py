import discord
from discord.ext import commands
from prefixes import prefix_for

ACCENT = discord.Color.dark_theme()

CATEGORY_META = {
    "Moderation": "Kick, ban and timeout members.",
    "Scrims": "Generate scrim threads for team matches.",
    "StickyNotes": "Keep a message pinned to the bottom of a channel.",
    "AutoResponder": "Auto-reply to trigger words.",
    "GamePass": "Look up Roblox gamepass prices.",
    "Vanity": "Award a role for a keyword in someone's status.",
    "Tickets": "Private support tickets with a custom panel and logging.",
    "Send": "Post a message or embed to a channel as the bot.",
    "Purge": "Bulk delete messages, optionally filtered.",
    "Prefix": "View or change the command prefix for this server.",
    "Autorole": "Give roles automatically when someone joins.",
    "Roles": "Add or remove roles from members.",
    "Welcome": "Greet joiners and announce leavers.",
    "Help": "This menu.",
}

COMMAND_HELP = {
    "kick": "Remove a member from the server.",
    "ban": "Ban a member from the server.",
    "unban": "Lift a ban by user ID.",
    "timeout": "Mute a member for a number of minutes.",
    "untimeout": "End a member's timeout early.",
    "scrim": "Open the scrim thread generator.",
    "stickynote": "Open the sticky note panel.",
    "unsticky": "Remove the active sticky in this channel.",
    "autoresponder": "Open the autoresponder panel.",
    "gamepass": "Look up a Roblox gamepass and its payout.",
    "vanity": "Configure the vanity status role.",
    "unvanity": "Disable the vanity status role.",
    "ticketsetup": "Set up or reconfigure the ticket system.",
    "ticketstats": "Show open, unclaimed and lifetime ticket counts.",
    "send": "Post a message or embed now, or schedule it for later.",
    "purge": "Bulk delete messages in this channel.",
    "purgehelp": "Show the purge filters and examples.",
    "prefix": "Show the current command prefix.",
    "help": "Show this menu.",
    "scheduled": "List and cancel scheduled messages.",
    "timezone": "View or change the server timezone.",
    "autorole": "Configure roles given on join.",
    "role": "Add or remove a role from a member.",
    "welcome": "Configure the welcome message.",
    "goodbye": "Configure the leave message.",
    "prefix": "View or change the command prefix.",
}

SUBCOMMAND_HELP = {
    "ticketsetup fast": "Quick setup with one default button.",
    "ticketsetup custom": "Full builder, starting empty.",
    "ticketsetup edit": "Reopen the builder on an existing setup.",
    "prefix set": "Change the prefix for this server.",
    "prefix reset": "Go back to the default prefix.",
}


def describe(command) -> str:
    if command.help:
        return command.help
    qualified = SUBCOMMAND_HELP.get(command.qualified_name)
    if qualified:
        return qualified
    return COMMAND_HELP.get(command.name, "No description yet.")


def subcommands_of(command):
    if not isinstance(command, commands.Group):
        return []
    return sorted(
        (c for c in command.commands if not c.hidden), key=lambda c: c.name
    )


def usage(command, prefix) -> str:
    sig = command.signature
    return f"{prefix}{command.qualified_name} {sig}".strip()


async def visible_commands(cog, ctx):
    """Commands in this cog the invoker is actually allowed to run."""
    out = []
    for command in cog.get_commands():
        if command.hidden:
            continue
        try:
            if not await command.can_run(ctx):
                continue
        except commands.CommandError:
            continue
        out.append(command)
    return sorted(out, key=lambda c: c.name)


def home_embed(bot, ctx, categories, prefix):
    embed = discord.Embed(
        title="Command Help",
        description=(
            f"Prefix is `{prefix}`\n"
            f"Use the menu below to browse a category, or "
            f"`{prefix}help <command>` for details on one command."
        ),
        color=ACCENT,
    )

    for cog_name, cmds in categories.items():
        blurb = CATEGORY_META.get(cog_name, "")
        names = " ".join(f"`{c.name}`" for c in cmds)
        value = f"{blurb}\n{names}" if blurb else names
        embed.add_field(name=cog_name, value=value, inline=False)

    if not categories:
        embed.description = "You don't have access to any commands here."

    if bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
    return embed


def category_embed(cog_name, cmds, prefix):
    blurb = CATEGORY_META.get(cog_name, "")
    embed = discord.Embed(
        title=cog_name,
        description=blurb,
        color=ACCENT,
    )

    for command in cmds:
        aliases = ""
        if command.aliases:
            aliases = " | " + " ".join(f"`{a}`" for a in command.aliases)

        value = describe(command)
        subs = subcommands_of(command)
        if subs:
            listed = "\n".join(
                f"`{prefix}{s.qualified_name}` - {describe(s)}" for s in subs
            )
            value = f"{value}\n{listed}"

        embed.add_field(
            name=f"`{usage(command, prefix)}`{aliases}",
            value=value,
            inline=False,
        )

    embed.set_footer(text=f"{len(cmds)} command{'s' if len(cmds) != 1 else ''}")
    return embed


def command_embed(command, prefix):
    embed = discord.Embed(
        title=f"{prefix}{command.qualified_name}",
        description=describe(command),
        color=ACCENT,
    )
    embed.add_field(name="Usage", value=f"`{usage(command, prefix)}`", inline=False)

    if command.aliases:
        embed.add_field(
            name="Aliases",
            value=" ".join(f"`{prefix}{a}`" for a in command.aliases),
            inline=False,
        )

    subs = subcommands_of(command)
    if subs:
        embed.add_field(
            name="Subcommands",
            value="\n".join(
                f"`{prefix}{s.qualified_name}` - {describe(s)}" for s in subs
            ),
            inline=False,
        )

    if command.cog:
        embed.set_footer(text=command.cog.qualified_name)
    return embed


class HelpSelect(discord.ui.Select):
    def __init__(self, categories, prefix):
        self.categories = categories
        self.prefix = prefix

        options = [
            discord.SelectOption(
                label="Overview",
                value="__home__",
                description="Back to the main list",
            )
        ]
        for cog_name, cmds in categories.items():
            blurb = CATEGORY_META.get(cog_name, "")
            options.append(
                discord.SelectOption(
                    label=cog_name,
                    value=cog_name,
                    description=(blurb[:100] if blurb else None),
                )
            )

        super().__init__(placeholder="Pick a category...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__home__":
            embed = home_embed(
                interaction.client, self.view.ctx, self.categories, self.prefix
            )
        else:
            name = self.values[0]
            embed = category_embed(name, self.categories[name], self.prefix)

        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self, ctx, categories, prefix):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.prefix = prefix
        self.message = None
        self.add_item(HelpSelect(categories, prefix))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                f"Run `{self.prefix}help` yourself to use this menu.", ephemeral=True
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


class Help(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="help", aliases=["h", "commands"])
    async def help_command(self, ctx: commands.Context, *, query: str = None):
        prefix = prefix_for(ctx.guild.id if ctx.guild else None)

        if query:
            wanted = query.strip()
            if wanted.startswith(prefix):
                wanted = wanted[len(prefix):]

            command = self.bot.get_command(wanted.strip())
            if command is None or command.hidden:
                await ctx.send(
                    f"No command called `{query}`. Try `{prefix}help`."
                )
                return
            await ctx.send(embed=command_embed(command, prefix))
            return

        categories = {}
        for cog_name, cog in self.bot.cogs.items():
            if cog_name == "Help":
                continue
            cmds = await visible_commands(cog, ctx)
            if cmds:
                categories[cog_name] = cmds

        categories = dict(sorted(categories.items()))

        view = HelpView(ctx, categories, prefix)
        message = await ctx.send(
            embed=home_embed(self.bot, ctx, categories, prefix),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = message


async def setup(bot):
    bot.remove_command("help")
    await bot.add_cog(Help(bot))