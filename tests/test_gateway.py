"""Ownership policy and update routing, without sockets."""
import asyncio

import pytest
from telethon import errors
from telethon.tl import functions, types

from tgmux.errors import ChatNotOwnedError, ChatOwnedByOtherError, RequestNotAllowedError
from tgmux.gateway import Gateway
from tgmux.peers import channel_id

from fakes import NOW, FakeUpstream, RecordingEvents, channel, channel_message, registry_with, updates_for, user

CHAN = channel_id(5)


def make():
    upstream = FakeUpstream()
    registry = registry_with("barabot", "lofi")
    recorded = RecordingEvents()
    return upstream, registry, recorded, Gateway(upstream, registry, recorded)


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


def test_create_channel_claims_the_new_chat():
    upstream, registry, _, gw = make()
    upstream.on(functions.channels.CreateChannelRequest, lambda r: updates_for([channel(5)]))
    run(gw.execute("lofi", functions.channels.CreateChannelRequest("radio", "", megagroup=True)))
    assert registry.owner(CHAN) == "lofi"
    run(gw.execute("lofi", send(types.InputPeerChannel(5, 50))))


def test_create_basic_group_claims_from_invited_users():
    upstream, registry, _, gw = make()
    group = types.Chat(id=7, title="radio", photo=types.ChatPhotoEmpty(), participants_count=1, date=None, version=1)
    upstream.on(functions.messages.CreateChatRequest,
                lambda r: types.messages.InvitedUsers(updates=updates_for([group]), missing_invitees=[]))
    run(gw.execute("lofi", functions.messages.CreateChatRequest(users=[], title="radio")))
    assert registry.owner(-7) == "lofi"


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
    upstream, registry, recorded, gw = make()
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
    assert recorded.keys() == [f"unowned:{channel_id(6)}", "no-peer:UpdateDeleteMessages"]
    assert "Чужой" in recorded.reports[0][1]


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
    recorded = RecordingEvents()
    clock = Clock()
    gw = Gateway(upstream, registry, recorded, clock=clock, offline_grace=30, **kwargs)
    gw._mark_offline("barabot")  # what serve() does at startup
    return upstream, recorded, clock, gw


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
    upstream, recorded, clock, gw = availability_gateway()
    clock.now = 29
    gw.sweep()
    assert recorded.keys() == []
    clock.now = 31
    gw.sweep()
    gw.sweep()
    assert recorded.keys() == ["offline:barabot"]
    assert "недоступна" in recorded.reports[0][1]
    clock.now = 31 + 3600
    gw.sweep()
    assert recorded.keys() == ["offline:barabot", "offline:barabot"]
    assert "всё ещё недоступна" in recorded.reports[1][1]


def test_quick_reconnect_is_silent():
    upstream, recorded, clock, gw = availability_gateway()
    connect(gw, "barabot")
    disconnect(gw, "barabot")
    clock.now = 10
    gw.sweep()
    push(upstream, "hi", 1)
    conn = connect(gw, "barabot")
    gw.sweep()
    assert len(conn.sent) == 1
    assert recorded.keys() == []


def test_return_after_reported_outage_is_reported_with_counts():
    upstream, recorded, clock, gw = availability_gateway()
    push(upstream, "a", 1)
    clock.now = 60
    gw.sweep()
    conn = connect(gw, "barabot")
    assert len(conn.sent) == 1
    assert recorded.keys() == ["offline:barabot", "online:barabot"]
    assert "доставлено из очереди: 1" in recorded.reports[1][1]


def test_overflow_is_reported_immediately_and_summed_on_return():
    upstream, recorded, clock, gw = availability_gateway(backlog_limit=2)
    for i in range(1, 6):            # 5 updates, limit 2 -> 3 dropped
        push(upstream, str(i), i)
    assert recorded.keys() == ["backlog-overflow:barabot"]
    assert gw.backlog_size("barabot") == 2
    conn = connect(gw, "barabot")
    assert len(conn.sent) == 2
    assert recorded.keys()[-1] == "online:barabot"
    assert "потеряно: 3 (переполнение: 3" in recorded.reports[-1][1]


def test_ttl_expiry_is_reported_without_waiting_for_reconnect():
    upstream, recorded, clock, gw = availability_gateway(backlog_ttl=100)
    push(upstream, "old", 1)
    clock.now = 50
    push(upstream, "newer", 2)
    clock.now = 120                  # "old" expired, "newer" not yet
    gw.sweep()
    assert "backlog-expired:barabot" in recorded.keys()
    assert gw.backlog_size("barabot") == 1
    clock.now = 130
    conn = connect(gw, "barabot")
    assert len(conn.sent) == 1
    assert "старше 100 с: 1" in recorded.reports[-1][1]


def test_removed_integration_stops_being_reported():
    upstream, recorded, clock, gw = availability_gateway()
    gw.registry.remove_integration("barabot")
    clock.now = 100
    gw.sweep()
    assert recorded.keys() == []


# --- pending join requests (one mechanism for JoinChannel and invite links) ---

def joined_update(cid, title):
    """What arrives when a request is approved: UpdateChannel with the (now joined) channel."""
    return types.UpdateChannel(cid), [types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(),
                                                    date=channel(cid).date, access_hash=cid * 10)]


def private_invite(title):
    return types.ChatInvite(title=title, photo=types.PhotoEmpty(0), participants_count=3, color=0, request_needed=True)


def test_join_channel_request_is_pending_then_assigned_by_id():
    upstream, registry, recorded, gw = make()
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
    assert recorded.keys() == []


def test_invite_link_request_is_pending_then_assigned_by_title():
    upstream, registry, recorded, gw = make()
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
    upstream, registry, recorded, gw = make()
    upstream.on(functions.messages.CheckChatInviteRequest, lambda r: private_invite("Flats"))
    upstream.on(functions.messages.ImportChatInviteRequest, lambda r: errors.InviteRequestSentError(r))
    for name, link in (("barabot", "a"), ("lofi", "b")):
        with pytest.raises(errors.InviteRequestSentError):
            run(gw.execute(name, functions.messages.ImportChatInviteRequest(link)))
    update, entities = joined_update(7, "Flats")
    run(upstream.push(update, entities))
    assert registry.owner(channel_id(7)) is None
    assert len(registry.pendings()) == 2
    assert recorded.keys() == [f"pending-ambiguous:{channel_id(7)}"]
    assert "tgmux grant" in recorded.reports[0][1]


def test_stale_requests_expire_with_a_report():
    upstream, registry, recorded, gw = make()
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
    assert any(k.startswith("pending-expired:") for k in recorded.keys())


# --- deletes outside channels, blocked peers ---------------------------------

BASIC = -20  # marked id of basic group 20


def dm_message(user_id, msg_id):
    return types.Message(id=msg_id, peer_id=types.PeerUser(user_id), date=NOW, message="hi")


def group_message(raw_chat, msg_id):
    return types.Message(id=msg_id, peer_id=types.PeerChat(raw_chat), date=NOW, message="hi")


def routed():
    upstream, registry, recorded, gw = make()
    delivered = []
    gw._deliver = lambda owner, update, entities, chat: delivered.append((owner, update))
    registry.claim(10, "barabot", "dm")
    registry.claim(BASIC, "lofi", "join")
    return upstream, registry, recorded, gw, delivered


def test_delete_is_split_between_owners_by_seen_message_ids():
    upstream, registry, recorded, gw, delivered = routed()

    async def scenario():
        await upstream.push(types.UpdateNewMessage(dm_message(10, 100), 1, 1), [user(10)])
        await upstream.push(types.UpdateShortChatMessage(101, 30, 20, "hi", 2, 1, NOW), [])
        await upstream.push(types.UpdateDeleteMessages([100, 101, 999], 3, 3), [])
    run(scenario())

    deletes = [(owner, u.messages) for owner, u in delivered if isinstance(u, types.UpdateDeleteMessages)]
    assert sorted(deletes) == [("barabot", [100]), ("lofi", [101])]
    assert "no-peer:UpdateDeleteMessages" in recorded.keys()
    assert registry.message_chats([100, 101]) == {}  # a delete is final


def test_messages_from_request_results_are_remembered():
    upstream, registry, _, gw, delivered = routed()
    upstream.on(functions.messages.GetHistoryRequest, lambda r: types.messages.Messages(
        messages=[group_message(20, 300)], chats=[], users=[], topics=[]))
    run(gw.execute("lofi", functions.messages.GetHistoryRequest(
        types.InputPeerChat(20), offset_id=0, offset_date=None, add_offset=0, limit=10, max_id=0, min_id=0, hash=0)))
    upstream.on(functions.messages.SendMessageRequest, lambda r: types.UpdateShortSentMessage(
        id=301, pts=1, pts_count=1, date=NOW))
    run(gw.execute("barabot", send(types.InputPeerUser(10, 100))))

    run(upstream.push(types.UpdateDeleteMessages([300, 301], 4, 2), []))
    assert sorted((o, u.messages) for o, u in delivered) == [("barabot", [301]), ("lofi", [300])]


def test_delete_in_unowned_chat_is_dropped_silently():
    upstream, registry, recorded, gw, delivered = routed()

    async def scenario():
        await upstream.push(types.UpdateNewMessage(group_message(55, 400), 1, 1), [])
        await upstream.push(types.UpdateDeleteMessages([400], 2, 1), [])
    run(scenario())
    assert delivered == []
    assert "no-peer:UpdateDeleteMessages" not in recorded.keys()


def test_old_message_ids_expire():
    upstream = FakeUpstream()
    registry = registry_with("barabot")
    wall = Clock()
    gw = Gateway(upstream, registry, RecordingEvents(), wall_clock=wall, message_chat_ttl=100)
    run(upstream.push(types.UpdateNewMessage(dm_message(10, 500), 1, 1), []))
    wall.now = 50
    gw.sweep()
    assert registry.message_chats([500]) == {500: 10}
    wall.now = 101
    gw.sweep()
    assert registry.message_chats([500]) == {}


def test_peer_blocked_goes_to_private_chat_owner():
    upstream, _, recorded, gw, delivered = routed()
    run(upstream.push(types.UpdatePeerBlocked(types.PeerUser(10), blocked=True), [user(10)]))
    assert [(o, type(u).__name__) for o, u in delivered] == [("barabot", "UpdatePeerBlocked")]
    assert recorded.keys() == []


def test_short_private_message_goes_to_private_chat_owner():
    upstream, _, recorded, gw, delivered = routed()
    run(upstream.push(types.UpdateShortMessage(id=600, user_id=10, message="hi", pts=1, pts_count=1, date=NOW), []))
    assert [(o, type(u).__name__) for o, u in delivered] == [("barabot", "UpdateShortMessage")]
    assert recorded.keys() == []
