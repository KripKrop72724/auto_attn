from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_strict_terminal_clock_parser(tmp_path):
    main = ROOT / "firmware/zone_lite/main"
    unit = tmp_path / "clock.c"
    unit.write_text(r'''#include "hikvision_clock.h"
#include <assert.h>
int main(void){
 int64_t a,b;
 assert(hik_clock_parse("2026-09-18T15:41:23+05:00",&a));
 assert(hik_clock_parse("2026-09-18T10:41:23Z",&b));assert(a==b);
 assert(hik_clock_parse("2026-09-18T05:11:23-05:30",&b));assert(a==b);
 assert(hik_clock_parse("2024-02-29T00:00:00Z",&a));
 const char *bad[]={"2026-02-29T00:00:00Z","2026-09-31T00:00:00Z","2026-09-18T25:00:00Z",
 "2026-09-18T10:00:00","2026-09-18T10:00:00+15:00","2026-09-18T10:00:00+05:60",
 "2026-09-18T10:00:00+05:00junk","2026-09-18T10:00:00+14:01","2026-09-18T10:0x:00Z"};
 for(unsigned i=0;i<sizeof(bad)/sizeof(*bad);i++)assert(!hik_clock_parse(bad[i],&a));
 return 0;
}''')
    exe = tmp_path / "clock"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-I", str(main), str(unit),
                    str(main / "hikvision_clock.c"), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
