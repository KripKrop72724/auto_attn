"""The priority-delivery hold ends with its deadline at any uptime.

A 32-bit millisecond deadline compared as a signed difference read as a hold
from 24.9 to 49.7 days of uptime when it was never set, and again 24.9 days
after any deadline. Each outbox pass then waited 3 s before one delivery and
put background lanes ahead of live punches until the next restart."""
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_priority_hold_never_returns_after_long_uptime(tmp_path: Path) -> None:
    source = (ROOT / "firmware/zone_lite/main/add_connector.c").read_text()
    declaration = re.search(r"^static [^\n]*\bs_priority_delivery_until_ms;$", source, re.M)
    assert declaration
    start = source.index("static bool priority_delivery_hold_active(void)")
    end = source.index("\n}\n", start) + 3
    # Every deadline is the full 64-bit uptime plus a timeout.
    writes = re.findall(r"atomic_store\(&s_priority_delivery_until_ms, ([^;]*)\);", source)
    assert len(writes) == 3 and all(write.startswith("monotonic_ms() +") for write in writes)
    assert source.count("s_priority_delivery_until_ms") == 2 + len(writes)
    harness = r'''
#include <assert.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define DAY_MS (24LL * 60 * 60 * 1000)
static int64_t now_ms;
static int64_t monotonic_ms(void){return now_ms;}
/* PRODUCTION */
static bool hold_at(int64_t uptime_ms){now_ms=uptime_ms;return priority_delivery_hold_active();}
int main(void)
{
    /* Never set since boot: no hold at any uptime. */
    const int64_t uptimes[]={0,DAY_MS,(1LL<<31)-1,(1LL<<31)+1,25*DAY_MS,30*DAY_MS,(1LL<<32)-1,
                             (1LL<<32)+1,75*DAY_MS,400*DAY_MS};
    for(size_t i=0;i<sizeof(uptimes)/sizeof(uptimes[0]);++i)assert(!hold_at(uptimes[i]));
    /* A deadline holds until it passes, and never again. */
    s_priority_delivery_until_ms=DAY_MS+3000;
    assert(hold_at(DAY_MS)&&hold_at(DAY_MS+2999)&&!hold_at(DAY_MS+3000));
    assert(!hold_at(DAY_MS+(1LL<<31)+1)&&!hold_at(26*DAY_MS)&&!hold_at(DAY_MS+(1LL<<32)-1));
    /* Across the 32-bit millisecond boundary. */
    s_priority_delivery_until_ms=(1LL<<32)+1000;
    assert(hold_at((1LL<<32)-1000)&&hold_at((1LL<<32)+999)&&!hold_at((1LL<<32)+1000));
    assert(!hold_at((1LL<<32)+(1LL<<31)+1000)&&!hold_at((1LL<<33)+999));
    return 0;
}
'''
    unit = tmp_path / "hold.c"
    unit.write_text(harness.replace(
        "/* PRODUCTION */", declaration.group(0) + "\n" + source[start:end]))
    executable = tmp_path / "hold"
    subprocess.run([
        shutil.which("cc"), "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(unit), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
