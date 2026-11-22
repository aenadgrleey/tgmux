"""Errors the gateway returns to integrations.

They subclass Telethon's RPC errors, so code written against Telethon handles
them like any other Telegram error (``except ForbiddenError``).
"""
from __future__ import annotations

from telethon.errors import ForbiddenError


class _GatewayRPCError(ForbiddenError):
    MESSAGE = ""

    def __init__(self, request=None, detail: str | None = None):
        super().__init__(request, self.MESSAGE)
        self.detail = detail

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} ({self.detail})" if self.detail else base


class ChatNotOwnedError(_GatewayRPCError):
    """The request touches a chat this integration does not own."""
    MESSAGE = "CHAT_NOT_OWNED"


class ChatOwnedByOtherError(_GatewayRPCError):
    """Joining a chat that already belongs to another integration."""
    MESSAGE = "CHAT_OWNED_BY_OTHER"


class RequestNotAllowedError(_GatewayRPCError):
    """The request is not scoped to a chat and is not on the global allowlist."""
    MESSAGE = "REQUEST_NOT_ALLOWED"


class CallNotResolvedError(_GatewayRPCError):
    """A group call the gateway cannot map to a chat yet (fetch the full chat first)."""
    MESSAGE = "CALL_NOT_RESOLVED"


GATEWAY_ERRORS: dict[str, type[_GatewayRPCError]] = {
    cls.MESSAGE: cls
    for cls in (
        ChatNotOwnedError,
        ChatOwnedByOtherError,
        RequestNotAllowedError,
        CallNotResolvedError,
    )
}


class GatewayRejectedError(ConnectionError):
    """The gateway refused the connection (unknown integration, protocol version)."""


class IntegrationInUseError(RuntimeError):
    """Another process already runs a SharedClient for this integration."""


class ProtocolError(ConnectionError):
    """Malformed frame on the gateway connection."""
