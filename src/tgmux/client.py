"""SharedClient: a Telethon TelegramClient that talks to the tgmux gateway.

Only the transport is replaced (``self._sender``). Everything above it —
``_call`` with retries and flood waits, events, entity cache, iter_* helpers —
is stock Telethon, so integration code does not change.
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import itertools
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from telethon import TelegramClient, utils
from telethon.sessions import MemorySession
from telethon.tl import types
from telethon.tl.tlobject import TLRequest

from tgmux import config as config_mod
from tgmux import protocol
from tgmux.errors import GatewayRejectedError, IntegrationInUseError, ProtocolError
from tgmux.protocol import Frame

log = logging.getLogger(__name__)

def resolve_socket(instance: str | None = None, socket_path: str | Path | None = None) -> Path:
    """socket_path, else the instance's socket (default "default"). Environment variables are not read."""
    if instance is not None and socket_path is not None:
        raise TypeError("pass instance= or socket_path=, not both")
    if socket_path is not None:
        return Path(socket_path)
    return config_mod.default_socket(config_mod.instance_name(instance or config_mod.DEFAULT_INSTANCE))

UpdatesHandler = Callable[[types.Updates], Awaitable[None]]


class IntegrationLock:
    """One process per integration: an flock on ``<socket dir>/locks/<name>.lock``.

    The OS drops the lock when the holder exits, even on a crash.
    """

    def __init__(self, socket_path: str | Path, integration: str) -> None:
        self.path = Path(socket_path).parent / "locks" / f"{integration}.lock"
        self.integration = integration
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        config_mod.private_dir(self.path.parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = os.pread(fd, 32, 0).decode(errors="replace").strip() or "?"
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise IntegrationInUseError(
                    f"integration {self.integration!r} is already used by pid {holder} ({self.path})"
                ) from None
            raise
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor drops the flock
            self._fd = None


class _DcSender:
    """What ``_borrow_exported_sender`` returns: same connection, other DC."""

    def __init__(self, parent: "GatewaySender", dc_id: int) -> None:
        self._parent = parent
        self.dc_id = dc_id

    def send(self, request, ordered: bool = False):
        return self._parent.send(request, ordered, dc_id=self.dc_id)

    def is_connected(self) -> bool:
        return self._parent.is_connected()

    async def disconnect(self) -> None:
        return None


class GatewaySender:
    """Stands in for Telethon's MTProtoSender."""

    def __init__(
        self,
        socket_path: str | Path,
        integration: str,
        *,
        on_updates: UpdatesHandler,
        receive_updates: bool = True,
        auto_reconnect: bool = True,
        retries: int | None = None,
        delay: float = 1,
        max_delay: float = 30,
        timeout: float = 10,
    ) -> None:
        self.socket_path = str(socket_path)
        self.integration = integration
        self._lock = IntegrationLock(socket_path, integration)
        self._on_updates = on_updates
        self._receive_updates = receive_updates
        self._auto_reconnect = auto_reconnect
        # None or negative: retry forever (the gateway may be restarting or not up yet).
        self._retries = 10**9 if retries is None or retries < 0 else retries
        self._delay = max(delay, 0.1)
        self._max_delay = max(max_delay, self._delay)
        self._timeout = timeout

        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[TLRequest, asyncio.Future]] = {}
        self._ordered_tasks: set[asyncio.Task] = set()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._recv_task: asyncio.Task | None = None
        self._dispatch_task: asyncio.Task | None = None
        self._update_queue: asyncio.Queue[types.Updates] | None = None
        self._user_connected = False
        self._disconnected: asyncio.Future | None = None
        self.welcome: dict[str, Any] = {}
        self.auth_key = None  # Telethon reads this attribute in a few places

    # --- MTProtoSender surface used by Telethon ---------------------------

    @property
    def disconnected(self) -> asyncio.Future:
        if self._disconnected is None:
            self._disconnected = asyncio.get_running_loop().create_future()
            self._disconnected.set_result(None)
        return asyncio.shield(self._disconnected)

    def is_connected(self) -> bool:
        return self._user_connected

    def _transport_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    def send(self, request, ordered: bool = False, *, dc_id: int = 0):
        if not self._user_connected:
            raise ConnectionError("Cannot send requests while disconnected")
        if utils.is_list_like(request):
            if ordered:
                return self._send_ordered(list(request), dc_id)
            return [self._enqueue(r, dc_id) for r in request]
        return self._enqueue(request, dc_id)

    def _send_ordered(self, requests: list[TLRequest], dc_id: int) -> list[asyncio.Future]:
        """Like MTProto invokeAfterMsg: send each request only after the previous one
        has an answer (result or error). The gateway runs CALLs concurrently, so the
        ordering has to come from here."""
        loop = asyncio.get_running_loop()
        outer = [loop.create_future() for _ in requests]

        async def run() -> None:
            try:
                for request, out in zip(requests, outer):
                    try:
                        result = await self._enqueue(request, dc_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if not out.done():
                            out.set_exception(exc)
                    else:
                        if not out.done():
                            out.set_result(result)
            except asyncio.CancelledError:
                for out in outer:
                    if not out.done():
                        out.set_exception(ConnectionError("Disconnected from the tgmux gateway"))
                raise

        task = loop.create_task(run())
        self._ordered_tasks.add(task)
        task.add_done_callback(self._ordered_tasks.discard)
        return outer

    async def disconnect(self) -> None:
        self._user_connected = False
        for task in (self._recv_task, self._dispatch_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for task in list(self._ordered_tasks):
            task.cancel()
        self._close_transport(ConnectionError("Disconnected from the tgmux gateway"))
        self._lock.release()
        if self._disconnected is not None and not self._disconnected.done():
            self._disconnected.set_result(None)

    # --- tgmux specifics --------------------------------------------------

    def for_dc(self, dc_id: int) -> _DcSender:
        return _DcSender(self, dc_id)

    async def connect(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        self._lock.acquire()  # before touching the gateway: fail fast on a second process
        try:
            attempt = 0
            while True:
                try:
                    await self._open()
                    break
                except GatewayRejectedError:
                    raise  # unknown integration / version: retrying will not help
                except (OSError, asyncio.TimeoutError, ProtocolError) as exc:
                    # Socket missing or refused: the gateway is starting or restarting.
                    if not self._auto_reconnect or attempt >= self._retries:
                        raise
                    delay = self._backoff(attempt)
                    attempt += 1
                    log.warning("tgmux gateway at %s is not reachable (%s); retrying in %.0f s",
                                self.socket_path, exc, delay)
                    await asyncio.sleep(delay)
        except BaseException:
            self._lock.release()
            raise
        self._disconnected = loop.create_future()
        self._update_queue = asyncio.Queue()
        self._user_connected = True
        self._recv_task = loop.create_task(self._recv_loop())
        self._dispatch_task = loop.create_task(self._dispatch_loop())
        return self.welcome

    async def _open(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.socket_path), self._timeout
        )
        hello = {
            "integration": self.integration,
            "version": protocol.PROTOCOL_VERSION,
            "updates": self._receive_updates,
        }
        writer.write(protocol.encode_frame(Frame.HELLO, json.dumps(hello).encode()))
        await writer.drain()
        kind, payload = await asyncio.wait_for(protocol.read_frame(reader), self._timeout)
        if kind is Frame.FATAL:
            writer.close()
            raise GatewayRejectedError(payload.decode(errors="replace"))
        if kind is not Frame.WELCOME:
            writer.close()
            raise ProtocolError(f"expected WELCOME, got {kind.name}")
        self.welcome = json.loads(payload)
        self._reader, self._writer = reader, writer

    def _enqueue(self, request: TLRequest, dc_id: int) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        if not self._transport_connected():
            future.set_exception(ConnectionError("tgmux gateway connection is down"))
            return future
        call_id = next(self._ids)
        self._pending[call_id] = (request, future)
        frame = protocol.encode_frame(Frame.CALL, protocol.encode_call(call_id, dc_id, bytes(request)))
        self._writer.write(frame)
        return future

    async def _recv_loop(self) -> None:
        while self._user_connected:
            try:
                await self._read_frames()
            except asyncio.CancelledError:
                raise
            except (asyncio.IncompleteReadError, ConnectionError, ProtocolError, OSError) as exc:
                log.warning("tgmux gateway connection lost: %s", exc)
                self._close_transport(ConnectionError(f"tgmux gateway connection lost: {exc}"))
            if not self._user_connected:
                return
            if not self._auto_reconnect or not await self._reconnect():
                self._user_connected = False
                self._lock.release()
                if self._disconnected is not None and not self._disconnected.done():
                    self._disconnected.set_result(None)
                return

    async def _read_frames(self) -> None:
        assert self._reader is not None
        while True:
            kind, payload = await protocol.read_frame(self._reader)
            if kind is Frame.RESULT:
                call_id, body = protocol.decode_result(payload)
                entry = self._pending.pop(call_id, None)
                if entry is None:
                    continue
                request, future = entry
                if future.done():
                    continue
                try:
                    future.set_result(protocol.parse_result(request, body))
                except Exception as exc:  # malformed result must fail the call, not the loop
                    future.set_exception(exc)
            elif kind is Frame.ERROR:
                call_id, code, message = protocol.decode_error(payload)
                entry = self._pending.pop(call_id, None)
                if entry is not None and not entry[1].done():
                    entry[1].set_exception(protocol.error_from_wire(code, message, entry[0]))
            elif kind is Frame.UPDATE:
                container = protocol.parse_object(payload)
                if isinstance(container, types.Updates):
                    self._update_queue.put_nowait(container)
            elif kind is Frame.FATAL:
                raise ConnectionError(payload.decode(errors="replace"))
            else:
                raise ProtocolError(f"unexpected frame {kind.name} from gateway")

    def _backoff(self, attempt: int) -> float:
        return min(self._delay * 2 ** min(attempt, 16), self._max_delay)

    async def _reconnect(self) -> bool:
        for attempt in range(self._retries):
            await asyncio.sleep(self._backoff(attempt))
            if not self._user_connected:
                return False
            try:
                await self._open()
                log.info("reconnected to the tgmux gateway")
                return True
            except GatewayRejectedError as exc:
                log.error("tgmux gateway refused the connection (%s); giving up", exc)
                return False
            except (OSError, asyncio.TimeoutError, ProtocolError) as exc:
                log.warning("reconnect attempt %d failed: %s; next in %.0f s",
                            attempt + 1, exc, self._backoff(attempt + 1))
        return False

    async def _dispatch_loop(self) -> None:
        # Separate from _recv_loop so a handler awaiting a request cannot block its own result.
        assert self._update_queue is not None
        while True:
            container = await self._update_queue.get()
            try:
                await self._on_updates(container)
            except Exception:
                log.exception("failed to dispatch updates from the gateway")

    def _close_transport(self, error: Exception) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = self._writer = None
        pending, self._pending = self._pending, {}
        for _request, future in pending.values():
            if not future.done():
                future.set_exception(error)


class SharedClient(TelegramClient):
    """Drop-in TelegramClient backed by the tgmux gateway.

    >>> client = SharedClient("barabot")                     # the gateway of instance "default"
    >>> other = SharedClient("barabot", instance="second")   # another account's gateway
    >>> await client.start()
    """

    def __init__(
        self,
        integration: str,
        *,
        instance: str | None = None,
        socket_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs["catch_up"] = False  # the gateway does not replay missed updates
        # Unlike Telethon's default of 5 attempts, wait for the gateway forever:
        # it restarts on deploys and on Telegram disconnects. Pass
        # connection_retries=N to give up after N attempts instead.
        kwargs.setdefault("connection_retries", -1)
        kwargs.setdefault("retry_delay", 1)
        super().__init__(MemorySession(), api_id=1, api_hash="0" * 32, **kwargs)
        self._sender = GatewaySender(
            resolve_socket(instance, socket_path),
            integration,
            on_updates=self._on_gateway_updates,
            receive_updates=not self._no_updates,
            auto_reconnect=self._auto_reconnect,
            retries=self._connection_retries,
            delay=self._retry_delay,
            timeout=self._timeout,
        )

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("The asyncio event loop must not change after connection")
        await self._sender.connect()
        me = await self.get_me()  # caches the self user for event builders
        if me is None:
            raise RuntimeError("the gateway's Telegram session is not authorized")

    async def _borrow_exported_sender(self, dc_id: int) -> _DcSender:
        return self._sender.for_dc(dc_id)

    async def _return_exported_sender(self, sender) -> None:
        return None

    async def _switch_dc(self, new_dc: int):
        raise RuntimeError("SharedClient cannot switch data centers; the tgmux gateway owns the connection")

    async def _on_gateway_updates(self, container: types.Updates) -> None:
        updates = await self._preprocess_updates(container.updates, container.users, container.chats)
        for update in updates:
            if self._sequential_updates:
                await self._dispatch_update(update)
            else:
                task = self.loop.create_task(self._dispatch_update(update))
                self._event_handler_tasks.add(task)
                task.add_done_callback(self._event_handler_tasks.discard)

    # Account management belongs to the gateway owner, not to integrations.
    async def send_code_request(self, *args, **kwargs):
        raise RuntimeError("SharedClient cannot log in; authorize the gateway session instead")

    async def sign_in(self, *args, **kwargs):
        raise RuntimeError("SharedClient cannot log in; authorize the gateway session instead")

    async def log_out(self, *args, **kwargs):
        raise RuntimeError("SharedClient cannot log out the shared account")
