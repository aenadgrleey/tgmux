"""CLI and config: everything must work without a TTY or prompts."""
import io
import os
import stat
import subprocess
import sys
import asyncio

import pytest

from tgmux import cli, config, reporter as reporter_mod
from tgmux.reporter import BotApiError
from tgmux.upstream import TelethonUpstream


@pytest.fixture
def cfg_path(tmp_path):
    return tmp_path / "conf" / "config.toml"


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(reporter_mod, "send_bot_message", lambda token, chat, text: calls.append((token, chat)))
    return calls


def run(cfg_path, *argv):
    return cli.main(["--config", str(cfg_path), *argv])


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_reporter_requires_chat_id_in_config(cfg_path):
    cfg_path.parent.mkdir()
    cfg_path.write_text('[reporter]\ntoken_file = "/tmp/x"\n')
    with pytest.raises(config.ConfigError, match="chat_id is required"):
        config.load(cfg_path)
    with pytest.raises(SystemExit, match="config error"):
        run(cfg_path, "chats")
    cfg_path.write_text('[reporter]\nchat_id = 1\n')
    with pytest.raises(config.ConfigError, match="token_file is missing"):
        config.load(cfg_path)


def test_reporter_set_requires_chat_id(cfg_path, capsys):
    with pytest.raises(SystemExit) as info:
        run(cfg_path, "reporter", "set", "--token-stdin")
    assert info.value.code == 2
    assert "--chat-id" in capsys.readouterr().err


def test_reporter_set_from_stdin_verifies_and_stores_privately(cfg_path, sent, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("123:abc\n"))
    assert run(cfg_path, "reporter", "set", "--chat-id", "42", "--token-stdin") == 0
    assert sent == [("123:abc", 42)]
    loaded = config.load(cfg_path)
    assert loaded.reporter.chat_id == 42
    assert loaded.reporter.token() == "123:abc"
    assert mode(loaded.reporter.token_file) == 0o600
    assert mode(cfg_path) == 0o600


def test_failed_verification_writes_nothing(cfg_path, monkeypatch):
    def refuse(token, chat, text):
        raise BotApiError("Forbidden: bot can't initiate conversation with a user")
    monkeypatch.setattr(reporter_mod, "send_bot_message", refuse)
    monkeypatch.setattr(sys, "stdin", io.StringIO("123:abc"))
    with pytest.raises(SystemExit, match="cannot write to 42"):
        run(cfg_path, "reporter", "set", "--chat-id", "42", "--token-stdin")
    assert not cfg_path.exists()
    assert not (cfg_path.parent / "reporter.token").exists()


def test_token_file_and_no_verify(cfg_path, tmp_path, sent):
    token_file = tmp_path / "bot.token"
    token_file.write_text("1:x\n")
    assert run(cfg_path, "reporter", "set", "--chat-id", "7", "--token-file", str(token_file), "--no-verify") == 0
    assert sent == []
    assert config.load(cfg_path).reporter.token_file == token_file.resolve()


def test_sections_are_updated_independently(cfg_path, tmp_path, sent, monkeypatch):
    session, creds = tmp_path / "s.session", tmp_path / "s.env"
    session.write_text("")
    creds.write_text("API_ID=1\nAPI_HASH=abc\n")
    run(cfg_path, "telegram", "set", "--session", str(session), "--credentials", str(creds))
    monkeypatch.setattr(sys, "stdin", io.StringIO("1:x"))
    run(cfg_path, "reporter", "set", "--chat-id", "7", "--token-stdin")
    loaded = config.load(cfg_path)
    assert loaded.session == session.resolve() and loaded.reporter.chat_id == 7
    assert config.api_credentials(loaded) == (1, "abc")
    run(cfg_path, "reporter", "disable")
    loaded = config.load(cfg_path)
    assert loaded.reporter is None and loaded.session == session.resolve()


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
        ["reporter", "set", "--chat-id", "7", "--token-file", str(token), "--no-verify"],
        ["reporter", "show"],
        ["reporter", "disable"],
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
