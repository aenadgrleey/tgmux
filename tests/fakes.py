"""A scripted stand-in for the real Telegram connection."""
from __future__ import annotations

import datetime
import inspect
from typing import Any, Callable

from telethon import utils
from telethon.tl import functions, types
from telethon.tl.tlobject import TLObject

from tgmux.registry import Registry
from tgmux.reporter import Deduplicator

SELF_ID = 777
NOW = datetime.datetime(2026, 11, 22, tzinfo=datetime.timezone.utc)


def channel(cid: int, title: str = "chan") -> types.Channel:
    return types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(), date=NOW, access_hash=cid * 10, megagroup=True)


def user(uid: int, name: str = "user") -> types.User:
    return types.User(id=uid, access_hash=uid * 10, first_name=name)


def channel_message(cid: int, text: str, msg_id: int = 1, from_user: int | None = None, out: bool = False) -> types.Message:
    return types.Message(
        id=msg_id, peer_id=types.PeerChannel(cid), date=NOW, message=text, out=out,
        from_id=types.PeerUser(from_user) if from_user else None,
    )


def updates_for(chats: list[TLObject]) -> types.Updates:
    return types.Updates(updates=[], users=[], chats=chats, date=NOW, seq=0)


class FakeUpstream:
    self_id = SELF_ID

    def __init__(self) -> None:
        self.calls: list[TLObject] = []
        self.handlers: dict[type, Callable[[Any], Any]] = {}
        self.input_peers: dict[int, TLObject] = {}
        self.update_handler = None

    def on(self, request_type: type, handler: Callable[[Any], Any]) -> None:
        self.handlers[request_type] = handler

    def set_update_handler(self, handler) -> None:
        self.update_handler = handler

    async def invoke(self, request: TLObject, dc_id: int | None = None) -> Any:
        self.calls.append(request)
        handler = self.handlers.get(type(request))
        if handler is None:
            if isinstance(request, functions.users.GetUsersRequest):
                return [types.User(id=self.self_id, access_hash=1, first_name="service", is_self=True)]
            return updates_for([])
        result = handler(request)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Exception):
            raise result
        return result

    async def input_peer(self, chat_id: int) -> TLObject:
        if chat_id not in self.input_peers:
            raise ValueError(chat_id)
        return self.input_peers[chat_id]

    async def push(self, update: TLObject, entities: list[TLObject]) -> None:
        await self.update_handler(update, {utils.get_peer_id(e): e for e in entities})

    def called(self, request_type: type) -> list[TLObject]:
        return [c for c in self.calls if isinstance(c, request_type)]


class RecordingReporter:
    def __init__(self) -> None:
        self.reports: list[tuple[str, str]] = []
        self._dedup = Deduplicator(3600)

    def report(self, key: str, text: str, *, window: float | None = None) -> None:
        if self._dedup.admit(key, window)[0]:
            self.reports.append((key, text))

    def keys(self) -> list[str]:
        return [k for k, _ in self.reports]


def registry_with(*names: str) -> Registry:
    registry = Registry(":memory:")
    for name in names:
        registry.add_integration(name)
    return registry
