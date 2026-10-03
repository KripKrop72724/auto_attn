import hashlib
import struct

import pytest

from zk_add.zkt_packet import FRAGMENT_DATA, PacketFragment, parse_fragment, reassemble


def parts(packet):
    return [parse_fragment(b"ZJF1" + bytes(range(1, 17)) + struct.pack("<II", len(packet), offset)
                          + hashlib.sha256(packet).digest() + packet[offset:offset + FRAGMENT_DATA])
            for offset in range(0, len(packet), FRAGMENT_DATA)]


@pytest.mark.parametrize("size", [513, 904, 4096, 65536])
def test_complete_packet_reassembles_out_of_order_and_on_replay(size):
    packet = bytes((7 + i * 23) % 256 for i in range(size))
    fragments = parts(packet)
    assert reassemble(list(reversed(fragments)) + [fragments[0]]) == packet
    assert reassemble(fragments[:-1]) is None


def test_conflicting_extents_and_wrong_digest_are_not_attendance():
    fragments = parts(b"a" * 600)
    first = fragments[0]
    conflicting = PacketFragment(first.group, first.total, first.offset, first.packet_digest, b"b" * len(first.data))
    with pytest.raises(ValueError, match="CONFLICTING"):
        reassemble(fragments + [conflicting])
    with pytest.raises(ValueError, match="DIGEST_MISMATCH"):
        reassemble([conflicting, fragments[1]])
    with pytest.raises(ValueError, match="FRAGMENT_SET"):
        reassemble([first] * 1000)


@pytest.mark.parametrize("total,offset,size", [(512, 0, 452), (65537, 0, 452), (600, 1, 452),
                                             (600, 0, 451), (600, 600, 1), (600, 452, 149)])
def test_fragment_extent_is_validated_before_any_reassembly(total, offset, size):
    with pytest.raises(ValueError):
        parse_fragment(b"ZJF1" + bytes(range(1, 17)) + struct.pack("<II", total, offset)
                       + bytes(32) + bytes(size))
