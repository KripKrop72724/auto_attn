"""Synthetic facts are codec tests, never installed-model qualification."""
from datetime import datetime, timedelta, timezone
import struct

import pytest

from zk_add.zkt_decode import (DecodeError, MODEL_PROFILES, PAKISTAN_TIME, decode_live_packet,
                               decode_live_record, decode_source, decode_time, encode_time)


LOCAL = datetime(2026, 10, 3, 9, 14, 22, tzinfo=PAKISTAN_TIME)
# Calculated fixture value is independent of the implementation's encoder.
ENCODED = 859972462


def live(size, *, year=26, month=10, day=3, hour=9, minute=14, second=22, user=123):
    raw = bytearray(size)
    if size == 12:
        struct.pack_into("<I", raw, 0, user)
        base = 4
    else:
        value = str(user).encode()
        raw[:len(value)] = value
        base = 24
    raw[base:base + 8] = bytes([7, 2, year, month, day, hour, minute, second])
    return bytes(raw)


def packet(body, *, command=500, session=23):
    return struct.pack("<HHHH", command, 4321, session, 9) + body


@pytest.mark.parametrize("size", [8, 16, 40])
def test_source_layouts_preserve_facts_and_do_not_join_historical_uid(size):
    raw = bytearray(size)
    if size == 8:
        struct.pack_into("<HBI", raw, 0, 456, 7, ENCODED)
        raw[7] = 2
    elif size == 16:
        struct.pack_into("<IIBB", raw, 0, 123, ENCODED, 7, 2)
    else:
        struct.pack_into("<H", raw, 0, 456)
        raw[2:5] = b"123"
        raw[26] = 7
        struct.pack_into("<I", raw, 27, ENCODED)
        raw[31] = 2
    original = bytes(raw)
    facts = decode_source(original, record_size=size, offset=104)
    assert facts.user_id == (None if size == 8 else "123")
    assert facts.attendance_uid == (None if size == 16 else 456)
    assert facts.local_time == LOCAL
    assert facts.utc_time == datetime(2026, 10, 3, 4, 14, 22, tzinfo=timezone.utc)
    assert (facts.status, facts.punch, facts.encoded_time, facts.offset, facts.length) == (7, 2, ENCODED, 104, size)
    assert bytes(raw) == original


@pytest.mark.parametrize("size", [12, 32, 36, 52])
def test_live_layouts_decode_only_explicitly_allowed_profile(size):
    raw = live(size)
    facts = decode_live_record(raw, record_size=size)
    assert facts.user_id == "123" and facts.attendance_uid is None and facts.local_time == LOCAL
    result = decode_live_packet(packet(raw + raw), allowed_sizes=frozenset({size}), expected_session=23)
    assert result.record_size == size and len(result.records) == 2
    assert result.records[0].offset == 8 and result.records[1].offset == 8 + size
    # Identical same-second records remain two observations; there is no set/dedup.
    assert result.records[0].encoded_time == result.records[1].encoded_time
    assert (result.session_id, result.reply_id, result.checksum) == (23, 9, 4321)


@pytest.mark.parametrize("date", [
    datetime(2000, 1, 1, tzinfo=PAKISTAN_TIME),
    datetime(2000, 2, 29, 23, 59, 59, tzinfo=PAKISTAN_TIME),
    datetime(2024, 2, 29, tzinfo=PAKISTAN_TIME),
    datetime(2038, 1, 19, 10, 1, 1, tzinfo=PAKISTAN_TIME),
    datetime(2099, 12, 31, 23, 59, 59, tzinfo=PAKISTAN_TIME),
])
def test_calendar_and_timezone_boundaries(date):
    local, utc = decode_time(encode_time(date))
    assert local == date and local.utcoffset() == timedelta(hours=5)
    assert utc.utcoffset() == timedelta(0)
    assert utc == date


@pytest.mark.parametrize("fields", [dict(month=0), dict(month=13), dict(day=0), dict(day=32),
    dict(month=2, day=29, year=25), dict(month=2, day=31), dict(month=4, day=31),
    dict(hour=24), dict(minute=60), dict(second=60), dict(year=100)])
def test_invalid_calendar_is_never_normalized_into_a_valid_punch(fields):
    with pytest.raises(DecodeError):
        decode_live_record(live(32, **fields), record_size=32)
    with pytest.raises(DecodeError, match="NO_VALID_LIVE_LAYOUT"):
        decode_live_packet(packet(live(32, **fields)), allowed_sizes=frozenset({32}))


@pytest.mark.parametrize("encoded", [-1, 2**32, True, 0xFFFFFFFF, 0.5, "123"])
def test_encoded_time_rejects_wrong_type_and_outside_profile(encoded):
    with pytest.raises(DecodeError):
        decode_time(encoded)


def test_encoded_february_31_remains_invalid():
    impossible = (((((26 * 12 + 1) * 31 + 30) * 24 + 9) * 60 + 14) * 60 + 22)
    with pytest.raises(DecodeError, match="INVALID_CALENDAR_DATE"):
        decode_time(impossible)


@pytest.mark.parametrize("size", [0, 7, 12, 32, 39, 41, 512])
def test_source_unknown_sizes_preserved_for_another_decoder(size):
    with pytest.raises(DecodeError, match="SOURCE_RECORD_BOUNDARY"):
        decode_source(bytes(size), record_size=size)


def test_source_length_must_match_qualified_record_size():
    with pytest.raises(DecodeError, match="SOURCE_RECORD_BOUNDARY"):
        decode_source(bytes(41), record_size=40)


@pytest.mark.parametrize("body", [b"", live(32)[:-1], live(32) + b"x", bytes(65536)])
def test_truncated_or_oversized_packet_cannot_be_partially_interpreted(body):
    with pytest.raises(DecodeError):
        decode_live_packet(packet(body), allowed_sizes=frozenset({32}))


@pytest.mark.parametrize("args", [dict(command=1), dict(session=0), dict(session=22)])
def test_wrong_command_or_session_is_rejected(args):
    with pytest.raises(DecodeError, match="LIVE_PACKET_SESSION_OR_COMMAND"):
        decode_live_packet(packet(live(32), **args), allowed_sizes=frozenset({32}), expected_session=23)


@pytest.mark.parametrize("allowed", [frozenset(), frozenset({10}), frozenset({32, 99})])
def test_unqualified_lengths_are_not_silently_added(allowed):
    with pytest.raises(DecodeError, match="UNSUPPORTED_LIVE_PROFILE"):
        decode_live_packet(packet(live(32)), allowed_sizes=allowed)


@pytest.mark.parametrize("prefix", [b"", b"\x01", b"\xff", b" "])
def test_empty_control_or_nonascii_identity_is_a_hold(prefix):
    raw = bytearray(live(32))
    raw[:24] = prefix.ljust(24, b"\0")
    with pytest.raises(DecodeError, match="INVALID_USER_REFERENCE"):
        decode_live_record(bytes(raw), record_size=32)


def test_all_installed_models_have_distinct_selectors_without_qualification_claim():
    assert len(MODEL_PROFILES) == len(set(MODEL_PROFILES.values())) == 6
    assert "unsupported" not in MODEL_PROFILES


def test_ambiguous_packet_waits_even_when_both_dates_are_plausible():
    raw = bytearray(live(12, user=65) * 3)
    raw[24:32] = bytes([7, 2, 26, 10, 3, 9, 14, 7])
    value = packet(bytes(raw))
    assert len(decode_live_packet(value, allowed_sizes=frozenset({12})).records) == 3
    assert len(decode_live_packet(value, allowed_sizes=frozenset({36})).records) == 1
    with pytest.raises(DecodeError, match="AMBIGUOUS_LIVE_LAYOUT"):
        decode_live_packet(value, allowed_sizes=frozenset({12, 36}))


@pytest.mark.parametrize("value", [LOCAL.replace(tzinfo=None), LOCAL.astimezone(timezone.utc)])
def test_encoder_requires_explicit_terminal_local_timezone(value):
    with pytest.raises(DecodeError, match="TERMINAL_LOCAL_TIME_REQUIRED"):
        encode_time(value)
