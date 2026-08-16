# beibot

A discord.py bot for community moderation, tickets and Roblox lookups.

Default prefix: `,` (changeable per server with `,prefix set`)

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

### Server
- `,welcome` / `,goodbye` - join and leave messages
- `,scrim` - scrim thread generator
- `,prefix` - view or change the prefix
- `,timezone` - view or change the server timezone
- `,help` - command menu

## Setup

Put a `DISCORD_TOKEN` in `beibot/.env`. Runtime state lives in `beibot/data/`,
which is gitignored so a redeploy never overwrites it.

From the repo root, `python run.py` supervises every bot folder as its own
process and restarts them with backoff on crash.

## Intents

Message Content, Server Members and Presence Intent all need enabling in the
Discord developer portal. Presence is used only by the vanity role feature.