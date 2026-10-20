"""Repair power-cut damage against ESP-IDF's own SPIFFS on the host (ZKT 2.7.5).

Builds tests/firmware/spiffs_repair_host.c with ESP-IDF 5.5.3's SPIFFS sources,
the boot repair engine and the firmware's own legacy queue, file transaction
and append code (through a stdio shim onto the emulated flash), then runs every
damage scenario, the metadata mirror equivalence, resumes, restores and guards,
power cuts inside the repair and the power-cut simulator's workload long enough
to run the store crammed (about 70% live data, every block holding deleted
pages). CI runs it in espressif/idf:v5.5.3; the runs go in parallel."""
from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MAIN = ROOT / "firmware/zone_lite/main"
HOST = ROOT / "tests/firmware"
SPIFFS_SOURCES = ["spiffs_nucleus.c", "spiffs_gc.c", "spiffs_hydrogen.c", "spiffs_cache.c", "spiffs_check.c"]
SHIMMED = ["legacy_queue.c", "file_transaction.c", "reliability.c", "command_inbox_merge.c"]
PLAIN = ["durable_queue.c", "zkt_clock.c", "storage_repair_plan.c", "storage_repair_mirror.c"]
FLAGS = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-fsanitize=address,undefined",
         "-fno-omit-frame-pointer"]
# The CI run: 1 MiB stores, every scenario with several seeds, the special
# and review runs and cuts inside the repair; more cut seeds; the simulator's
# workload to a crammed store (odd seeds also retire and recreate the queue),
# on the 8 MB store too; then the scenarios on the 8 MB store.
CI_RUNS = [["--seeds=3", "--organic=0"], ["cuts", "--seeds=12", "--first-seed=3"],
           ["organic", "--organic=3", "--ticks=2500", "--first-seed=0"],
           ["organic", "--organic=3", "--ticks=2500", "--first-seed=3", "--gap=150"],
           # Seed 832 once ended with SPIFFS_check deleting a consistent queue
           # over a half-deleted hidden header's lookup entry (1.2 item 7).
           ["organic", "--organic=1", "--ticks=2500", "--first-seed=821"],
           # The 8 MB store crammed (74 to 77% used, no free block, deleted
           # pages spread thin): seeds 14 (one of the third boot review's
           # failing seeds 11 to 15, finding 1) and 16 left a queue unreadable
           # and every boot deferred until the reserve rule took the erased
           # pages the work fits in. Seed 16 then ends wedged (no erased page
           # at all; section 12 of the design): its rows are checked with room.
           ["organic", "--organic=1", "--ticks=3000", "--prefill=70", "--gap=2500", "--kib=8192", "--first-seed=3"],
           ["organic", "--organic=1", "--ticks=3000", "--prefill=70", "--gap=2500", "--kib=8192", "--first-seed=5"],
           ["scenarios", "--seeds=1", "--kib=8192"]]


def spiffs_source_dir() -> Path | None:
    idf = Path(os.environ.get("IDF_PATH", Path.home() / "esp/esp-idf-v5.5.3"))
    source = idf / "components/spiffs/spiffs/src"
    return source if (source / "spiffs_check.c").is_file() else None


def build(spiffs: Path, output: Path) -> Path:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required"
    config = HOST / "spiffs_host"
    objects = []

    def compile_one(source: Path, *extra: str) -> None:
        target = output / (source.stem + ".o")
        subprocess.run([compiler, *FLAGS, *extra, "-c", str(source), "-o", str(target)], check=True)
        objects.append(str(target))

    for source in SPIFFS_SOURCES:
        # Upstream SPIFFS is compiled as ESP-IDF compiles it, warnings relaxed;
        # its index tables are unaligned by design.
        compile_one(spiffs / source, "-fno-sanitize=alignment", "-w", "-I", str(config), "-I", str(spiffs))
    strict = ["-Wall", "-Wextra", "-Werror", "-I", str(MAIN)]
    for source in SHIMMED:
        compile_one(MAIN / source, *strict, "-include", str(HOST / "spiffs_repair_host/stdio_shim.h"))
    for source in PLAIN:
        compile_one(MAIN / source, *strict)
    spiffs_includes = ["-fno-sanitize=alignment", "-I", str(config), "-I", str(spiffs)]
    compile_one(MAIN / "storage_repair_core.c", *strict, *spiffs_includes)
    compile_one(HOST / "spiffs_repair_host.c", *strict, *spiffs_includes)
    binary = output / "spiffs-repair"
    subprocess.run([compiler, "-fsanitize=address,undefined", *objects, "-o", str(binary)], check=True)
    return binary


def run(binary: Path, arguments: list[str]) -> str:
    # GCC's ASan on Linux checks stack-use-after-return by default, and its
    # fake stack frames make this harness about a hundred times slower
    # (fourteen minutes instead of seconds). That one check is off here; heap,
    # global and in-scope stack checks and UBSan stay on.
    options = os.environ.get("ASAN_OPTIONS", "")
    environment = dict(os.environ, ASAN_OPTIONS=":".join(filter(None, [options, "detect_stack_use_after_return=0"])))
    result = subprocess.run([str(binary), *arguments], capture_output=True, text=True, env=environment)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    assert "spiffs repair host tests passed" in result.stdout, result.stdout[-4000:]
    return result.stdout


def main(argv: list[str]) -> int:
    spiffs = spiffs_source_dir()
    if spiffs is None:
        print("ESP-IDF SPIFFS sources not found (set IDF_PATH)", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="spiffs-repair-") as temporary:
        binary = build(spiffs, Path(temporary))
        runs = [argv] if argv else CI_RUNS
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(runs), os.cpu_count() or 1))) as pool:
            for output in pool.map(lambda arguments: run(binary, arguments), runs):
                print(output, end="")
    print("SPIFFS repair verified against ESP-IDF SPIFFS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
