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

from fakes import SELF_ID, FakeUpstream, RecordingEvents, channel, channel_message, registry_with, updates_for, user


async def start_gateway(**gateway_kwargs):
    upstream = FakeUpstream()
    registry = registry_with("barabot", "lofi")
    recorded = RecordingEvents()
    gateway = Gateway(upstream, registry, recorded, **gateway_kwargs)
    socket_path = Path(tempfile.mkdtemp(dir="/tmp")) / "gw.sock"  # macOS limits unix socket paths to 104 chars
    server = await gateway.serve(socket_path)
    return upstream, registry, recorded, gateway, server, socket_path


async def wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def test_shared_client_full_flow():
    async def scenario():
        upstream, registry, recorded, gateway, server, sock = await start_gateway()
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


def test_unknown_integration_is_rejected_without_auto_register():
    async def scenario():
        *_, server, sock = await start_gateway(auto_register=False)
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


def test_unknown_integration_registers_itself_on_connect():
    async def scenario():
        _, registry, recorded, gateway, server, sock = await start_gateway()
        client = SharedClient("newbie", socket_path=sock)
        try:
            await client.connect()
            assert registry.has_integration("newbie")
            assert gateway.connected() == ["newbie"]
            assert "registered:newbie" in recorded.keys()
            await client.disconnect()
            await client.connect()  # second time: already registered, no new report
            assert recorded.keys().count("registered:newbie") == 1
        finally:
            await client.disconnect()
            await gateway.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_invalid_integration_name_is_still_rejected():
    async def scenario():
        _, registry, _, gateway, server, sock = await start_gateway()
        client = SharedClient("Bad Name", socket_path=sock)
        try:
            with pytest.raises(GatewayRejectedError):
                await client.connect()
            assert not registry.has_integration("Bad Name")
        finally:
            await gateway.close()
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
        upstream, registry, recorded, gateway, server, sock = await start_gateway()
        registry.claim(channel_id(5), "barabot", "join")
        upstream.on(functions.messages.SendMessageRequest, lambda r: RuntimeError("boom"))
        client = SharedClient("barabot", socket_path=sock)
        try:
            await client.connect()
            with pytest.raises(errors.BadRequestError) as info:
                await client.send_message(types.InputPeerChannel(5, 50), "hi")
            assert info.value.message == "TGMUX_INTERNAL"
            assert len(upstream.called(functions.messages.SendMessageRequest)) == 1  # not retried
            assert recorded.keys() == ["call-error:SendMessageRequest:RuntimeError"]
            assert "boom" in recorded.reports[0][1]
        finally:
            await client.disconnect()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_updates_wait_for_an_offline_integration():
    """Covers integration restarts and the catch_up burst right after a gateway restart."""
    async def scenario():
        upstream, registry, recorded, gateway, server, sock = await start_gateway()
        registry.claim(channel_id(5), "barabot", "join")
        for i, text in enumerate(["first", "second", "third"], start=1):
            await upstream.push(
                types.UpdateNewChannelMessage(channel_message(5, text, msg_id=i), i, 1), [channel(5)]
            )
        assert gateway.backlog_size("barabot") == 3

        got = []
        client = SharedClient("barabot", socket_path=sock, sequential_updates=True)
        client.add_event_handler(lambda e: got.append(e.raw_text), events.NewMessage)
        try:
            await client.connect()
            await wait_for(lambda: len(got) == 3)
            assert got == ["first", "second", "third"]
            assert gateway.backlog_size("barabot") == 0
            await upstream.push(types.UpdateNewChannelMessage(channel_message(5, "live", msg_id=4), 4, 1), [channel(5)])
            await wait_for(lambda: got[-1:] == ["live"])
            assert recorded.keys() == []
        finally:
            await client.disconnect()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_ordered_batch_runs_one_after_another():
    async def scenario():
        upstream, registry, _, gateway, server, sock = await start_gateway()
        registry.claim(channel_id(5), "barabot", "join")
        started, finished = [], []

        async def slow_first(request):
            started.append(request.message)
            await asyncio.sleep(0.2 if request.message == "first" else 0)
            finished.append(request.message)
            if request.message == "second":
                return errors.ChatWriteForbiddenError(request)
            return updates_for([])
        upstream.on(functions.messages.SendMessageRequest, slow_first)

        peer = types.InputPeerChannel(5, 50)
        batch = lambda: [functions.messages.SendMessageRequest(peer, text, random_id=i)
                         for i, text in enumerate(["first", "second", "third"], start=1)]
        client = SharedClient("barabot", socket_path=sock)
        try:
            await client.connect()

            # ordered: strictly sequential; an error in the middle does not stop the rest
            with pytest.raises(errors.MultiError) as info:
                await client(batch(), ordered=True)
            assert started == finished == ["first", "second", "third"]
            assert isinstance(info.value.exceptions[1], errors.ChatWriteForbiddenError)
            assert info.value.exceptions[0] is None and info.value.exceptions[2] is None

            # unordered: all start at once, the slow one finishes last
            started.clear(); finished.clear()
            with pytest.raises(errors.MultiError):
                await client(batch())
            assert finished[-1] == "first" and len(started) == 3
        finally:
            await client.disconnect()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_socket_disconnect_is_noticed_and_reported():
    async def scenario():
        now = [0.0]
        upstream, registry, recorded, gateway, server, sock = await start_gateway(clock=lambda: now[0])
        client = SharedClient("barabot", socket_path=sock)
        try:
            await client.connect()
            await wait_for(lambda: "barabot" in gateway.connected())
            now[0] = 100
            gateway.sweep()
            assert "offline:barabot" not in recorded.keys()   # connected
            assert "offline:lofi" in recorded.keys()          # never came
            await client.disconnect()
            await wait_for(lambda: "barabot" not in gateway.connected())
            now[0] = 200
            gateway.sweep()
            assert "offline:barabot" in recorded.keys()
        finally:
            await client.disconnect()
            await gateway.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
