# beibot

A discord.py bot for community moderation, tickets and Roblox lookups.

Default prefix: `,` (changeable per server with `,prefix set`)

Nearly every command is also a slash command. The prefix form and the
slash form run the same code, so they never drift apart.

## Commands

### Moderation
- `,kick <member> [reason]`
- `,ban <user> [reason]`
- `,unban <user> [reason]`
- `,timeout <member> <minutes> [reason]`
- `,untimeout <member> [reason]`

### Tickets
- `,ticketsetup fast` - quick setup with one default button
- `,ticketsetup custom` - full builder
- `,ticketsetup edit` - reopen the builder
- `,ticketstats` - open, unclaimed and lifetime counts

### Roblox
- `,gamepass <link or ID>` - price and creator payout
- `,eligible <user>` - group membership and payout eligibility (alias: `,elig`)
- `,gc addgroup <link>`
- `,gc removegroup <link>`
- `,gc list`
- `,gc setdays <days>`
- `,gc override <user> <group> <days>`
- `,gc clear <user>`

### Roles
- `,role <member> <role>` - add or remove manually
- `,autorole` - roles given on join
- `,vanity` - award a role for a keyword in a custom status
- `,unvanity` - disable it

### Messages
- `,send` - post a message or embed as the bot
- `,scheduled` - list and cancel scheduled messages
- `,purge [filter]` - bulk delete
- `,purgehelp` - filters and examples
- `,stickynote` - pin a message to the bottom of a channel
- `,unsticky` - remove the active sticky
- `,autoresponder` - auto-reply to trigger words

### Stamps
- `,stamps [member]` - show a stamp card (alias: `,mycard`)
- `,stamp <member>` - give one stamp
- `,stamp add <member> [amount]`
- `,stamp remove <member> [amount]`
- `,stamp set <member> <count> [completed]`
- `,stamp clear <member>`
- `,stamp preview [stage]`
- `,stamp setup` - show what is configured and what is missing
- `,stamp setup upload [start]` - attach the card images in order (prefix only)
- `,stamp setup image <stage>` - set one stage
- `,stamp setup reward <stamp> <text>` - `list` and `clear` also accepted
- `,stamp setup reset`

### Server
- `,welcome` / `,goodbye` - join and leave messages
- `,scrim` - scrim thread generator
- `,prefix` - view or change the prefix
- `,timezone` - view or change the server timezone
- `,help` - command menu

## Setup

Put a `DISCORD_TOKEN` in `beibot/.env`. Runtime state lives in `beibot/data/`,
which is gitignored so a redeploy never overwrites it.

## Slash commands

Invite the bot with both the `bot` and `applications.commands` scopes, or
the slash commands cannot be registered.

Syncing is manual on purpose, since a global sync is rate limited and this
host restarts often. The owner runs it once after a deploy that changed a
command name, description or argument.

- `,sync` - register globally, can take up to an hour to appear
- `,sync here` - register to this server only, appears immediately
- `,sync list` - show what is currently in the tree
- `,sync clear` / `,sync clearhere` - unregister

Two things stay prefix only. `,purge` accepts its arguments in any order,
which a slash command cannot express, so `/purge` takes the same three as
named options instead. `,stamp setup upload` reads image attachments off
your message, so use `/stamp setup image` to set stages one at a time.

From the repo root, `python run.py` supervises every bot folder as its own
process and restarts them with backoff on crash.

## Intents

Message Content, Server Members and Presence Intent all need enabling in the
Discord developer portal. Presence is used only by the vanity role feature.