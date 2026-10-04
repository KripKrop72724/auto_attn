"""Exercise actual blocked identity/evidence consumers with pinned cJSON."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
main = ROOT / "firmware/zone_lite/main"
source = (main / "zone_lite.c").read_text()
end = source.index("} user_table_t;") + len("} user_table_t;")
start = source.rfind("typedef struct {", 0, source.index("} zkt_user_t;"))
actual = source[start:end]
actual += source[source.index("static const zkt_user_t *find_user_by_user_id("):
                 source.index("static const zkt_user_t *find_user_by_uid(")]
actual += source[source.index("static bool recover_blocked_events_from_snapshot("):
                 source.index("static bool g_queue_store_ready;")]
actual += source[source.index("static char *g_blocked_drain_buffer;"):
                 source.index("/* Old quarantine generations carry evidence")]
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    (temporary / "blocked_consumers_actual.inc").write_text(actual)
    for owner in (0, 1):
        binary = temporary / f"blocked-consumers-{owner}"
        subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", f"-DZONE_LITE_QUEUE_OWNER={owner}",
                        "-I", str(temporary), "-I", str(main), "-I", str(cjson),
                        str(ROOT / "tests/firmware/zkt_blocked_owner_host.c"), str(cjson / "cJSON.c"),
                        str(main / "reliability.c"), str(main / "durable_queue.c"), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=30)
