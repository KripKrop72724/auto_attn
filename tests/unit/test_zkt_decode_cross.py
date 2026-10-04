"""Independent production C/ADD facts and clock agreement on synthetic bytes."""
from datetime import datetime, timedelta
from pathlib import Path
import random
import shutil
import struct
import subprocess

import pytest

from zk_add.zkt_decode import DecodeError, PAKISTAN_TIME, decode_live_packet, decode_live_record, decode_source

ROOT = Path(__file__).resolve().parents[2]


def test_production_c_and_add_agree_without_using_either_encoder_for_fixtures(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    source = tmp_path / "cross.c"
    source.write_text(r'''
#include "zkt_record.h"
#include "zkt_clock.h"
#include "reliability.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
int main(void) {
    setenv("TZ", "UTC0", 1); tzset();
    unsigned kind, length; long long epoch; char hex[105];
    while (scanf("%u %u %lld %104s", &kind, &length, &epoch, hex) == 4) {
        assert(length <= 52 && strlen(hex) == 2 * length);
        unsigned char raw[52] = {0};
        for (unsigned i = 0; i < length; ++i) {
            unsigned byte; assert(sscanf(hex + 2 * i, "%2x", &byte) == 1); raw[i] = byte;
        }
        unsigned packed = 0; assert(zkt_clock_pack_pst((time_t)epoch, &packed));
        if (kind == 0) {
            zkt_record_t row; assert(zkt_record_decode(raw, length, &row));
            assert(row.encoded_time == packed);
            printf("%s %u %u %u %u\n", row.user_id[0] ? row.user_id : "-",
                row.encoded_time, row.status, row.punch, row.attendance_uid);
        } else {
            rel_live_record_t row; assert(rel_parse_live_record(raw, length, &row));
            assert(row.timestamp == packed);
            printf("%s %u %u %u 0\n", row.user_id, row.timestamp, row.status, row.punch);
        }
    }
    return 0;
}
''')
    exe = tmp_path / "cross"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra",
        "-Werror", "-fsanitize=address,undefined", "-I", str(main), str(source),
        *(str(main / name) for name in ("zkt_record.c", "reliability.c", "zkt_clock.c")),
        "-o", str(exe)], check=True)
    randomizer = random.Random(270)
    inputs, expected = [], []
    origin = datetime(2000, 1, 1, tzinfo=PAKISTAN_TIME)
    for _ in range(500):
        local = origin + timedelta(seconds=randomizer.randrange(36524 * 86400))
        # Wire integer directly from documented fields, not either implementation.
        encoded = (((((local.year - 2000) * 12 + local.month - 1) * 31 + local.day - 1)
                    * 24 + local.hour) * 60 + local.minute) * 60 + local.second
        uid, user = randomizer.randrange(65536), randomizer.randrange(2**32)
        status, punch = randomizer.randrange(256), randomizer.randrange(256)
        for kind, size in [(0, 8), (0, 16), (0, 40), (1, 12), (1, 32), (1, 36), (1, 52)]:
            raw = bytearray(size)
            if kind == 0:
                if size == 8:
                    struct.pack_into("<HBI", raw, 0, uid, status, encoded)
                    raw[7] = punch
                elif size == 16:
                    struct.pack_into("<IIBB", raw, 0, user, encoded, status, punch)
                else:
                    struct.pack_into("<H", raw, 0, uid)
                    name = str(user).encode()
                    raw[2:2 + len(name)] = name
                    raw[26] = status
                    struct.pack_into("<I", raw, 27, encoded)
                    raw[31] = punch
                facts = decode_source(bytes(raw), record_size=size)
            else:
                if size == 12:
                    struct.pack_into("<I", raw, 0, user)
                    base = 4
                else:
                    name = str(user).encode()
                    raw[:len(name)] = name
                    base = 24
                raw[base:base + 8] = bytes([status, punch, local.year - 2000, local.month,
                                            local.day, local.hour, local.minute, local.second])
                facts = decode_live_record(bytes(raw), record_size=size)
            assert facts.local_time == local and facts.utc_time.timestamp() == local.timestamp()
            inputs.append(f"{kind} {size} {int(local.timestamp())} {raw.hex()}")
            expected.append(f"{facts.user_id or '-'} {facts.encoded_time} {facts.status} {facts.punch} {facts.attendance_uid or 0}")
    result = subprocess.run([str(exe)], input="\n".join(inputs) + "\n", capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == expected


def test_c_and_add_reject_missing_historical_text_identity(tmp_path):
    """Synthetic zeros/spaces reproduce the field pattern without personal data."""
    main = ROOT / "firmware/zone_lite/main"
    unit = tmp_path / "invalid.c"
    unit.write_text(r'''
#include "zkt_record.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
int main(void) {
    char hex[81];
    while (scanf("%80s", hex) == 1) {
        unsigned char raw[40]; zkt_record_t record;
        assert(strlen(hex) == 80);
        for (unsigned i=0; i<40; ++i) {
            unsigned byte; assert(sscanf(hex + 2*i, "%2x", &byte) == 1); raw[i]=byte;
        }
        assert(!zkt_record_decode(raw, sizeof(raw), &record));
        printf("%u\n", zkt_record_identity_missing(raw, sizeof(raw)));
    }
    return 0;
}
''')
    exe = tmp_path / "invalid"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-I", str(main), str(unit),
                    str(main / "zkt_record.c"), "-o", str(exe)], check=True)
    rows, expected = [], []
    for field, missing in [(b"", True), (b" " * 24, True), (b" \0", True),
                           (b"\0garbage", True), (b"A\x01", False), (b"\xff", False)]:
        raw = bytearray(40)
        struct.pack_into("<H", raw, 0, 40)
        raw[2:2 + len(field)] = field
        struct.pack_into("<I", raw, 27, 859972462)
        with pytest.raises(DecodeError, match="INVALID_USER_REFERENCE"):
            decode_source(bytes(raw), record_size=40)
        rows.append(raw.hex())
        expected.append(str(int(missing)))
    result = subprocess.run([str(exe)], input="\n".join(rows) + "\n",
                            capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == expected


def test_c_and_add_reject_invalid_live_calendar_and_text_without_changing_bytes(tmp_path):
    """Negative vectors are explicit wire fields, not generated by either decoder."""
    main = ROOT / "firmware/zone_lite/main"
    unit = tmp_path / "invalid-live.c"
    unit.write_text(r'''
#include "reliability.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
int main(void) {
    unsigned kind,size; char hex[105];
    while (scanf("%u %u %104s", &kind,&size, hex) == 3) {
        unsigned char raw[52]={0}, original[52];rel_live_record_t record;
        assert(size<=52 && strlen(hex)==size*2);
        for(unsigned i=0;i<size;++i){unsigned byte;assert(sscanf(hex+2*i,"%2x",&byte)==1);raw[i]=byte;}
        memcpy(original,raw,sizeof(raw));
        size_t shape=0;
        printf("%u\n",kind?rel_live_frame_size(raw,size,0,&shape):rel_parse_live_record(raw,size,&record));
        assert(!memcmp(raw,original,sizeof(raw)));
    }
    return 0;
}
''')
    binary = tmp_path / "invalid-live"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra",
        "-Werror", "-fsanitize=address,undefined", "-I", str(main), str(unit),
        str(main / "reliability.c"), "-o", str(binary)], check=True)
    invalid_dates = [(26, 2, 30, 9, 0, 0), (25, 2, 29, 9, 0, 0), (26, 4, 31, 9, 0, 0),
        (100, 1, 1, 9, 0, 0), (99, 12, 31, 24, 0, 0), (26, 1, 1, 9, 60, 0),
        (26, 1, 1, 9, 0, 60), (26, 0, 1, 9, 0, 0), (26, 1, 0, 9, 0, 0)]
    valid_dates = [(0, 2, 29, 0, 0, 0), (24, 2, 29, 23, 59, 59), (99, 12, 31, 23, 59, 59)]
    inputs, expected = [], []
    def check(raw, accepted):
        before = bytes(raw)
        if accepted:
            decode_live_record(before, record_size=len(raw))
        else:
            with pytest.raises(DecodeError):
                decode_live_record(before, record_size=len(raw))
        assert bytes(raw) == before
        inputs.append(f"0 {len(raw)} {before.hex()}")
        expected.append(str(int(accepted)))
    for size in (12, 32, 36, 52):
        for date in invalid_dates + valid_dates:
            raw = bytearray(size)
            if size == 12:
                struct.pack_into("<I", raw, 0, 123)
                base = 4
            else:
                raw[:3] = b"123"
                base = 24
            raw[base:base+8] = bytes([7, 2, *date])
            check(raw, date in valid_dates)
        if size == 12:
            continue
        for name, accepted in [(b"A\x01", False), (b"A\t", False), (b"\xff", False),
            (b"A\n", False), (b"A\x7f", False), (b"", False), (b" "*24, False),
            (b"00123 ", True), (b"A!", True), (b"A\0\xff", True)]:
            raw = bytearray(size)
            raw[:len(name)] = name
            raw[24:32] = bytes([7, 2, 26, 10, 4, 9, 0, 0])
            check(raw, accepted)
    # All three compact records and the single extended record are valid;
    # neither implementation may choose one interpretation from equal length.
    ambiguous = bytearray(36)
    for offset in (0, 12, 24):
        ambiguous[offset] = 7
        ambiguous[offset+6:offset+12] = bytes([26, 9, 28, 14, 55, 17])
    ambiguous[0] = ord('7')
    ambiguous[27:30] = bytes([9, 1, 1])
    decode_live_record(bytes(ambiguous), record_size=36)
    for offset in (0, 12, 24):
        decode_live_record(bytes(ambiguous[offset:offset+12]), record_size=12)
    with pytest.raises(DecodeError, match="AMBIGUOUS_LIVE_LAYOUT"):
        decode_live_packet(struct.pack("<HHHH", 500, 4321, 23, 9)+ambiguous,
            allowed_sizes=frozenset({12, 36}), expected_session=23)
    inputs.append(f"1 36 {ambiguous.hex()}")
    expected.append("0")
    result = subprocess.run([str(binary)], input="\n".join(inputs)+"\n", capture_output=True,
        text=True, check=True)
    assert result.stdout.splitlines() == expected


def test_shared_calendar_fields_agree_with_independent_gregorian_dates(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    unit = tmp_path / "calendar.c"
    unit.write_text(r'''
#include "zkt_clock.h"
#include <stdio.h>
int main(void) {
    unsigned y,m,d,h,n,s;
    while(scanf("%u %u %u %u %u %u",&y,&m,&d,&h,&n,&s)==6)
        printf("%u\n",zkt_clock_fields_valid(y,m,d,h,n,s));
    return 0;
}
''')
    binary = tmp_path / "calendar"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-I", str(main), str(unit), "-o", str(binary)], check=True)
    dates = [(year, month, day, 9, 0, 0) for year in range(1999, 2101)
        for month in range(14) for day in range(33)]
    dates += [(2000, 1, 1, hour, minute, second) for hour in (0, 23, 24, 2**32-1)
        for minute in (0, 59, 60, 2**32-1) for second in (0, 59, 60, 2**32-1)]
    dates += [(0, 1, 1, 0, 0, 0), (2**32-1, 1, 1, 0, 0, 0)]
    expected = []
    for fields in dates:
        try:
            datetime(*fields)
            accepted = 2000 <= fields[0] <= 2099
        except (ValueError, OverflowError):
            accepted = False
        expected.append(str(int(accepted)))
    result = subprocess.run([str(binary)], input="\n".join(" ".join(map(str, row)) for row in dates)+"\n",
        capture_output=True, text=True, check=True, timeout=60)
    assert result.stdout.splitlines() == expected
