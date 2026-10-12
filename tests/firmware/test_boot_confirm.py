"""ZKT 2.7.1: a new image is judged on the time it is accountable for, not on
a site outage, within a hard cap."""
from pathlib import Path
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "firmware/zone_lite/main"


def test_confirmation_clock_pauses_for_the_site_and_caps_everything(tmp_path: Path) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    executable = tmp_path / "boot-confirm"
    subprocess.run([
        compiler, "-std=c11", "-g", "-O1", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-I", str(MAIN), str(ROOT / "tests/firmware/boot_confirm_host.c"),
        str(MAIN / "boot_confirm.c"), "-o", str(executable),
    ], check=True)
    result = subprocess.run([str(executable)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "boot confirmation host tests passed" in result.stdout


def test_legacy_confirmation_uses_the_clock_and_reports_why_it_rolled_back() -> None:
    ota = (MAIN / "ota_manager.c").read_text()
    legacy = ota[ota.index("static bool confirm_or_report_rollback(void)"):]
    legacy = legacy[:legacy.index("\n}\n")]
    assert "_Static_assert(BOOT_CONFIRM_ACTIVE_SECONDS == OTA_BOOT_CONFIRM_SECONDS" in legacy
    assert ".local_failure = add_connector_boot_local_failure()," in legacy
    assert ".environment_ready = add_connector_boot_environment_ready()," in legacy
    assert "if (verdict == BOOT_CONFIRM_EXPIRED || verdict == BOOT_CONFIRM_CAPPED) break;" in legacy
    assert 'verdict == BOOT_CONFIRM_CAPPED ? "BOOT_CONFIRM_CAP" : "BOOT_HEALTH_TIMEOUT"' in legacy
    # ADD still acknowledges health before the image is marked valid.
    healthy = legacy.index('report_state("BOOTED_PENDING", "RUNTIME_HEALTHY")')
    assert healthy < legacy.index("esp_ota_mark_app_valid_cancel_rollback()", healthy)
    assert '"boot_confirm_active_seconds"' in ota and '"boot_confirm_paused_seconds"' in ota
    connector = (MAIN / "add_connector.c").read_text()
    environment = connector[connector.index("bool add_connector_boot_environment_ready(void)"):]
    environment = environment[:environment.index("\n}\n")]
    assert "ready = s_connected && s_zkt.online;" in environment
