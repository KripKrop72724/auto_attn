"""ZKT 2.7.5: the custody-first boot storage repair; 2.7.6: its last-resort
in-place reformat.

The decisions that need no filesystem, the metadata mirror and the redo
records (codec, block plan, writer, boot recovery) run with plain cc in every
test shard. The engine runs against ESP-IDF 5.5.3's own SPIFFS with
power cuts (tools/check_spiffs_repair.py); CI's firmware-production job runs it
in espressif/idf:v5.5.3, and here it runs wherever the ESP-IDF sources are."""
from pathlib import Path
import importlib.util
import os
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"
FLAGS = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", "-fno-omit-frame-pointer"]


def _tool():
    spec = importlib.util.spec_from_file_location("check_spiffs_repair",
                                                  ROOT / "tests/firmware/tools/check_spiffs_repair.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan_and_mirror(tmp_path: Path) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    executable = tmp_path / "storage-repair-plan"
    subprocess.run([compiler, *FLAGS, "-I", str(MAIN), str(ROOT / "tests/firmware/storage_repair_plan_host.c"),
                    *(str(MAIN / name) for name in ("storage_repair_plan.c", "storage_repair_mirror.c",
                                                     "storage_repair_redo.c", "legacy_queue.c", "durable_queue.c", "reliability.c",
                                                     "zkt_clock.c")),
                    "-o", str(executable)], check=True)
    result = subprocess.run([str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "storage repair plan host tests passed" in result.stdout


def test_repair_against_esp_idf_spiffs_with_power_cuts(tmp_path: Path) -> None:
    tool = _tool()
    spiffs = tool.spiffs_source_dir()
    if spiffs is None:
        pytest.skip("ESP-IDF 5.5.3 SPIFFS sources are required (CI runs this in espressif/idf:v5.5.3)")
    binary = tool.build(spiffs, tmp_path)
    output = tool.run(binary, ["--seeds=2", "--organic=2", "--ticks=1200"])
    assert "cuts inside the repair" in output and "mirror equivalence passed" in output
    assert "0 committed rows lost" in output
    # 2.7.6: P02's store reformatted in place, confirmed and unconfirmed,
    # with cuts in the round and through the old image.
    assert "p02 1024 KiB unconfirmed: 2.7.5 DEFERRED (full, 1 page needed, 0 erased" in output and "through the old image" in output
    assert "reclaim mirror equivalence passed" in output
    # The round 1 review of the implementation: records keep only verified
    # cursors and an offset never settles bytes the file can get back (D1);
    # a boot without the area leaves them alone (low); a restore short of
    # room waits for the round (D2); a torn erase reading a valid magic is
    # redone (D3); sources without memory fail the run (low 6); the boot's
    # report covers every run (low 1); the remount keeps the mirror (low 4).
    assert "p02, a stale cursor: a cut in block 133's window (14 pages the queue's index names)" in output
    assert "t5 /processed_commands.txt freed by the check on a store without room: the round made room" in output
    assert "t5 /processed_commands.txt changed by the check on a store without room: the round made room" in output
    assert "torn magic: 6 erases cut short with the magic reading valid" in output
    assert "byte sources without memory for them: the run failed before writing" in output
    assert "2.7.6 first: the boot's report keeps the first run's strays, duplicate, cursor reset" in output
    assert "p02 confirmed, mirrored:" in output
    # The round 2 review: a block that will not verify is closed and the
    # round goes on (R2-1); a record recovery left open is that boot's source
    # (R2-2); a range is recorded once per boot (R2-3); a source bit that will
    # not take is journaled, never redone (R2-4); an ended entry is no source
    # for a later generation (note 1).
    assert "t5 /processed_commands.txt freed by the check, a worn cell in the first window, closed:" in output
    assert "t5 /processed_commands.txt freed by the check, a worn magic, the block not closed:" in output
    assert "open record, its settle word stuck: a cut in block 133's window" in output
    assert "open record, the journal lost: a cut in block 133's window" in output
    assert "one unreadable span over 2 runs of a boot: one gap record, 251 bytes in the boot's report" in output
    assert "source bit, attempts and journal refused: block 135 closed, the record never redone" in output
    assert "source generations, a reported entry over a filled span: the live bytes kept" in output
    assert "p02 confirmed, a worn first window:" in output and "a worn first window, old image:" in output
    # 2.7.7: the confirm-first exception on P02's field store: the gate on the
    # unconfirmed boot's own report, the confirmed round (same image tag), a
    # refusal with too few custody pages, the guard carried over, the redo
    # area's program/erase test through cuts, and cuts in the confirmed round.
    assert "p02field 1024 KiB hidden header: the gate ELIGIBLE; the confirmed boot reformatted" in output
    assert "p02field 1024 KiB custody short:" in output and "the gate refuses CUSTODY_SHORT" in output
    assert "the attempts guard's state carries over to the round boot (same image tag)" in output
    assert "the redo area test cut at each of its" in output
    assert "p02field confirmed:" in output and "custody byte-exact, no gap" in output
    # A refusal rolls back as 2.7.6: the unconfirmed 2.7.7 boot leaves 2.7.6's
    # store byte for byte, and the round boot takes nothing without the hold.
    assert "the unconfirmed 2.7.7 boot leaves 2.7.6's store byte for byte" in output
    assert "takes no block (short hold, retryable)" in output


def test_ci_runs_the_repair_harness_in_the_esp_idf_image() -> None:
    workflow = (ROOT / ".github/workflows/add-ci.yml").read_text()
    job = workflow[workflow.index("  firmware-production:"):workflow.index("  firmware-journal:")]
    step = job[job.index("Repair power-cut damage against ESP-IDF's own SPIFFS"):]
    step = step[:step.index("      - name:", 10)]
    assert "espressif/idf:v5.5.3" in step
    assert "python tests/firmware/tools/check_spiffs_repair.py" in step
