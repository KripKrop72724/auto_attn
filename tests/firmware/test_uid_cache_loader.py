"""Compile the actual startup cache loader; fragments cannot establish dedup."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_startup_dedup_requires_complete_rows(tmp_path: Path):
    firmware = ROOT / "firmware/zone_lite/main"
    source = (firmware / "zone_lite.c").read_text()
    start = source.index("static bool extract_event_uid(")
    end = source.index("static bool file_has_nonempty_line(", start)
    unit = tmp_path / "loader.c"
    unit.write_text(r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "reliability.h"
#include "uid_cache.h"
#define MAX_EVENT_JSON 256
#define pdMS_TO_TICKS(x) (x)
#define vTaskDelay(x) ((void)(x))
static uint8_t keys[32*8], occupied[1];
static uid_cache_t cache = {keys,occupied,8,0};
static bool seen_add(const char *uid) { return uid_cache_add(&cache,uid); }
''' + source[start:end] + r'''
int main(void) {
    char good[65], malformed[65], oversized[65], partial[65], acknowledged[65];
    snprintf(good,sizeof(good),"%064x",1);
    snprintf(malformed,sizeof(malformed),"%064x",2);
    snprintf(oversized,sizeof(oversized),"%064x",3);
    snprintf(partial,sizeof(partial),"%064x",4);
    snprintf(acknowledged,sizeof(acknowledged),"%064x",5);
    FILE *f=fopen("rows","w"); assert(f);
    fprintf(f,"{\"event_uid\":\"%s\",}\n",malformed);
    fprintf(f,"{\"event_uid\":\"%s\",\"padding\":\"",oversized);
    for(unsigned i=0;i<600;++i) fputc('x',f);
    fputs("\"}\n",f);
    fprintf(f,"{\"event_uid\":\"%s\"}\n",good);
    fprintf(f,"%s\n",acknowledged);
    fprintf(f,"{\"event_uid\":\"%s\"}",partial);
    assert(!fclose(f));
    load_seen_from_file("rows");
    assert(cache.count==2);
    assert(uid_cache_contains(&cache,good));
    assert(uid_cache_contains(&cache,acknowledged));
    assert(!uid_cache_contains(&cache,malformed));
    assert(!uid_cache_contains(&cache,oversized));
    assert(!uid_cache_contains(&cache,partial));
    return 0;
}
''')
    compiler = shutil.which("cc")
    assert compiler
    executable = tmp_path / "loader"
    subprocess.run([
        compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-I", str(firmware), str(unit),
        str(firmware / "reliability.c"), str(firmware / "uid_cache.c"),
        "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True)
