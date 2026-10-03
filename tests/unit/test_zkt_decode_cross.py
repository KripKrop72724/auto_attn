"""Independent production C/ADD facts and clock agreement on synthetic bytes."""
from datetime import datetime, timedelta
from pathlib import Path
import random
import shutil
import struct
import subprocess

from zk_add.zkt_decode import PAKISTAN_TIME, decode_live_record, decode_source

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
