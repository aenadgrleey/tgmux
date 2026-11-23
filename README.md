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

## Several accounts: instances

One instance = one gateway = one Telegram account. Instances are named and do
not share anything (config, registry, socket, locks, reports):

```
~/.config/tgmux/<instance>/config.toml      root: $TGMUX_CONFIG_ROOT
~/.local/state/tgmux/<instance>/            root: $TGMUX_STATE_ROOT
    tgmux.db  gateway.sock  locks/  gateway.lock
```

Every command takes `--instance NAME` (or `$TGMUX_INSTANCE`, default `default`);
anything run without one goes to `default`, which always exists;
`tgmux instances` lists them and shows which are running. Integrations pick
the account the same way:

```python
SharedClient("barabot")                      # $TGMUX_INSTANCE, else "default"
SharedClient("barabot", instance="second")   # another account
```

The same integration name can be connected to several instances at once.
Socket lookup order: `socket_path=` > `instance=` > `$TGMUX_SOCKET` > `$TGMUX_INSTANCE`.

A gateway refuses to start if its instance is already running, if another
instance serves the same session file (Telegram would revoke the auth key), or
if another instance serves the same account through a different session (both
would route every update); the error names the instance holding it. Reports
are prefixed `tgmux[<instance>]`.

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

## Join requests

Chats that need an admin's approval go through one mechanism, whether the
integration used `JoinChannel` or a private invite link:

1. Telegram answers `INVITE_REQUEST_SENT`; the integration gets that error as
   usual and the gateway records a pending request (`chat_id` when known, the
   invite hash, the title). It is kept in SQLite, so restarts do not lose it.
2. While pending, the same chat or link is refused to other integrations.
3. The first update from a chat with no owner is checked against pending
   requests: by `chat_id` (any update), otherwise by title, but only on a
   join signal (`UpdateChannel` for a channel we are now in, or a
   "joined by request" / "added" service message). Exactly one match: the
   chat is assigned and the update delivered to the new owner. Several:
   reported to the service bot, nothing is guessed.
4. Requests not approved in 30 days are dropped with a report.

`tgmux pending` lists them, `tgmux pending cancel <id>` forgets one.

## Service bot

Things tgmux drops or does not expect go to a separate BotFather bot (Bot API,
not the shared account), to exactly one configured recipient:

- an integration is unavailable for more than 30 s (disconnected, or not back
  after a gateway start), with a reminder every hour while it stays away;
- it came back after a reported outage: how many queued updates were
  delivered and how many were lost;
- its queue overflowed (limit 10k) or queued updates expired (24 h): reported
  the moment it starts happening, totals come with the "came back" report;
- updates without a chat, messages in chats nobody owns (once a day per chat),
  internal errors, protocol errors, upstream disconnects.

## Setup

Everything except `tgmux telegram login` is non-interactive (flags, files,
stdin), so it works from scripts, CI or systemd provisioning.

```bash
uv venv && uv pip install -e '.[test]'

tgmux telegram set --session ~/Dev/barabot/config/scraper.session \
                   --credentials ~/Dev/barabot/config/scraper.env
tgmux integration add barabot

# optional service bot; --chat-id is mandatory and is the only recipient
printf '%s' "$BOT_TOKEN" | tgmux reporter set --chat-id 123456789 --token-stdin
#   or: tgmux reporter set --chat-id 123456789 --token-file /path/to/token [--no-verify]

tgmux serve                          # same with --instance NAME for other accounts
```

`reporter set` sends a test message first and writes nothing if the bot cannot
reach the recipient (the user must have pressed /start in the bot). Settings
live in `~/.config/tgmux/<instance>/config.toml` (0600):

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
`tgmux reporter show|test|disable`, `tgmux instances`.

## Deploy: dev on the laptop, prod on the VPS

An instance is a tgmux thing (one gateway = one Telegram account on one
machine), not an environment. Dev and prod are machines; each runs its
gateway in the `default` instance with its **own session** of the same
account (own auth key, logged in separately), like a phone and a desktop:

| | where | how it runs |
|---|---|---|
| dev | laptop | `tgmux serve` by hand |
| prod | VPS | systemd `tgmux.service`, code in `/opt/tgmux` |

Never copy a session file between machines: two live copies of one auth key
make Telegram revoke it. Both gateways see every update of the account, so
chats the dev Barabot joins show up in prod as "nobody owns this chat"
reports (once a day per chat).

First install on the VPS (as root):

```bash
git clone https://github.com/aenadgrleey/tgmux.git /opt/tgmux
python3 -m venv /opt/tgmux/.venv && /opt/tgmux/.venv/bin/pip install -e /opt/tgmux
ln -s /opt/tgmux/.venv/bin/tgmux /usr/local/bin/tgmux
ln -s /opt/tgmux/deploy/prod.sh /usr/local/bin/tgmux-deploy

tgmux telegram login                 # asks API_ID/API_HASH, phone, code, 2FA
tgmux integration add barabot
tgmux reporter set --chat-id 123456789 --token-file /root/.config/tgmux/default/reporter.token
tgmux serve --check                  # offline: config, session, credentials, registry

cp /opt/tgmux/deploy/tgmux.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now tgmux
journalctl -u tgmux -f
```

Updates, on the server (`ssh` in first):

```bash
tgmux-deploy                 # the latest origin/master
tgmux-deploy v0.1.1          # a tag or commit instead
tgmux-deploy --dry-run       # what it would do
```

`tgmux-deploy` is `/opt/tgmux/deploy/prod.sh`. It fetches from GitHub, checks
out the ref, reinstalls, runs `serve --check`, restarts and requires the unit
to stay up for 10 s; otherwise it restores the previous commit and restarts
that. If the ref is already deployed and running, it does nothing. The server
pulls with a read-only deploy key (`Host github-tgmux` in `/root/.ssh/config`).

Integrations reach the gateway at `/root/.local/state/tgmux/default/gateway.sock`
(no `TGMUX_INSTANCE` needed). In their systemd unit that path must be writable
(`ProtectHome=read-only` + `ReadWritePaths=/root/.local/state/tgmux/default`;
`ProtectHome=true` hides it). `SharedClient` waits for the gateway forever
with backoff up to 30 s, so integrations survive gateway restarts and may
start before it; pass `connection_retries=N` to give up instead.

## Known limits

- Updates without a chat (e.g. `UpdateDeleteMessages` in private chats and basic groups) are dropped and reported.
- Delivery is at-least-once and in memory only:
  - integration offline (restart, deploy, crash): its updates wait in the gateway
    backlog (10k per integration, 24 h) and are delivered in order on reconnect;
  - gateway restart: the gateway runs Telethon with `catch_up=True`, so missed
    updates are fetched with `getDifference` and land in the backlog. The update
    state is saved about once a minute, so after a crash some updates can come
    twice; after a very long downtime Telegram may cut the difference short;
  - `SharedClient` itself has no `catch_up`; it relies on the gateway backlog.

## Tests

```bash
.venv/bin/python -m pytest -q
```
