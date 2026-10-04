"""Run the production command-inbox adapter with pinned cJSON and real files."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
main = ROOT / "firmware/zone_lite/main"
source = (main / "add_connector.c").read_text()
start = source.index("static zc_client_t s_command_storage_client")
actual = "#if 1\n" + source[start:source.index("static bool queue_command_if_idle(", start)]
actual += source[source.index("bool add_connector_command_complete("):
                 source.index("static bool add_connector_lookup_identity_locked(")]
actual += source[source.index("static void restore_command_inbox(void)\n{"):
                 source.index("static void parse_inbound(")]
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    (temporary / "command_actual.inc").write_text(actual)
    executable = temporary / "command-owner"
    subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
                    "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                    "-fno-omit-frame-pointer", "-I", str(temporary), "-I", str(main), "-I", str(cjson),
                    str(ROOT / "tests/firmware/zkt_command_owner_host.c"), str(cjson / "cJSON.c"),
                    *(str(main / name) for name in ["zkt_catalog_client.c", "zkt_catalog_store.c",
                        "file_transaction.c", "durable_queue.c"]), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=temporary, check=True, timeout=60)
