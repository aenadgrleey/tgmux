"""CLI and config: everything must work without a TTY or prompts."""
import io
import os
import stat
import subprocess
import sys
import asyncio

import pytest

from tgmux import cli, config
from tgmux.upstream import TelethonUpstream


@pytest.fixture
def cfg_path(tmp_path):
    return tmp_path / "conf" / "config.toml"


def run(cfg_path, *argv):
    return cli.main(["--config", str(cfg_path), *argv])


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_whole_cli_runs_without_a_tty(tmp_path):
    """Separate process, stdin closed: any prompt would fail or hang."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'state_dir = "{tmp_path / "state"}"\n')
    token = tmp_path / "bot.token"
    token.write_text("1:x")
    session, creds = tmp_path / "s.session", tmp_path / "s.env"
    session.write_text("")
    creds.write_text("API_ID=1\nAPI_HASH=abc\n")
    steps = [
        ["integration", "add", "barabot"],
        ["integration", "list"],
        ["grant", "-1005", "barabot"],
        ["chats"],
        ["release", "-1005"],
        ["pending"],
        ["pending", "list"],
        ["telegram", "set", "--session", str(session), "--credentials", str(creds)],
    ]
    for step in steps:
        result = subprocess.run(
            [sys.executable, "-m", "tgmux", "--config", str(cfg), *step],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, (step, result.stderr)


def test_unauthorized_session_fails_instead_of_prompting():
    class Client:
        disconnected_called = False

        def add_event_handler(self, handler, event):
            pass

        async def connect(self):
            pass

        async def is_user_authorized(self):
            return False

        async def disconnect(self):
            Client.disconnected_called = True

    with pytest.raises(RuntimeError, match="not authorized"):
        asyncio.run(TelethonUpstream(Client()).start())
    assert Client.disconnected_called


def test_update_handler_is_installed_before_connect():
    """catch_up replays missed updates inside connect(); the handler must already be there."""
    order = []

    class Client:
        def add_event_handler(self, handler, event):
            order.append("handler")

        async def connect(self):
            order.append("connect")

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return type("Me", (), {"id": 1})()

    asyncio.run(TelethonUpstream(Client()).start())
    assert order == ["handler", "connect"]


def test_pending_list_and_cancel(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'state_dir = "{tmp_path / "state"}"\n')
    run(cfg, "integration", "add", "barabot")
    registry = config.load(cfg)
    from tgmux.registry import Registry
    pending = Registry(registry.db).add_pending("barabot", chat_id=None, invite_hash="abc", title="Flats")
    capsys.readouterr()
    run(cfg, "pending")
    assert f"#{pending.id}\tbarabot\t-\tabc\tFlats" in capsys.readouterr().out
    run(cfg, "pending", "cancel", str(pending.id))
    with pytest.raises(SystemExit, match="no pending"):
        run(cfg, "pending", "cancel", str(pending.id))


def test_waiting_for_disconnect_accepts_telethon_future():
    """Telethon exposes `disconnected` as a shielded Future; the fake used to return a coroutine."""
    from telethon import TelegramClient
    from telethon.sessions import MemorySession

    async def scenario():
        client = TelegramClient(MemorySession(), 1, "0" * 32)
        assert asyncio.isfuture(client.disconnected)  # the real thing, not a coroutine
        waiter = cli.when_disconnected(client)
        await asyncio.wait_for(waiter, 1)  # not connected: resolves at once

        class Coro:
            @property
            def disconnected(self):
                async def done():
                    return None
                return done()

        await asyncio.wait_for(cli.when_disconnected(Coro()), 1)

    asyncio.run(scenario())


