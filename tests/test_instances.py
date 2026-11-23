"""Several gateways = several Telegram accounts on one machine."""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from telethon import events
from telethon.tl import types

from tgmux import SharedClient, cli, config
from tgmux.client import resolve_socket
from tgmux.gateway import Gateway
from tgmux.peers import channel_id

from fakes import FakeUpstream, RecordingReporter, channel, channel_message, registry_with


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("TGMUX_CONFIG_ROOT", str(tmp_path / "conf"))
    monkeypatch.setenv("TGMUX_STATE_ROOT", str(tmp_path / "st"))
    monkeypatch.delenv("TGMUX_INSTANCE", raising=False)
    monkeypatch.delenv("TGMUX_SOCKET", raising=False)
    monkeypatch.delenv("TGMUX_CONFIG", raising=False)
    return tmp_path


def test_each_instance_gets_its_own_paths(roots):
    main, second = config.load(), config.load(instance="second")
    assert main.instance == "default" and main.path == roots / "conf/default/config.toml"
    assert second.db == roots / "st/second/tgmux.db"
    assert second.socket == roots / "st/second/gateway.sock"
    assert len({main.db, second.db, main.socket, second.socket, main.lock_file, second.lock_file}) == 6


def test_instance_name_from_env_and_validation(roots, monkeypatch):
    monkeypatch.setenv("TGMUX_INSTANCE", "work")
    assert config.load().instance == "work"
    with pytest.raises(config.ConfigError):
        config.instance_name("../etc")


def test_client_socket_resolution(roots, monkeypatch):
    assert resolve_socket() == roots / "st/default/gateway.sock"
    assert resolve_socket("second") == roots / "st/second/gateway.sock"
    monkeypatch.setenv("TGMUX_SOCKET", "/tmp/custom.sock")
    assert resolve_socket() == config.Path("/tmp/custom.sock")
    assert resolve_socket("second") == roots / "st/second/gateway.sock"  # explicit instance wins
    assert resolve_socket("second", "/x.sock") == config.Path("/x.sock")


def test_cli_commands_are_per_instance(roots, capsys):
    cli.main(["integration", "add", "barabot"])
    cli.main(["--instance", "second", "integration", "add", "lofi"])
    capsys.readouterr()
    cli.main(["integration", "list"])
    assert capsys.readouterr().out.split() == ["barabot", "0", "chats"]
    cli.main(["--instance", "second", "integration", "list"])
    assert capsys.readouterr().out.split() == ["lofi", "0", "chats"]


def test_instances_lists_and_shows_running(roots, capsys):
    for name in ("default", "second"):
        cli.main(["--instance", name, "telegram", "set", "--session", __file__, "--credentials", __file__])
    held = cli.hold_lock(config.load(instance="second").lock_file, "instance second (pid 42)")
    try:
        capsys.readouterr()
        cli.main(["instances"])
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].startswith("default\tstopped")
        assert lines[1].startswith("second\trunning (instance second (pid 42))")
    finally:
        held.close()


def test_locks_name_their_holder(tmp_path):
    lock = tmp_path / "x.lock"
    held = cli.hold_lock(lock, "instance main (pid 1)")
    try:
        with pytest.raises(cli.LockBusy) as info:
            cli.hold_lock(lock, "instance second (pid 2)")
        assert info.value.holder == "instance main (pid 1)"
    finally:
        held.close()
    cli.hold_lock(lock, "instance second (pid 2)").close()  # free again


def test_same_session_or_account_cannot_be_served_twice(roots, tmp_path):
    session = tmp_path / "acc.session"
    session.write_text("")
    creds = tmp_path / "acc.env"
    creds.write_text("API_ID=1\nAPI_HASH=abc\n")
    held = cli.hold_lock(session.with_name(session.name + ".tgmux.lock"), "instance main (pid 1)")
    try:
        cli.main(["--instance", "second", "telegram", "set", "--session", str(session), "--credentials", str(creds)])
        with pytest.raises(SystemExit, match="already served by instance main"):
            asyncio.run(cli._serve(config.load(instance="second")))
    finally:
        held.close()


def test_one_integration_in_two_instances_at_once():
    """Same integration name, two accounts: separate locks, registries and updates."""
    async def start(name, self_id):
        upstream = FakeUpstream()
        upstream.self_id = self_id
        registry = registry_with("barabot")
        gateway = Gateway(upstream, registry, RecordingReporter())
        sock = Path(tempfile.mkdtemp(dir="/tmp")) / name / "gateway.sock"  # macOS: 104-char socket paths
        return upstream, registry, gateway, await gateway.serve(sock), sock

    async def scenario():
        up_a, reg_a, gw_a, srv_a, sock_a = await start("a", 101)
        up_b, reg_b, gw_b, srv_b, sock_b = await start("b", 202)
        reg_a.claim(channel_id(5), "barabot", "join")
        reg_b.claim(channel_id(5), "barabot", "join")
        got = {"a": [], "b": []}
        a = SharedClient("barabot", socket_path=sock_a)
        b = SharedClient("barabot", socket_path=sock_b)
        a.add_event_handler(lambda e: got["a"].append(e.raw_text), events.NewMessage)
        b.add_event_handler(lambda e: got["b"].append(e.raw_text), events.NewMessage)
        try:
            await a.connect()
            await b.connect()
            assert (await a.get_me()).id == 101 and (await b.get_me()).id == 202
            await up_a.push(types.UpdateNewChannelMessage(channel_message(5, "from A"), 1, 1), [channel(5)])
            await up_b.push(types.UpdateNewChannelMessage(channel_message(5, "from B"), 1, 1), [channel(5)])
            for _ in range(200):
                if got["a"] and got["b"]:
                    break
                await asyncio.sleep(0.01)
            assert got == {"a": ["from A"], "b": ["from B"]}
        finally:
            for client in (a, b):
                await client.disconnect()
            for gw, srv in ((gw_a, srv_a), (gw_b, srv_b)):
                await gw.close()
                srv.close()
                await srv.wait_closed()

    asyncio.run(scenario())


def test_reports_carry_the_instance_name(monkeypatch):
    from tgmux import reporter as reporter_mod

    sent = []
    monkeypatch.setattr(reporter_mod, "send_bot_message", lambda token, chat, text: sent.append(text))

    async def scenario():
        bot = reporter_mod.BotReporter("t", 1, prefix="tgmux[second]")
        bot.start()
        bot.report("k", "hello", window=0)
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.01)
        await bot.stop()

    asyncio.run(scenario())
    assert sent == ["tgmux[second]: hello"]


def test_instances_without_a_tty(tmp_path):
    env = {**os.environ, "TGMUX_CONFIG_ROOT": str(tmp_path / "c"), "TGMUX_STATE_ROOT": str(tmp_path / "s")}
    env.pop("TGMUX_INSTANCE", None)
    for step in (["--instance", "a", "integration", "add", "x"],
                 ["--instance", "a", "telegram", "set", "--session", __file__, "--credentials", __file__],
                 ["instances"]):
        result = subprocess.run([sys.executable, "-m", "tgmux", *step], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, (step, result.stderr)
    names = [line.split("\t")[:2] for line in result.stdout.splitlines()]
    assert names == [["default", "stopped"], ["a", "stopped"]]  # default is always listed, first
