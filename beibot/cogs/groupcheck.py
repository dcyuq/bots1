import re
import time
import asyncio

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from storage import Store

_store = Store("groupcheck.json")

USERS_API = "https://users.roblox.com/v1/usernames/users"
USER_INFO_API = "https://users.roblox.com/v1/users/{user_id}"
USER_GROUPS_API = "https://groups.roblox.com/v2/users/{user_id}/groups/roles"
GROUP_INFO_API = "https://groups.roblox.com/v1/groups/{group_id}"
GROUP_LINK = "https://www.roblox.com/groups/{group_id}"
AVATAR_API = (
    "https://thumbnails.roblox.com/v1/users/avatar-headshot"
    "?userIds={user_id}&size=150x150&format=Png&isCircular=false"
)

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "beibot-group-checker",
}

GROUP_LINK_PATTERN = re.compile(r"roblox\.com/(?:groups|communities)/(\d+)", re.I)
PROFILE_LINK_PATTERN = re.compile(r"roblox\.com/users/(\d+)", re.I)
BARE_ID_PATTERN = re.compile(r"^\d+$")
USERNAME_PATTERN = re.compile(r"^\w{3,20}$")

SUBCOMMANDS = (
    "addgroup",
    "removegroup",
    "list",
    "setdays",
    "override",
    "clear",
)

FOOTER_WAIT = "-# {days} d\u03b1y{plural} w\u03b1\u0131t \u2018t\u0131l you\u2019re el\u0131g\u0131ble !"

PUBLIC_COMMANDS = ("eligible",)

DEFAULT_DAYS = 14
REQUEST_TIMEOUT = 15
MAX_DAYS = 3650


def load_data():
    return _store.load()


def save_data(data):
    _store.save(data)


config = load_data()


def guild_config(guild_id):
    entry = config.setdefault(
        str(guild_id), {"groups": {}, "days": DEFAULT_DAYS, "seen": {}}
    )
    entry.setdefault("groups", {})
    entry.setdefault("days", DEFAULT_DAYS)
    entry.setdefault("seen", {})
    return entry


def can_manage(member):
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild


def parse_group_id(value):
    value = (value or "").strip()
    match = GROUP_LINK_PATTERN.search(value)
    if match:
        return int(match.group(1))
    if BARE_ID_PATTERN.match(value):
        return int(value)
    return None


def parse_user_reference(value):
    value = (value or "").strip().lstrip("@")
    match = PROFILE_LINK_PATTERN.search(value)
    if match:
        return "id", int(match.group(1))
    if BARE_ID_PATTERN.match(value):
        return "id", int(value)
    return "name", value


def looks_like_username(value):
    """Roblox usernames are 3-20 word characters with no spaces.

    Anything else is almost always a mistyped subcommand, and resolving it
    would silently write a tracking stamp against garbage.
    """
    return bool(USERNAME_PATTERN.match(value or ""))


def humanize(seconds):
    days = int(seconds // 86400)
    if days >= 1:
        return "{0} day{1}".format(days, "" if days == 1 else "s")
    hours = int(seconds // 3600)
    if hours >= 1:
        return "{0} hour{1}".format(hours, "" if hours == 1 else "s")
    minutes = max(int(seconds // 60), 0)
    return "{0} minute{1}".format(minutes, "" if minutes == 1 else "s")


class ResultView(discord.ui.LayoutView):
    """Components V2 output.

    Embeds have no divider primitive, so the grey rules come from Separator
    components. A message using these cannot also carry content or embeds,
    which is why the whole result is built as a view.
    """

    def __init__(self, username, avatar_url, lines, threshold_days, passed):
        super().__init__(timeout=None)

        container = discord.ui.Container(
            accent_colour=(
                discord.Colour.green() if passed else discord.Colour.red()
            )
        )

        name = discord.ui.TextDisplay("**{0}**".format(username))
        if avatar_url:
            container.add_item(
                discord.ui.Section(
                    name, accessory=discord.ui.Thumbnail(avatar_url)
                )
            )
        else:
            container.add_item(name)

        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        container.add_item(
            discord.ui.TextDisplay(
                FOOTER_WAIT.format(
                    days=threshold_days, plural="" if threshold_days == 1 else "s"
                )
            )
        )

        self.add_item(container)


class GroupCheck(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session = None
        self.lock = asyncio.Lock()

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            headers=HEADERS,
        )

    async def cog_unload(self):
        if self.session is not None and not self.session.closed:
            await self.session.close()

    async def cog_check(self, ctx):
        """Checking a user is open to everyone, configuration is not."""
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if ctx.command.qualified_name in PUBLIC_COMMANDS:
            return True
        if can_manage(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_guild"])

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                "Slow down, try again in {0:.0f}s.".format(error.retry_after)
            )
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send(
                "Only staff can configure the checker. Anyone can run "
                "`{0}eligible <user>`.".format(ctx.prefix)
            )
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command can only be used in a server.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Missing argument: `{0}`".format(error.param.name))
        elif isinstance(error, commands.BadArgument):
            await ctx.send("That argument wasn't valid.")
        else:
            await ctx.send("Something went wrong: {0}".format(error))

    async def api_get(self, url):
        try:
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def api_post(self, url, payload):
        try:
            async with self.session.post(url, json=payload) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def resolve_user(self, reference):
        kind, value = parse_user_reference(reference)

        if kind == "id":
            data = await self.api_get(USER_INFO_API.format(user_id=value))
            if not data or data.get("id") is None:
                return None
            return {"id": int(data["id"]), "name": data.get("name") or str(value)}

        payload = {"usernames": [value], "excludeBannedUsers": False}
        data = await self.api_post(USERS_API, payload)
        if not data:
            return None
        results = data.get("data") or []
        if not results:
            return None
        entry = results[0]
        if entry.get("id") is None:
            return None
        return {"id": int(entry["id"]), "name": entry.get("name") or value}

    async def fetch_user_groups(self, user_id):
        data = await self.api_get(USER_GROUPS_API.format(user_id=user_id))
        if not data:
            return None
        mapping = {}
        for entry in data.get("data") or []:
            group = entry.get("group") or {}
            role = entry.get("role") or {}
            group_id = group.get("id")
            if group_id is None:
                continue
            mapping[int(group_id)] = {
                "name": group.get("name") or str(group_id),
                "role": role.get("name") or "Member",
            }
        return mapping

    async def fetch_avatar(self, user_id):
        """Headshot URL for the embed thumbnail, or None.

        Roblox renders thumbnails lazily, so a request can succeed while the
        image is still Pending or the account is Blocked. Only Completed
        entries carry a usable URL.
        """
        data = await self.api_get(AVATAR_API.format(user_id=user_id))
        if not data:
            return None
        entries = data.get("data") or []
        if not entries:
            return None
        entry = entries[0]
        if entry.get("state") != "Completed":
            return None
        return entry.get("imageUrl")

    async def fetch_group_name(self, group_id):
        data = await self.api_get(GROUP_INFO_API.format(group_id=group_id))
        if not data:
            return None
        return data.get("name")

    @commands.hybrid_command(
        name="eligible",
        aliases=["elig"],
        description="Check a Roblox user against the configured groups.",
    )
    @app_commands.describe(user="Roblox username, profile link, or user ID")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def eligible(self, ctx, *, user: str = None):
        """Check one Roblox user against every configured group."""
        if user is None:
            await ctx.send(
                f"Give me a Roblox username, profile link, or user ID. "
                f"Example: `{ctx.prefix}eligible builderman`"
            )
            return

        await self.run_check(ctx, user)

    @commands.hybrid_group(
        name="gc",
        aliases=["groupcheck"],
        usage="<subcommand>",
        invoke_without_command=True,
        description="Configure the eligibility checker.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @commands.guild_only()
    async def groupcheck(self, ctx, *, unknown: str = None):
        """Configuration for the eligibility checker."""
        prefix = ctx.prefix

        if unknown and looks_like_username(unknown.strip()):
            await ctx.send(
                f"To check someone, use `{prefix}eligible {unknown.strip()}`. "
                f"`{prefix}gc` is for configuration."
            )
            return

        if unknown:
            await ctx.send(
                f"Unknown subcommand `{unknown.split()[0]}`. "
                f"Options: {', '.join(f'`{s}`' for s in SUBCOMMANDS)}"
            )
            return

        await ctx.send(
            f"Check someone with `{prefix}eligible <user>`.\n"
            f"Configure with: {', '.join(f'`{s}`' for s in SUBCOMMANDS)}"
        )

    @groupcheck.command(name="addgroup", description="Start tracking a Roblox group.")
    @app_commands.describe(link="Group link or group ID")
    async def addgroup(self, ctx, *, link: str):
        group_id = parse_group_id(link)
        if group_id is None:
            await ctx.send("That doesn't look like a group link or group ID.")
            return

        name = await self.fetch_group_name(group_id)
        if name is None:
            await ctx.send("Couldn't find a Roblox group with that ID.")
            return

        async with self.lock:
            entry = guild_config(ctx.guild.id)
            entry["groups"][str(group_id)] = name
            save_data(config)

        await ctx.send("Added group: **{0}** ({1})".format(name, group_id))

    @groupcheck.command(
        name="removegroup",
        description="Stop tracking a Roblox group.",
    )
    @app_commands.describe(link="Group link or group ID")
    async def removegroup(self, ctx, *, link: str):
        group_id = parse_group_id(link)
        if group_id is None:
            await ctx.send("That doesn't look like a group link or group ID.")
            return

        async with self.lock:
            entry = guild_config(ctx.guild.id)
            removed = entry["groups"].pop(str(group_id), None)
            entry["seen"].pop(str(group_id), None)
            save_data(config)

        if removed is None:
            await ctx.send("That group isn't in the list.")
            return

        await ctx.send("Removed group: **{0}** ({1})".format(removed, group_id))

    @groupcheck.command(
        name="list",
        description="Show the tracked groups and the day threshold.",
    )
    async def list_groups(self, ctx):
        entry = guild_config(ctx.guild.id)
        groups = entry["groups"]

        if not groups:
            await ctx.send(
                f"No groups configured. Add one with "
                f"`{ctx.prefix}gc addgroup <link>`."
            )
            return

        lines = ["`{0}` {1}".format(gid, name) for gid, name in groups.items()]

        embed = discord.Embed(
            title="Configured Roblox groups",
            description="\n".join(lines),
            color=discord.Color.dark_theme(),
        )
        embed.set_footer(text="Eligibility threshold: {0} days".format(entry["days"]))
        await ctx.send(embed=embed)

    @groupcheck.command(
        name="setdays",
        description="Set how many days a user must be tracked for.",
    )
    @app_commands.describe(days="Number of days required")
    async def setdays(self, ctx, days: int):
        if days < 0 or days > MAX_DAYS:
            await ctx.send("Pick a value between 0 and {0}.".format(MAX_DAYS))
            return

        async with self.lock:
            entry = guild_config(ctx.guild.id)
            entry["days"] = days
            save_data(config)

        await ctx.send("Eligibility threshold set to {0} days.".format(days))

    @groupcheck.command(
        name="override",
        description="Backdate a user's first-seen date for one group.",
    )
    @app_commands.describe(
        user="Roblox username, profile link, or user ID",
        group="Group link or group ID",
        days="How many days ago to backdate to",
    )
    async def override(self, ctx, user: str, group: str, days: int):
        group_id = parse_group_id(group)
        if group_id is None:
            await ctx.send("That doesn't look like a group link or group ID.")
            return

        if days < 0 or days > MAX_DAYS:
            await ctx.send("Days must be between 0 and {0}.".format(MAX_DAYS))
            return

        entry = guild_config(ctx.guild.id)
        if str(group_id) not in entry["groups"]:
            await ctx.send("That group isn't configured for this server.")
            return

        resolved = await self.resolve_user(user)
        if resolved is None:
            await ctx.send("Couldn't resolve that Roblox user.")
            return

        stamp = int(time.time()) - (days * 86400)

        async with self.lock:
            seen = entry["seen"].setdefault(str(group_id), {})
            seen[str(resolved["id"])] = stamp
            save_data(config)

        await ctx.send(
            "Set **{0}** as first seen {1} day(s) ago in group {2}.".format(
                resolved["name"], days, group_id
            )
        )

    @groupcheck.command(
        name="clear",
        description="Wipe every tracking record for a user.",
    )
    @app_commands.describe(user="Roblox username, profile link, or user ID")
    async def clear(self, ctx, *, user: str):
        resolved = await self.resolve_user(user)
        if resolved is None:
            await ctx.send("Couldn't resolve that Roblox user.")
            return

        removed = 0
        async with self.lock:
            entry = guild_config(ctx.guild.id)
            for members in entry["seen"].values():
                if members.pop(str(resolved["id"]), None) is not None:
                    removed += 1
            save_data(config)

        await ctx.send(
            "Cleared {0} tracking record(s) for **{1}**.".format(
                removed, resolved["name"]
            )
        )

    async def run_check(self, ctx, user):
        user = user.strip()
        kind, value = parse_user_reference(user)

        if kind == "name" and not looks_like_username(value):
            first = value.split()[0].lower() if value.split() else ""
            if first in SUBCOMMANDS:
                await ctx.send(
                    f"`{first}` is a configuration subcommand. "
                    f"Did you mean `{ctx.prefix}gc {value}`?"
                )
                return
            await ctx.send(
                "That isn't a valid Roblox username. Give me a username, "
                "profile link, or user ID."
            )
            return

        entry = guild_config(ctx.guild.id)
        groups = entry["groups"]

        if not groups:
            await ctx.send(
                f"No groups configured. Add one with "
                f"`{ctx.prefix}gc addgroup <link>`."
            )
            return

        async with ctx.typing():
            resolved = await self.resolve_user(user)
            if resolved is None:
                await ctx.send("Couldn't find that Roblox user.")
                return

            memberships, avatar_url = await asyncio.gather(
                self.fetch_user_groups(resolved["id"]),
                self.fetch_avatar(resolved["id"]),
            )
            if memberships is None:
                await ctx.send("Roblox didn't respond. Try again in a moment.")
                return

        threshold_days = entry["days"]
        threshold = threshold_days * 86400
        now = int(time.time())
        lines = []
        eligible_count = 0
        total = len(groups)

        async with self.lock:
            for group_id_str, group_name in groups.items():
                group_id = int(group_id_str)
                seen = entry["seen"].setdefault(group_id_str, {})
                key = str(resolved["id"])
                membership = memberships.get(group_id)

                if membership is None:
                    seen.pop(key, None)
                    lines.append(
                        "{0} : not a member yet, [join now]({1})".format(
                            group_name, GROUP_LINK.format(group_id=group_id)
                        )
                    )
                    continue

                first_seen = seen.get(key)
                if first_seen is None:
                    first_seen = now
                    seen[key] = first_seen

                elapsed = now - first_seen
                if elapsed >= threshold:
                    eligible_count += 1

                lines.append(
                    "{0} : {1} since <t:{2}:d>".format(
                        group_name, humanize(elapsed), first_seen
                    )
                )

            save_data(config)

        passed = total > 0 and eligible_count == total

        view = ResultView(
            username=resolved["name"],
            avatar_url=avatar_url,
            lines=lines,
            threshold_days=threshold_days,
            passed=passed,
        )

        await ctx.send(view=view)


async def setup(bot):
    await bot.add_cog(GroupCheck(bot))