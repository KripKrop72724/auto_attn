"""Independent synthetic wire fixtures. These are not terminal-model evidence."""
import base64
import hashlib
import json
import struct


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def vectors():
    serial = "TEST01"
    epoch = bytes(range(16)).hex()
    sequence = 2**63 - 1
    result = []
    for index, length in enumerate([1, 2, 3, 512, 40, 512, 20, 512, 12]):
        raw = bytes(position % 256 for position in range(length))
        if index == 7:
            packet = bytes(position % 256 for position in range(600))
            raw = (b"ZJF1" + bytes(range(1, 17)) + struct.pack("<II", len(packet), 0)
                   + hashlib.sha256(packet).digest() + packet[:452])
        raw_digest = hashlib.sha256(raw).hexdigest()
        value = dict(capture_epoch=epoch, capture_sequence=str(sequence),
                     captured_at="2023-11-14T22:13:20Z", captured_at_seconds="1700000000",
                     captured_uptime_ms=str(2**64 - 1), decoder_profile="zkt-g3-v1", decoder_version="1",
                     encoded_time=2**32 - 1, identity_snapshot="7",
                     observation_id=digest(["zkt-observation-v1", serial, epoch, sequence]),
                     raw_b64=base64.b64encode(raw).decode(), raw_digest=raw_digest,
                     raw_format="LIVE_FRAME", terminal_serial=serial, time_quality="UNSYNCED")
        # The bytes change across cases, so independent tests must not submit
        # all these mutually exclusive synthetic states as one real capture.
        if index == 4:
            value.update(raw_format="SOURCE_RECORD", occurrence={
                "ordinal": 200000, "source_epoch": "01020304-0506-0708-090a-0b0c0d0e0f10"})
        if index == 5:
            value = dict(capture_epoch=epoch, end_offset=66012, exception_kind="AUTH",
                         item_type="JOURNAL_EXCEPTION",
                         observation_id=digest(["zkt-journal-exception-v1", serial, epoch,
                                                sequence, 65500, 66012, raw_digest]),
                         raw_b64=base64.b64encode(raw).decode(), raw_digest=raw_digest,
                         segment_id=str(sequence), start_offset=65500, terminal_serial=serial)
        if index >= 6:
            value["raw_format"] = "PACKET_FRAGMENT" if index == 7 else "LIVE_PACKET"
        if index == 8:
            value.update(captured_at=None, captured_at_seconds=str(2**63 - 1))
        result.append(value)
    return result
