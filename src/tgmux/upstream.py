"""The real Telegram connection the gateway multiplexes."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

from telethon import TelegramClient, events
from telethon.tl.tlobject import TLObject

UpdateHandler = Callable[[TLObject, dict[int, TLObject]], Awaitable[None]]


class Upstream(Protocol):
    self_id: int

    async def invoke(self, request: TLObject, dc_id: int | None = None) -> Any: ...

    async def input_peer(self, chat_id: int) -> TLObject: ...

    def set_update_handler(self, handler: UpdateHandler) -> None: ...


class TelethonUpstream:
    """Wraps an authorized TelegramClient. The only place that talks MTProto."""

    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.self_id = 0
        self._handler: UpdateHandler | None = None

    async def start(self) -> None:
        # Register before connect(): with catch_up the client replays missed updates
        # (getDifference) right inside connect(), and they must not hit an empty handler list.
        self.client.add_event_handler(self._on_raw, events.Raw)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise RuntimeError("Telegram session is not authorized; authorize it with Telethon and point `tgmux telegram set` at it")
        me = await self.client.get_me()
        self.self_id = me.id

    async def stop(self) -> None:
        await self.client.disconnect()

    def set_update_handler(self, handler: UpdateHandler) -> None:
        self._handler = handler

    async def _on_raw(self, update: TLObject) -> None:
        if self._handler is not None:
            await self._handler(update, getattr(update, "_entities", None) or {})

    async def invoke(self, request: TLObject, dc_id: int | None = None) -> Any:
        client = self.client
        # Flood waits go back to the integration; its own SharedClient decides whether to sleep.
        if dc_id and dc_id != client.session.dc_id:
            sender = await client._borrow_exported_sender(dc_id)
            try:
                return await client._call(sender, request, flood_sleep_threshold=0)
            finally:
                await client._return_exported_sender(sender)
        return await client._call(client._sender, request, flood_sleep_threshold=0)

    async def input_peer(self, chat_id: int) -> TLObject:
        return await self.client.get_input_entity(chat_id)
