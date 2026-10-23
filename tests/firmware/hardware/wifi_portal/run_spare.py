"""Run only the MAC-bound unsigned portal regression, never fleet provisioning."""
import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import esptool
import serial
from esptool.targets.esp32s3 import ESP32S3ROM

MAC = "14:c1:9f:46:ae:1c"
BACKUP_SHA256 = "cd0f30ac9491da499eaf2167f07b5accf23cf6779c506110431aa766f346273e"
STORAGE_START = 0x7A0000
STORAGE_SIZE = 0x800000


def connect(port):
    esp = ESP32S3ROM(port, 115200)
    esp.connect("usb-reset")
    mac = ":".join(f"{v:02x}" for v in esp.read_mac())
    if mac != MAC:
        esp._port.close()
        raise RuntimeError("Wrong board: refusing all flash writes")
    security = esp.get_security_info(cache=False)
    if (security["flags"] or security["flash_crypt_cnt"]
            or any(security["key_purposes"][:6])):
        esp._port.close()
        raise RuntimeError("Board is no longer blank-security; refusing test")
    esp = esp.run_stub()
    esptool.cmds.attach_flash(esp)
    esp.flash_set_parameters(0x1000000)
    return esp, security


def storage_digests(esp):
    return [esp.flash_md5sum(offset, 0x100000).lower()
            for offset in range(STORAGE_START, STORAGE_START + STORAGE_SIZE, 0x100000)]


def boot_application(esp):
    # Native USB can retain a forced download state across an RTS reset.
    # Clear the volatile flag and use IDF/esptool's supported watchdog reset.
    esp.write_reg(esp.RTC_CNTL_OPTION1_REG, 0, esp.RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK)
    esp.watchdog_reset()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--initialize-storage-fixture", action="store_true")
    parser.add_argument("--expect-empty-ap", type=int, choices=(0, 1), default=0)
    args = parser.parse_args()
    backup = args.backup.read_bytes()
    if len(backup) != 0x1000000 or hashlib.sha256(backup).hexdigest() != BACKUP_SHA256:
        raise RuntimeError("Verified original full-flash backup is required")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    files = [(0, "bootloader/bootloader.bin"),
             (0x10000, "partition_table/partition-table.bin"),
             (0x17000, "ota_data_initial.bin"),
             (0x20000, "wifi_portal_spare_test.bin")]
    images = [(offset, (args.image_dir / name).read_bytes()) for offset, name in files]
    if b"wifi_portal_spare_test\x00" not in images[-1][1]:
        raise RuntimeError("Not the test-only harness application")
    for offset, data in images:
        if offset + len(data) > 0x2A0000:
            raise RuntimeError("Image exceeds harness flash bounds")
    # Reset only test NVS. Preserve the backed-up original for full restoration.
    images.append((0x11000, b"\xff" * 0x6000))
    fixture = bytearray(b"\xff" * STORAGE_SIZE)
    sentinel = hashlib.sha256(b"ADD spare raw attendance preservation fixture v1").digest() * 128
    fixture[:4096] = sentinel
    fixture[-4096:] = sentinel[::-1]
    expected = [hashlib.md5(fixture[i:i+0x100000]).hexdigest()
                for i in range(0, STORAGE_SIZE, 0x100000)]
    esp, before_security = connect(args.port)
    if args.initialize_storage_fixture:
        if storage_digests(esp) != [hashlib.md5(b"\xff" * 0x100000).hexdigest()] * 8:
            raise RuntimeError("Storage is not erased; refusing fixture initialization")
        esptool.cmds.write_flash(esp, [(STORAGE_START, bytes(fixture[:4096])),
                                    (STORAGE_START + STORAGE_SIZE - 4096, bytes(fixture[-4096:]))],
                                flash_size="16MB")
    before_storage = storage_digests(esp)
    if before_storage != expected:
        raise RuntimeError("Storage fixture differs; refusing harness flash")
    esptool.cmds.write_flash(esp, sorted(images), flash_mode="dio",
                            flash_freq="80m", flash_size="16MB")
    # Explicit readback hash of every flashed range before running the image.
    for offset, data in images:
        if esp.flash_md5sum(offset, len(data)).lower() != hashlib.md5(data).hexdigest():
            raise RuntimeError("Flashed range failed device checksum")
    boot_application(esp)
    esp._port.close()
    start = time.monotonic()
    transcript = bytearray()
    with (args.output_dir / "serial.bin").open("wb") as raw:
        while time.monotonic() - start < 330:
            try:
                port = serial.Serial(port=None, baudrate=115200, timeout=1)
                port.dtr = False
                port.rts = False
                port.port = args.port
                port.open()
                with port:
                    while time.monotonic() - start < 330:
                        data = port.read(port.in_waiting or 1)
                        if data:
                            raw.write(data)
                            raw.flush()
                            transcript.extend(data)
                            if b"HIL_FINISHED" in transcript:
                                break
                    if b"HIL_FINISHED" in transcript:
                        break
            except serial.SerialException:
                time.sleep(0.5)
    (args.output_dir / "serial.txt").write_text(transcript.decode("utf-8", errors="replace"))
    cases = {name.decode(): bool(int(active)) for name, active in re.findall(
        rb"HIL_RESULT case=(EMPTY|RE7) ap_active=([01])", transcript)}
    esp, after_security = connect(args.port)
    after_storage = storage_digests(esp)
    boot_application(esp)
    esp._port.close()
    result = {
        "schema_version": 1, "purpose": "unsigned_spare_portal_regression_only",
        "mac": MAC, "backup_sha256": BACKUP_SHA256,
        "application_sha256": hashlib.sha256(images[3][1]).hexdigest(),
        "security_unchanged": before_security == after_security,
        "security_before": before_security,
        "security_after": after_security,
        "storage_raw_fixture_unchanged": before_storage == after_storage == expected,
        "storage_md5_ranges": after_storage,
        "cases_finished": b"HIL_FINISHED" in transcript,
        "cases_ap_active": cases,
        "case_expectations_met": cases == {"EMPTY": bool(args.expect_empty_ap), "RE7": True},
        "factory_acceptance": "NOT_ASSERTED",
        "efuse_burns_performed": False,
        "signed_lahore_recovery_qualified": False,
    }
    (args.output_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    if (not result["cases_finished"] or not result["storage_raw_fixture_unchanged"]
            or not result["case_expectations_met"]):
        raise RuntimeError("Incomplete physical test or changed fixture")


if __name__ == "__main__":
    main()
