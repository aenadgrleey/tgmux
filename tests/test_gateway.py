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


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeConn:
    def __init__(self, name):
        self.name, self.wants_updates, self.sent = name, True, []

    def send(self, kind, payload):
        self.sent.append(payload)


def availability_gateway(**kwargs):
    upstream = FakeUpstream()
    registry = registry_with("barabot")
    registry.claim(CHAN, "barabot", "join")
    reporter = RecordingReporter()
    clock = Clock()
    gw = Gateway(upstream, registry, reporter, clock=clock, offline_grace=30, **kwargs)
    gw._mark_offline("barabot")  # what serve() does at startup
    return upstream, reporter, clock, gw


def push(upstream, text, msg_id):
    run(upstream.push(types.UpdateNewChannelMessage(channel_message(5, text, msg_id=msg_id), msg_id, 1), [channel(5)]))


def connect(gw, name):
    """Mirrors the registration step of Gateway._handle_connection."""
    conn = FakeConn(name)
    gw._connections[name] = conn
    gw._mark_online(name, *gw._flush_backlog(conn))
    return conn


def disconnect(gw, name):
    del gw._connections[name]
    gw._mark_offline(name)


def test_unavailable_integration_is_reported_after_grace_and_reminded():
    upstream, reporter, clock, gw = availability_gateway()
    clock.now = 29
    gw.sweep()
    assert reporter.keys() == []
    clock.now = 31
    gw.sweep()
    gw.sweep()
    assert reporter.keys() == ["offline:barabot"]
    assert "недоступна" in reporter.reports[0][1]
    clock.now = 31 + 3600
    gw.sweep()
    assert reporter.keys() == ["offline:barabot", "offline:barabot"]
    assert "всё ещё недоступна" in reporter.reports[1][1]


def test_quick_reconnect_is_silent():
    upstream, reporter, clock, gw = availability_gateway()
    connect(gw, "barabot")
    disconnect(gw, "barabot")
    clock.now = 10
    gw.sweep()
    push(upstream, "hi", 1)
    conn = connect(gw, "barabot")
    gw.sweep()
    assert len(conn.sent) == 1
    assert reporter.keys() == []


def test_return_after_reported_outage_is_reported_with_counts():
    upstream, reporter, clock, gw = availability_gateway()
    push(upstream, "a", 1)
    clock.now = 60
    gw.sweep()
    conn = connect(gw, "barabot")
    assert len(conn.sent) == 1
    assert reporter.keys() == ["offline:barabot", "online:barabot"]
    assert "доставлено из очереди: 1" in reporter.reports[1][1]


def test_overflow_is_reported_immediately_and_summed_on_return():
    upstream, reporter, clock, gw = availability_gateway(backlog_limit=2)
    for i in range(1, 6):            # 5 updates, limit 2 -> 3 dropped
        push(upstream, str(i), i)
    assert reporter.keys() == ["backlog-overflow:barabot"]
    assert gw.backlog_size("barabot") == 2
    conn = connect(gw, "barabot")
    assert len(conn.sent) == 2
    assert reporter.keys()[-1] == "online:barabot"
    assert "потеряно: 3 (переполнение: 3" in reporter.reports[-1][1]


def test_ttl_expiry_is_reported_without_waiting_for_reconnect():
    upstream, reporter, clock, gw = availability_gateway(backlog_ttl=100)
    push(upstream, "old", 1)
    clock.now = 50
    push(upstream, "newer", 2)
    clock.now = 120                  # "old" expired, "newer" not yet
    gw.sweep()
    assert "backlog-expired:barabot" in reporter.keys()
    assert gw.backlog_size("barabot") == 1
    clock.now = 130
    conn = connect(gw, "barabot")
    assert len(conn.sent) == 1
    assert "старше 100 с: 1" in reporter.reports[-1][1]


def test_removed_integration_stops_being_reported():
    upstream, reporter, clock, gw = availability_gateway()
    gw.registry.remove_integration("barabot")
    clock.now = 100
    gw.sweep()
    assert reporter.keys() == []


# --- pending join requests (one mechanism for JoinChannel and invite links) ---

def joined_update(cid, title):
    """What arrives when a request is approved: UpdateChannel with the (now joined) channel."""
    return types.UpdateChannel(cid), [types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(),
                                                    date=channel(cid).date, access_hash=cid * 10)]


def private_invite(title):
    return types.ChatInvite(title=title, photo=types.PhotoEmpty(0), participants_count=3, color=0, request_needed=True)


def test_join_channel_request_is_pending_then_assigned_by_id():
    upstream, registry, reporter, gw = make()
    upstream.on(functions.channels.JoinChannelRequest, lambda r: errors.InviteRequestSentError(r))
    upstream.on(functions.channels.GetChannelsRequest, lambda r: types.messages.Chats([channel(5, "Квартиры")]))
    with pytest.raises(errors.InviteRequestSentError):
        run(gw.execute("barabot", functions.channels.JoinChannelRequest(types.InputChannel(5, 50))))
    [pending] = registry.pendings()
    assert (pending.integration, pending.chat_id, pending.title) == ("barabot", CHAN, "Квартиры")
    assert registry.owner(CHAN) is None

    # someone else cannot jump in while the request is pending
    upstream.calls.clear()
    with pytest.raises(ChatOwnedByOtherError):
        run(gw.execute("lofi", functions.channels.JoinChannelRequest(types.InputChannel(5, 50))))
    assert upstream.calls == []

    delivered = []
    gw._deliver = lambda owner, update, entities, chat: delivered.append(owner)
    # any update from that chat id resolves it, even an ordinary message
    run(upstream.push(types.UpdateNewChannelMessage(channel_message(5, "hi"), 1, 1), [channel(5, "Renamed")]))
    assert registry.owner(CHAN) == "barabot" and registry.pendings() == []
    assert delivered == ["barabot"]
    assert reporter.keys() == []


def test_invite_link_request_is_pending_then_assigned_by_title():
    upstream, registry, reporter, gw = make()
    upstream.on(functions.messages.CheckChatInviteRequest, lambda r: private_invite("Аренда Тбилиси"))
    upstream.on(functions.messages.ImportChatInviteRequest, lambda r: errors.InviteRequestSentError(r))
    with pytest.raises(errors.InviteRequestSentError):
        run(gw.execute("barabot", functions.messages.ImportChatInviteRequest("abc")))
    [pending] = registry.pendings()
    assert (pending.chat_id, pending.invite_hash) == (None, "abc")

    # same link from another integration is refused before Telegram
    with pytest.raises(ChatOwnedByOtherError):
        run(gw.execute("lofi", functions.messages.ImportChatInviteRequest("abc")))

    gw._deliver = lambda *a: None
    # an ordinary message in an unowned chat with the same title is NOT a join signal
    run(upstream.push(types.UpdateNewChannelMessage(channel_message(6, "x"), 1, 1), [channel(6, "Аренда Тбилиси")]))
    assert registry.owner(channel_id(6)) is None and len(registry.pendings()) == 1

    update, entities = joined_update(7, "  аренда   тбилиси ")
    run(upstream.push(update, entities))
    assert registry.owner(channel_id(7)) == "barabot" and registry.pendings() == []


def test_ambiguous_titles_are_reported_not_guessed():
    upstream, registry, reporter, gw = make()
    upstream.on(functions.messages.CheckChatInviteRequest, lambda r: private_invite("Flats"))
    upstream.on(functions.messages.ImportChatInviteRequest, lambda r: errors.InviteRequestSentError(r))
    for name, link in (("barabot", "a"), ("lofi", "b")):
        with pytest.raises(errors.InviteRequestSentError):
            run(gw.execute(name, functions.messages.ImportChatInviteRequest(link)))
    update, entities = joined_update(7, "Flats")
    run(upstream.push(update, entities))
    assert registry.owner(channel_id(7)) is None
    assert len(registry.pendings()) == 2
    assert reporter.keys() == [f"pending-ambiguous:{channel_id(7)}"]
    assert "tgmux grant" in reporter.reports[0][1]


def test_stale_requests_expire_with_a_report():
    upstream, registry, reporter, gw = make()
    wall = [1_000_000.0]
    gw._wall_clock = lambda: wall[0]
    gw._pending_ttl = 100
    upstream.on(functions.channels.JoinChannelRequest, lambda r: errors.InviteRequestSentError(r))
    with pytest.raises(errors.InviteRequestSentError):
        run(gw.execute("barabot", functions.channels.JoinChannelRequest(types.InputChannel(5, 50))))
    wall[0] += 50
    gw.sweep()
    assert len(registry.pendings()) == 1
    wall[0] += 100
    gw.sweep()
    assert registry.pendings() == []
    assert any(k.startswith("pending-expired:") for k in reporter.keys())
