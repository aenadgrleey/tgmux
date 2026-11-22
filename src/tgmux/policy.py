"""Which requests integrations may make, and which ones change ownership."""
from __future__ import annotations

from telethon.tl import functions

# Requests that touch no chat and are safe for any integration.
GLOBAL_ALLOW: frozenset[type] = frozenset({
    functions.help.GetConfigRequest,
    functions.help.GetNearestDcRequest,
    functions.help.GetAppConfigRequest,
    functions.updates.GetStateRequest,
    functions.users.GetUsersRequest,
    functions.users.GetFullUserRequest,
    functions.contacts.ResolveUsernameRequest,
    functions.messages.CheckChatInviteRequest,
    functions.upload.SaveFilePartRequest,
    functions.upload.SaveBigFilePartRequest,
    functions.upload.GetFileRequest,
    functions.upload.GetFileHashesRequest,
    functions.upload.GetCdnFileRequest,
    functions.upload.GetCdnFileHashesRequest,
    functions.upload.ReuploadCdnFileRequest,
    functions.messages.GetStickerSetRequest,
    functions.messages.GetCustomEmojiDocumentsRequest,
    functions.messages.GetAvailableReactionsRequest,
    functions.phone.GetCallConfigRequest,
})

# Sending into a private chat nobody owns yet claims it ("first to write").
DM_CLAIMING: frozenset[type] = frozenset({
    functions.messages.SendMessageRequest,
    functions.messages.SendMediaRequest,
    functions.messages.SendMultiMediaRequest,
    functions.messages.ForwardMessagesRequest,
})

# Wrappers Telethon itself may add; unwrapped before policy checks.
TRANSPARENT_WRAPPERS: tuple[type, ...] = (functions.InvokeWithoutUpdatesRequest,)


def unwrap(request):
    while isinstance(request, TRANSPARENT_WRAPPERS):
        request = request.query
    return request
