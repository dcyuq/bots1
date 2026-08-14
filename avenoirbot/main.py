import discord
from discord.ext import commands
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from pathlib import Path
from prefixes import resolve_prefix
import os
import asyncio

BOT_NAME = "Avenoir"

ROOT = Path(__file__).resolve().parent
COGS_DIR = ROOT / "cogs"


load_dotenv(ROOT / ".env")
token = os.getenv("DISCORD_TOKEN")

handler = RotatingFileHandler(
    filename=ROOT / "discord.log",
    encoding="utf-8",
    maxBytes=2 * 1024 * 1024,
    backupCount=2,
)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=resolve_prefix,
    intents=intents,
    allowed_mentions=discord.AllowedMentions(
        everyone=False, roles=False, users=True
    ),
)


@bot.event
async def on_ready():
    print(f"{BOT_NAME} online as {bot.user} in {len(bot.guilds)} guild(s)")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if ctx.command is not None and ctx.command.has_error_handler():
        return
    if ctx.cog is not None and ctx.cog.has_error_handler():
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`")
        return
    if isinstance(error, (commands.MissingPermissions, commands.CheckFailure)):
        await ctx.send("You don't have permission to use that.")
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Slow down, try again in {error.retry_after:.0f}s.")
        return

    await ctx.send(f"`{type(error).__name__}: {error}`")
    raise error


async def load_cogs():
    loaded, failed = 0, 0

    for path in sorted(COGS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            await bot.load_extension(f"cogs.{path.stem}")
            print(f"  loaded  {path.name}")
            loaded += 1
        except Exception as exc:
            print(f"  FAILED  {path.name}: {type(exc).__name__}: {exc}")
            failed += 1

    print(f"Cogs: {loaded} loaded, {failed} failed")


async def main():
    if not token:
        print(f"DISCORD_TOKEN is missing. Check {ROOT / '.env'}")
        return

    async with bot:
        discord.utils.setup_logging(handler=handler, level=logging.INFO)
        await load_cogs()
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"{BOT_NAME} shutting down.")