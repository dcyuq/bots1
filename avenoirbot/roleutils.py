import discord

DANGEROUS_PERMS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "manage_messages",
    "ban_members",
    "kick_members",
    "moderate_members",
    "mention_everyone",
)


def dangerous_perms(role: discord.Role):
    """Return the list of escalation-capable permissions a role grants."""
    perms = role.permissions
    return [name for name in DANGEROUS_PERMS if getattr(perms, name)]


def check_role_assignable(role, actor, allow_privileged=False):
    """Validate a role the bot is being asked to hand out.

    Returns an error string, or None when the role is safe to use.

    allow_privileged relaxes the escalation blocklist. Use it only for
    deliberate one-off assignments where a human named both the role and
    the recipient. Automatic assignment should always leave it off.
    """
    guild = role.guild

    if role.is_default():
        return "That's the @everyone role, which can't be assigned."

    if role.managed:
        return "That role is managed by an integration, so a bot can't assign it."

    if not guild.me.guild_permissions.manage_roles:
        return "I don't have the Manage Roles permission."

    if role >= guild.me.top_role:
        return (
            "That role is above me in the hierarchy. Move my role higher in "
            "Server Settings first."
        )

    if actor.id != guild.owner_id and role >= actor.top_role:
        return "That role is at or above your own highest role."

    if not allow_privileged:
        granted = dangerous_perms(role)
        if granted:
            listed = ", ".join(granted)
            return (
                f"That role grants **{listed}**. I won't hand out privileged "
                "roles automatically."
            )

    return None


def assignable_now(role, me):
    """Cheap recheck immediately before an automatic assignment.

    Config is validated when it's set, but roles get edited afterwards. A
    role that was harmless at setup time can be granted Administrator a
    week later, so the blocklist is re-tested at the moment of use.
    """
    return (
        role is not None
        and not role.is_default()
        and not role.managed
        and role < me.top_role
        and not dangerous_perms(role)
    )