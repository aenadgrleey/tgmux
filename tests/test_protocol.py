import pytest
from telethon import errors
from telethon.tl import functions, types

from tgmux import protocol
from tgmux.errors import ChatNotOwnedError, ProtocolError

from fakes import NOW, channel, user


def roundtrip(request, result):
    return protocol.parse_result(request, protocol.serialize_result(request, result))


def test_request_roundtrip():
    request = functions.messages.SendMessageRequest(types.InputPeerChannel(5, 50), "hi", random_id=1)
    parsed = protocol.parse_request(bytes(request))
    assert bytes(parsed) == bytes(request)


def test_parse_request_rejects_non_requests():
    with pytest.raises(ProtocolError):
        protocol.parse_request(bytes(types.InputPeerSelf()))


def test_object_result():
    request = functions.channels.JoinChannelRequest(types.InputChannel(5, 50))
    result = types.Updates(updates=[], users=[user(1)], chats=[channel(5)], date=NOW, seq=0)
    assert bytes(roundtrip(request, result)) == bytes(result)


def test_vector_of_objects_and_bool():
    users = [user(1), user(2)]
    assert [bytes(u) for u in roundtrip(functions.users.GetUsersRequest([types.InputUserSelf()]), users)] == [bytes(u) for u in users]
    assert roundtrip(functions.messages.SetTypingRequest(types.InputPeerSelf(), types.SendMessageTypingAction()), True) is True


@pytest.mark.parametrize("request_obj, values", [
    (functions.contacts.GetContactIDsRequest(hash=0), [1, -2, 3]),
    (functions.messages.ReceivedQueueRequest(max_qts=1), [2**40, 7]),
])
def test_int_vectors(request_obj, values):
    assert roundtrip(request_obj, values) == values


def test_errors_keep_their_class_and_capture():
    request = functions.help.GetConfigRequest()
    for exc in (errors.FloodWaitError(request, capture=30), errors.FileMigrateError(request, capture=4)):
        rebuilt = protocol.error_from_wire(*protocol.error_to_wire(exc), request)
        assert type(rebuilt) is type(exc)
    assert protocol.error_from_wire(*protocol.error_to_wire(errors.FloodWaitError(request, capture=30)), request).seconds == 30
    assert type(protocol.error_from_wire(*protocol.error_to_wire(errors.ChannelPrivateError(request)), request)) is errors.ChannelPrivateError


def test_gateway_errors_roundtrip():
    rebuilt = protocol.error_from_wire(*protocol.error_to_wire(ChatNotOwnedError(None, "chat 1")), None)
    assert isinstance(rebuilt, ChatNotOwnedError)
    assert isinstance(rebuilt, errors.ForbiddenError)
