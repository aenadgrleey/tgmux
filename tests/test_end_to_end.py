"""Real SharedClient (stock Telethon on top) <-> gateway over a unix socket.

Guards the Telethon internals tgmux relies on: if a Telethon upgrade changes
_sender/_call/_dispatch_update, these tests are the ones that break.
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from telethon import errors, events
from telethon.tl import functions, types

from tgmux import ChatNotOwnedError, GatewayRejectedError, IntegrationInUseError, SharedClient
from tgmux.gateway import Gateway
from tgmux.peers import channel_id

from fakes import SELF_ID, FakeUpstream, RecordingReporter, channel, channel_message, registry_with, updates_for, user


async def start_gateway():
    upstream = FakeUpstream()
    registry = registry_with("barabot", "lofi")
    reporter = RecordingReporter()
    gateway = Gateway(upstream, registry, reporter)
    socket_path = Path(tempfile.mkdtemp(dir="/tmp")) / "gw.sock"  # macOS limits unix socket paths to 104 chars
    server = await gateway.serve(socket_path)
    return upstream, registry, reporter, gateway, server, socket_path


async def wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def test_shared_client_full_flow():
    async def scenario():
        upstream, registry, reporter, gateway, server, sock = await start_gateway()
        barabot = SharedClient("barabot", socket_path=sock)
        lofi = SharedClient("lofi", socket_path=sock)
        got_barabot, got_lofi = [], []
        barabot.add_event_handler(lambda e: got_barabot.append(e.raw_text), events.NewMessage)
        lofi.add_event_handler(lambda e: got_lofi.append(e.raw_text), events.NewMessage)
        try:
            await barabot.start()
            await lofi.connect()
            assert (await barabot.get_me()).id == SELF_ID
            await wait_for(lambda: gateway.connected() == ["barabot", "lofi"])

            # plain Telethon join -> barabot owns the chat
            upstream.on(functions.channels.JoinChannelRequest, lambda r: updates_for([channel(5)]))
            await barabot(functions.channels.JoinChannelRequest(types.InputChannel(5, 50)))
            assert registry.owner(channel_id(5)) == "barabot"

            # lofi cannot write there; the error is a normal Telethon ForbiddenError
            with pytest.raises(ChatNotOwnedError) as info:
                await lofi.send_message(types.InputPeerChannel(5, 50), "hi")
            assert isinstance(info.value, errors.ForbiddenError)

            # Telegram errors come back with their own class
            upstream.on(functions.messages.SendMessageRequest, lambda r: errors.ChatWriteForbiddenError(r))
            with pytest.raises(errors.ChatWriteForbiddenError):
                await barabot.send_message(types.InputPeerChannel(5, 50), "hi")

            # updates reach the owner's event handlers only
            await upstream.push(
                types.UpdateNewChannelMessage(channel_message(5, "квартира 2к", from_user=1), 1, 1),
                [channel(5), user(1, "seller")],
            )
            await wait_for(lambda: got_barabot == ["квартира 2к"])
            await asyncio.sleep(0.05)
            assert got_lofi == []
        finally:
            await barabot.disconnect()
            await lofi.disconnect()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_entities_from_updates_are_cached_for_replies():
    async def scenario():
        upstream, registry, _, gateway, server, sock = await start_gateway()
        registry.claim(channel_id(5), "barabot", "join")
        client = SharedClient("barabot", socket_path=sock)
        replied = asyncio.Event()

        @client.on(events.NewMessage)
        async def handler(event):
            await event.reply("ok")  # needs the channel's access_hash from the update entities
            replied.set()

        try:
            await client.connect()
            await wait_for(lambda: gateway.connected() == ["barabot"])
            await upstream.push(types.UpdateNewChannelMessage(channel_message(5, "hi"), 1, 1), [channel(5)])
            await asyncio.wait_for(replied.wait(), 2)
            sent = upstream.called(functions.messages.SendMessageRequest)[-1]
            assert sent.peer == types.InputPeerChannel(5, 50)
        finally:
            await client.disconnect()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_unknown_integration_is_rejected():
    async def scenario():
        *_, server, sock = await start_gateway()
        client = SharedClient("ghost", socket_path=sock)
        try:
            with pytest.raises(GatewayRejectedError):
                await client.connect()
            # a refused connect must not leave the lock behind
            assert _lock_is_free(sock.parent / "locks" / "ghost.lock")
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def _lock_is_free(path):
    import fcntl, os
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def test_one_process_per_integration():
    async def scenario():
        *_, gateway, server, sock = await start_gateway()
        first = SharedClient("barabot", socket_path=sock)
        second = SharedClient("barabot", socket_path=sock)
        try:
            await first.connect()
            with pytest.raises(IntegrationInUseError, match=str(os.getpid())):
                await second.connect()
            assert gateway.connected() == ["barabot"]
            await first.disconnect()
            await second.connect()  # free again after disconnect
        finally:
            await first.disconnect()
            await second.disconnect()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_lock_held_by_another_process():
    async def scenario():
        *_, server, sock = await start_gateway()
        holder = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, str(sock.parent / "locks" / "barabot.lock")],
                                  stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "locked"
            with pytest.raises(IntegrationInUseError, match=str(holder.pid)):
                await SharedClient("barabot", socket_path=sock).connect()
            holder.kill()
            holder.wait()
            client = SharedClient("barabot", socket_path=sock)  # a dead holder releases the lock
            await client.connect()
            await client.disconnect()
        finally:
            holder.kill()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


HOLD_LOCK = """
import fcntl, os, sys, time
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0); os.pwrite(fd, f"{os.getpid()}\\n".encode(), 0)
print("locked", flush=True)
time.sleep(60)
"""


def test_internal_failure_reaches_client_and_service_bot():
    async def scenario():
        upstream, registry, reporter, gateway, server, sock = await start_gateway()
        registry.claim(channel_id(5), "barabot", "join")
        upstream.on(functions.messages.SendMessageRequest, lambda r: RuntimeError("boom"))
        client = SharedClient("barabot", socket_path=sock)
        try:
            await client.connect()
            with pytest.raises(errors.BadRequestError) as info:
                await client.send_message(types.InputPeerChannel(5, 50), "hi")
            assert info.value.message == "TGMUX_INTERNAL"
            assert len(upstream.called(functions.messages.SendMessageRequest)) == 1  # not retried
            assert reporter.keys() == ["call-error:SendMessageRequest:RuntimeError"]
            assert "boom" in reporter.reports[0][1]
        finally:
            await client.disconnect()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
