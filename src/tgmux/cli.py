"""tgmux command line: run the gateway and manage integrations / chat ownership."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import signal
import sys
from pathlib import Path

from tgmux import config as config_mod
from tgmux.registry import Registry, UnknownIntegrationError

log = logging.getLogger("tgmux")


def _registry(cfg: config_mod.Config) -> Registry:
    return Registry(cfg.db)


# --- serve -------------------------------------------------------------------

class LockBusy(RuntimeError):
    def __init__(self, path: Path, holder: str) -> None:
        super().__init__(f"{path} is held by {holder}")
        self.path, self.holder = path, holder


def hold_lock(path: Path, label: str):
    """Take an flock and write ``label`` into the file; raise LockBusy with the holder's label."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = handle.read().strip() or "?"
        handle.close()
        raise LockBusy(path, holder) from None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{label}\n")
    handle.flush()
    return handle


def _label(cfg) -> str:
    return f"instance {cfg.instance} (pid {os.getpid()})"


async def _serve(cfg: config_mod.Config) -> int:
    from telethon import TelegramClient

    from tgmux.gateway import Gateway
    from tgmux.reporter import BotReporter, LogReporter
    from tgmux.upstream import TelethonUpstream

    if cfg.session is None or not cfg.session.exists():
        raise SystemExit(f"Telegram session not found ({cfg.session}); run `tgmux telegram set --session ... --credentials ...`")
    api_id, api_hash = config_mod.api_credentials(cfg)

    # Two gateways on one session file would get the auth key revoked by Telegram.
    try:
        session_lock = hold_lock(cfg.session.with_name(cfg.session.name + ".tgmux.lock"), _label(cfg))
    except LockBusy as busy:
        raise SystemExit(f"session {cfg.session} is already served by {busy.holder}")

    registry = _registry(cfg)
    reporter = LogReporter()
    if cfg.reporter is not None:
        reporter = BotReporter(cfg.reporter.token(), cfg.reporter.chat_id, prefix=f"tgmux[{cfg.instance}]")
        reporter.start()

    # catch_up: after a gateway restart Telethon replays what was missed (getDifference)
    # from the update state saved in the session; the gateway backlog holds it until
    # integrations reconnect.
    upstream = TelethonUpstream(TelegramClient(str(cfg.session), api_id, api_hash, catch_up=True))
    gateway = Gateway(upstream, registry, reporter)  # installs the update handler before start()
    await upstream.start()
    # Two gateways on one account (different session files) would both route every update.
    try:
        account_lock = hold_lock(config_mod.account_lock(upstream.self_id), _label(cfg))
    except LockBusy as busy:
        await upstream.stop()
        raise SystemExit(f"account {upstream.self_id} is already served by {busy.holder}")
    server = await gateway.serve(cfg.socket)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    stop_wait = loop.create_task(stop.wait())
    upstream_lost = loop.create_task(upstream.client.disconnected)
    reporter.report("lifecycle", "шлюз запущен", window=0)
    await asyncio.wait({stop_wait, upstream_lost}, return_when=asyncio.FIRST_COMPLETED)
    exit_code = 0
    if upstream_lost.done() and not stop.is_set():
        reporter.report("upstream-lost", "соединение с Telegram потеряно, шлюз завершается", window=0)
        exit_code = 1
    stop_wait.cancel()
    upstream_lost.cancel()

    server.close()
    await gateway.close()  # drops integration connections, otherwise wait_closed() hangs
    await server.wait_closed()
    await upstream.stop()
    account_lock.close()
    session_lock.close()
    if isinstance(reporter, BotReporter):
        await asyncio.sleep(1)  # let the last report go out
        await reporter.stop()
    return exit_code


def _session_has_auth_key(path: Path) -> bool:
    """Offline: an authorized Telethon session stores a non-empty auth key."""
    import sqlite3

    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = db.execute("SELECT auth_key FROM sessions LIMIT 1").fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def check(cfg: config_mod.Config) -> list[str]:
    """Everything `serve` needs, without connecting to Telegram.

    Safe to run while the gateway is up (deploy scripts do): a second live
    connection with the same auth key is exactly what must never happen.
    """
    problems = []
    if cfg.session is None:
        problems.append("[telegram].session is not set; run `tgmux telegram login`")
    elif not cfg.session.exists():
        problems.append(f"session file not found: {cfg.session}")
    elif not _session_has_auth_key(cfg.session):
        problems.append(f"session has no auth key (not logged in): {cfg.session}")
    try:
        config_mod.api_credentials(cfg)
    except (config_mod.ConfigError, OSError, ValueError) as exc:
        problems.append(f"API credentials: {exc}")
    if cfg.reporter is not None:
        try:
            cfg.reporter.token()
        except (config_mod.ConfigError, OSError) as exc:
            problems.append(f"reporter: {exc}")
    try:
        registry = _registry(cfg)
        if not registry.integrations():
            problems.append("no integrations registered; run `tgmux integration add NAME`")
    except Exception as exc:  # unreadable or corrupt DB
        problems.append(f"registry {cfg.db}: {exc}")
    for directory in {cfg.state_dir, cfg.socket.parent}:
        if directory.exists() and not os.access(directory, os.W_OK):
            problems.append(f"not writable: {directory}")
    return problems


def cmd_serve(args) -> int:
    cfg = _load(args)
    if args.check:
        problems = check(cfg)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"instance {cfg.instance}: ok ({cfg.path})")
        return 0
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s %(levelname)s [{cfg.instance}] %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        instance_lock = hold_lock(cfg.lock_file, _label(cfg))
    except LockBusy as busy:
        raise SystemExit(f"instance {cfg.instance} is already running: {busy.holder}")
    try:
        return asyncio.run(_serve(cfg))
    finally:
        instance_lock.close()


def cmd_instances(args) -> int:
    for name in config_mod.instances():
        cfg = config_mod.load(None, name)
        running = "stopped"
        try:
            hold_lock(cfg.lock_file, "probe").close()
        except LockBusy as busy:
            running = f"running ({busy.holder})"
        print(f"{name}\t{running}\t{cfg.socket}")
    return 0


# --- integrations ------------------------------------------------------------

def cmd_integration_add(args) -> int:
    cfg = _load(args)
    try:
        _registry(cfg).add_integration(args.name)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print(f"integration {args.name} created")
    return 0


def cmd_integration_remove(args) -> int:
    cfg = _load(args)
    if not _registry(cfg).remove_integration(args.name):
        raise SystemExit(f"unknown integration {args.name}")
    print(f"integration {args.name} removed; its chats are free")
    return 0


def cmd_integration_list(args) -> int:
    cfg = _load(args)
    registry = _registry(cfg)
    for name in registry.integrations():
        print(f"{name}\t{len(registry.owned_by(name))} chats")
    return 0


# --- chats -------------------------------------------------------------------

def cmd_chats(args) -> int:
    cfg = _load(args)
    for binding in _registry(cfg).bindings():
        if args.integration and binding.integration != args.integration:
            continue
        print(f"{binding.chat_id}\t{binding.integration}\t{binding.source}")
    return 0


def cmd_grant(args) -> int:
    cfg = _load(args)
    try:
        previous = _registry(cfg).assign(args.chat_id, args.integration)
    except UnknownIntegrationError:
        raise SystemExit(f"unknown integration {args.integration}")
    suffix = f" (was {previous})" if previous and previous != args.integration else ""
    print(f"chat {args.chat_id} -> {args.integration}{suffix}")
    return 0


def cmd_release(args) -> int:
    cfg = _load(args)
    if not _registry(cfg).release(args.chat_id):
        raise SystemExit(f"chat {args.chat_id} has no owner")
    print(f"chat {args.chat_id} released")
    return 0


# --- pending join requests ---------------------------------------------------

def cmd_pending(args) -> int:
    import time

    registry = _registry(_load(args))
    if args.action == "cancel":
        if not registry.remove_pending(args.id):
            raise SystemExit(f"no pending join request #{args.id}")
        print(f"join request #{args.id} cancelled")
        return 0
    now = int(time.time())
    for p in registry.pendings():
        age_h = (now - p.created_at) // 3600
        print(f"#{p.id}\t{p.integration}\t{p.chat_id or '-'}\t{p.invite_hash or '-'}\t{p.title}\t{age_h}h")
    return 0


# --- telegram / reporter settings (non-interactive) -------------------------

def cmd_telegram_set(args) -> int:
    for path in (args.session, args.credentials):
        if not path.expanduser().exists():
            raise SystemExit(f"not found: {path}")
    path = config_mod.config_path(args.config, args.instance)
    config_mod.update(path, "telegram", {
        "session": str(args.session.expanduser().resolve()),
        "credentials": str(args.credentials.expanduser().resolve()),
    }, args.instance)
    print(f"[telegram] written to {path}")
    return 0


def cmd_telegram_login(args) -> int:
    """Interactive: phone, code from Telegram, 2FA password. Run it over ssh on the gateway host."""
    import getpass

    cfg = _load(args)
    if not sys.stdin.isatty():
        raise SystemExit("`tgmux telegram login` is interactive; run it in a terminal "
                         "(or use `tgmux telegram set` with an existing session)")
    try:
        instance_lock = hold_lock(cfg.lock_file, _label(cfg))
    except LockBusy as busy:
        raise SystemExit(f"instance {cfg.instance} is running ({busy.holder}); stop it before logging in")

    path = config_mod.config_path(args.config, args.instance)
    credentials = args.credentials.expanduser().resolve() if args.credentials else cfg.credentials
    if credentials is None:
        credentials = path.parent / "telegram.env"
        print(f"API credentials from https://my.telegram.org (saved to {credentials}, mode 0600)")
        api_id = input("API_ID: ").strip()
        api_hash = getpass.getpass("API_HASH (hidden): ").strip()
        if not api_id.isdigit() or not api_hash:
            raise SystemExit("API_ID must be a number and API_HASH must not be empty")
        config_mod.write_private(credentials, f"API_ID={api_id}\nAPI_HASH={api_hash}\n")
    elif not credentials.exists():
        raise SystemExit(f"not found: {credentials}")
    session = (args.session.expanduser().resolve() if args.session
               else cfg.session or cfg.state_dir / "telegram.session")
    session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Validate credentials and write [telegram] before talking to Telegram.
    config_mod.update(path, "telegram", {"session": str(session), "credentials": str(credentials)}, args.instance)
    api_id, api_hash = config_mod.api_credentials(_load(args))

    async def login() -> str:
        from telethon import TelegramClient

        client = TelegramClient(str(session), api_id, api_hash)
        try:
            # Telethon prompts for the phone, the code and (if set) the 2FA password.
            await client.start()
            me = await client.get_me()
            return f"{me.first_name or ''} (id {me.id})".strip()
        finally:
            await client.disconnect()

    try:
        who = asyncio.run(login())
    finally:
        instance_lock.close()
    if session.exists():
        os.chmod(session, 0o600)
    print(f"logged in as {who}; session {session}; [telegram] written to {path}")
    print(f"next: tgmux --instance {cfg.instance} serve --check")
    return 0


def _read_token(args) -> tuple[str, Path | None]:
    """Return (token, file to save it to or None if it already lives in a file)."""
    if args.token_file is not None:
        token = args.token_file.expanduser().read_text().strip()
        return token, None
    token = sys.stdin.read().strip()
    return token, config_mod.config_path(args.config, args.instance).parent / "reporter.token"


def cmd_reporter_set(args) -> int:
    from tgmux.reporter import BotApiError, send_bot_message

    try:
        token, save_to = _read_token(args)
    except FileNotFoundError as exc:
        raise SystemExit(f"token file not found: {exc.filename}")
    if not token:
        raise SystemExit("empty bot token")
    if not args.no_verify:
        # Nothing is written unless the bot can actually reach the recipient.
        try:
            send_bot_message(token, args.chat_id, "tgmux: отчёты будут приходить сюда")
        except BotApiError as exc:
            raise SystemExit(f"the bot cannot write to {args.chat_id}: {exc} (press /start in the bot first)")
        except OSError as exc:
            raise SystemExit(f"cannot reach the Bot API: {exc} (use --no-verify to skip)")
    if save_to is not None:
        config_mod.write_private(save_to, token + "\n")
        token_file = save_to
    else:
        token_file = args.token_file.expanduser().resolve()
    path = config_mod.config_path(args.config, args.instance)
    config_mod.update(path, "reporter", {"token_file": str(token_file), "chat_id": args.chat_id}, args.instance)
    print(f"reports go to {args.chat_id}; [reporter] written to {path}")
    return 0


def cmd_reporter_disable(args) -> int:
    path = config_mod.config_path(args.config, args.instance)
    config_mod.update(path, "reporter", None, args.instance)
    print(f"[reporter] removed from {path}; reports go to the log only")
    return 0


def cmd_reporter_show(args) -> int:
    cfg = _load(args)
    if cfg.reporter is None:
        print("not configured")
    else:
        print(f"chat_id\t{cfg.reporter.chat_id}\ntoken_file\t{cfg.reporter.token_file}")
    return 0


def cmd_reporter_test(args) -> int:
    from tgmux.reporter import BotApiError, send_bot_message

    cfg = _load(args)
    if cfg.reporter is None:
        raise SystemExit("reporter is not configured; run `tgmux reporter set`")
    try:
        send_bot_message(cfg.reporter.token(), cfg.reporter.chat_id, "tgmux: тестовый отчёт")
    except BotApiError as exc:
        raise SystemExit(f"Bot API error: {exc}")
    print("sent")
    return 0


# --- entry point -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tgmux", description=__doc__)
    parser.add_argument("--instance", help="gateway instance = one Telegram account (default: $TGMUX_INSTANCE or main)")
    parser.add_argument("--config", help="path to config.toml (default: ~/.config/tgmux/<instance>/config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--check", action="store_true",
                       help="validate config, session and credentials offline, then exit (safe while running)")
    serve.set_defaults(func=cmd_serve)
    sub.add_parser("instances", help="list configured instances").set_defaults(func=cmd_instances)

    integration = sub.add_parser("integration", help="manage integrations").add_subparsers(dest="action", required=True)
    for action, func, helptext in (
        ("add", cmd_integration_add, "register an integration name"),
        ("remove", cmd_integration_remove, "delete an integration and free its chats"),
    ):
        p = integration.add_parser(action, help=helptext)
        p.add_argument("name")
        p.set_defaults(func=func)
    integration.add_parser("list", help="list integrations").set_defaults(func=cmd_integration_list)

    chats = sub.add_parser("chats", help="list chat ownership")
    chats.add_argument("integration", nargs="?")
    chats.set_defaults(func=cmd_chats)

    grant = sub.add_parser("grant", help="give a chat to an integration")
    grant.add_argument("chat_id", type=int, help="marked chat id (-100... for channels)")
    grant.add_argument("integration")
    grant.set_defaults(func=cmd_grant)

    release = sub.add_parser("release", help="remove a chat's owner")
    release.add_argument("chat_id", type=int)
    release.set_defaults(func=cmd_release)

    pending = sub.add_parser("pending", help="join requests waiting for approval")
    pending.set_defaults(func=cmd_pending, action="list")
    pending_sub = pending.add_subparsers(dest="action")
    pending_sub.add_parser("list", help="list pending join requests")
    cancel = pending_sub.add_parser("cancel", help="forget a pending join request")
    cancel.add_argument("id", type=int)

    reporter = sub.add_parser("reporter", help="service bot for reports").add_subparsers(dest="action", required=True)
    rset = reporter.add_parser("set", help="configure the service bot and its only recipient")
    rset.add_argument("--chat-id", type=int, required=True, help="the one peer reports go to (must have pressed /start)")
    token = rset.add_mutually_exclusive_group(required=True)
    token.add_argument("--token-file", type=Path, help="existing file with the BotFather token")
    token.add_argument("--token-stdin", action="store_true",
                       help="read the token from stdin and store it 0600 next to the config")
    rset.add_argument("--no-verify", action="store_true", help="do not send a test message before saving")
    rset.set_defaults(func=cmd_reporter_set)
    reporter.add_parser("disable", help="remove the service bot settings").set_defaults(func=cmd_reporter_disable)
    reporter.add_parser("show", help="print the reporter settings").set_defaults(func=cmd_reporter_show)
    reporter.add_parser("test", help="send a test report").set_defaults(func=cmd_reporter_test)

    telegram = sub.add_parser("telegram", help="Telegram session settings").add_subparsers(dest="action", required=True)
    tset = telegram.add_parser("set", help="point the gateway at an authorized Telethon session")
    tset.add_argument("--session", type=Path, required=True, help="authorized .session file")
    tset.add_argument("--credentials", type=Path, required=True, help="env file with API_ID and API_HASH")
    tset.set_defaults(func=cmd_telegram_set)
    tlogin = telegram.add_parser("login", help="log in interactively (phone, code, 2FA) and save the session")
    tlogin.add_argument("--credentials", type=Path,
                        help="env file with API_ID and API_HASH (default: from config, else asked and saved)")
    tlogin.add_argument("--session", type=Path,
                        help="where to store the session (default: <state>/<instance>/telegram.session)")
    tlogin.set_defaults(func=cmd_telegram_login)
    return parser


def _load(args) -> config_mod.Config:
    return config_mod.load(args.config, args.instance)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except config_mod.ConfigError as exc:
        raise SystemExit(f"config error: {exc}")


if __name__ == "__main__":
    sys.exit(main())
