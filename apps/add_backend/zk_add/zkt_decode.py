"""Interpret preserved ZKT bytes without resolving identity or inventing scope.

Callers must separately qualify the model, terminal and exact allowed layouts.
A model label, plausible date or packet length is not qualification evidence.
The input bytes stay in custody; these immutable facts are derived evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import struct

from zk_add.zkt_packet import PACKET_MAX

PAKISTAN_TIME = timezone(timedelta(hours=5))
SOURCE_SIZES = frozenset({8, 16, 40})
LIVE_SIZES = frozenset({12, 32, 36, 52})
MODEL_PROFILES = {
    "G3": "zkt-g3-v1",
    "SilkBio-101TC/ID": "zkt-silkbio-101tc-id-v1",
    "MB40-VL/ID": "zkt-mb40-vl-id-v1",
    "uFace800": "zkt-uface800-v1",
    "uFace800/ID": "zkt-uface800-id-v1",
    "uFace800 Plus/ID": "zkt-uface800-plus-id-v1",
}
DECODER_VERSION = "zkt-facts-1"


class DecodeError(ValueError):
    """A safe fixed category, never raw source bytes or employee identifiers."""


@dataclass(frozen=True)
class PunchFacts:
    user_id: str | None
    attendance_uid: int | None
    encoded_time: int
    local_time: datetime
    utc_time: datetime
    status: int
    punch: int
    offset: int
    length: int


@dataclass(frozen=True)
class LivePacket:
    session_id: int
    reply_id: int
    checksum: int
    record_size: int
    records: tuple[PunchFacts, ...]


def encode_time(local: datetime) -> int:
    if local.utcoffset() != timedelta(hours=5):
        raise DecodeError("TERMINAL_LOCAL_TIME_REQUIRED")
    if not 2000 <= local.year <= 2099 or local.microsecond:
        raise DecodeError("TIME_OUTSIDE_PROFILE")
    value = local.year - 2000
    for multiplier, component in ((12, local.month - 1), (31, local.day - 1),
                                  (24, local.hour), (60, local.minute), (60, local.second)):
        value = value * multiplier + component
    return value


def decode_time(encoded: int) -> tuple[datetime, datetime]:
    if type(encoded) is not int or not 0 <= encoded <= 0xFFFFFFFF:
        raise DecodeError("ENCODED_TIME_RANGE")
    value = encoded
    parts = []
    for divisor in (60, 60, 24, 31, 12):
        value, component = divmod(value, divisor)
        parts.append(component)
    second, minute, hour, day, month = parts
    try:
        local = datetime(2000 + value, month + 1, day + 1, hour, minute, second,
                         tzinfo=PAKISTAN_TIME)
    except ValueError as exc:
        raise DecodeError("INVALID_CALENDAR_DATE") from exc
    if encode_time(local) != encoded:
        raise DecodeError("TIME_OUTSIDE_PROFILE")
    return local, local.astimezone(timezone.utc)


def _text_id(raw: bytes) -> str:
    value = raw.split(b"\0", 1)[0].rstrip(b" ")
    if not value or any(byte < 32 or byte > 126 for byte in value):
        raise DecodeError("INVALID_USER_REFERENCE")
    return value.decode("ascii")


def _facts(user_id: str | None, attendance_uid: int | None, encoded: int,
           status: int, punch: int, offset: int, length: int) -> PunchFacts:
    local, utc = decode_time(encoded)
    return PunchFacts(user_id, attendance_uid, encoded, local, utc, status, punch, offset, length)


def decode_source(raw: bytes, *, record_size: int, offset: int = 0) -> PunchFacts:
    if record_size not in SOURCE_SIZES or len(raw) != record_size or offset < 0:
        raise DecodeError("SOURCE_RECORD_BOUNDARY")
    # The 8/40-byte historical UID is an attendance field. It is never silently
    # joined to the current enrollment table, including when user_id is absent.
    attendance_uid = int.from_bytes(raw[:2], "little") if record_size != 16 else None
    if record_size == 8:
        return _facts(None, attendance_uid, int.from_bytes(raw[3:7], "little"), raw[2], raw[7], offset, 8)
    if record_size == 16:
        return _facts(str(int.from_bytes(raw[:4], "little")), None,
                      int.from_bytes(raw[4:8], "little"), raw[8], raw[9], offset, 16)
    return _facts(_text_id(raw[2:26]), attendance_uid, int.from_bytes(raw[27:31], "little"),
                  raw[26], raw[31], offset, 40)


def decode_live_record(raw: bytes, *, record_size: int, offset: int = 0) -> PunchFacts:
    if record_size not in LIVE_SIZES or len(raw) != record_size or offset < 0:
        raise DecodeError("LIVE_RECORD_BOUNDARY")
    base = 4 if record_size == 12 else 24
    user_id = str(int.from_bytes(raw[:4], "little")) if base == 4 else _text_id(raw[:24])
    year, month, day, hour, minute, second = raw[base + 2:base + 8]
    try:
        local = datetime(2000 + year, month, day, hour, minute, second, tzinfo=PAKISTAN_TIME)
    except ValueError as exc:
        raise DecodeError("INVALID_CALENDAR_DATE") from exc
    return _facts(user_id, None, encode_time(local), raw[base], raw[base + 1], offset, record_size)


def decode_live_packet(packet: bytes, *, allowed_sizes: frozenset[int],
                       expected_session: int | None = None) -> LivePacket:
    """A complete stored ZK packet includes its eight-byte protocol header.

    Never guess a batched layout from length. When more than one allowed
    interpretation succeeds, all original bytes stay held for source recovery.
    A checksum is retained as evidence; this module does not claim that it has
    authenticated a network sender or validated a model's checksum convention.
    """
    if not 8 < len(packet) <= PACKET_MAX:
        raise DecodeError("LIVE_PACKET_BOUNDARY")
    if not allowed_sizes or not allowed_sizes <= LIVE_SIZES:
        raise DecodeError("UNSUPPORTED_LIVE_PROFILE")
    command, checksum, session, reply = struct.unpack_from("<HHHH", packet)
    if command != 500 or not session or (expected_session is not None and session != expected_session):
        raise DecodeError("LIVE_PACKET_SESSION_OR_COMMAND")
    body = packet[8:]
    valid = []
    for size in sorted(allowed_sizes):
        if len(body) % size:
            continue
        try:
            rows = tuple(decode_live_record(body[start:start + size], record_size=size, offset=8 + start)
                         for start in range(0, len(body), size))
        except DecodeError:
            continue
        valid.append((size, rows))
        if len(valid) > 1:
            raise DecodeError("AMBIGUOUS_LIVE_LAYOUT")
    if not valid:
        raise DecodeError("NO_VALID_LIVE_LAYOUT")
    size, rows = valid[0]
    return LivePacket(session, reply, checksum, size, rows)
