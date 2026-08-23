from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

from storage import Store

_store = Store("bets.json")
_wallet = Store("cookies.json")

TZ = timezone(timedelta(hours=8))
COOKIE = "\N{COOKIE}"
MIN_STAKE = 1
MAX_OPTIONS = 10
LOCK_TICK = 20
MAX_GRANT = 100_000
GRANT_LOG_LIMIT = 50

STATUS_COLOURS = {
    "open": discord.Colour.from_str("#57F287"),
    "locked": discord.Colour.from_str("#FEE75C"),
    "settled": discord.Colour.from_str("#5865F2"),
    "cancelled": discord.Colour.from_str("#ED4245"),
}

STATUS_LABELS = {
    "open": "Open for bets",
    "locked": "Betting closed",
    "settled": "Settled",
    "cancelled": "Cancelled",
}


class BetError(commands.CommandError):
    pass


def _load() -> dict:
    data = _store.load()
    if not isinstance(data, dict):
        data = {}
    data.setdefault("guilds", {})
    return data


def _save(data: dict) -> None:
    _store.save(data)


def _guild_state(data: dict, guild_id: int) -> dict:
    state = data.setdefault("guilds", {}).setdefault(str(guild_id), {})
    state.setdefault("counter", 0)
    state.setdefault("games", {})
    return state


def _load_wallet() -> dict:
    data = _wallet.load()
    if not isinstance(data, dict):
        data = {}
    return data


def _balance(guild_id: int, user_id: int) -> int:
    data = _load_wallet()
    return int(data.get(str(guild_id), {}).get(str(user_id), 0))


def _adjust(guild_id: int, user_id: int, delta: int) -> int:
    return _adjust_many(guild_id, {user_id: delta})[user_id]


def _adjust_many(guild_id: int, deltas: dict[int, int]) -> dict[int, int]:
    data = _load_wallet()
    guild = data.setdefault(str(guild_id), {})
    results: dict[int, int] = {}
    for user_id, delta in deltas.items():
        current = int(guild.get(str(user_id), 0))
        updated = max(0, current + delta)
        guild[str(user_id)] = updated
        results[user_id] = updated
    _wallet.save(data)
    return results


def _now() -> int:
    return int(time.time())


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def parse_odds(raw: str) -> float:
    token = raw.strip().replace(",", "").replace("x", "").replace("\u00d7", "")
    if not token:
        raise BetError("An outcome is missing its odds.")
    try:
        if token[0] in "+-":
            value = float(token)
            if value == 0:
                raise ValueError
            decimal = 1 + value / 100 if value > 0 else 1 + 100 / abs(value)
        elif "/" in token:
            num, _, den = token.partition("/")
            decimal = 1 + float(num) / float(den)
        else:
            decimal = float(token)
    except (ValueError, ZeroDivisionError):
        raise BetError(f"`{raw.strip()}` is not valid odds. Use `2.5`, `+150`, `-200` or `3/2`.")
    if decimal < 1.02 or decimal > 100:
        raise BetError("Odds must land between `1.02` and `100.0` in decimal form.")
    return round(decimal, 2)


def parse_options(raw: str) -> list[dict]:
    options: list[dict] = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-\u2022").strip()
        if not line:
            continue
        if "|" in line:
            label, _, odds_raw = line.rpartition("|")
        else:
            parts = line.rsplit(None, 1)
            if len(parts) != 2:
                raise BetError(f"Could not read odds from `{line}`. Format each line as `Name | 2.5`.")
            label, odds_raw = parts
        label = label.strip()
        if not label:
            raise BetError("One of the outcomes has no name.")
        options.append({"label": label[:70], "odds": parse_odds(odds_raw)})
    if len(options) < 2:
        raise BetError("Give at least two outcomes, one per line.")
    if len(options) > MAX_OPTIONS:
        raise BetError(f"That is more than {MAX_OPTIONS} outcomes.")
    seen = set()
    for option in options:
        key = option["label"].lower()
        if key in seen:
            raise BetError(f"`{option['label']}` is listed twice.")
        seen.add(key)
    return options


_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_PART = re.compile(r"(\d+)\s*([smhdw])")


def parse_when(raw: str, base: Optional[int] = None) -> int:
    text = raw.strip().lower()
    base = base or _now()
    if not text:
        raise BetError("No time was given.")
    if text in {"now", "instant", "immediately"}:
        return base
    if text.startswith("in "):
        text = text[3:].strip()
    if text.isdigit() and len(text) >= 9:
        return int(text)
    parts = _DURATION_PART.findall(text)
    if parts and not _DURATION_PART.sub("", text).strip():
        total = sum(int(value) * _UNIT_SECONDS[unit] for value, unit in parts)
        if total > 0:
            return base + total
    reference = datetime.fromtimestamp(base, TZ)
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m-%d %H:%M", "%m/%d %H:%M", "%H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=reference.year)
        if "%m" not in fmt:
            parsed = parsed.replace(month=reference.month, day=reference.day)
        parsed = parsed.replace(tzinfo=TZ)
        stamp = int(parsed.timestamp())
        if stamp <= base and "%Y" not in fmt:
            rolled = parsed + (timedelta(days=1) if "%m" not in fmt else timedelta(days=365))
            stamp = int(rolled.timestamp())
        return stamp
    raise BetError(
        "I could not read that time. Try `2h30m`, `45m`, `1d`, `2026-08-25 19:00` or `21:00` (UTC+8)."
    )


_AMOUNT_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_amount(raw: str, balance: int) -> int:
    text = raw.strip().lower().replace(",", "").replace("_", "")
    if not text:
        raise BetError("No amount was given.")
    try:
        if text in {"all", "max", "allin", "all-in"}:
            return balance
        if text == "half":
            return balance // 2
        if text.endswith("%"):
            return int(balance * float(text[:-1]) / 100)
        if text[-1] in _AMOUNT_SUFFIX:
            return int(float(text[:-1]) * _AMOUNT_SUFFIX[text[-1]])
        return int(float(text))
    except (ValueError, IndexError):
        raise BetError(f"`{raw.strip()}` is not a valid amount. Try `500`, `2.5k`, `half` or `all`.")


def _totals(game: dict) -> tuple[int, list[int], list[int]]:
    per_option = [0] * len(game["options"])
    counts = [0] * len(game["options"])
    pool = 0
    for wager in game.get("wagers", {}).values():
        index = wager["option"]
        if 0 <= index < len(per_option):
            per_option[index] += wager["amount"]
            counts[index] += 1
            pool += wager["amount"]
    return pool, per_option, counts


def game_embed(game: dict, game_id: int, guild: discord.Guild) -> discord.Embed:
    status = game["status"]
    embed = discord.Embed(
        title=game["title"],
        description=game.get("description") or discord.utils.MISSING,
        colour=STATUS_COLOURS.get(status, discord.Colour.blurple()),
    )
    pool, per_option, counts = _totals(game)
    lines = []
    for index, option in enumerate(game["options"]):
        marker = "\N{TROPHY}" if status == "settled" and game.get("winner") == index else f"`{index + 1}.`"
        lines.append(
            f"{marker} **{option['label']}** \u2014 `x{option['odds']:.2f}`\n"
            f"\u2003{_plural(counts[index], 'bet')} \u00b7 {per_option[index]:,} {COOKIE}"
        )
    embed.add_field(name="Outcomes", value="\n".join(lines), inline=False)

    timing = [f"Starts {discord.utils.format_dt(datetime.fromtimestamp(game['starts_at'], timezone.utc), 'F')}"]
    if game.get("locks_at"):
        locked = datetime.fromtimestamp(game["locks_at"], timezone.utc)
        verb = "Betting closed" if status != "open" else "Betting closes"
        timing.append(f"{verb} {discord.utils.format_dt(locked, 'R')}")
    embed.add_field(name="Schedule", value="\n".join(timing), inline=False)

    if status == "settled":
        winner = game["options"][game["winner"]]
        embed.add_field(
            name="Result",
            value=f"**{winner['label']}** paid out at `x{winner['odds']:.2f}`",
            inline=False,
        )

    host = guild.get_member(game.get("host", 0))
    footer = f"Game #{game_id} \u00b7 {STATUS_LABELS.get(status, status)} \u00b7 pool {pool:,} {COOKIE}"
    if host:
        footer = f"{footer} \u00b7 hosted by {host.display_name}"
    embed.set_footer(text=footer)
    return embed


def _open_games(state: dict) -> list[tuple[int, dict]]:
    games = [(int(gid), game) for gid, game in state["games"].items() if game["status"] == "open"]
    games.sort(key=lambda item: item[1].get("starts_at", 0))
    return games


def _live_games(state: dict) -> list[tuple[int, dict]]:
    games = [
        (int(gid), game)
        for gid, game in state["games"].items()
        if game["status"] in {"open", "locked"}
    ]
    games.sort(key=lambda item: item[1].get("starts_at", 0))
    return games


def _resolve_choice(game: dict, choice: str) -> int:
    text = choice.strip()
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(game["options"]):
            return index
        raise BetError(f"Pick a number between 1 and {len(game['options'])}.")
    lowered = text.lower()
    matches = [i for i, option in enumerate(game["options"]) if option["label"].lower() == lowered]
    if not matches:
        matches = [i for i, option in enumerate(game["options"]) if lowered in option["label"].lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise BetError(f"No outcome on that game matches `{text}`.")
    raise BetError(f"`{text}` matches more than one outcome. Use the number instead.")


class GameModal(discord.ui.Modal, title="Create a betting game"):
    event = discord.ui.TextInput(
        label="What is the game?",
        placeholder="Beibot vs Avenoir \u2014 Bo3 scrim",
        max_length=200,
    )
    details = discord.ui.TextInput(
        label="Details (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=600,
    )
    outcomes = discord.ui.TextInput(
        label="Outcomes and odds, one per line",
        style=discord.TextStyle.paragraph,
        placeholder="Beibot | 1.80\nAvenoir | 2.10\nDraw | +450",
        max_length=800,
    )
    starts = discord.ui.TextInput(
        label="When does it happen?",
        placeholder="2h30m  \u00b7  21:00  \u00b7  2026-08-25 19:00",
        max_length=60,
    )
    locks = discord.ui.TextInput(
        label="When does betting close? (optional)",
        placeholder="Defaults to the start time",
        required=False,
        max_length=60,
    )

    def __init__(self, cog: "Bets"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        options = parse_options(str(self.outcomes))
        starts_at = parse_when(str(self.starts))
        locks_at = parse_when(str(self.locks)) if str(self.locks).strip() else starts_at
        if locks_at > starts_at:
            locks_at = starts_at
        if locks_at <= _now():
            raise BetError("Betting would close before anyone could bet. Push the time out a little.")

        draft = {
            "title": str(self.event).strip(),
            "description": str(self.details).strip() or None,
            "options": options,
            "starts_at": starts_at,
            "locks_at": locks_at,
            "status": "open",
            "host": interaction.user.id,
            "wagers": {},
        }
        embed = game_embed(draft, 0, interaction.guild)
        embed.set_footer(text="Preview \u00b7 nothing is saved yet")
        await interaction.response.send_message(
            content="Here is how it will look. Pick a channel if you want it posted somewhere else.",
            embed=embed,
            view=PreviewPanel(self.cog, draft),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.cog.report(interaction, error)


class PreviewPanel(discord.ui.View):
    def __init__(self, cog: "Bets", draft: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.draft = draft
        self.channel_id: Optional[int] = None

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Post it in\u2026 (defaults to here)",
        min_values=1,
        max_values=1,
        row=0,
    )
    async def pick_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.channel_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.success, row=1)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.guild.get_channel(self.channel_id) if self.channel_id else interaction.channel
        if channel is None:
            raise BetError("That channel is gone. Pick another one.")
        game_id = await self.cog.publish_game(interaction.guild, channel, self.draft)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Posted as game **#{game_id}** in {channel.mention}.",
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary, row=1)
    async def discard(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Discarded.", embed=None, view=self)
        self.stop()

    async def on_error(self, interaction, error, item):
        await self.cog.report(interaction, error)


class SetupPanel(discord.ui.View):
    def __init__(self, cog: "Bets", author: discord.Member, state: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.author = author
        live = _live_games(state)
        if live:
            self.add_item(ManageSelect(cog, live))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("That panel is not yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Create Game", style=discord.ButtonStyle.success, row=0)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GameModal(self.cog))

    async def on_error(self, interaction, error, item):
        await self.cog.report(interaction, error)


class ManageSelect(discord.ui.Select):
    def __init__(self, cog: "Bets", games: list[tuple[int, dict]]):
        self.cog = cog
        options = [
            discord.SelectOption(
                label=f"#{gid} \u2014 {game['title']}"[:100],
                value=str(gid),
                description=STATUS_LABELS.get(game["status"]),
            )
            for gid, game in games[:25]
        ]
        super().__init__(placeholder="Manage an existing game\u2026", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        game_id = int(self.values[0])
        game = self.cog.fetch_game(interaction.guild.id, game_id)
        await interaction.response.send_message(
            embed=game_embed(game, game_id, interaction.guild),
            view=ManagePanel(self.cog, game_id, game),
            ephemeral=True,
        )


class ManagePanel(discord.ui.View):
    def __init__(self, cog: "Bets", game_id: int, game: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.game_id = game_id
        self.lock_now.disabled = game["status"] != "open"

    @discord.ui.button(label="Close Betting", style=discord.ButtonStyle.secondary, row=0)
    async def lock_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = await self.cog.lock_game(interaction.guild, self.game_id)
        button.disabled = True
        await interaction.response.edit_message(
            embed=game_embed(game, self.game_id, interaction.guild), view=self
        )

    @discord.ui.button(label="Settle", style=discord.ButtonStyle.success, row=0)
    async def settle(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.cog.fetch_game(interaction.guild.id, self.game_id)
        await interaction.response.send_message(
            "Which outcome won?",
            view=SettleView(self.cog, self.game_id, game),
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel & Refund", style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        game, refunded = await self.cog.cancel_game(interaction.guild, self.game_id)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Cancelled. Refunded {refunded:,} {COOKIE} across {_plural(len(game['wagers']), 'bettor')}.",
            embed=game_embed(game, self.game_id, interaction.guild),
            view=self,
        )

    async def on_error(self, interaction, error, item):
        await self.cog.report(interaction, error)


class SettleView(discord.ui.View):
    def __init__(self, cog: "Bets", game_id: int, game: dict):
        super().__init__(timeout=180)
        self.add_item(SettleSelect(cog, game_id, game))


class SettleSelect(discord.ui.Select):
    def __init__(self, cog: "Bets", game_id: int, game: dict):
        self.cog = cog
        self.game_id = game_id
        options = [
            discord.SelectOption(label=option["label"][:100], value=str(index), description=f"x{option['odds']:.2f}")
            for index, option in enumerate(game["options"])
        ]
        super().__init__(placeholder="Winning outcome\u2026", options=options)

    async def callback(self, interaction: discord.Interaction):
        index = int(self.values[0])
        game, payouts = await self.cog.settle_game(interaction.guild, self.game_id, index)
        total = sum(payout for _, _, payout in payouts)
        winner = game["options"][index]["label"]
        summary = f"Settled game **#{self.game_id}** on **{winner}**.\n"
        if payouts:
            summary += f"Paid {total:,} {COOKIE} to {_plural(len(payouts), 'winner')}."
        else:
            summary += "Nobody backed that outcome."
        self.disabled = True
        await interaction.response.edit_message(content=summary, view=self.view)


class GamePickSelect(discord.ui.Select):
    def __init__(self, cog: "Bets", games: list[tuple[int, dict]]):
        self.cog = cog
        options = [
            discord.SelectOption(label=f"#{gid} \u2014 {game['title']}"[:100], value=str(gid))
            for gid, game in games[:25]
        ]
        super().__init__(placeholder="Which game?", options=options)

    async def callback(self, interaction: discord.Interaction):
        game_id = int(self.values[0])
        game = self.cog.fetch_game(interaction.guild.id, game_id)
        await interaction.response.edit_message(
            content=None,
            embed=game_embed(game, game_id, interaction.guild),
            view=OutcomeView(self.cog, game_id, game),
        )


class GamePickView(discord.ui.View):
    def __init__(self, cog: "Bets", games: list[tuple[int, dict]]):
        super().__init__(timeout=180)
        self.add_item(GamePickSelect(cog, games))


class OutcomeSelect(discord.ui.Select):
    def __init__(self, cog: "Bets", game_id: int, game: dict):
        self.cog = cog
        self.game_id = game_id
        options = [
            discord.SelectOption(
                label=option["label"][:100],
                value=str(index),
                description=f"x{option['odds']:.2f} \u00b7 100 {COOKIE} returns {int(100 * option['odds']):,}",
            )
            for index, option in enumerate(game["options"])
        ]
        super().__init__(placeholder="Back an outcome\u2026", options=options)

    async def callback(self, interaction: discord.Interaction):
        index = int(self.values[0])
        await interaction.response.send_modal(WagerModal(self.cog, self.game_id, index))


class OutcomeView(discord.ui.View):
    def __init__(self, cog: "Bets", game_id: int, game: dict):
        super().__init__(timeout=180)
        self.add_item(OutcomeSelect(cog, game_id, game))


class WagerModal(discord.ui.Modal, title="Place your bet"):
    stake = discord.ui.TextInput(
        label="How many cookies?",
        placeholder="500  \u00b7  2.5k  \u00b7  half  \u00b7  all",
        max_length=20,
    )

    def __init__(self, cog: "Bets", game_id: int, option_index: int):
        super().__init__()
        self.cog = cog
        self.game_id = game_id
        self.option_index = option_index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        balance = _balance(interaction.guild.id, interaction.user.id)
        amount = parse_amount(str(self.stake), balance)
        embed = await self.cog.place_wager(
            interaction.guild, interaction.user, self.game_id, self.option_index, amount
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.cog.report(interaction, error)


class PlaceBetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avenoir:bet:place:(?P<game>\d+)",
):
    def __init__(self, game_id: int, disabled: bool = False):
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                label="Place Bet",
                style=discord.ButtonStyle.success,
                custom_id=f"avenoir:bet:place:{game_id}",
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["game"]))

    async def callback(self, interaction: discord.Interaction):
        cog: Bets = interaction.client.get_cog("Bets")
        game = cog.fetch_game(interaction.guild.id, self.game_id)
        if game["status"] != "open":
            raise BetError("Betting on that game is already closed.")
        await interaction.response.send_message(
            embed=cog.odds_embed(game, interaction.user),
            view=OutcomeView(cog, self.game_id, game),
            ephemeral=True,
        )


class MyBetButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avenoir:bet:mine:(?P<game>\d+)",
):
    def __init__(self, game_id: int):
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                label="My Bet",
                style=discord.ButtonStyle.secondary,
                custom_id=f"avenoir:bet:mine:{game_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["game"]))

    async def callback(self, interaction: discord.Interaction):
        cog: Bets = interaction.client.get_cog("Bets")
        game = cog.fetch_game(interaction.guild.id, self.game_id)
        wager = game.get("wagers", {}).get(str(interaction.user.id))
        if wager is None:
            await interaction.response.send_message("You have not bet on this one yet.", ephemeral=True)
            return
        option = game["options"][wager["option"]]
        payout = int(math.floor(wager["amount"] * option["odds"]))
        embed = discord.Embed(
            title=game["title"],
            colour=STATUS_COLOURS.get(game["status"], discord.Colour.blurple()),
            description=(
                f"Backing **{option['label']}** at `x{option['odds']:.2f}`\n"
                f"Stake **{wager['amount']:,}** {COOKIE}\n"
                f"Returns **{payout:,}** {COOKIE} (profit {payout - wager['amount']:,})"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class GameView(discord.ui.View):
    def __init__(self, game_id: int, closed: bool = False):
        super().__init__(timeout=None)
        self.add_item(PlaceBetButton(game_id, disabled=closed))
        self.add_item(MyBetButton(game_id))


class Bets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(PlaceBetButton, MyBetButton)
        self.lock_sweep.start()

    async def cog_unload(self) -> None:
        self.lock_sweep.cancel()

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        error = getattr(error, "original", error)
        if isinstance(error, commands.NoPrivateMessage):
            message = "Betting only runs inside a server."
        elif isinstance(error, BetError):
            message = str(error)
        elif isinstance(error, commands.MissingRequiredArgument):
            message = f"Missing `{error.param.name}`. Try `{ctx.clean_prefix}bets` to see what is running."
        elif isinstance(error, (commands.BadArgument, commands.BadUnionArgument)):
            message = "I could not read those arguments."
        elif isinstance(error, commands.CheckFailure):
            message = "You need **Manage Server** to do that."
        else:
            raise error
        await ctx.reply(message, mention_author=False, ephemeral=True)

    async def report(self, interaction: discord.Interaction, error: Exception) -> None:
        error = getattr(error, "original", error)
        message = str(error) if isinstance(error, BetError) else "Something went wrong with that."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        if not isinstance(error, BetError):
            raise error

    def fetch_game(self, guild_id: int, game_id: int) -> dict:
        state = _guild_state(_load(), guild_id)
        game = state["games"].get(str(game_id))
        if game is None:
            raise BetError(f"There is no game **#{game_id}**.")
        return game

    def require_manager(self, member: discord.Member, game: Optional[dict] = None) -> None:
        perms = member.guild_permissions
        if perms.manage_guild or perms.administrator:
            return
        if game is not None and game.get("host") == member.id:
            return
        raise commands.CheckFailure()

    def odds_embed(self, game: dict, member: discord.Member) -> discord.Embed:
        balance = _balance(member.guild.id, member.id)
        lines = [
            f"`{index + 1}.` **{option['label']}** \u2014 `x{option['odds']:.2f}`\n"
            f"\u2003100 {COOKIE} returns **{int(100 * option['odds']):,}** \u00b7 "
            f"1,000 returns **{int(1000 * option['odds']):,}**"
            for index, option in enumerate(game["options"])
        ]
        embed = discord.Embed(
            title=game["title"],
            description="\n".join(lines),
            colour=discord.Colour.from_str("#57F287"),
        )
        embed.set_footer(text=f"Your wallet: {balance:,} cookies")
        return embed

    async def publish_game(self, guild: discord.Guild, channel: discord.abc.Messageable, draft: dict) -> int:
        data = _load()
        state = _guild_state(data, guild.id)
        state["counter"] += 1
        game_id = state["counter"]
        game = dict(draft)
        game["channel"] = channel.id
        game["message"] = None
        game["created_at"] = _now()
        state["games"][str(game_id)] = game
        _save(data)

        message = await channel.send(embed=game_embed(game, game_id, guild), view=GameView(game_id))
        data = _load()
        state = _guild_state(data, guild.id)
        state["games"][str(game_id)]["message"] = message.id
        _save(data)
        return game_id

    async def refresh_message(self, guild: discord.Guild, game_id: int, game: dict) -> None:
        channel = guild.get_channel(game.get("channel") or 0)
        if channel is None or not game.get("message"):
            return
        try:
            message = await channel.fetch_message(game["message"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        view = GameView(game_id, closed=game["status"] != "open")
        try:
            await message.edit(embed=game_embed(game, game_id, guild), view=view)
        except discord.HTTPException:
            pass

    async def place_wager(
        self,
        guild: discord.Guild,
        member: discord.Member,
        game_id: int,
        option_index: int,
        amount: int,
    ) -> discord.Embed:
        data = _load()
        state = _guild_state(data, guild.id)
        game = state["games"].get(str(game_id))
        if game is None:
            raise BetError(f"There is no game **#{game_id}**.")
        if game["status"] == "cancelled":
            raise BetError("That game was cancelled.")
        if game["status"] == "settled":
            raise BetError("That game is already settled.")
        if game["status"] != "open" or (game.get("locks_at") and game["locks_at"] <= _now()):
            raise BetError("Betting on that game is closed.")
        if not 0 <= option_index < len(game["options"]):
            raise BetError("That outcome does not exist.")
        if amount < MIN_STAKE:
            raise BetError(f"Bet at least {MIN_STAKE} {COOKIE}.")

        balance = _balance(guild.id, member.id)
        if balance <= 0:
            raise BetError(f"Your wallet is empty. You have 0 {COOKIE}.")
        if amount > balance:
            raise BetError(f"You only have **{balance:,}** {COOKIE}.")

        wagers = game.setdefault("wagers", {})
        existing = wagers.get(str(member.id))
        if existing and existing["option"] != option_index:
            backed = game["options"][existing["option"]]["label"]
            raise BetError(f"You already backed **{backed}** on this game. You cannot split sides.")

        _adjust(guild.id, member.id, -amount)
        if existing:
            existing["amount"] += amount
            staked = existing["amount"]
        else:
            wagers[str(member.id)] = {"option": option_index, "amount": amount, "at": _now()}
            staked = amount
        _save(data)

        option = game["options"][option_index]
        payout = int(math.floor(staked * option["odds"]))
        remaining = _balance(guild.id, member.id)

        embed = discord.Embed(
            title=f"Bet placed \u00b7 {game['title']}",
            colour=discord.Colour.from_str("#57F287"),
            description=(
                f"Backing **{option['label']}** at `x{option['odds']:.2f}`\n\n"
                f"Stake \u2014 **{staked:,}** {COOKIE}\n"
                f"Returns if it lands \u2014 **{payout:,}** {COOKIE}\n"
                f"Profit \u2014 **+{payout - staked:,}** {COOKIE}\n"
                f"Wallet left \u2014 **{remaining:,}** {COOKIE}"
            ),
        )
        if game.get("locks_at"):
            closes = datetime.fromtimestamp(game["locks_at"], timezone.utc)
            embed.set_footer(text=f"Game #{game_id}")
            embed.add_field(
                name="Betting closes",
                value=discord.utils.format_dt(closes, "R"),
                inline=False,
            )
        await self.refresh_message(guild, game_id, game)
        return embed

    async def lock_game(self, guild: discord.Guild, game_id: int) -> dict:
        data = _load()
        state = _guild_state(data, guild.id)
        game = state["games"].get(str(game_id))
        if game is None:
            raise BetError(f"There is no game **#{game_id}**.")
        if game["status"] != "open":
            raise BetError("That game is not taking bets anymore.")
        game["status"] = "locked"
        game["locks_at"] = min(game.get("locks_at") or _now(), _now())
        _save(data)
        await self.refresh_message(guild, game_id, game)
        return game

    async def cancel_game(self, guild: discord.Guild, game_id: int) -> tuple[dict, int]:
        data = _load()
        state = _guild_state(data, guild.id)
        game = state["games"].get(str(game_id))
        if game is None:
            raise BetError(f"There is no game **#{game_id}**.")
        if game["status"] == "settled":
            raise BetError("That game is already settled. Cookies have gone out.")
        if game["status"] == "cancelled":
            raise BetError("That game is already cancelled.")
        refunds = {int(uid): wager["amount"] for uid, wager in game.get("wagers", {}).items()}
        if refunds:
            _adjust_many(guild.id, refunds)
        game["status"] = "cancelled"
        game["cancelled_at"] = _now()
        _save(data)
        await self.refresh_message(guild, game_id, game)
        return game, sum(refunds.values())

    async def settle_game(
        self, guild: discord.Guild, game_id: int, option_index: int
    ) -> tuple[dict, list[tuple[int, int, int]]]:
        data = _load()
        state = _guild_state(data, guild.id)
        game = state["games"].get(str(game_id))
        if game is None:
            raise BetError(f"There is no game **#{game_id}**.")
        if game["status"] == "settled":
            raise BetError("That game is already settled.")
        if game["status"] == "cancelled":
            raise BetError("That game was cancelled.")
        if not 0 <= option_index < len(game["options"]):
            raise BetError("That outcome does not exist.")

        odds = game["options"][option_index]["odds"]
        payouts: list[tuple[int, int, int]] = []
        credits: dict[int, int] = {}
        for uid, wager in game.get("wagers", {}).items():
            if wager["option"] != option_index:
                continue
            payout = int(math.floor(wager["amount"] * odds))
            credits[int(uid)] = payout
            payouts.append((int(uid), wager["amount"], payout))
        if credits:
            _adjust_many(guild.id, credits)

        game["status"] = "settled"
        game["winner"] = option_index
        game["settled_at"] = _now()
        _save(data)
        await self.refresh_message(guild, game_id, game)
        await self.announce_results(guild, game_id, game, payouts)
        return game, payouts

    async def announce_results(
        self, guild: discord.Guild, game_id: int, game: dict, payouts: list[tuple[int, int, int]]
    ) -> None:
        channel = guild.get_channel(game.get("channel") or 0)
        if channel is None:
            return
        winner = game["options"][game["winner"]]
        embed = discord.Embed(
            title=f"Results \u00b7 {game['title']}",
            colour=STATUS_COLOURS["settled"],
            description=f"**{winner['label']}** came in at `x{winner['odds']:.2f}`.",
        )
        if payouts:
            payouts.sort(key=lambda item: item[2], reverse=True)
            lines = []
            for user_id, stake, payout in payouts[:15]:
                member = guild.get_member(user_id)
                name = member.mention if member else f"<@{user_id}>"
                lines.append(f"{name} \u2014 {stake:,} \u2192 **{payout:,}** {COOKIE}")
            if len(payouts) > 15:
                lines.append(f"\u2026and {len(payouts) - 15} more")
            embed.add_field(name="Paid out", value="\n".join(lines), inline=False)
            embed.set_footer(text=f"Game #{game_id} \u00b7 {sum(p for _, _, p in payouts):,} cookies paid")
        else:
            embed.add_field(name="Paid out", value="Nobody backed it. The house keeps everything.", inline=False)
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @tasks.loop(seconds=LOCK_TICK)
    async def lock_sweep(self) -> None:
        data = _load()
        now = _now()
        changed = False
        for guild_id, state in list(data.get("guilds", {}).items()):
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue
            for gid, game in list(state.get("games", {}).items()):
                if game.get("status") != "open":
                    continue
                if not game.get("locks_at") or game["locks_at"] > now:
                    continue
                game["status"] = "locked"
                changed = True
                await self.refresh_message(guild, int(gid), game)
        if changed:
            _save(data)

    @lock_sweep.before_loop
    async def before_lock_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_group(name="bet", fallback="place", invoke_without_command=True)
    async def bet(
        self,
        ctx: commands.Context,
        game: Optional[int] = None,
        choice: Optional[str] = None,
        amount: Optional[str] = None,
    ) -> None:
        state = _guild_state(_load(), ctx.guild.id)
        if game is None:
            games = _open_games(state)
            if not games:
                await ctx.reply(
                    f"Nothing is taking bets right now. Someone with **Manage Server** can run `{ctx.clean_prefix}bet setup`.",
                    mention_author=False,
                )
                return
            await ctx.reply(
                "Pick a game to bet on.",
                view=GamePickView(self, games),
                mention_author=False,
                ephemeral=True,
            )
            return

        record = state["games"].get(str(game))
        if record is None:
            raise BetError(f"There is no game **#{game}**.")

        if choice is None:
            await ctx.reply(
                embed=self.odds_embed(record, ctx.author),
                view=OutcomeView(self, game, record),
                mention_author=False,
                ephemeral=True,
            )
            return

        index = _resolve_choice(record, choice)
        if amount is None:
            raise BetError(f"How much? Try `{ctx.clean_prefix}bet {game} {choice} 500`.")

        stake = parse_amount(amount, _balance(ctx.guild.id, ctx.author.id))
        embed = await self.place_wager(ctx.guild, ctx.author, game, index, stake)
        await ctx.reply(embed=embed, mention_author=False, ephemeral=True)

    @bet.command(name="setup")
    async def bet_setup(self, ctx: commands.Context) -> None:
        self.require_manager(ctx.author)
        state = _guild_state(_load(), ctx.guild.id)
        live = _live_games(state)
        summary = f"{_plural(len(live), 'game')} currently running." if live else "No games running yet."
        await ctx.reply(
            f"**Bet setup** \u2014 {summary}",
            view=SetupPanel(self, ctx.author, state),
            mention_author=False,
            ephemeral=True,
        )

    @bet.command(name="lock")
    async def bet_lock(self, ctx: commands.Context, game: int) -> None:
        self.require_manager(ctx.author, self.fetch_game(ctx.guild.id, game))
        await self.lock_game(ctx.guild, game)
        await ctx.reply(f"Betting closed on game **#{game}**.", mention_author=False)

    @bet.command(name="settle")
    async def bet_settle(self, ctx: commands.Context, game: int, *, choice: Optional[str] = None) -> None:
        record = self.fetch_game(ctx.guild.id, game)
        self.require_manager(ctx.author, record)
        if choice is None:
            await ctx.reply(
                f"Which outcome won game **#{game}**?",
                view=SettleView(self, game, record),
                mention_author=False,
                ephemeral=True,
            )
            return
        index = _resolve_choice(record, choice)
        _, payouts = await self.settle_game(ctx.guild, game, index)
        total = sum(payout for _, _, payout in payouts)
        await ctx.reply(
            f"Settled **#{game}** on **{record['options'][index]['label']}**. "
            f"Paid {total:,} {COOKIE} to {_plural(len(payouts), 'winner')}.",
            mention_author=False,
        )

    @bet.command(name="cancel")
    async def bet_cancel(self, ctx: commands.Context, game: int) -> None:
        self.require_manager(ctx.author, self.fetch_game(ctx.guild.id, game))
        record, refunded = await self.cancel_game(ctx.guild, game)
        await ctx.reply(
            f"Cancelled **#{game}** and refunded {refunded:,} {COOKIE} "
            f"to {_plural(len(record.get('wagers', {})), 'bettor')}.",
            mention_author=False,
        )

    @bet.command(name="mine")
    async def bet_mine(self, ctx: commands.Context) -> None:
        state = _guild_state(_load(), ctx.guild.id)
        lines = []
        pending = 0
        for gid, game in sorted(state["games"].items(), key=lambda item: int(item[0])):
            wager = game.get("wagers", {}).get(str(ctx.author.id))
            if wager is None or game["status"] in {"cancelled"}:
                continue
            option = game["options"][wager["option"]]
            payout = int(math.floor(wager["amount"] * option["odds"]))
            if game["status"] == "settled":
                landed = game.get("winner") == wager["option"]
                tail = f"won **+{payout:,}**" if landed else "lost"
            else:
                tail = f"returns **{payout:,}**"
                pending += wager["amount"]
            lines.append(
                f"`#{gid}` {game['title']} \u2014 **{option['label']}** "
                f"`x{option['odds']:.2f}` \u00b7 {wager['amount']:,} {COOKIE} \u00b7 {tail}"
            )
        if not lines:
            await ctx.reply("You have not placed any bets yet.", mention_author=False, ephemeral=True)
            return
        embed = discord.Embed(
            title=f"Bets \u2014 {ctx.author.display_name}",
            description="\n".join(lines[-20:]),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(
            text=f"{pending:,} cookies still riding \u00b7 wallet {_balance(ctx.guild.id, ctx.author.id):,}"
        )
        await ctx.reply(embed=embed, mention_author=False, ephemeral=True)

    @commands.hybrid_command(name="bets")
    async def bets(self, ctx: commands.Context) -> None:
        state = _guild_state(_load(), ctx.guild.id)
        games = _live_games(state)
        if not games:
            await ctx.reply(
                f"No games are up. Run `{ctx.clean_prefix}bet setup` to start one.",
                mention_author=False,
            )
            return

        embed = discord.Embed(
            title="Games taking bets",
            colour=discord.Colour.from_str("#57F287"),
        )
        for game_id, game in games[:10]:
            pool, per_option, counts = _totals(game)
            odds_line = " \u00b7 ".join(
                f"{option['label']} `x{option['odds']:.2f}`" for option in game["options"][:4]
            )
            if len(game["options"]) > 4:
                odds_line += f" \u00b7 +{len(game['options']) - 4} more"
            when = datetime.fromtimestamp(game["starts_at"], timezone.utc)
            state_line = (
                f"Closes {discord.utils.format_dt(datetime.fromtimestamp(game['locks_at'], timezone.utc), 'R')}"
                if game["status"] == "open" and game.get("locks_at")
                else STATUS_LABELS.get(game["status"], game["status"])
            )
            embed.add_field(
                name=f"#{game_id} \u2014 {game['title']}",
                value=(
                    f"{odds_line}\n"
                    f"{state_line} \u00b7 starts {discord.utils.format_dt(when, 't')}\n"
                    f"Pool {pool:,} {COOKIE} across {_plural(sum(counts), 'bet')}"
                ),
                inline=False,
            )
        embed.set_footer(text=f"{ctx.clean_prefix}bet <id> <outcome> <amount> to get on")
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Bets(bot))