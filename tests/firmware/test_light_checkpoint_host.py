"""Execute the light-check adapter with successful and failed durable commits."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_light_checkpoint_retains_live_evidence(tmp_path):
    source = (ROOT / "firmware/zone_lite/main/zone_lite.c").read_text()
    start = source.index("static bool commit_reconcile_count(")
    adapter = source[start:source.index("static void nvs_load_runtime_state", start)]
    program = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
static int32_t g_last_synced_attendance_count=100, durable=100;
static bool fail, recovery;
static unsigned writes;
static bool nvs_save_runtime_state(void) {
    writes++;
    if (fail) {g_last_synced_attendance_count=durable;recovery=true;return false;}
    durable=g_last_synced_attendance_count;return true;
}
""" + adapter + r"""
int main(void) {
 size_t live=3;
 fail=true;
 assert(!commit_reconcile_count(103,&live));
 assert(live==3 && durable==100 && g_last_synced_attendance_count==100 && recovery);
 fail=false;
 assert(commit_reconcile_count(103,&live));
 assert(live==0 && durable==103 && writes==2);
 assert(!commit_reconcile_count(105,NULL) && writes==2 && durable==103);
 return 0;
}
"""
    unit = tmp_path / "light.c"
    unit.write_text(program)
    executable = tmp_path / "light"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(unit), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
