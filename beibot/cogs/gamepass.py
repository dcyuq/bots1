import re
import math
import asyncio

import aiohttp
import discord
from discord.ext import commands

CREATOR_SHARE = 0.70

PRODUCT_INFO_URL = "https://apis.roblox.com/game-passes/v1/game-passes/{id}/product-info"
THUMBNAIL_URL = (
    "https://thumbnails.roblox.com/v1/game-passes"
    "?gamePassIds={id}&size=150x150&format=Png&isCircular=false"
)
GAMEPASS_LINK = "https://www.roblox.com/game-pass/{id}"

HEADERS = {"User-Agent": "beibot-gamepass-scanner"}

REQUEST_TIMEOUT = 15
DIVIDER = "-" * 20
SEP = "|"

ID_PATTERN = re.compile(r"(?:game-passe?s?/)?(\d{3,})")


class GamePass(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            headers=HEADERS,
        )

    async def cog_unload(self):
        if self.session is not None and not self.session.closed:
            await self.session.close()

    @staticmethod
    def parse_id(text: str):
        match = ID_PATTERN.search(text or "")
        return int(match.group(1)) if match else None

    async def fetch_json(self, url):
        """Return parsed JSON, or None.

        product-info answers with an empty body rather than a 404 when the
        gamepass does not exist, so an empty response is treated as a miss.
        """
        try:
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()
                if not text.strip():
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    @commands.command(name="gamepass", aliases=["gp", "scan"])
    async def gamepass(self, ctx, *, link: str = None):
        if link is None:
            await ctx.send(
                "Give me a gamepass link or ID. Example: `,gamepass 1923992452`"
            )
            return

        gp_id = self.parse_id(link)
        if gp_id is None:
            await ctx.send("That doesn't look like a valid gamepass link or ID.")
            return

        async with ctx.typing():
            info = await self.fetch_json(PRODUCT_INFO_URL.format(id=gp_id))
            if info is None:
                await ctx.send("Couldn't find a gamepass with that ID.")
                return

            thumb = await self.fetch_json(THUMBNAIL_URL.format(id=gp_id))

        name = info.get("Name") or "Unknown"
        price = info.get("PriceInRobux")
        for_sale = info.get("IsForSale", False)
        creator = (info.get("Creator") or {}).get("Name", "Unknown")

        if price is None:
            price_text = "Not for sale / no price set"
            net_text = "-"
        else:
            net = math.floor(price * CREATOR_SHARE)
            price_text = "{0:,} Robux".format(price)
            net_text = "{0:,} Robux".format(net)

        icon_url = None
        if thumb and thumb.get("data"):
            icon_url = thumb["data"][0].get("imageUrl")

        accessible = "yes" if for_sale else "no"
        pass_link = GAMEPASS_LINK.format(id=gp_id)

        description = (
            "-# {0}\n"
            " **{1}**\n"
            "-# you will receive {2} {3}\n"
            "-# {0}\n"
            "-# **creator** {2} {4}\n"
            "-# **accessible?** {2} {5}\n"
            "-# {0}\n"
            "-# **g-pass ID** {2} {6} - [link]({7})"
        ).format(
            DIVIDER, price_text, SEP, net_text, creator, accessible, gp_id, pass_link
        )

        embed = discord.Embed(
            title=name,
            description=description,
            color=discord.Color.dark_theme(),
        )
        if icon_url:
            embed.set_thumbnail(url=icon_url)

        await ctx.send(embed=embed)

    @gamepass.error
    async def gamepass_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send("That argument wasn't valid.")
            return
        await ctx.send("Couldn't look that gamepass up right now.")
        raise error


async def setup(bot):
    await bot.add_cog(GamePass(bot))