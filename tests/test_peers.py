from telethon.tl import functions, types

from tgmux.peers import CallMap, channel_id, chat_id, request_scope, update_chat

from fakes import SELF_ID, channel_message


def scope(request, calls=None):
    return request_scope(request, self_id=SELF_ID, calls=calls or CallMap())


def test_channel_and_private_chat_peers():
    assert scope(functions.messages.SendMessageRequest(types.InputPeerChannel(5, 1), "x")).chats == {channel_id(5)}
    assert scope(functions.messages.SendMessageRequest(types.InputPeerUser(42, 1), "x")).chats == {42}
    assert scope(functions.messages.SendMessageRequest(types.InputPeerSelf(), "x")).chats == {SELF_ID}
    assert scope(functions.messages.GetFullChatRequest(chat_id=9)).chats == {chat_id(9)}


def test_user_references_are_not_chats():
    request = functions.channels.EditBannedRequest(
        types.InputChannel(5, 1), types.InputPeerUser(42, 1), types.ChatBannedRights(until_date=None)
    )
    assert scope(request).chats == {channel_id(5)}
    assert scope(functions.users.GetFullUserRequest(types.InputUser(42, 1))).chats == set()


def test_forward_touches_both_chats():
    request = functions.messages.ForwardMessagesRequest(types.InputPeerChannel(1, 1), [1], types.InputPeerUser(42, 1))
    assert scope(request).chats == {channel_id(1), 42}


def test_group_calls_need_a_known_chat():
    calls = CallMap()
    join = functions.phone.JoinGroupCallRequest(types.InputGroupCall(99, 1), types.InputPeerSelf(), types.DataJSON("{}"))
    assert scope(join, calls).unresolved_calls == {99}
    calls.learn(types.UpdateGroupCall(call=types.GroupCallDiscarded(99, 1, 0), peer=types.PeerChannel(5)))
    assert scope(join, calls).chats == {channel_id(5)}


def test_update_routing():
    calls = CallMap()
    assert update_chat(types.UpdateNewChannelMessage(channel_message(5, "hi"), 1, 1), calls=calls) == channel_id(5)
    assert update_chat(types.UpdateReadChannelInbox(5, 1, 1, 1), calls=calls) == channel_id(5)
    assert update_chat(types.UpdateDeleteMessages([1], 1, 1), calls=calls) is None
    assert update_chat(types.UpdateUserTyping(42, 0, types.SendMessageTypingAction()), calls=calls) == 42
