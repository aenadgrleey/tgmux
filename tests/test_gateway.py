"""Ownership policy and update routing, without sockets."""
import asyncio

import pytest
from telethon import errors
from telethon.tl import functions, types

from tgmux.errors import ChatNotOwnedError, ChatOwnedByOtherError, RequestNotAllowedError
from tgmux.gateway import Gateway
from tgmux.peers import channel_id

from fakes import FakeUpstream, RecordingReporter, channel, channel_message, registry_with, updates_for, user

CHAN = channel_id(5)


def make():
    upstream = FakeUpstream()
    registry = registry_with("barabot", "lofi")
    reporter = RecordingReporter()
    return upstream, registry, reporter, Gateway(upstream, registry, reporter)


def run(coro):
    return asyncio.run(coro)


def send(peer, text="hi"):
    return functions.messages.SendMessageRequest(peer, text, random_id=1)


def test_cannot_touch_unowned_or_foreign_chat():
    upstream, registry, _, gw = make()
    with pytest.raises(ChatNotOwnedError):
        run(gw.execute("barabot", send(types.InputPeerChannel(5, 50))))
    registry.claim(CHAN, "lofi", "join")
    with pytest.raises(ChatNotOwnedError):
        run(gw.execute("barabot", send(types.InputPeerChannel(5, 50))))
    assert upstream.calls == []
    run(gw.execute("lofi", send(types.InputPeerChannel(5, 50))))
    assert len(upstream.called(functions.messages.SendMessageRequest)) == 1


def test_join_claims_free_chat():
    upstream, registry, _, gw = make()
    run(gw.execute("barabot", functions.channels.JoinChannelRequest(types.InputChannel(5, 50))))
    assert registry.owner(CHAN) == "barabot"
    run(gw.execute("barabot", send(types.InputPeerChannel(5, 50))))


def test_join_of_taken_chat_is_refused_before_telegram():
    upstream, registry, _, gw = make()
    registry.claim(CHAN, "lofi", "join")
    with pytest.raises(ChatOwnedByOtherError):
        run(gw.execute("barabot", functions.channels.JoinChannelRequest(types.InputChannel(5, 50))))
    assert upstream.calls == []


def test_join_when_already_participant_still_claims():
    upstream, registry, _, gw = make()
    upstream.on(functions.channels.JoinChannelRequest, lambda r: errors.UserAlreadyParticipantError(r))
    with pytest.raises(errors.UserAlreadyParticipantError):
        run(gw.execute("barabot", functions.channels.JoinChannelRequest(types.InputChannel(5, 50))))
    assert registry.owner(CHAN) == "barabot"


def test_invite_for_new_chat_claims_from_result():
    upstream, registry, _, gw = make()
    upstream.on(functions.messages.CheckChatInviteRequest,
                lambda r: types.ChatInvite(title="t", photo=types.PhotoEmpty(0), participants_count=3, color=0))
    upstream.on(functions.messages.ImportChatInviteRequest, lambda r: updates_for([channel(5)]))
    run(gw.execute("barabot", functions.messages.ImportChatInviteRequest("abc")))
    assert registry.owner(CHAN) == "barabot"


def test_invite_to_chat_owned_by_other_is_refused():
    upstream, registry, _, gw = make()
    registry.claim(CHAN, "lofi", "join")
    upstream.on(functions.messages.CheckChatInviteRequest, lambda r: types.ChatInviteAlready(channel(5)))
    with pytest.raises(ChatOwnedByOtherError):
        run(gw.execute("barabot", functions.messages.ImportChatInviteRequest("abc")))
    assert not upstream.called(functions.messages.ImportChatInviteRequest)


def test_leave_releases():
    upstream, registry, _, gw = make()
    registry.claim(CHAN, "barabot", "join")
    with pytest.raises(ChatNotOwnedError):
        run(gw.execute("lofi", functions.channels.LeaveChannelRequest(types.InputChannel(5, 50))))
    run(gw.execute("barabot", functions.channels.LeaveChannelRequest(types.InputChannel(5, 50))))
    assert registry.owner(CHAN) is None


def test_first_to_write_owns_private_chat():
    upstream, registry, _, gw = make()
    run(gw.execute("lofi", send(types.InputPeerUser(42, 420))))
    assert registry.owner(42) == "lofi"
    with pytest.raises(ChatNotOwnedError):
        run(gw.execute("barabot", send(types.InputPeerUser(42, 420))))
    # reading a private chat does not claim it
    with pytest.raises(ChatNotOwnedError):
        run(gw.execute("barabot", functions.messages.GetHistoryRequest(types.InputPeerUser(43, 1), 0, None, 0, 10, 0, 0, 0)))
    assert registry.owner(43) is None


def test_requests_without_chat_need_allowlist():
    upstream, _, _, gw = make()
    run(gw.execute("barabot", functions.contacts.ResolveUsernameRequest("flats")))
    with pytest.raises(RequestNotAllowedError):
        run(gw.execute("barabot", functions.account.UpdateProfileRequest(first_name="pwned")))
    with pytest.raises(RequestNotAllowedError):
        run(gw.execute("barabot", functions.auth.LogOutRequest()))


def test_dialogs_only_list_owned_chats():
    upstream, registry, _, gw = make()
    registry.claim(CHAN, "barabot", "join")
    registry.claim(channel_id(6), "lofi", "join")
    upstream.input_peers = {CHAN: types.InputPeerChannel(5, 50), channel_id(6): types.InputPeerChannel(6, 60)}
    seen = []

    def peer_dialogs(request):
        seen.extend(p.peer.channel_id for p in request.peers)
        return types.messages.PeerDialogs(dialogs=[], messages=[], chats=[channel(5)], users=[],
                                          state=types.updates.State(1, 1, None, 1, 0))
    upstream.on(functions.messages.GetPeerDialogsRequest, peer_dialogs)
    request = functions.messages.GetDialogsRequest(None, 0, types.InputPeerEmpty(), 100, 0)
    result = run(gw.execute("barabot", request))
    assert seen == [5]
    assert [c.id for c in result.chats] == [5]


def test_updates_go_to_owner_only_and_unexpected_ones_are_reported():
    upstream, registry, reporter, gw = make()
    delivered = []
    gw._deliver = lambda owner, update, entities, chat: delivered.append((owner, type(update).__name__))
    registry.claim(CHAN, "barabot", "join")

    async def scenario():
        await upstream.push(types.UpdateNewChannelMessage(channel_message(5, "hi", from_user=1), 1, 1), [channel(5), user(1)])
        await upstream.push(types.UpdateNewChannelMessage(channel_message(6, "x"), 1, 1), [channel(6, "Чужой")])
        await upstream.push(types.UpdateNewChannelMessage(channel_message(6, "y"), 2, 1), [channel(6, "Чужой")])
        await upstream.push(types.UpdateDeleteMessages([1], 1, 1), [])
    run(scenario())

    assert delivered == [("barabot", "UpdateNewChannelMessage")]
    assert reporter.keys() == [f"unowned:{channel_id(6)}", "no-peer:UpdateDeleteMessages"]
    assert "Чужой" in reporter.reports[0][1]
