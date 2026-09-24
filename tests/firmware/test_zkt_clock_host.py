from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_zkt_clock_pakistan_time_roundtrip(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    unit = tmp_path / "zkt-clock.c"
    unit.write_text(r'''#include "zkt_clock.h"
#include <assert.h>
#include <stdlib.h>
#include <time.h>
int main(void) {
    setenv("TZ", "UTC0", 1); tzset();
    time_t cases[] = {1704067200, 1735685999, 1735686000, 1798761599, 1798761600};
    for (unsigned i=0; i<sizeof(cases)/sizeof(*cases); ++i) {
        uint32_t packed=0;
        assert(zkt_clock_pack_pst(cases[i], &packed));
        struct tm local={0};
        uint32_t raw=packed;
        local.tm_sec=raw%60;raw/=60;
        local.tm_min=raw%60;raw/=60;
        local.tm_hour=raw%24;raw/=24;
        local.tm_mday=raw%31+1;raw/=31;
        local.tm_mon=raw%12;raw/=12;
        local.tm_year=(int)raw+100;
        time_t readback=0;
        assert(zkt_clock_pst_to_utc(&local,&readback));
        assert(readback==cases[i]);
    }
    struct tm invalid={.tm_year=126,.tm_mon=1,.tm_mday=30};
    time_t unused=0;
    assert(!zkt_clock_pst_to_utc(&invalid,&unused));
    assert(!zkt_clock_pack_pst(946684800-18001,&(uint32_t){0}));
    return 0;
}''')
    exe = tmp_path / "zkt-clock"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L",
                    "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                    "-I", str(main), str(unit), str(main / "zkt_clock.c"), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
