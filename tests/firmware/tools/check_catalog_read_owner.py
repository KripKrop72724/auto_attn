"""Exercise actual restore, lookup and tombstone reads through the storage owner."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
main = ROOT / "firmware/zone_lite/main"
source = (main / "add_connector.c").read_text()
actual = source[source.index("/* Catalog stream adapter:"):
                source.index("static void recover_identity_catalog_backup_if_active_missing(")]
actual += source[source.index("static bool restore_valid_identity_catalog_locked("):
                 source.index("static bool restore_valid_identity_catalog(void)")]
actual += source[source.index("static cJSON *load_catalog_for_tombstone("):
                 source.index("static bool append_cancelled_command(")]
actual += source[source.index("static bool add_connector_lookup_identity_locked("):
                 source.index("uint32_t add_connector_identity_catalog_generation(")]
cjson = Path(os.environ["IDF_PATH"]) / "components/json/cJSON"
with tempfile.TemporaryDirectory() as directory:
    temporary = Path(directory)
    (temporary / "catalog_read_actual.inc").write_text(actual)
    binary = temporary / "catalog-read-owner"
    subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                    "-I", str(temporary), "-I", str(main), "-I", str(cjson),
                    str(ROOT / "tests/firmware/zkt_catalog_read_owner_host.c"), str(cjson / "cJSON.c"),
                    *(str(main / name) for name in ["zkt_catalog_client.c", "zkt_catalog_store.c",
                        "file_transaction.c", "durable_queue.c"]), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], cwd=temporary, check=True, timeout=60)
