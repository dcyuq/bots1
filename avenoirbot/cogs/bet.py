from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
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

ODDS_CUSHION = 500
ODDS_MIN = 1.01
ODDS_MAX = 100.0

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


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def parse_options(raw: str) -> list[dict]:
    """One outcome per line. No odds — the pool sets those now."""
    options: list[dict] = []
    for line in raw.splitlines():
        label = line.strip().lstrip("-•").strip()
        if "|" in label:
            head, _, tail = label.rpartition("|")
            if head.strip() and _looks_like_odds(tail):
                label = head.strip()
        if not label:
            continue
        options.append({"label": label[:70]})
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


def _looks_like_odds(token: str) -> bool:
    token = token.strip().replace(",", "").replace("x", "").replace("×", "")
    if not token:
        return False
    if "/" in token:
        num, _, den = token.partition("/")
        return num.strip().isdigit() and den.strip().isdigit()
    try:
        float(token)
        return True
    except ValueError:
        return False


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


def _live_odds(game: dict) -> list[float]:
    """Current pool-driven odds for every outcome.

    odds_i = (cushion*N + pool) / (cushion + stake_on_i)

    Empty pool -> every outcome is N.0x. Money on an outcome shrinks its odds
    and, by growing the numerator, lengthens the others. Bounded to [MIN, MAX].
    """
    options = game["options"]
    n = len(options)
    if n == 0:
        return []
    cushion = game.get("cushion", ODDS_CUSHION)
    _, per_option, _ = _totals(game)
    effective = [cushion + staked for staked in per_option]
    total = sum(effective)
    odds = []
    for eff in effective:
        raw = total / eff if eff else ODDS_MAX
        odds.append(max(ODDS_MIN, min(ODDS_MAX, round(raw, 2))))
    return odds


def _display_odds(game: dict) -> list[float]:
    """What to show next to each outcome: pool odds for the new dynamic games,
    the frozen host line for any legacy fixed-odds game still on file."""
    if game.get("model") == "dynamic":
        return _live_odds(game)
    return [float(option.get("odds", 1.0)) for option in game["options"]]


def _wager_odds(game: dict, wager: dict) -> float:
    """The multiplier a single wager pays at. Dynamic games lock this in per
    bettor at bet time; legacy games use the outcome's fixed odds."""
    if game.get("model") == "dynamic":
        odds = wager.get("odds")
        if odds is None:
            live = _live_odds(game)
            odds = live[wager["option"]] if 0 <= wager["option"] < len(live) else 1.0
        return float(odds)
    option = game["options"][wager["option"]]
    return float(option.get("odds", 1.0))


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


def _rule() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def _status_colour(game: dict) -> discord.Colour:
    return STATUS_COLOURS.get(game.get("status"), discord.Colour.blurple())


def _outcomes_block(game: dict) -> str:
    status = game["status"]
    odds = _display_odds(game)
    _, per_option, counts = _totals(game)
    lines = []
    for index, option in enumerate(game["options"]):
        won = status == "settled" and game.get("winner") == index
        marker = "**✓**" if won else f"`{index + 1}.`"
        lines.append(
            f"{marker}  **{option['label']}**  —  `{odds[index]:.2f}×`\n"
            f" {_plural(counts[index], 'bet')} · {per_option[index]:,} {COOKIE}"
        )
    return "\n\n".join(lines)


def _schedule_block(game: dict) -> str:
    status = game["status"]
    parts = [f"Starts {discord.utils.format_dt(_dt(game['starts_at']), 'F')}"]
    if game.get("locks_at"):
        verb = "Betting closes" if status == "open" else "Betting closed"
        parts.append(f"{verb} {discord.utils.format_dt(_dt(game['locks_at']), 'R')}")
    return "**Schedule**\n" + "\n".join(parts)


def _footer_block(game: dict, game_id: int, guild: Optional[discord.Guild], preview: bool) -> str:
    if preview:
        return "-# Preview · nothing is saved yet"
    pool, _, _ = _totals(game)
    bits = [f"Game #{game_id}", STATUS_LABELS.get(game["status"], game["status"]), f"pool {pool:,} {COOKIE}"]
    host = guild.get_member(game.get("host", 0)) if guild else None
    if host:
        bits.append(f"hosted by {host.display_name}")
    return "-# " + " · ".join(bits)


def _game_container(
    game: dict, game_id: int, guild: Optional[discord.Guild], *, preview: bool = False
) -> discord.ui.Container:
    container = discord.ui.Container(accent_colour=_status_colour(game))
    container.add_item(discord.ui.TextDisplay(f"### {game['title']}"))
    if game.get("description"):
        container.add_item(discord.ui.TextDisplay(game["description"]))
    container.add_item(_rule())
    container.add_item(discord.ui.TextDisplay(_outcomes_block(game)))
    container.add_item(_rule())
    container.add_item(discord.ui.TextDisplay(_schedule_block(game)))
    if game["status"] == "settled" and game.get("winner") is not None:
        winner = game["options"][game["winner"]]
        closed_at = _display_odds(game)[game["winner"]]
        container.add_item(_rule())
        container.add_item(
            discord.ui.TextDisplay(
                f"**Result**\n**{winner['label']}** came in — closing line `{closed_at:.2f}×`"
            )
        )
    container.add_item(_rule())
    container.add_item(discord.ui.TextDisplay(_footer_block(game, game_id, guild, preview)))
    return container


def _notice(text: str, colour: Optional[discord.Colour] = None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=colour or discord.Colour.greyple())
    container.add_item(discord.ui.TextDisplay(text))
    view.add_item(container)
    return view


def _confirmation(
    game: dict, game_id: int, option: dict, odds: float, staked: int, payout: int, remaining: int
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=STATUS_COLOURS["open"])
    container.add_item(discord.ui.TextDisplay(f"### Bet placed — {game['title']}"))
    container.add_item(_rule())
    container.add_item(discord.ui.TextDisplay(f"Backing **{option['label']}** at `{odds:.2f}×`"))
    container.add_item(_rule())
    container.add_item(
        discord.ui.TextDisplay(
            f"Stake  —  **{staked:,}** {COOKIE}\n"
            f"Returns if it lands  —  **{payout:,}** {COOKIE}\n"
            f"Profit  —  **+{payout - staked:,}** {COOKIE}\n"
            f"Wallet left  —  **{remaining:,}** {COOKIE}"
        )
    )
    footer = f"-# Game #{game_id}"
    if game.get("locks_at"):
        footer += f" · closes {discord.utils.format_dt(_dt(game['locks_at']), 'R')}"
    container.add_item(_rule())
    container.add_item(discord.ui.TextDisplay(footer))
    view.add_item(container)
    return view


class GameModal(discord.ui.Modal, title="Create a betting game"):
    event = discord.ui.TextInput(
        label="What is the game?",
        placeholder="Beibot vs Avenoir — Bo3 scrim",
        max_length=200,
    )
    details = discord.ui.TextInput(
        label="Details (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=600,
    )
    outcomes = discord.ui.TextInput(
        label="Outcomes, one per line",
        style=discord.TextStyle.paragraph,
        placeholder="Beibot\nAvenoir\nDraw",
        max_length=800,
    )
    starts = discord.ui.TextInput(
        label="When does it happen?",
        placeholder="2h30m  ·  21:00  ·  2026-08-25 19:00",
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
            "model": "dynamic",
            "cushion": ODDS_CUSHION,
        }
        await interaction.response.send_message(
            view=PreviewPanel(self.cog, draft, interaction.guild),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.cog.report(interaction, error)


class PreviewChannelRow(discord.ui.ActionRow):
    def __init__(self, panel: "PreviewPanel"):
        super().__init__()
        self.panel = panel

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Post it in… (defaults to here)",
        min_values=1,
        max_values=1,
    )
    async def pick_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.panel.channel_id = select.values[0].id
        await interaction.response.defer()


class PreviewButtons(discord.ui.ActionRow):
    def __init__(self, panel: "PreviewPanel"):
        super().__init__()
        self.panel = panel

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button):
        panel = self.panel
        channel = (
            interaction.guild.get_channel(panel.channel_id)
            if panel.channel_id
            else interaction.channel
        )
        if channel is None:
            raise BetError("That channel is gone. Pick another one.")
        game_id = await panel.cog.publish_game(interaction.guild, channel, panel.draft)
        await interaction.response.edit_message(
            view=_notice(f"Posted as game **#{game_id}** in {channel.mention}.", STATUS_COLOURS["open"])
        )
        panel.stop()

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary)
    async def discard(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=_notice("Discarded."))
        self.panel.stop()


class PreviewPanel(discord.ui.LayoutView):
    def __init__(self, cog: "Bets", draft: dict, guild: Optional[discord.Guild]):
        super().__init__(timeout=300)
        self.cog = cog
        self.draft = draft
        self.channel_id: Optional[int] = None
        container = _game_container(draft, 0, guild, preview=True)
        container.add_item(_rule())
        container.add_item(PreviewChannelRow(self))
        container.add_item(PreviewButtons(self))
        self.add_item(container)

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
                label=f"#{gid} — {game['title']}"[:100],
                value=str(gid),
                description=STATUS_LABELS.get(game["status"]),
            )
            for gid, game in games[:25]
        ]
        super().__init__(placeholder="Manage an existing game…", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        game_id = int(self.values[0])
        game = self.cog.fetch_game(interaction.guild.id, game_id)
        await interaction.response.send_message(
            view=_manage_view(self.cog, game_id, game, interaction.guild),
            ephemeral=True,
        )


class ManageRow(discord.ui.ActionRow):
    def __init__(self, cog: "Bets", game_id: int, game: dict):
        super().__init__()
        self.cog = cog
        self.game_id = game_id
        finished = game["status"] in {"settled", "cancelled"}
        self.lock_now.disabled = game["status"] != "open"
        self.settle.disabled = finished
        self.cancel.disabled = finished

    @discord.ui.button(label="Close Betting", style=discord.ButtonStyle.secondary)
    async def lock_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = await self.cog.lock_game(interaction.guild, self.game_id)
        await interaction.response.edit_message(
            view=_manage_view(self.cog, self.game_id, game, interaction.guild, note="Betting closed.")
        )

    @discord.ui.button(label="Settle", style=discord.ButtonStyle.success)
    async def settle(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.cog.fetch_game(interaction.guild.id, self.game_id)
        await interaction.response.send_message(
            content="Which outcome won?",
            view=SettleView(self.cog, self.game_id, game),
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel & Refund", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        game, refunded = await self.cog.cancel_game(interaction.guild, self.game_id)
        note = f"Cancelled. Refunded {refunded:,} {COOKIE} across {_plural(len(game['wagers']), 'bettor')}."
        await interaction.response.edit_message(
            view=_manage_view(self.cog, self.game_id, game, interaction.guild, note=note)
        )

    async def on_error(self, interaction, error, item):
        await self.cog.report(interaction, error)


def _manage_view(
    cog: "Bets", game_id: int, game: dict, guild: Optional[discord.Guild], *, note: Optional[str] = None
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=300)
    container = _game_container(game, game_id, guild)
    if note:
        container.add_item(_rule())
        container.add_item(discord.ui.TextDisplay(f"**{note}**"))
    if game["status"] not in {"settled", "cancelled"}:
        container.add_item(_rule())
        container.add_item(ManageRow(cog, game_id, game))
    view.add_item(container)
    return view


class SettleView(discord.ui.View):
    def __init__(self, cog: "Bets", game_id: int, game: dict):
        super().__init__(timeout=180)
        self.add_item(SettleSelect(cog, game_id, game))


class SettleSelect(discord.ui.Select):
    def __init__(self, cog: "Bets", game_id: int, game: dict):
        self.cog = cog
        self.game_id = game_id
        odds = _display_odds(game)
        options = [
            discord.SelectOption(
                label=option["label"][:100],
                value=str(index),
                description=f"{odds[index]:.2f}× now",
            )
            for index, option in enumerate(game["options"])
        ]
        super().__init__(placeholder="Winning outcome…", options=options)

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
            discord.SelectOption(label=f"#{gid} — {game['title']}"[:100], value=str(gid))
            for gid, game in games[:25]
        ]
        super().__init__(placeholder="Which game?", options=options)

    async def callback(self, interaction: discord.Interaction):
        game_id = int(self.values[0])
        game = self.cog.fetch_game(interaction.guild.id, game_id)
        await interaction.response.edit_message(content=f"Betting on game **#{game_id}**.", view=None)
        await interaction.followup.send(
            view=OddsPanel(self.cog, game_id, game, interaction.user),
            ephemeral=True,
        )


class GamePickView(discord.ui.View):
    def __init__(self, cog: "Bets", games: list[tuple[int, dict]]):
        super().__init__(timeout=180)
        self.add_item(GamePickSelect(cog, games))


class OutcomeSelect(discord.ui.Select):
    def __init__(self, cog: "Bets", game_id: int, game: dict):
        self.cog = cog
        self.game_id = game_id
        odds = _display_odds(game)
        options = [
            discord.SelectOption(
                label=option["label"][:100],
                value=str(index),
                description=f"{odds[index]:.2f}× · 100 {COOKIE} → {int(100 * odds[index]):,}",
            )
            for index, option in enumerate(game["options"])
        ]
        super().__init__(placeholder="Back an outcome…", options=options)

    async def callback(self, interaction: discord.Interaction):
        index = int(self.values[0])
        await interaction.response.send_modal(WagerModal(self.cog, self.game_id, index))


class OutcomeRow(discord.ui.ActionRow):
    def __init__(self, cog: "Bets", game_id: int, game: dict):
        super().__init__()
        self.add_item(OutcomeSelect(cog, game_id, game))


class OddsPanel(discord.ui.LayoutView):
    def __init__(self, cog: "Bets", game_id: int, game: dict, member: discord.Member):
        super().__init__(timeout=180)
        odds = _display_odds(game)
        lines = []
        for index, option in enumerate(game["options"]):
            lines.append(
                f"`{index + 1}.` **{option['label']}** — `{odds[index]:.2f}×`\n"
                f" 100 {COOKIE} → **{int(100 * odds[index]):,}**  ·  "
                f"1,000 → **{int(1000 * odds[index]):,}**"
            )
        container = discord.ui.Container(accent_colour=_status_colour(game))
        container.add_item(discord.ui.TextDisplay(f"### {game['title']}"))
        container.add_item(_rule())
        container.add_item(discord.ui.TextDisplay("\n\n".join(lines)))
        container.add_item(_rule())
        container.add_item(
            discord.ui.TextDisplay(
                f"-# Your wallet: {_balance(member.guild.id, member.id):,} {COOKIE} · "
                "you keep whatever odds you bet at"
            )
        )
        container.add_item(OutcomeRow(cog, game_id, game))
        self.add_item(container)


class WagerModal(discord.ui.Modal, title="Place your bet"):
    stake = discord.ui.TextInput(
        label="How many cookies?",
        placeholder="500  ·  2.5k  ·  half  ·  all",
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
        view = await self.cog.place_wager(
            interaction.guild, interaction.user, self.game_id, self.option_index, amount
        )
        await interaction.response.send_message(view=view, ephemeral=True)

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
            view=OddsPanel(cog, self.game_id, game, interaction.user),
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
        odds = _wager_odds(game, wager)
        payout = int(math.floor(wager["amount"] * odds))
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_colour=_status_colour(game))
        container.add_item(discord.ui.TextDisplay(f"### Your bet — {game['title']}"))
        container.add_item(_rule())
        container.add_item(
            discord.ui.TextDisplay(
                f"Backing **{option['label']}** at `{odds:.2f}×`\n"
                f"Stake **{wager['amount']:,}** {COOKIE}\n"
                f"Returns **{payout:,}** {COOKIE} (profit +{payout - wager['amount']:,})"
            )
        )
        view.add_item(container)
        await interaction.response.send_message(view=view, ephemeral=True)


class GameActions(discord.ui.ActionRow):
    def __init__(self, game_id: int, closed: bool = False):
        super().__init__()
        self.add_item(PlaceBetButton(game_id, disabled=closed))
        self.add_item(MyBetButton(game_id))


class GamePost(discord.ui.LayoutView):
    def __init__(self, game: dict, game_id: int, guild: Optional[discord.Guild], closed: bool = False):
        super().__init__(timeout=None)
        container = _game_container(game, game_id, guild)
        container.add_item(_rule())
        container.add_item(GameActions(game_id, closed=closed))
        self.add_item(container)


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

        message = await channel.send(view=GamePost(game, game_id, guild))
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
        view = GamePost(game, game_id, guild, closed=game["status"] != "open")
        try:
            await message.edit(view=view)
        except discord.HTTPException:
            pass

    async def place_wager(
        self,
        guild: discord.Guild,
        member: discord.Member,
        game_id: int,
        option_index: int,
        amount: int,
    ) -> discord.ui.LayoutView:
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

        dynamic = game.get("model") == "dynamic"
        current_odds = _live_odds(game)[option_index] if dynamic else float(
            game["options"][option_index].get("odds", 1.0)
        )

        _adjust(guild.id, member.id, -amount)
        if existing:
            if dynamic:
                prior_amount = existing["amount"]
                prior_odds = float(existing.get("odds", current_odds))
                blended = (prior_amount * prior_odds + amount * current_odds) / (prior_amount + amount)
                existing["odds"] = round(blended, 2)
            existing["amount"] += amount
            staked = existing["amount"]
            locked = float(existing.get("odds", current_odds))
        else:
            record = {"option": option_index, "amount": amount, "at": _now()}
            if dynamic:
                record["odds"] = current_odds
            wagers[str(member.id)] = record
            staked = amount
            locked = current_odds
        _save(data)

        option = game["options"][option_index]
        payout = int(math.floor(staked * locked))
        remaining = _balance(guild.id, member.id)

        await self.refresh_message(guild, game_id, game)
        return _confirmation(game, game_id, option, locked, staked, payout, remaining)

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

        payouts: list[tuple[int, int, int]] = []
        credits: dict[int, int] = {}
        for uid, wager in game.get("wagers", {}).items():
            if wager["option"] != option_index:
                continue
            odds = _wager_odds(game, wager)
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
        closing = _display_odds(game)[game["winner"]]
        container = discord.ui.Container(accent_colour=STATUS_COLOURS["settled"])
        container.add_item(discord.ui.TextDisplay(f"### Results — {game['title']}"))
        container.add_item(_rule())
        container.add_item(
            discord.ui.TextDisplay(f"**{winner['label']}** came in — closing line `{closing:.2f}×`.")
        )
        container.add_item(_rule())
        if payouts:
            payouts.sort(key=lambda item: item[2], reverse=True)
            lines = []
            for user_id, stake, payout in payouts[:15]:
                member = guild.get_member(user_id)
                name = member.mention if member else f"<@{user_id}>"
                lines.append(f"{name} — {stake:,} → **{payout:,}** {COOKIE}")
            if len(payouts) > 15:
                lines.append(f"…and {len(payouts) - 15} more")
            container.add_item(discord.ui.TextDisplay("**Paid out**\n" + "\n".join(lines)))
            container.add_item(_rule())
            container.add_item(
                discord.ui.TextDisplay(f"-# Game #{game_id} · {sum(p for _, _, p in payouts):,} {COOKIE} paid")
            )
        else:
            container.add_item(discord.ui.TextDisplay("Nobody backed it. Nothing to pay out."))
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        try:
            await channel.send(view=view)
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

    @commands.hybrid_group(
        name="bet",
        fallback="place",
        invoke_without_command=True,
        description="Place a bet on a live game, or open the betting menu.",
    )
    @app_commands.describe(
        game="Game id from /bets",
        choice="Outcome number or name",
        amount="500, 2.5k, half, all or 40%",
    )
    async def bet(
        self,
        ctx: commands.Context,
        game: Optional[int] = None,
        choice: Optional[str] = None,
        amount: Optional[str] = None,
    ) -> None:
        state = _guild_state(_load(), ctx.guild.id)
        if game is None:
            passed = (ctx.subcommand_passed or "").strip() if ctx.interaction is None else ""
            if passed and not passed.isdigit():
                raise BetError(
                    f"`{passed}` is not a bet subcommand or a game id. "
                    f"Try `{ctx.clean_prefix}help bet` for the list."
                )
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
                view=OddsPanel(self, game, record, ctx.author),
                mention_author=False,
                ephemeral=True,
            )
            return

        index = _resolve_choice(record, choice)
        if amount is None:
            raise BetError(f"How much? Try `{ctx.clean_prefix}bet {game} {choice} 500`.")

        stake = parse_amount(amount, _balance(ctx.guild.id, ctx.author.id))
        view = await self.place_wager(ctx.guild, ctx.author, game, index, stake)
        await ctx.reply(view=view, mention_author=False, ephemeral=True)

    @bet.command(name="setup", description="Create or manage betting games (Manage Server).")
    async def bet_setup(self, ctx: commands.Context) -> None:
        self.require_manager(ctx.author)
        state = _guild_state(_load(), ctx.guild.id)
        live = _live_games(state)
        summary = f"{_plural(len(live), 'game')} currently running." if live else "No games running yet."
        await ctx.reply(
            f"**Bet setup** — {summary}",
            view=SetupPanel(self, ctx.author, state),
            mention_author=False,
            ephemeral=True,
        )

    @bet.command(name="lock", description="Close betting on a game early.")
    @app_commands.describe(game="Game id to close betting on")
    async def bet_lock(self, ctx: commands.Context, game: int) -> None:
        self.require_manager(ctx.author, self.fetch_game(ctx.guild.id, game))
        await self.lock_game(ctx.guild, game)
        await ctx.reply(f"Betting closed on game **#{game}**.", mention_author=False)

    @bet.command(name="settle", description="Settle a game and pay out the winners.")
    @app_commands.describe(game="Game id to settle", choice="Winning outcome, leave blank to pick from a menu")
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

    @bet.command(name="cancel", description="Cancel a game and refund every bet.")
    @app_commands.describe(game="Game id to cancel and refund")
    async def bet_cancel(self, ctx: commands.Context, game: int) -> None:
        self.require_manager(ctx.author, self.fetch_game(ctx.guild.id, game))
        record, refunded = await self.cancel_game(ctx.guild, game)
        await ctx.reply(
            f"Cancelled **#{game}** and refunded {refunded:,} {COOKIE} "
            f"to {_plural(len(record.get('wagers', {})), 'bettor')}.",
            mention_author=False,
        )

    @bet.command(name="mine", description="See the bets you have placed.")
    async def bet_mine(self, ctx: commands.Context) -> None:
        state = _guild_state(_load(), ctx.guild.id)
        lines = []
        pending = 0
        for gid, game in sorted(state["games"].items(), key=lambda item: int(item[0])):
            wager = game.get("wagers", {}).get(str(ctx.author.id))
            if wager is None or game["status"] in {"cancelled"}:
                continue
            option = game["options"][wager["option"]]
            odds = _wager_odds(game, wager)
            payout = int(math.floor(wager["amount"] * odds))
            if game["status"] == "settled":
                landed = game.get("winner") == wager["option"]
                tail = f"won **+{payout:,}**" if landed else "lost"
            else:
                tail = f"returns **{payout:,}**"
                pending += wager["amount"]
            lines.append(
                f"`#{gid}` {game['title']} — **{option['label']}** "
                f"`{odds:.2f}×` · {wager['amount']:,} {COOKIE} · {tail}"
            )
        if not lines:
            await ctx.reply("You have not placed any bets yet.", mention_author=False, ephemeral=True)
            return
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(f"### Bets — {ctx.author.display_name}"))
        container.add_item(_rule())
        container.add_item(discord.ui.TextDisplay("\n".join(lines[-20:])))
        container.add_item(_rule())
        container.add_item(
            discord.ui.TextDisplay(
                f"-# {pending:,} {COOKIE} still riding · wallet {_balance(ctx.guild.id, ctx.author.id):,}"
            )
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        await ctx.reply(view=view, mention_author=False, ephemeral=True)

    @bet.command(name="wallet", description="Check how many cookies you or someone else holds.")
    @app_commands.describe(member="Whose wallet to check, defaults to you")
    async def bet_wallet(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        target = member or ctx.author
        if target.bot:
            raise BetError("Bots do not hold cookies.")
        await ctx.reply(
            f"{target.display_name} holds **{_balance(ctx.guild.id, target.id):,}** {COOKIE}.",
            mention_author=False,
        )

    def _log_grant(self, guild_id: int, actor: int, target: int, delta: int, reason: Optional[str]) -> None:
        data = _load()
        state = _guild_state(data, guild_id)
        log = state.setdefault("grants", [])
        log.append({"actor": actor, "target": target, "delta": delta, "reason": reason, "at": _now()})
        del log[:-GRANT_LOG_LIMIT]
        _save(data)

    async def _transfer(
        self, ctx: commands.Context, member: discord.Member, amount: int, reason: Optional[str]
    ) -> None:
        self.require_manager(ctx.author)
        if member.bot:
            raise BetError("Bots do not hold cookies.")
        if amount == 0:
            raise BetError("That would do nothing.")
        if abs(amount) > MAX_GRANT:
            raise BetError(f"Cap is {MAX_GRANT:,} {COOKIE} per use.")

        before = _balance(ctx.guild.id, member.id)
        after = _adjust(ctx.guild.id, member.id, amount)
        moved = after - before
        self._log_grant(ctx.guild.id, ctx.author.id, member.id, moved, reason)

        verb = "Gave" if amount > 0 else "Took"
        line = f"{verb} **{abs(moved):,}** {COOKIE} — {member.mention} now holds **{after:,}**."
        if moved != amount:
            line += f"\n Clipped from {abs(amount):,} — their wallet bottomed out."
        if reason:
            line += f"\n Reason: {reason}"
        await ctx.reply(line, mention_author=False)

    @bet.command(name="give", description="Give cookies to a member (Manage Server).")
    @app_commands.describe(
        member="Who gets the cookies",
        amount="How many to hand over",
        reason="Optional note for the grant log",
    )
    async def bet_give(
        self, ctx: commands.Context, member: discord.Member, amount: int, *, reason: Optional[str] = None
    ) -> None:
        await self._transfer(ctx, member, abs(amount), reason)

    @bet.command(name="take", description="Take cookies from a member (Manage Server).")
    @app_commands.describe(
        member="Who loses the cookies",
        amount="How many to remove",
        reason="Optional note for the grant log",
    )
    async def bet_take(
        self, ctx: commands.Context, member: discord.Member, amount: int, *, reason: Optional[str] = None
    ) -> None:
        await self._transfer(ctx, member, -abs(amount), reason)

    @bet.command(name="grants", description="Show the recent cookie grant log (Manage Server).")
    async def bet_grants(self, ctx: commands.Context) -> None:
        self.require_manager(ctx.author)
        log = _guild_state(_load(), ctx.guild.id).get("grants", [])
        if not log:
            await ctx.reply("No cookies have been handed out yet.", mention_author=False, ephemeral=True)
            return
        lines = []
        for entry in reversed(log[-15:]):
            sign = "+" if entry["delta"] > 0 else "-"
            stamp = discord.utils.format_dt(_dt(entry["at"]), "R")
            tail = f" · {entry['reason']}" if entry.get("reason") else ""
            lines.append(
                f"<@{entry['actor']}> → <@{entry['target']}> "
                f"`{sign}{abs(entry['delta']):,}` {stamp}{tail}"
            )
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay("### Recent cookie grants"))
        container.add_item(_rule())
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        container.add_item(_rule())
        container.add_item(discord.ui.TextDisplay(f"-# Last {len(log)} kept"))
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        await ctx.reply(view=view, mention_author=False, ephemeral=True)

    @commands.hybrid_command(name="bets", description="List the games currently taking bets.")
    async def bets(self, ctx: commands.Context) -> None:
        state = _guild_state(_load(), ctx.guild.id)
        games = _live_games(state)
        if not games:
            await ctx.reply(
                f"No games are up. Run `{ctx.clean_prefix}bet setup` to start one.",
                mention_author=False,
            )
            return

        container = discord.ui.Container(accent_colour=STATUS_COLOURS["open"])
        container.add_item(discord.ui.TextDisplay("### Games taking bets"))
        for game_id, game in games[:10]:
            pool, per_option, counts = _totals(game)
            odds = _display_odds(game)
            odds_line = " · ".join(
                f"{option['label']} `{odds[index]:.2f}×`"
                for index, option in enumerate(game["options"][:4])
            )
            if len(game["options"]) > 4:
                odds_line += f" · +{len(game['options']) - 4} more"
            when = _dt(game["starts_at"])
            state_line = (
                f"Closes {discord.utils.format_dt(_dt(game['locks_at']), 'R')}"
                if game["status"] == "open" and game.get("locks_at")
                else STATUS_LABELS.get(game["status"], game["status"])
            )
            container.add_item(_rule())
            container.add_item(
                discord.ui.TextDisplay(
                    f"**#{game_id} — {game['title']}**\n"
                    f"{odds_line}\n"
                    f"{state_line} · starts {discord.utils.format_dt(when, 't')}\n"
                    f"Pool {pool:,} {COOKIE} across {_plural(sum(counts), 'bet')}"
                )
            )
        container.add_item(_rule())
        container.add_item(
            discord.ui.TextDisplay(f"-# {ctx.clean_prefix}bet <id> <outcome> <amount> to get on")
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        await ctx.reply(view=view, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Bets(bot))
