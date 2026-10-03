"""Lossless reassembly of immutable journal packet evidence, before decoding."""
from dataclasses import dataclass
import hashlib
import struct

PACKET_MAX = 65536
FRAGMENT_HEADER = 60
FRAGMENT_DATA = 512 - FRAGMENT_HEADER


@dataclass(frozen=True)
class PacketFragment:
    group: bytes
    total: int
    offset: int
    packet_digest: bytes
    data: bytes


def parse_fragment(raw: bytes) -> PacketFragment:
    if not FRAGMENT_HEADER < len(raw) <= 512 or raw[:4] != b"ZJF1":
        raise ValueError("INVALID_PACKET_FRAGMENT")
    total, offset = struct.unpack_from("<II", raw, 20)
    data = raw[FRAGMENT_HEADER:]
    if (not any(raw[4:20]) or not 512 < total <= PACKET_MAX or offset >= total
            or offset % FRAGMENT_DATA or len(data) != min(FRAGMENT_DATA, total - offset)):
        raise ValueError("INVALID_PACKET_EXTENT")
    return PacketFragment(raw[4:20], total, offset, raw[28:60], data)


def reassemble(fragments: list[PacketFragment]) -> bytes | None:
    """An incomplete packet is a hold, not a partial attendance record.

    Callers must group by authenticated connector, terminal, capture epoch and
    fragment group. Exact repeated chunks are harmless; conflicts fail closed.
    The input list and reconstructed packet have explicit memory bounds.
    """
    if not fragments or len(fragments) > 2 * ((PACKET_MAX + FRAGMENT_DATA - 1) // FRAGMENT_DATA):
        raise ValueError("INVALID_FRAGMENT_SET")
    first = fragments[0]
    if (not 512 < first.total <= PACKET_MAX or len(first.group) != 16
            or not any(first.group) or len(first.packet_digest) != 32):
        raise ValueError("INVALID_PACKET_EXTENT")
    offsets: dict[int, bytes] = {}
    for part in fragments:
        if ((part.group, part.total, part.packet_digest) != (first.group, first.total, first.packet_digest)
                or part.offset < 0 or part.offset >= first.total or part.offset % FRAGMENT_DATA
                or len(part.data) != min(FRAGMENT_DATA, first.total - part.offset)):
            raise ValueError("CONFLICTING_PACKET_FRAGMENTS")
        if part.offset in offsets and offsets[part.offset] != part.data:
            raise ValueError("CONFLICTING_PACKET_FRAGMENTS")
        offsets[part.offset] = part.data
    if len(offsets) != (first.total + FRAGMENT_DATA - 1) // FRAGMENT_DATA:
        return None
    packet = b"".join(offsets[offset] for offset in range(0, first.total, FRAGMENT_DATA))
    if hashlib.sha256(packet).digest() != first.packet_digest:
        raise ValueError("PACKET_DIGEST_MISMATCH")
    return packet
