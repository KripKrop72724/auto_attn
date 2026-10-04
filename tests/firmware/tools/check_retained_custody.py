"""Run the actual ADD outbox worker with receipt loss and pinned cJSON faults."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
main = ROOT / "firmware/zone_lite/main"
source = (main / "add_connector.c").read_text()
types = source[source.index("typedef struct {\n    const char *path;"):
               source.index("typedef struct {\n    char *data;\n    size_t length;")]
worker = source[source.index("static bool preserve_retained_outbox("):
                source.index("static void delivery_supervisor_task(")]
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    (temporary / "retained_types_actual.inc").write_text(types)
    (temporary / "retained_worker_actual.inc").write_text(worker)
    for owner in (0, 1):
        binary = temporary / f"retained-worker-{owner}"
        subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", f"-DZONE_LITE_QUEUE_OWNER={owner}",
                        "-I", str(temporary), "-I", str(main), "-I", str(cjson),
                        str(ROOT / "tests/firmware/zkt_retained_custody_host.c"), str(cjson / "cJSON.c"),
                        str(main / "reliability.c"), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=30)
