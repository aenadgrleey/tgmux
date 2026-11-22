"""The gateway: executes integrations' requests on the shared account,
enforces chat ownership and routes updates to the owning integration."""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any

from telethon import errors as tl_errors
from telethon import utils
from telethon.errors import RPCError
from telethon.tl import functions, types
from telethon.tl.tlobject import TLObject

from tgmux import protocol
from tgmux.errors import (
    CallNotResolvedError,
    ChatNotOwnedError,
    ChatOwnedByOtherError,
    ProtocolError,
    RequestNotAllowedError,
)
from tgmux.peers import CallMap, channel_id, chat_id, referenced_ids, request_scope, update_chat
from tgmux.policy import DM_CLAIMING, GLOBAL_ALLOW, unwrap
from tgmux.protocol import Frame
from tgmux.registry import Registry
from tgmux.reporter import LogReporter, Reporter
from tgmux.upstream import Upstream

log = logging.getLogger(__name__)

CALL_CONNECTION_TTL = 60          # seconds an UpdateGroupCallConnection is attributed to the last joiner
UNOWNED_REPORT_WINDOW = 24 * 3600
OUTBOX_LIMIT = 10_000


class _Connection:
    def __init__(self, name: str, writer: asyncio.StreamWriter, wants_updates: bool) -> None:
        self.name = name
        self.wants_updates = wants_updates
        self._writer = writer
        self._outbox: asyncio.Queue[bytes] = asyncio.Queue(OUTBOX_LIMIT)
        self._pump = asyncio.get_running_loop().create_task(self._run())
        self.overflowed = False

    def send(self, kind: Frame, payload: bytes) -> None:
        try:
            self._outbox.put_nowait(protocol.encode_frame(kind, payload))
        except asyncio.QueueFull:
            self.overflowed = True
            self._writer.close()

    async def _run(self) -> None:
        try:
            while True:
                data = await self._outbox.get()
                self._writer.write(data)
                await self._writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def close(self) -> None:
        self._pump.cancel()
        try:
            await self._pump
        except asyncio.CancelledError:
            pass
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass


class Gateway:
    def __init__(
        self,
        upstream: Upstream,
        registry: Registry,
        reporter: Reporter | None = None,
        *,
        global_allow: frozenset[type] = GLOBAL_ALLOW,
    ) -> None:
        self.upstream = upstream
        self.registry = registry
        self.reporter: Reporter = reporter or LogReporter()
        self.global_allow = global_allow
        self.calls = CallMap()
        self._connections: dict[str, _Connection] = {}
        self._chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_call_join: tuple[str, float] | None = None
        upstream.set_update_handler(self.on_update)

    # --- server -----------------------------------------------------------

    async def serve(self, socket_path: str | Path) -> asyncio.AbstractServer:
        path = Path(socket_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            path.unlink()
        old_umask = os.umask(0o177)  # socket is created 0600 directly, no chmod race
        try:
            server = await asyncio.start_unix_server(self._handle_connection, str(path))
        finally:
            os.umask(old_umask)
        log.info("tgmux gateway listening on %s", path)
        return server

    def connected(self) -> list[str]:
        return sorted(self._connections)

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn: _Connection | None = None
        calls: set[asyncio.Task] = set()
        try:
            kind, payload = await asyncio.wait_for(protocol.read_frame(reader), protocol.HELLO_TIMEOUT)
            hello = self._parse_hello(kind, payload)
            if hello is None:
                writer.write(protocol.encode_frame(Frame.FATAL, b"UNKNOWN_INTEGRATION"))
                await writer.drain()
                return
            name, wants_updates = hello
            conn = _Connection(name, writer, wants_updates)
            previous = self._connections.get(name)
            self._connections[name] = conn
            if previous is not None:
                log.info("integration %s reconnected; closing the previous connection", name)
                await previous.close()
            conn.send(Frame.WELCOME, json.dumps(
                {"self_id": self.upstream.self_id, "version": protocol.PROTOCOL_VERSION}
            ).encode())
            log.info("integration %s connected", name)
            while True:
                kind, payload = await protocol.read_frame(reader)
                if kind is not Frame.CALL:
                    raise ProtocolError(f"unexpected frame {kind.name} from client")
                task = asyncio.get_running_loop().create_task(self._handle_call(conn, payload))
                calls.add(task)
                task.add_done_callback(calls.discard)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        except ProtocolError as exc:
            self.reporter.report(f"protocol:{conn.name if conn else '?'}", f"Ошибка протокола: {exc}")
        finally:
            for task in calls:
                task.cancel()
            if conn is not None:
                if conn.overflowed:
                    self.reporter.report(f"overflow:{conn.name}", f"{conn.name} не успевал читать апдейты, соединение закрыто")
                if self._connections.get(conn.name) is conn:
                    del self._connections[conn.name]
                    log.info("integration %s disconnected", conn.name)
                await conn.close()
            else:
                writer.close()

    def _parse_hello(self, kind: Frame, payload: bytes) -> tuple[str, bool] | None:
        if kind is not Frame.HELLO:
            return None
        try:
            hello = json.loads(payload)
            name = str(hello["integration"])
            version = hello.get("version")
        except (ValueError, KeyError, TypeError):
            return None
        if version != protocol.PROTOCOL_VERSION or not self.registry.has_integration(name):
            log.warning("rejected connection for integration %r", name)
            return None
        return name, bool(hello.get("updates", True))

    async def _handle_call(self, conn: _Connection, payload: bytes) -> None:
        call_id, dc_id, data = protocol.decode_call(payload)
        request: TLObject | None = None
        try:
            request = protocol.parse_request(data)
            result = await self.execute(conn.name, request, dc_id or None)
            body = protocol.serialize_result(unwrap(request), result)
            conn.send(Frame.RESULT, protocol.encode_result(call_id, body))
        except RPCError as exc:
            code, message = protocol.error_to_wire(exc)
            conn.send(Frame.ERROR, protocol.encode_error(call_id, code, message))
        except ProtocolError as exc:
            conn.send(Frame.ERROR, protocol.encode_error(call_id, 400, "TGMUX_BAD_REQUEST"))
            self.reporter.report(f"bad-request:{conn.name}", f"{conn.name}: не удалось разобрать запрос: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 400, not 500: Telethon retries 5xx, which would resend non-idempotent requests.
            conn.send(Frame.ERROR, protocol.encode_error(call_id, 400, "TGMUX_INTERNAL"))
            name = type(unwrap(request)).__name__ if request is not None else "?"
            log.exception("request %s from %s failed", name, conn.name)
            self.reporter.report(
                f"call-error:{name}:{type(exc).__name__}",
                f"{conn.name}: {name} упал с {type(exc).__name__}: {exc}\n{_short_traceback(exc)}",
            )

    # --- requests ---------------------------------------------------------

    async def execute(self, integration: str, request: TLObject, dc_id: int | None = None) -> Any:
        inner = unwrap(request)

        if isinstance(inner, functions.channels.JoinChannelRequest):
            return await self._join_channel(integration, inner)
        if isinstance(inner, functions.messages.ImportChatInviteRequest):
            return await self._import_invite(integration, inner)
        if isinstance(inner, functions.channels.LeaveChannelRequest):
            return await self._leave(integration, _input_channel_id(inner, inner.channel), inner)
        if isinstance(inner, functions.messages.DeleteChatUserRequest) and self._is_self(inner.user_id):
            return await self._leave(integration, chat_id(inner.chat_id), inner)
        if isinstance(inner, functions.messages.GetDialogsRequest):
            return await self._dialogs(integration, inner)

        scope = request_scope(inner, self_id=self.upstream.self_id, calls=self.calls)
        if scope.unresolved_calls:
            raise CallNotResolvedError(inner, "request the full chat (GetFullChannel/GetFullChat) first")
        if scope.chats:
            self._check_owned(integration, inner, scope.chats)
        elif type(inner) not in self.global_allow:
            raise RequestNotAllowedError(inner, type(inner).__name__)

        if isinstance(inner, functions.phone.JoinGroupCallRequest):
            self._last_call_join = (integration, time.monotonic())
        result = await self.upstream.invoke(inner, dc_id)
        self.calls.learn(result)
        return result

    def _is_self(self, user: TLObject) -> bool:
        return isinstance(user, types.InputUserSelf) or (
            isinstance(user, types.InputUser) and user.user_id == self.upstream.self_id
        )

    def _check_owned(self, integration: str, request: TLObject, chats: set[int]) -> None:
        dm_target = self._dm_target(request)
        for chat in chats:
            owner = self.registry.owner(chat)
            if owner == integration:
                continue
            if owner is None and chat == dm_target:
                # First integration to write to a private chat owns it.
                if self.registry.claim(chat, integration, "dm") == integration:
                    log.info("%s claimed private chat %s by writing first", integration, chat)
                    continue
            raise ChatNotOwnedError(request, f"chat {chat}")

    def _dm_target(self, request: TLObject) -> int | None:
        if type(request) not in DM_CLAIMING:
            return None
        target = getattr(request, "to_peer", None) or getattr(request, "peer", None)
        if isinstance(target, (types.InputPeerUser, types.InputPeerUserFromMessage)):
            return target.user_id
        if isinstance(target, types.InputPeerSelf):
            return self.upstream.self_id
        return None

    async def _join_channel(self, integration: str, request) -> Any:
        chat = _input_channel_id(request, request.channel)
        async with self._chat_locks[chat]:
            self._ensure_claimable(integration, chat, request)
            try:
                result = await self.upstream.invoke(request)
            except tl_errors.UserAlreadyParticipantError:
                self._claim(integration, chat, "join", request)
                raise
            self._claim(integration, chat, "join", request)
            return result

    async def _import_invite(self, integration: str, request) -> Any:
        check = await self.upstream.invoke(functions.messages.CheckChatInviteRequest(request.hash))
        if isinstance(check, (types.ChatInviteAlready, types.ChatInvitePeek)):
            # The account is already in this chat; whoever "joins" it first takes it.
            chat = utils.get_peer_id(check.chat)
            async with self._chat_locks[chat]:
                self._ensure_claimable(integration, chat, request)
                self._claim(integration, chat, "invite", request)
                return await self.upstream.invoke(request)

        result = await self.upstream.invoke(request)
        joined = [c for c in getattr(result, "chats", []) if isinstance(c, (types.Channel, types.Chat))]
        if not joined:
            self.reporter.report(
                f"invite-no-chat:{integration}",
                f"{integration}: ImportChatInvite прошёл, но в ответе нет чата ({type(result).__name__}); владелец не назначен",
            )
            return result
        chat = utils.get_peer_id(joined[0])
        async with self._chat_locks[chat]:
            self._claim(integration, chat, "invite", request)
        return result

    def _ensure_claimable(self, integration: str, chat: int, request) -> None:
        owner = self.registry.owner(chat)
        if owner is not None and owner != integration:
            raise ChatOwnedByOtherError(request, f"chat {chat}")

    def _claim(self, integration: str, chat: int, source: str, request) -> None:
        owner = self.registry.claim(chat, integration, source)
        if owner != integration:
            self.reporter.report(
                f"claim-race:{chat}",
                f"{integration} вошёл в чат {chat}, но им уже владеет {owner}; аккаунт остался в чате",
            )
            raise ChatOwnedByOtherError(request, f"chat {chat}")
        log.info("%s owns chat %s (%s)", integration, chat, source)

    async def _leave(self, integration: str, chat: int, request) -> Any:
        async with self._chat_locks[chat]:
            if self.registry.owner(chat) != integration:
                raise ChatNotOwnedError(request, f"chat {chat}")
            try:
                result = await self.upstream.invoke(request)
            except (tl_errors.UserNotParticipantError, tl_errors.ChannelPrivateError):
                self.registry.release(chat, integration)
                raise
            self.registry.release(chat, integration)
            log.info("%s left chat %s", integration, chat)
            return result

    async def _dialogs(self, integration: str, request) -> Any:
        empty = types.messages.Dialogs(dialogs=[], messages=[], chats=[], users=[])
        # All owned dialogs fit in one page; any follow-up page is empty.
        if request.offset_id or request.offset_date or not isinstance(request.offset_peer, types.InputPeerEmpty):
            return empty
        peers = []
        for chat in self.registry.owned_by(integration):
            try:
                peers.append(types.InputDialogPeer(await self.upstream.input_peer(chat)))
            except (ValueError, TypeError):
                self.reporter.report(f"dialog-no-entity:{chat}", f"Нет access_hash для чата {chat} ({integration}), он пропущен в списке диалогов")
        dialogs, messages, chats, users = [], [], [], []
        for i in range(0, len(peers), 100):
            part = await self.upstream.invoke(functions.messages.GetPeerDialogsRequest(peers[i:i + 100]))
            dialogs += part.dialogs
            messages += part.messages
            chats += part.chats
            users += part.users
        return types.messages.Dialogs(dialogs=dialogs, messages=messages, chats=chats, users=users)

    # --- updates ----------------------------------------------------------

    async def on_update(self, update: TLObject, entities: dict[int, TLObject]) -> None:
        try:
            self._route(update, entities)
        except Exception as exc:
            log.exception("failed to route %s", type(update).__name__)
            self.reporter.report(
                f"route-error:{type(update).__name__}",
                f"Не смог разослать {type(update).__name__}: {type(exc).__name__}: {exc}",
            )

    def _route(self, update: TLObject, entities: dict[int, TLObject]) -> None:
        self.calls.learn(update)
        update_name = type(update).__name__

        if isinstance(update, types.UpdateGroupCallConnection):
            last = self._last_call_join
            if last and time.monotonic() - last[1] < CALL_CONNECTION_TTL:
                self._deliver(last[0], update, entities, None)
            else:
                self.reporter.report(f"no-peer:{update_name}", f"{update_name} без недавнего JoinGroupCall, отброшен")
            return

        chat = update_chat(update, calls=self.calls)
        if chat is None:
            self.reporter.report(f"no-peer:{update_name}", f"Апдейт без чата отброшен: {update_name}")
            return

        owner = self.registry.owner(chat)
        if owner is None:
            if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
                self.reporter.report(
                    f"unowned:{chat}",
                    f"Сообщение в чате без владельца отброшено: {_title(entities.get(chat))} ({chat})",
                    window=UNOWNED_REPORT_WINDOW,
                )
            return
        self._deliver(owner, update, entities, chat)

    def _deliver(self, owner: str, update: TLObject, entities: dict[int, TLObject], chat: int | None) -> None:
        conn = self._connections.get(owner)
        if conn is None:
            if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
                self.reporter.report(f"offline:{owner}", f"{owner} не подключён, его сообщения теряются", window=3600)
            return
        if not conn.wants_updates:
            return
        wanted = referenced_ids(update)
        if chat is not None:
            wanted.add(chat)
        users = [e for i, e in entities.items() if i in wanted and isinstance(e, types.User)]
        chats = [e for i, e in entities.items() if i in wanted and not isinstance(e, types.User)]
        container = types.Updates(
            updates=[update], users=users, chats=chats,
            date=datetime.datetime.now(datetime.timezone.utc), seq=0,
        )
        conn.send(Frame.UPDATE, bytes(container))


def _input_channel_id(request, channel) -> int:
    raw = getattr(channel, "channel_id", None)
    if raw is None:
        raise RequestNotAllowedError(request, "empty channel")
    return channel_id(raw)


def _title(entity: TLObject | None) -> str:
    if entity is None:
        return "?"
    return getattr(entity, "title", None) or utils.get_display_name(entity) or "?"


def _short_traceback(exc: BaseException, limit: int = 6) -> str:
    return "".join(traceback.format_exception(exc, limit=limit)[-limit:])
