"""ZKT 2.7.5: the boot storage repair writes queue and transaction checkpoints
with the same helpers as their owners, so the formats are never duplicated."""
from pathlib import Path
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"

HARNESS = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "file_transaction.h"
#include "legacy_queue.h"

static ft_checkpoint_t stored;
static int stored_state; /* 0 absent, 1 present, -1 unreadable */
static unsigned commits;
static int load(void *context, ft_checkpoint_t *cp) { (void)context; if (stored_state == 1) *cp = stored; return stored_state; }
static bool commit(void *context, const ft_checkpoint_t *cp) { (void)context; stored = *cp; stored_state = 1; commits++; return true; }
static const ft_port_t port = {load, commit, NULL};

int main(int argc, char **argv)
{
    assert(argc == 2);
    assert(chdir(argv[1]) == 0);
    /* lq: seal, validity and the cleared successor lq_reclaim commits. */
    lq_checkpoint_t cp = {.version = 1, .generation = 7, .offset = 50055, .prefix_crc = 0x1234};
    assert(!lq_checkpoint_valid(&cp));
    lq_checkpoint_seal(&cp);
    assert(lq_checkpoint_valid(&cp));
    lq_checkpoint_t broken = cp;
    broken.offset++;
    assert(!lq_checkpoint_valid(&broken));
    broken = cp; broken.version = 3; lq_checkpoint_seal(&broken);
    assert(!lq_checkpoint_valid(&broken));
    broken = cp; broken.generation = 0; lq_checkpoint_seal(&broken);
    assert(!lq_checkpoint_valid(&broken));
    lq_checkpoint_t next;
    assert(lq_checkpoint_cleared(&cp, &next));
    assert(next.version == 1 && next.generation == 8 && !next.offset && !next.prefix_crc && lq_checkpoint_valid(&next));
    cp.generation = UINT32_MAX; lq_checkpoint_seal(&cp);
    assert(!lq_checkpoint_cleared(&cp, &next));

    /* ft: idle with the next generation; ft_recover then expects nothing. */
    stored_state = 0;
    assert(ft_checkpoint_idle(port) && stored.generation == 1 && stored.phase == 0 && ft_checkpoint_valid(&stored));
    stored.phase = 2; stored.length = 99; stored.digest = 5;
    stored.crc = dq_crc32(&stored, offsetof(ft_checkpoint_t, crc));
    assert(ft_checkpoint_valid(&stored));
    /* Phase 2 with every file gone cannot recover; idle can. */
    assert(!ft_recover("active", "stage", "backup", port));
    assert(ft_checkpoint_idle(port) && stored.generation == 2 && !stored.phase && !stored.length && !stored.digest);
    assert(ft_recover("active", "stage", "backup", port));
    /* A checkpoint that fails its CRC restarts at generation 1. */
    stored.generation = 40; stored.crc ^= 1;
    assert(!ft_checkpoint_valid(&stored));
    assert(ft_checkpoint_idle(port) && stored.generation == 1 && ft_checkpoint_valid(&stored));
    /* One NVS cannot read is never overwritten, and a generation never wraps. */
    unsigned before = commits;
    stored_state = -1;
    assert(!ft_checkpoint_idle(port) && commits == before);
    stored_state = 1; stored.generation = UINT32_MAX; stored.crc = dq_crc32(&stored, offsetof(ft_checkpoint_t, crc));
    assert(!ft_checkpoint_idle(port) && commits == before);
    puts("checkpoint helper tests passed");
    return 0;
}
'''


def test_queue_and_transaction_checkpoint_helpers(tmp_path: Path) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    source = tmp_path / "checkpoint_helpers.c"
    source.write_text(HARNESS)
    executable = tmp_path / "checkpoint-helpers"
    subprocess.run([compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(MAIN), str(source),
                    str(MAIN / "legacy_queue.c"), str(MAIN / "file_transaction.c"), str(MAIN / "durable_queue.c"),
                    "-o", str(executable)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    result = subprocess.run([str(executable), str(work)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "checkpoint helper tests passed" in result.stdout


def test_owners_use_the_shared_checkpoint_rules() -> None:
    queue = (MAIN / "legacy_queue.c").read_text()
    assert "if (loaded && !lq_checkpoint_valid(cp)) {" in queue
    assert "lq_checkpoint_seal(&next);" in queue[queue.index("static bool persist("):]
    transaction = (MAIN / "file_transaction.c").read_text()
    assert "return found == 0 || (found == 1 && ft_checkpoint_valid(cp));" in transaction
