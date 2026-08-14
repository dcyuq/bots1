import re
import math
import discord
from discord.ext import commands
import aiohttp

CREATOR_SHARE = 0.70

PRODUCT_INFO_URL = "https://apis.roblox.com/game-passes/v1/game-passes/{id}/product-info"
THUMBNAIL_URL = (
    "https://thumbnails.roblox.com/v1/game-passes"
    "?gamePassIds={id}&size=150x150&format=Png&isCircular=false"
)
GAMEPASS_LINK = "https://www.roblox.com/game-pass/{id}"

HEADERS = {"User-Agent": "6ix-gamepass-scanner"}

SCANNER_LABEL = "gamepass checker"
DIVIDER = "⎯" * 20
SEP = "ㆍ"

ID_PATTERN = re.compile(r"(?:game-passe?s?/)?(\d{3,})")


class GamePass(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def parse_id(text: str):
        match = ID_PATTERN.search(text or "")
        return int(match.group(1)) if match else None

    async def fetch_json(self, session, url):
        async with session.get(url, headers=HEADERS) as resp:
            if resp.status != 200:
                return None
            # product-info returns an empty body for a non-existent pass
            text = await resp.text()
            if not text.strip():
                return None
            return await resp.json(content_type=None)

    @commands.command(name="gamepass", aliases=["gp", "scan"])
    async def gamepass(self, ctx, *, link: str = None):
        if link is None:
            await ctx.send("Give me a gamepass link or ID. Example: `,gamepass 1923992452`")
            return

        gp_id = self.parse_id(link)
        if gp_id is None:
            await ctx.send("That doesn't look like a valid gamepass link or ID.")
            return

        async with aiohttp.ClientSession() as session:
            info = await self.fetch_json(session, PRODUCT_INFO_URL.format(id=gp_id))
            if info is None:
                await ctx.send("Couldn't find a gamepass with that ID.")
                return

            thumb = await self.fetch_json(session, THUMBNAIL_URL.format(id=gp_id))

        name = info.get("Name") or "Unknown"
        price = info.get("PriceInRobux")
        for_sale = info.get("IsForSale", False)
        creator = info.get("Creator", {}).get("Name", "Unknown")

        if price is None:
            price_text = "Not for sale / no price set"
            net_text = "-"
        else:
            net = math.floor(price * CREATOR_SHARE)
            price_text = f"{price:,} Robux"
            net_text = f"{net:,} Robux"

        icon_url = None
        if thumb and thumb.get("data"):
            icon_url = thumb["data"][0].get("imageUrl")

        accessible = "yes" if for_sale else "no"
        link = GAMEPASS_LINK.format(id=gp_id)

        description = (
            f"-# {DIVIDER}\n"
            f" **{price_text}**\n"
            f"-# ╰   you will receive {SEP} {net_text}\n"
            f"-# {DIVIDER}\n"
            f"-# **creator**{SEP} {creator}\n"
            f"-# **accessible?**  {SEP} {accessible}\n"
            f"-# {DIVIDER}\n"
            f"-# **g – pass ID** {SEP} {gp_id} — [link]({link})"
        )

        embed = discord.Embed(
            title=name,
            description=description,
            color=discord.Color.dark_theme(),
        )

        await ctx.send(embed=embed)

    @gamepass.error
    async def gamepass_error(self, ctx, error):
        await ctx.send(f"Something went wrong: {error}")


async def setup(bot):
    await bot.add_cog(GamePass(bot))