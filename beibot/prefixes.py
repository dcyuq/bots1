from discord.ext import commands
from storage import Store

DEFAULT_PREFIX = ","
MAX_LENGTH = 8

RESERVED_STARTS = ("@", "#", "/")

_store = Store("prefixes.json")
prefixes = _store.load()


def save():
    _store.save(prefixes)


def prefix_for(guild_id):
    """The prefix in use for a guild. Falls back to the default."""
    if guild_id is None:
        return DEFAULT_PREFIX
    return prefixes.get(str(guild_id), DEFAULT_PREFIX)


def prefix_of(source):
    """Convenience for anything carrying a .guild, such as a ctx or message."""
    guild = getattr(source, "guild", None)
    return prefix_for(guild.id if guild else None)


def is_custom(guild_id):
    return str(guild_id) in prefixes


def set_prefix(guild_id, value):
    prefixes[str(guild_id)] = value
    save()


def clear_prefix(guild_id):
    """Drop a guild's override. Returns the prefix that was removed, or None."""
    removed = prefixes.pop(str(guild_id), None)
    if removed is not None:
        save()
    return removed


def validate(raw):
    """Check a candidate prefix. Returns (prefix, error) with one of them None.

    A trailing space is allowed if the whole thing is wrapped in quotes, so
    "6ix " gives you a word prefix that reads naturally in chat.
    """
    if raw is None:
        return None, "Give me a prefix to use."

    value = raw.strip()

    if len(value) > 1 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]

    if not value.strip():
        return None, "A prefix can't be empty."

    if len(value) > MAX_LENGTH:
        return None, f"Keep it to {MAX_LENGTH} characters or fewer."

    core = value.rstrip(" ")

    if any(ch.isspace() for ch in core):
        return None, (
            "A prefix can't contain spaces. If you want a trailing space, "
            'wrap it in quotes like "6ix ".'
        )

    if value.startswith(RESERVED_STARTS):
        return None, (
            "A prefix can't start with @, # or / since those collide with "
            "mentions, channels and slash commands."
        )

    return value, None


def resolve_prefix(bot, message):
    """Prefix callable for commands.Bot.

    Mentioning the bot always works alongside the configured prefix, so a
    guild can never lock itself out by setting something unusable.
    """
    guild = message.guild
    current = prefix_for(guild.id if guild else None)
    return commands.when_mentioned_or(current)(bot, message)