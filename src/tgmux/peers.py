"""Find which chats a request / update / result refers to.

All chat ids are Telethon "marked" peer ids (``telethon.utils.get_peer_id``):
users > 0, basic groups < 0, channels and supergroups -100....
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from telethon import utils
from telethon.tl import types
from telethon.tl.tlobject import TLObject

# Fields where an InputPeerUser / InputPeerSelf means "the private chat with that
# user" rather than "a user mentioned by this request" (participant, user_id, ...).
CHAT_PEER_FIELDS = frozenset({"peer", "from_peer", "to_peer", "peers"})

# Int fields (on Message/Update objects) that name a user; used to keep entities.
_USER_INT_FIELDS = frozenset({"user_id", "inviter_id", "admin_id", "kicked_by", "actor_id", "users"})

_MAX_DEPTH = 12


def channel_id(raw: int) -> int:
    return utils.get_peer_id(types.PeerChannel(raw))


def chat_id(raw: int) -> int:
    return utils.get_peer_id(types.PeerChat(raw))


def is_user_chat(marked_id: int) -> bool:
    return marked_id > 0


def _fields(obj: TLObject) -> Iterator[tuple[str, Any]]:
    for name, value in vars(obj).items():
        if not name.startswith("_"):
            yield name, value


class CallMap:
    """group call id -> chat id, learned from full chats and UpdateGroupCall."""

    def __init__(self) -> None:
        self._calls: dict[int, int] = {}

    def get(self, call_id: int) -> int | None:
        return self._calls.get(call_id)

    def put(self, call_id: int, chat: int) -> None:
        self._calls[call_id] = chat

    def learn(self, obj: Any) -> None:
        if isinstance(obj, types.messages.ChatFull):
            full = obj.full_chat
            if getattr(full, "call", None) is not None:
                chat = channel_id(full.id) if isinstance(full, types.ChannelFull) else chat_id(full.id)
                self.put(full.call.id, chat)
        elif isinstance(obj, (types.Updates, types.UpdatesCombined)):
            for update in obj.updates:
                self.learn(update)
        elif isinstance(obj, types.UpdateGroupCall) and obj.peer is not None:
            self.put(obj.call.id, utils.get_peer_id(obj.peer))


@dataclass
class RequestScope:
    chats: set[int] = field(default_factory=set)
    unresolved_calls: set[int] = field(default_factory=set)


def request_scope(request: TLObject, *, self_id: int, calls: CallMap) -> RequestScope:
    """Collect every chat a request touches."""
    scope = RequestScope()

    def visit(name: str, value: Any, top: bool, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        if isinstance(value, list):
            for item in value:
                visit(name, item, top, depth + 1)
            return
        if isinstance(value, (types.InputPeerChannel, types.InputChannel,
                              types.InputPeerChannelFromMessage, types.InputChannelFromMessage)):
            scope.chats.add(channel_id(value.channel_id))
        elif isinstance(value, types.InputPeerChat):
            scope.chats.add(chat_id(value.chat_id))
        elif isinstance(value, (types.InputPeerUser, types.InputPeerUserFromMessage)):
            if name in CHAT_PEER_FIELDS:
                scope.chats.add(value.user_id)
        elif isinstance(value, types.InputPeerSelf):
            if name in CHAT_PEER_FIELDS:
                scope.chats.add(self_id)
        elif isinstance(value, types.InputPeerPhotoFileLocation):
            return  # downloading an avatar does not touch the chat
        elif isinstance(value, types.InputGroupCall):
            owner_chat = calls.get(value.id)
            if owner_chat is None:
                scope.unresolved_calls.add(value.id)
            else:
                scope.chats.add(owner_chat)
        elif name == "chat_id" and top and isinstance(value, int) and not isinstance(value, bool):
            scope.chats.add(chat_id(value))
        elif isinstance(value, TLObject):
            for child_name, child in _fields(value):
                visit(child_name, child, False, depth + 1)

    for name, value in _fields(request):
        visit(name, value, True, 0)
    return scope


def update_chat(update: TLObject, *, calls: CallMap) -> int | None:
    """The chat an update belongs to, or None if it carries no peer."""
    message = getattr(update, "message", None)
    if isinstance(message, (types.Message, types.MessageService)):
        return utils.get_peer_id(message.peer_id)
    peer = getattr(update, "peer", None)
    if isinstance(peer, (types.PeerUser, types.PeerChat, types.PeerChannel)):
        return utils.get_peer_id(peer)
    raw_channel = getattr(update, "channel_id", None)
    if isinstance(raw_channel, int):
        return channel_id(raw_channel)
    raw_chat = getattr(update, "chat_id", None)
    if isinstance(raw_chat, int):
        return chat_id(raw_chat)
    participants = getattr(update, "participants", None)
    if isinstance(participants, (types.ChatParticipants, types.ChatParticipantsForbidden)):
        return chat_id(participants.chat_id)
    call = getattr(update, "call", None)
    if isinstance(call, (types.InputGroupCall, types.GroupCall, types.GroupCallDiscarded)):
        return calls.get(call.id)
    if isinstance(update, (types.UpdateUserTyping, types.UpdateShortMessage)):
        return update.user_id
    if isinstance(update, types.UpdatePeerBlocked):
        return utils.get_peer_id(update.peer_id)
    return None


def message_chats(obj: Any) -> list[tuple[int, int]]:
    """(msg_id, chat) for every message outside channels that an update or result carries.

    Outside channels a msg_id is unique per account, so it alone names the chat later.
    """
    found: list[tuple[int, int]] = []

    def visit(value: Any, depth: int) -> None:
        if depth > 4:
            return
        if isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, (types.Message, types.MessageService)):
            if isinstance(value.peer_id, (types.PeerUser, types.PeerChat)):
                found.append((value.id, utils.get_peer_id(value.peer_id)))
        elif isinstance(value, types.UpdateShortMessage):
            found.append((value.id, value.user_id))
        elif isinstance(value, types.UpdateShortChatMessage):
            found.append((value.id, chat_id(value.chat_id)))
        elif isinstance(value, types.UpdateShort):
            visit(value.update, depth + 1)
        elif isinstance(value, TLObject):
            for name in ("message", "messages", "updates", "new_messages"):
                child = getattr(value, name, None)
                if isinstance(child, (list, TLObject)):
                    visit(child, depth + 1)

    visit(obj, 0)
    return found


def referenced_ids(obj: Any) -> set[int]:
    """Marked ids of every peer an object mentions (to trim entity lists)."""
    found: set[int] = set()

    def visit(name: str, value: Any, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        if isinstance(value, list):
            for item in value:
                visit(name, item, depth + 1)
        elif isinstance(value, (types.PeerUser, types.PeerChat, types.PeerChannel)):
            found.add(utils.get_peer_id(value))
        elif isinstance(value, int) and not isinstance(value, bool):
            if name in _USER_INT_FIELDS:
                found.add(value)
            elif name == "channel_id":
                found.add(channel_id(value))
        elif isinstance(value, TLObject):
            for child_name, child in _fields(value):
                visit(child_name, child, depth + 1)

    visit("", obj, 0)
    return found
