"""ZKT 2.7.1: storage faults and the LED's LOCAL_FAILURE recover only after
verification, never on a timer, so one transient fault no longer withholds
boot health until the next reboot."""
from pathlib import Path
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"
FLAGS = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", "-fno-omit-frame-pointer"]


def _compiler() -> str:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    return compiler


def _run(executable: Path, *args: str) -> str:
    result = subprocess.run([str(executable), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_storage_faults_recover_after_a_fresh_audit_and_probe(tmp_path: Path) -> None:
    executable = tmp_path / "queue-recovery"
    subprocess.run([
        _compiler(), *FLAGS, "-I", str(ROOT / "tests/firmware/queue_recovery_host"), "-I", str(MAIN),
        str(ROOT / "tests/firmware/queue_recovery_host.c"), str(MAIN / "storage_budget.c"),
        str(MAIN / "legacy_storage_health.c"), "-o", str(executable),
    ], check=True)
    root = tmp_path / "root"
    root.mkdir()
    assert "queue recovery host tests passed" in _run(executable, str(root))


def test_local_failure_clears_only_after_a_later_verification(tmp_path: Path) -> None:
    executable = tmp_path / "local-recovery"
    subprocess.run([
        _compiler(), *FLAGS, "-I", str(MAIN), str(ROOT / "tests/firmware/local_recovery_host.c"),
        str(MAIN / "local_recovery.c"), "-o", str(executable),
    ], check=True)
    assert "local recovery host tests passed" in _run(executable)


def test_supervisor_runs_recovery_and_boot_health_still_requires_it() -> None:
    app = (MAIN / "zone_lite.c").read_text()
    loop = app[app.index("        if (g_queue_store_ready) {\n            bool recovered = qs_recover_step();"):]
    loop = loop[:loop.index("zj_runtime_step();")]
    # With a fault pending the probe is the recovery check, then the LED step runs.
    assert "} else if (qs_health().last_error) {" in loop
    assert loop.index("(void)qs_verify_persistence();") < loop.index("local_failure_recovery_step();")
    step = app[app.index("static void local_failure_recovery_step(void)"):]
    step = step[:step.index("\n}\n")]
    assert ".workers_healthy = add_connector_delivery_healthy()," in step
    assert "local_recovery_requested(&state, qs_request_verification());" in step
    assert "led_status_clear_local_failure(incidents)" in step
    assert "esp_timer" not in step and "now_ms" not in step  # never on a timer
    # Boot health is unchanged: a latched LED or an unrecovered fault still withholds it.
    connector = (MAIN / "add_connector.c").read_text()
    ready = connector[connector.index("bool add_connector_boot_health_ready(void)"):]
    ready = ready[:ready.index("\n}\n")]
    assert "return add_connector_boot_health_blockers() == 0;" in ready
    # ZKT 2.7.7: the same terms, each named; ready exactly when none fails.
    ready = connector[connector.index("uint32_t add_connector_boot_health_blockers(void)\n{"):]
    ready = ready[:ready.index("\n}\n")]
    assert 'storage_health.last_error' in ready and '!strcmp(led, "LOCAL_FAILURE")' in ready
