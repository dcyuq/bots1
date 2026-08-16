import re
import time
import asyncio

import aiohttp
import discord
from discord.ext import commands

from storage import Store

_store = Store("groupcheck.json")

USERS_API = "https://users.roblox.com/v1/usernames/users"
USER_INFO_API = "https://users.roblox.com/v1/users/{user_id}"
USER_GROUPS_API = "https://groups.roblox.com/v2/users/{user_id}/groups/roles"
GROUP_INFO_API = "https://groups.roblox.com/v1/groups/{group_id}"

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
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if can_manage(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_guild"])

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the Manage Server permission to use this.")
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

    async def fetch_group_name(self, group_id):
        data = await self.api_get(GROUP_INFO_API.format(group_id=group_id))
        if not data:
            return None
        return data.get("name")

    @commands.command(name="eligible", aliases=["elig"])
    async def eligible(self, ctx, *, user: str = None):
        """Check one Roblox user against every configured group."""
        if user is None:
            await ctx.send(
                f"Give me a Roblox username, profile link, or user ID. "
                f"Example: `{ctx.prefix}eligible builderman`"
            )
            return

        await self.run_check(ctx, user)

    @commands.group(
        name="gc",
        aliases=["groupcheck"],
        usage="<subcommand>",
        invoke_without_command=True,
    )
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

    @groupcheck.command(name="addgroup")
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

    @groupcheck.command(name="removegroup")
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

    @groupcheck.command(name="list")
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

    @groupcheck.command(name="setdays")
    async def setdays(self, ctx, days: int):
        if days < 0 or days > MAX_DAYS:
            await ctx.send("Pick a value between 0 and {0}.".format(MAX_DAYS))
            return

        async with self.lock:
            entry = guild_config(ctx.guild.id)
            entry["days"] = days
            save_data(config)

        await ctx.send("Eligibility threshold set to {0} days.".format(days))

    @groupcheck.command(name="override")
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

    @groupcheck.command(name="clear")
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

            memberships = await self.fetch_user_groups(resolved["id"])
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
                    lines.append("**{0}**\nNot in group".format(group_name))
                    continue

                first_seen = seen.get(key)
                if first_seen is None:
                    first_seen = now
                    seen[key] = first_seen

                elapsed = now - first_seen

                if elapsed >= threshold:
                    eligible_count += 1
                    status = "Eligible"
                else:
                    status = "Not yet, {0} remaining".format(
                        humanize(threshold - elapsed)
                    )

                lines.append(
                    "**{0}**\nRole: {1}\nTracked for {2} | {3}".format(
                        group_name, membership["role"], humanize(elapsed), status
                    )
                )

            save_data(config)

        passed = total > 0 and eligible_count == total

        embed = discord.Embed(
            title="Group eligibility: {0}".format(resolved["name"]),
            description="\n\n".join(lines),
            color=discord.Color.green() if passed else discord.Color.red(),
        )
        embed.add_field(
            name="Result",
            value="Eligible for payout" if passed else "Not eligible",
            inline=False,
        )
        embed.set_footer(
            text="User ID {0} | Threshold {1} days | Timer starts at first check".format(
                resolved["id"], threshold_days
            )
        )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(GroupCheck(bot))