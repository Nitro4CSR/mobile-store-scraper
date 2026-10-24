"""A tiny protobuf wire-format codec (no ``protobuf`` dependency, no generated code).

Messages are decoded into ``Msg`` objects: a mapping of field number -> list of raw values
(``int`` for varint/fixed, ``bytes`` for length-delimited, ``Msg`` for groups). Typed access
happens at read time (``msg.str(5)``, ``msg.msg(13).msg(1).int(3)``), which keeps us robust to
fields Google adds or drops: unknown fields are simply never looked at.
"""
from __future__ import annotations

import struct
from typing import Iterable, Iterator, Optional, Union

Value = Union[int, bytes, "Msg"]


class ProtoDecodeError(ValueError):
    pass


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoDecodeError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ProtoDecodeError("varint too long")


def _parse(buf: bytes, pos: int, end: int, group: Optional[int]) -> tuple["Msg", int]:
    fields: dict[int, list[Value]] = {}
    while pos < end:
        key, pos = _read_varint(buf, pos)
        num, wt = key >> 3, key & 7
        if wt == 0:
            val, pos = _read_varint(buf, pos)
        elif wt == 1:
            if pos + 8 > end:
                raise ProtoDecodeError("truncated fixed64")
            val = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
        elif wt == 2:
            ln, pos = _read_varint(buf, pos)
            if pos + ln > end:
                raise ProtoDecodeError("truncated bytes")
            val = buf[pos:pos + ln]
            pos += ln
        elif wt == 3:
            val, pos = _parse(buf, pos, end, num)
        elif wt == 4:
            if group != num:
                raise ProtoDecodeError("unexpected end-group")
            return Msg(fields), pos
        elif wt == 5:
            if pos + 4 > end:
                raise ProtoDecodeError("truncated fixed32")
            val = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        else:
            raise ProtoDecodeError(f"bad wire type {wt}")
        fields.setdefault(num, []).append(val)
    if group is not None:
        raise ProtoDecodeError("unterminated group")
    return Msg(fields), pos


class Msg:
    __slots__ = ("fields",)

    def __init__(self, fields: Optional[dict[int, list[Value]]] = None):
        self.fields = fields or {}

    @classmethod
    def decode(cls, data: bytes) -> "Msg":
        return _parse(bytes(data), 0, len(data), None)[0]

    def has(self, num: int) -> bool:
        return num in self.fields

    def raw(self, num: int) -> Optional[Value]:
        v = self.fields.get(num)
        return v[-1] if v else None  # protobuf: last occurrence wins

    def int(self, num: int, signed: bool = True) -> Optional[int]:
        v = self.raw(num)
        if not isinstance(v, int):
            return None
        if signed and v >= 1 << 63:
            v -= 1 << 64
        return v

    def bool(self, num: int) -> Optional[bool]:
        v = self.raw(num)
        return bool(v) if isinstance(v, int) else None

    def bytes(self, num: int) -> Optional[bytes]:
        v = self.raw(num)
        return v if isinstance(v, bytes) else None

    def str(self, num: int) -> Optional[str]:
        v = self.bytes(num)
        return v.decode("utf-8", "replace") if v is not None else None

    def strs(self, num: int) -> list[str]:
        return [v.decode("utf-8", "replace") for v in self.fields.get(num, []) if isinstance(v, bytes)]

    def msg(self, num: int) -> "Msg":
        """Sub-message (an empty ``Msg`` when absent, so chains never explode)."""
        v = self.raw(num)
        if isinstance(v, Msg):
            return v
        if isinstance(v, bytes):
            try:
                return Msg.decode(v)
            except ProtoDecodeError:
                return Msg()
        return Msg()

    def msgs(self, num: int) -> Iterator["Msg"]:
        for v in self.fields.get(num, []):
            if isinstance(v, Msg):
                yield v
            elif isinstance(v, bytes):
                try:
                    yield Msg.decode(v)
                except ProtoDecodeError:
                    continue

    def __bool__(self) -> bool:
        return bool(self.fields)

    def __repr__(self) -> str:
        return f"Msg({sorted(self.fields)})"


# --------------------------------------------------------------------------- encoding

def _varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class Fixed64(int):
    """Marker type: encode this int as fixed64 instead of varint."""


Field = tuple  # (field_number, value)


def encode(fields: Iterable[Field]) -> bytes:
    """Encode ``[(num, value), ...]``.

    ``value`` may be ``bool``/``int`` (varint), ``Fixed64`` (fixed64), ``str``/``bytes``
    (length-delimited), a nested list of fields (sub-message) or ``None`` (skipped).
    Repeat a field number to produce a repeated field.
    """
    out = bytearray()
    for num, value in fields:
        if value is None:
            continue
        if isinstance(value, Fixed64):
            out += _varint(num << 3 | 1) + struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF)
        elif isinstance(value, (bool, int)):
            out += _varint(num << 3) + _varint(int(value))
        elif isinstance(value, str):
            b = value.encode("utf-8")
            out += _varint(num << 3 | 2) + _varint(len(b)) + b
        elif isinstance(value, (bytes, bytearray)):
            out += _varint(num << 3 | 2) + _varint(len(value)) + bytes(value)
        elif isinstance(value, list):
            b = encode(value)
            out += _varint(num << 3 | 2) + _varint(len(b)) + b
        else:
            raise TypeError(f"cannot encode field {num} of type {type(value).__name__}")
    return bytes(out)
