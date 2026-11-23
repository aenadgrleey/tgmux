"""What deployment relies on: offline `serve --check`, and integrations that wait for the gateway."""
import asyncio
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from telethon.tl import types

from tgmux import SharedClient, cli
from tgmux.gateway import Gateway

from fakes import FakeUpstream, RecordingReporter, registry_with


def _session(path: Path, auth_key: bytes | None) -> Path:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (dc_id INTEGER, server_address TEXT, port INTEGER, auth_key BLOB)")
    db.execute("INSERT INTO sessions VALUES (2, '1.2.3.4', 443, ?)", (auth_key,))
    db.commit()
    db.close()
    return path


def _config(tmp_path, session: Path) -> Path:
    creds = tmp_path / "telegram.env"
    creds.write_text("API_ID=1\nAPI_HASH=abc\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'state_dir = "{tmp_path / "state"}"\n\n[telegram]\nsession = "{session}"\ncredentials = "{creds}"\n'
    )
    return cfg


def test_check_passes_for_a_complete_instance(tmp_path, capsys):
    cfg = _config(tmp_path, _session(tmp_path / "a.session", b"k" * 256))
    assert cli.main(["--config", str(cfg), "integration", "add", "barabot"]) == 0
    assert cli.main(["--config", str(cfg), "serve", "--check"]) == 0
    assert "ok" in capsys.readouterr().out


def test_check_lists_every_problem(tmp_path, capsys):
    cfg = _config(tmp_path, _session(tmp_path / "a.session", None))
    (tmp_path / "telegram.env").write_text("")
    assert cli.main(["--config", str(cfg), "serve", "--check"]) == 1
    err = capsys.readouterr().err
    assert "no auth key" in err
    assert "API credentials" in err
    assert "no integrations" in err


def test_check_without_session(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'state_dir = "{tmp_path / "state"}"\n')
    assert cli.main(["--config", str(cfg), "serve", "--check"]) == 1
    assert "telegram login" in capsys.readouterr().err


def test_login_refuses_without_a_terminal(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'state_dir = "{tmp_path / "state"}"\n')
    result = subprocess.run(
        [sys.executable, "-m", "tgmux", "--config", str(cfg), "telegram", "login"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode != 0
    assert "interactive" in result.stderr


def test_client_waits_for_a_gateway_that_starts_later():
    """Integrations start before the gateway (boot, deploy): connect() must wait, not fail."""
    async def scenario():
        sock = Path(tempfile.mkdtemp(dir="/tmp")) / "gw.sock"
        client = SharedClient("barabot", socket_path=sock, retry_delay=0.05)
        connecting = asyncio.ensure_future(client.connect())
        await asyncio.sleep(0.3)
        assert not connecting.done()
        gateway = Gateway(FakeUpstream(), registry_with("barabot"), RecordingReporter())
        server = await gateway.serve(sock)
        try:
            await asyncio.wait_for(connecting, 3)
            assert client.is_connected()
        finally:
            await client.disconnect()
            await gateway.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_client_survives_a_long_gateway_restart():
    """More than Telethon's default 5 attempts: the client keeps waiting and comes back."""
    async def scenario():
        sock = Path(tempfile.mkdtemp(dir="/tmp")) / "gw.sock"
        registry = registry_with("barabot")
        gateway = Gateway(FakeUpstream(), registry, RecordingReporter())
        server = await gateway.serve(sock)
        client = SharedClient("barabot", socket_path=sock, retry_delay=0.02)
        await client.connect()
        try:
            await gateway.close()
            server.close()
            await server.wait_closed()
            sock.unlink(missing_ok=True)
            await asyncio.sleep(0.5)   # ~10 attempts at the 0.02 s base delay, capped backoff
            assert client.is_connected()  # still trying, not given up
            gateway = Gateway(FakeUpstream(), registry, RecordingReporter())
            server = await gateway.serve(sock)
            deadline = asyncio.get_running_loop().time() + 5
            while "barabot" not in gateway.connected():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.02)
        finally:
            await client.disconnect()
            await gateway.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_explicit_retry_limit_still_gives_up():
    async def scenario():
        sock = Path(tempfile.mkdtemp(dir="/tmp")) / "gw.sock"
        client = SharedClient("barabot", socket_path=sock, connection_retries=2, retry_delay=0.01)
        try:
            await asyncio.wait_for(client.connect(), 3)
        except OSError:
            return
        raise AssertionError("connect() should fail after 2 retries")

    asyncio.run(scenario())
