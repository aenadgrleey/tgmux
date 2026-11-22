# tgmux

Share one Telegram user (service) account between several services. Every chat
belongs to exactly one integration; an integration sees and touches only its chats.

```
Telegram ◄──► tgmux gateway (the only process holding the session)
                  ▲ unix socket, raw TL
       SharedClient("barabot")   SharedClient("lofi")
```

Integrations use `SharedClient`, a drop-in Telethon `TelegramClient`:

```python
from tgmux import SharedClient

client = SharedClient("barabot")                 # instead of TelegramClient(session, api_id, api_hash)
await client.start()

@client.on(events.NewMessage)                     # only updates from chats barabot owns
async def handler(event): ...

await client(JoinChannelRequest("@flats"))        # free chat -> now owned by barabot
```

One process per integration: `SharedClient` holds an `flock` on
`<socket dir>/locks/<integration>.lock` while connected; a second process gets
`IntegrationInUseError` with the holder's pid. The gateway only accepts names
registered with `tgmux integration add`. There are no tokens: on one machine the
security boundary is the socket's 0600 permissions (same OS user).

Only Telethon's transport (`_sender`) is replaced; `_call`, retries, flood
waits, events and the entity cache are stock Telethon. Telethon is pinned
because these are internals; `tests/test_end_to_end.py` guards them.

## Ownership rules

| What the integration does | Result |
|---|---|
| `channels.JoinChannel` / `messages.ImportChatInvite` into a free chat | the chat becomes its own |
| the same into a chat owned by someone else | `ChatOwnedByOtherError`, nothing is sent to Telegram |
| first `SendMessage`/`SendMedia`/`SendMultiMedia`/`ForwardMessages` into a free private chat | the private chat becomes its own |
| `channels.LeaveChannel` / leaving a basic group | leaves and frees the chat |
| any request touching a chat it does not own | `ChatNotOwnedError` |
| a request without a chat that is not in `policy.GLOBAL_ALLOW` | `RequestNotAllowedError` |
| `iter_dialogs()` | only owned chats |

All gateway errors subclass Telethon's `ForbiddenError`. Admins can override
ownership with `tgmux grant` / `tgmux release`.

Updates are delivered only to the owner of their chat, with entities trimmed to
the ones the update mentions. Group calls are mapped to chats from
`GetFullChannel`/`GetFullChat` results and `UpdateGroupCall`, so pytgcalls can
run inside an integration on top of `SharedClient`.

## Service bot

Things tgmux drops or does not expect go to a separate BotFather bot (Bot API,
not the shared account): updates without a chat, messages in chats nobody owns
(once a day per chat), messages for an owner that is offline, internal errors,
upstream disconnects. Repeats are folded per key.

## Setup

Everything is non-interactive (flags, files, stdin), so it works from scripts,
CI or systemd provisioning.

```bash
uv venv && uv pip install -e '.[test]'

tgmux telegram set --session ~/Dev/barabot/config/scraper.session \
                   --credentials ~/Dev/barabot/config/scraper.env
tgmux integration add barabot

# optional service bot; --chat-id is mandatory and is the only recipient
printf '%s' "$BOT_TOKEN" | tgmux reporter set --chat-id 123456789 --token-stdin
#   or: tgmux reporter set --chat-id 123456789 --token-file /path/to/token [--no-verify]

tgmux serve
```

`reporter set` sends a test message first and writes nothing if the bot cannot
reach the recipient (the user must have pressed /start in the bot). Settings
live in `~/.config/tgmux/config.toml` (0600):

```toml
[telegram]
session     = "/Users/me/Dev/barabot/config/scraper.session"
credentials = "/Users/me/Dev/barabot/config/scraper.env"

[reporter]
token_file = "/Users/me/.config/tgmux/reporter.token"
chat_id    = 123456789          # required whenever token_file is set
```

Other commands: `tgmux chats [integration]`, `tgmux grant <chat_id> <integration>`,
`tgmux release <chat_id>`, `tgmux integration list|remove`,
`tgmux reporter show|test|disable`.

## Known limits

- Updates without a chat (e.g. `UpdateDeleteMessages` in private chats and basic groups) are dropped and reported.
- Updates for an offline integration are lost (no replay); `catch_up` is not supported.
- `ordered=True` for batched requests is ignored.
- Invite links that need admin approval: ownership is not assigned when the request is approved later.

## Tests

```bash
.venv/bin/python -m pytest -q
```
