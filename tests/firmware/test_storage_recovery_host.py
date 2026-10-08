"""Execute the one-shot storage recovery custody engine with sanitizers."""
from pathlib import Path
import os
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_storage_recovery_transfers_every_row_before_retirement(tmp_path: Path) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    assert compiler, "A C compiler is required for the firmware regression gate"
    firmware = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "storage-recovery"
    subprocess.run([
        compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-g", "-O1",
        "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer", "-I", str(firmware),
        # Inject unreadable byte ranges into the engine's own reads. glibc's
        # fortified fread is an inline wrapper that would bypass the rename.
        "-U_FORTIFY_SOURCE",
        "-Dfread=sr_test_fread", "-Dferror=sr_test_ferror", "-Dfclose=sr_test_fclose",
        "-Dfseek=sr_test_fseek",
        str(firmware / "storage_recovery_core.c"), str(firmware / "reliability.c"),
        str(ROOT / "tests/firmware/storage_recovery_host.c"), "-o", str(executable),
    ], check=True)
    result = subprocess.run([str(executable)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "storage recovery custody tests passed" in result.stdout


def test_recovery_image_never_confirms_itself_or_writes_an_ota_journal() -> None:
    source = (ROOT / "firmware/zone_lite/main/ota_manager.c").read_text()
    start = source.index("static bool storage_recovery_boot(void)")
    end = source.index("#endif", start)
    body = source[start:end]
    assert "esp_ota_mark_app_valid_cancel_rollback" not in body
    assert "save_journal" not in body and "clear_journal" not in body
    assert "esp_ota_mark_app_invalid_rollback_and_reboot" in body
    assert 'report_state("FAILED", code)' in body


def test_recovery_image_only_targets_the_two_peshawar_connectors() -> None:
    source = (ROOT / "firmware/zone_lite/main/storage_recovery.c").read_text()
    assert source.count('{"') == 2
    assert '"bf4badc7-5f9c-42aa-8b3a-8a43f8daeb5e", {0xe0, 0x72, 0xa1, 0xd7, 0x05, 0xc4}' in source
    assert '"233dac02-eb1b-4598-a876-e3a7b1ecfd54", {0xe0, 0x72, 0xa1, 0xd5, 0x08, 0xa0}' in source
    assert 'ug_direct_predecessor_matches("2.5.2", digest)' in source
    assert "ESP_OTA_IMG_PENDING_VERIFY" in source and "ESP_OTA_IMG_VALID" in source
    assert ".format_if_mount_failed = false" in source
