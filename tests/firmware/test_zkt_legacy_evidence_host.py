"""ZKT 2.7.7: the OTA guard accepts ZONE-SLICTOWER-3FL's bridge-era journal
leftovers, narrowly (zkt_legacy_evidence.c), and nothing else.

The decision runs with plain cc under ASan and UBSan; the guard's wiring is
read from the sources."""
from pathlib import Path
import os
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"
FLAGS = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-fno-sanitize-recover=all"]


def test_legacy_evidence_decisions(tmp_path: Path) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    executable = tmp_path / "legacy-evidence"
    subprocess.run([compiler, *FLAGS, "-I", str(MAIN), str(ROOT / "tests/firmware/zkt_legacy_evidence_host.c"),
                    *(str(MAIN / name) for name in ("zkt_legacy_evidence.c", "storage_repair_plan.c",
                                                     "legacy_queue.c", "durable_queue.c", "reliability.c",
                                                     "zkt_clock.c")),
                    "-o", str(executable)], check=True)
    result = subprocess.run([str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "zkt legacy evidence host tests passed" in result.stdout


def _code(source: str) -> str:
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))


def test_the_decision_has_no_esp_idf_dependency() -> None:
    for name in ("zkt_legacy_evidence.c", "zkt_legacy_evidence.h"):
        includes = [line.split()[1] for line in (MAIN / name).read_text().splitlines() if line.startswith("#include")]
        assert set(includes) <= {"<stdbool.h>", "<stddef.h>", "<stdint.h>", "<stdio.h>", "<string.h>",
                                 '"zkt_legacy_evidence.h"', '"durable_queue.h"', '"storage_repair_plan.h"'}, includes


def test_the_guard_accepts_only_on_storage_repair_images_and_after_the_floor() -> None:
    guard = _code((MAIN / "zkt_ota_guard.c").read_text())
    repair, legacy = guard.split("#else\nstatic const char *legacy_evidence(void)", 1)
    # Journal roles and the recovery image keep the pre-2.7.7 refusal of any namespace.
    assert 'return "JOURNAL_LEGACY_EVIDENCE_PRESENT";' in legacy.split("#endif", 1)[0]
    assert "zle_decide(&inventory)" in repair
    # Read-only: the guard never writes NVS or the store, never removes a file.
    for forbidden in ("NVS_READWRITE", "nvs_set_", "nvs_erase", "unlink(", "remove(", "rename(", '"wb"', '"ab"'):
        assert forbidden not in guard, forbidden
    # The OTA floor is checked before any legacy evidence.
    download = guard[guard.index("const char *zj_ota_before_download("):]
    assert download.index('return "STORAGE_REDO_FLOOR"') < download.index("return legacy_evidence();")
    # The journal runtime predicate is zkt_journal_runtime.c's mode().
    assert "strcmp(app->version, ZJ_BRIDGE_VERSION) && strcmp(app->version, ZJ_WRITER_VERSION)" in repair
    assert "inventory.owner_started = zj_owner_started();" in repair
    # An unmounted store: any journal name the boot survey saw still refuses.
    assert 'else if (journal_names) error = "JOURNAL_LEGACY_EVIDENCE_PRESENT";' in repair
    # The root's key material is erased after classification.
    assert "wipe[i] = 0" in repair
    runtime = (MAIN / "zkt_journal_runtime.c").read_text()
    assert "if (!strcmp(app->version, ZJ_BRIDGE_VERSION)) return ZJ_BOOT_BRIDGE;" in runtime
    assert "if (!strcmp(app->version, ZJ_WRITER_VERSION)) return ZJ_BOOT_WRITER;" in runtime


def test_the_root_layout_matches_the_journal_state() -> None:
    state = (MAIN / "zkt_journal_state.c").read_text()
    assert '!memcmp(bytes, "ZJROOT01", 8)' in state
    assert "bytes[145] <= 1" in state and "!present(bytes + 146, 10)" in state
    assert "(bytes[145] == 1 || limit == 1)" in state
    assert "return state->root[145] == 1 ? ZJ_AUTHORITY_ADD : ZJ_AUTHORITY_LEGACY;" in state
    # Legacy authority never reserves a sequence, so no segment can hold a record.
    assert "if (zj_state_authority(state) != ZJ_AUTHORITY_ADD || exclusive_limit <= state->limit" in state
    assert "#define ZJ_ROOT_BYTES 160U" in (MAIN / "zkt_journal_state.h").read_text()
    store = (MAIN / "zkt_journal_store.h").read_text()
    assert "#define ZJ_SEGMENT_BYTES (64U * 1024U)" in store
    assert "#define ZJ_META_BYTES 240U" in (MAIN / "zkt_journal_codec.h").read_text()
    # The only keys a journal image writes in the namespace.
    names = set(re.findall(r'port\.(?:read|write)\(port\.context, "([a-z_0-9]+)"', state))
    assert names == {"root", "retirement", "reader_v1"}, names


def test_the_heartbeat_carries_the_inventory() -> None:
    connector = (MAIN / "add_connector.c").read_text()
    assert "!storage_repair_append(storage) || !zj_ota_append_legacy_evidence(storage)" in connector
    ota = (MAIN / "ota_manager.c").read_text()
    assert "(void)zj_ota_precheck_cached();" in ota
    cmake = (MAIN / "CMakeLists.txt").read_text()
    assert '"zkt_legacy_evidence.c"' in cmake
