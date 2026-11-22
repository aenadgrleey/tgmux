"""Wire protocol between SharedClient and the gateway.

Frame: ``u32 length | u8 kind | payload``. Requests, results and updates are
carried as raw TL bytes, so both sides reuse Telethon's own (de)serializers.
"""
from __future__ import annotations

import asyncio
import enum
import functools
import inspect
import struct
from typing import Any

from telethon import errors as tl_errors
from telethon.errors import RPCError, rpcerrorlist
from telethon.extensions import BinaryReader
from telethon.tl import types
from telethon.tl.tlobject import TLObject, TLRequest

from tgmux.errors import GATEWAY_ERRORS, ProtocolError

PROTOCOL_VERSION = 1
MAX_FRAME = 64 * 1024 * 1024
HELLO_TIMEOUT = 10

_HEADER = struct.Struct("<IB")
_CALL = struct.Struct("<Qi")
_RESULT = struct.Struct("<Q")
_ERROR = struct.Struct("<Qi")

_VECTOR = 0x1CB5C415
_BOOL_TRUE = 0x997275B5
_BOOL_FALSE = 0xBC799737


class Frame(enum.IntEnum):
    HELLO = 1    # c->s json {"integration", "version", "updates"}
    WELCOME = 2  # s->c json {"self_id", "version"}
    CALL = 3     # c->s u64 call_id, i32 dc_id (0 = main), TL request
    RESULT = 4   # s->c u64 call_id, TL result
    ERROR = 5    # s->c u64 call_id, i32 code, utf-8 message
    UPDATE = 6   # s->c TL types.Updates
    FATAL = 7    # s->c utf-8 reason, then close


def encode_frame(kind: Frame, payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME:
        raise ProtocolError(f"frame too large: {len(payload)}")
    return _HEADER.pack(len(payload), kind) + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[Frame, bytes]:
    length, kind = _HEADER.unpack(await reader.readexactly(_HEADER.size))
    if length > MAX_FRAME:
        raise ProtocolError(f"frame too large: {length}")
    try:
        kind = Frame(kind)
    except ValueError as exc:
        raise ProtocolError(f"unknown frame kind {kind}") from exc
    return kind, await reader.readexactly(length)


def encode_call(call_id: int, dc_id: int, request: bytes) -> bytes:
    return _CALL.pack(call_id, dc_id) + request


def decode_call(payload: bytes) -> tuple[int, int, bytes]:
    if len(payload) < _CALL.size:
        raise ProtocolError("short CALL frame")
    call_id, dc_id = _CALL.unpack_from(payload)
    return call_id, dc_id, payload[_CALL.size:]


def encode_result(call_id: int, result: bytes) -> bytes:
    return _RESULT.pack(call_id) + result


def decode_result(payload: bytes) -> tuple[int, bytes]:
    if len(payload) < _RESULT.size:
        raise ProtocolError("short RESULT frame")
    (call_id,) = _RESULT.unpack_from(payload)
    return call_id, payload[_RESULT.size:]


def encode_error(call_id: int, code: int, message: str) -> bytes:
    return _ERROR.pack(call_id, code) + message.encode()


def decode_error(payload: bytes) -> tuple[int, int, str]:
    if len(payload) < _ERROR.size:
        raise ProtocolError("short ERROR frame")
    call_id, code = _ERROR.unpack_from(payload)
    return call_id, code, payload[_ERROR.size:].decode(errors="replace")


# --- TL values -------------------------------------------------------------

def parse_request(data: bytes) -> TLRequest:
    with BinaryReader(data) as reader:
        obj = reader.tgread_object()
    if not isinstance(obj, TLRequest):
        raise ProtocolError(f"expected a TL request, got {type(obj).__name__}")
    return obj


def parse_object(data: bytes) -> Any:
    with BinaryReader(data) as reader:
        return reader.tgread_object()


def parse_result(request: TLRequest, data: bytes) -> Any:
    with BinaryReader(data) as reader:
        return request.read_result(reader)


@functools.cache
def _int_vector_format(request_cls: type) -> str | None:
    """Telethon only overrides read_result for Vector<int> / Vector<long>."""
    if "read_result" not in request_cls.__dict__:
        return None
    source = inspect.getsource(request_cls.read_result)
    if "read_long()" in source:
        return "<q"
    if "read_int() for" in source:
        return "<i"
    return None


def _serialize(value: Any, int_format: str | None) -> bytes:
    if isinstance(value, bool):
        return struct.pack("<I", _BOOL_TRUE if value else _BOOL_FALSE)
    if isinstance(value, TLObject):
        return bytes(value)
    if isinstance(value, list):
        parts = [struct.pack("<Ii", _VECTOR, len(value))]
        for item in value:
            if isinstance(item, int) and not isinstance(item, bool):
                if int_format is None:
                    raise TypeError("integer vector for a request without an int read_result")
                parts.append(struct.pack(int_format, item))
            else:
                parts.append(_serialize(item, int_format))
        return b"".join(parts)
    raise TypeError(f"cannot serialize result of type {type(value).__name__}")


def serialize_result(request: TLRequest, result: Any) -> bytes:
    return _serialize(result, _int_vector_format(type(request)))


# --- errors ----------------------------------------------------------------

_ERROR_NAMES = {cls: name for name, cls in rpcerrorlist.rpc_errors_dict.items()}
_ERROR_PATTERNS = {cls: pattern for pattern, cls in rpcerrorlist.rpc_errors_re}


def error_to_wire(exc: RPCError) -> tuple[int, str]:
    """Recover the original Telegram error string so the client rebuilds the same class."""
    cls = type(exc)
    code = exc.code if isinstance(exc.code, int) else 500
    if cls in _ERROR_NAMES:
        return code, _ERROR_NAMES[cls]
    if cls in _ERROR_PATTERNS:
        capture = exc.__reduce__()[1][1]
        return code, _ERROR_PATTERNS[cls].replace(r"(\d+)", str(capture))
    return code, exc.message or "UNKNOWN"


def error_from_wire(code: int, message: str, request: TLRequest | None) -> RPCError:
    if message in GATEWAY_ERRORS:
        return GATEWAY_ERRORS[message](request)
    return tl_errors.rpc_message_to_error(types.RpcError(code, message), request)
