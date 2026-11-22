"""tgmux: share one Telegram user account between several services.

Integrations use :class:`SharedClient`, a drop-in Telethon ``TelegramClient``
that talks to the tgmux gateway. The gateway owns the real session, gives each
chat to exactly one integration and routes updates to that owner.
"""
from tgmux.client import SharedClient
from tgmux.errors import (
    CallNotResolvedError,
    ChatNotOwnedError,
    ChatOwnedByOtherError,
    GatewayRejectedError,
    IntegrationInUseError,
    RequestNotAllowedError,
)

__all__ = [
    "SharedClient",
    "ChatNotOwnedError",
    "ChatOwnedByOtherError",
    "RequestNotAllowedError",
    "CallNotResolvedError",
    "GatewayRejectedError",
    "IntegrationInUseError",
]
