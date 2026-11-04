"""Board inspection: esptool 5.1.0 output parsing and lock-state classification."""
from __future__ import annotations

import pytest

from sl_installer.core import board, texts
from sl_installer.core.api import (CancelToken, HardwareVerdict, InstallerError, SecurityState,
                                   UsbCandidate, UsbKind)
from sl_installer.core.board import BoardInspector, classify_efuses

from . import support
from .support import MAC, FakeBoard, FakeRunner, Failure


def native(mac=MAC, port=support.PORT):
    return UsbCandidate(port, UsbKind.NATIVE, 0x303A, 0x1001, mac, "USB JTAG/serial debug unit")


def inspect(fake: FakeBoard, candidate=None):
    runner = FakeRunner(fake)
    info = BoardInspector(runner).inspect(candidate or native(), CancelToken())
    return info, runner


def test_parse_banner_normal_and_sdm():
    banner = board.parse_banner(support.connect_banner(MAC))
    assert banner.chip == "ESP32-S3" and banner.mac == MAC and not banner.secure_download_mode
    assert board.parse_psram_mb(banner.features) == 8
    sdm = board.parse_banner(support.connect_banner(None, sdm=True))
    assert sdm.secure_download_mode and sdm.mac is None and sdm.chip == "ESP32-S3"


def test_parse_security_report_fingerprint():
    info = board.parse_security_info(support.security_report(0xC5, (8, 9, 10, 11, 0, 0, 12),
                                                             secure_boot=True))
    assert info.flags == 0xC5 and info.secure_download_mode and info.secure_boot
    assert info.key_purposes == (8, 9, 10, 11, 0, 0, 12)
    assert board.classify_sdm(info) is SecurityState.SECURED_STATE_LIFE


@pytest.mark.parametrize("flags, purposes, expected", [
    (0xC5, (8, 9, 10, 11, 0, 0, 12), SecurityState.SECURED_STATE_LIFE),
    (0xCD, (8, 9, 10, 11, 0, 0, 12), SecurityState.SECURED_STATE_LIFE),  # a key revoked later
    (0xC7, (8, 9, 10, 11, 0, 0, 12), SecurityState.SECURED_OTHER),       # aggressive revoke
    (0xC5, (9, 10, 11, 0, 0, 0, 12), SecurityState.SECURED_OTHER),       # no HMAC key in KEY0
    (0xC5, (4, 9, 10, 11, 0, 0, 12), SecurityState.SECURED_OTHER),
    (0x1C5, (8, 9, 10, 11, 0, 0, 12), SecurityState.SECURED_OTHER),      # USB disabled
])
def test_classify_sdm(flags, purposes, expected):
    info = board.parse_security_info(support.security_report(flags, purposes, secure_boot=True))
    assert board.classify_sdm(info) is expected


@pytest.mark.parametrize("fixture, expected", [
    ("blank", SecurityState.BLANK),
    ("key-burned", SecurityState.KEY_BURNED_NOT_STARTED),
    ("secured-not-sdm", SecurityState.SECURED_STATE_LIFE),
    ("other-key", SecurityState.UNSECURED_USED),
])
def test_classify_efuse_fixtures(fixture, expected):
    state, note = classify_efuses(support.efuse(fixture))
    assert state is expected and note is None


def test_classify_reads_value_readable_writeable_not_raw_value():
    summary = support.efuse("blank")
    assert "raw_value" not in summary["BLOCK_KEY0"]
    # A key block that is writeable but holds data is not blank (the old raw_value test
    # reduced "empty" to "writeable").
    summary["BLOCK_KEY0"]["value"] = "01 " + "00 " * 30 + "00"
    assert classify_efuses(summary)[0] is SecurityState.UNSECURED_USED


def test_hmac_key_left_readable_is_not_ours():
    summary = support.efuse("key-burned")
    summary["BLOCK_KEY0"].update(readable=True, value="00 " * 31 + "00")
    assert classify_efuses(summary)[0] is SecurityState.UNSECURED_USED


def test_first_start_that_stopped_halfway():
    summary = support.efuse("secured-not-sdm")
    summary["SECURE_BOOT_EN"]["value"] = False
    assert classify_efuses(summary) == (SecurityState.UNKNOWN, "FIRST_START_UNFINISHED")


def test_flash_encryption_means_someone_else():
    summary = support.efuse("blank")
    summary["SPI_BOOT_CRYPT_CNT"]["value"] = "Enable"
    assert classify_efuses(summary)[0] is SecurityState.SECURED_OTHER


def test_inspect_blank_board_uses_three_safe_calls():
    info, runner = inspect(FakeBoard())
    assert (info.mac, info.chip, info.flash_size_mb, info.psram_mb) == (MAC, "ESP32-S3", 16, 8)
    assert info.hardware is HardwareVerdict.OK and info.security is SecurityState.BLANK
    assert not info.secure_download_mode
    commands = [(c.tool, c.args) for c in runner.calls]
    assert commands[0] == ("esptool", ("--chip", "esp32s3", "--port", support.PORT, "--before",
                                       "default-reset", "--after", "no-reset", "--no-stub",
                                       "get-security-info"))
    assert commands[1][1][-2:] == ("--no-stub", "flash-id")
    assert commands[2][0] == "espefuse" and "summary" in commands[2][1]
    assert all("hard-reset" not in args for _tool, args in commands)
    assert "BLOCK_KEY0" not in str(info.evidence) or "value" not in str(info.evidence["efuse"])
    assert info.evidence["efuse"]["key0"]["empty"] is True


def test_inspect_secure_download_mode_board_only_asks_security_info():
    info, runner = inspect(FakeBoard(efuse_state="sdm", sdm=True))
    assert len(runner.calls) == 1
    assert info.secure_download_mode and info.mac == MAC  # from the USB serial number
    assert info.hardware is HardwareVerdict.NOT_CHECKED
    assert info.security is SecurityState.SECURED_STATE_LIFE


def test_inspect_sdm_board_without_usb_serial_cannot_be_identified():
    with pytest.raises(InstallerError) as caught:
        inspect(FakeBoard(efuse_state="sdm", sdm=True), native(mac=None))
    assert caught.value.advice is texts.BOARD_ID_UNKNOWN


def test_usb_board_id_that_is_not_the_chips_is_refused():
    # Every install call finds the board by its USB board ID, so it must be the chip's own.
    with pytest.raises(InstallerError) as caught:
        inspect(FakeBoard(), native(mac=support.OTHER_MAC))
    assert caught.value.advice is texts.BOARD_ID_UNKNOWN
    info, _ = inspect(FakeBoard(), native(mac=None))  # no USB board ID: the chip's is used
    assert info.mac == MAC and info.evidence["usb_mac"] is None


def test_inspect_key_burned_board():
    info, _ = inspect(FakeBoard(efuse_state="key-burned"))
    assert info.security is SecurityState.KEY_BURNED_NOT_STARTED


@pytest.mark.parametrize("change, verdict", [
    (dict(flash_mb=8), HardwareVerdict.WRONG_FLASH_SIZE),
    (dict(psram="Embedded PSRAM 2MB (AP_3v3)"), HardwareVerdict.BOARD_TYPE_UNSUPPORTED),
    (dict(psram=""), HardwareVerdict.WRONG_PSRAM),
])
def test_hardware_gate(change, verdict):
    info, _ = inspect(FakeBoard(**change))
    assert info.hardware is verdict


# ------------------------------------------------------- the 1.1.0 board check

def sample_board(security: str, flash: str, mac: str, efuse_state: str = "blank") -> FakeBoard:
    """A board answering with real esptool 5.1.0 outputs (fixtures/boards)."""
    return FakeBoard(mac, efuse_state=efuse_state,
                     outputs={"get-security-info": support.board_sample(security),
                              "flash-id": support.board_sample(flash)})


XMC_MAC = "98:c3:77:ac:9f:28"


@pytest.mark.parametrize("security, flash, mac, verdict, flash_id", [
    # 3 Nov 2026: looped in the ESP-IDF 5.5.3 bootloader after its key burn.
    ("xmc-20-7018-security-info.txt", "xmc-20-7018-flash-id.txt", XMC_MAC,
     HardwareVerdict.FLASH_CHIP_UNSUPPORTED, "20 70 18"),
    # Good boards, read by the app on a Mac.
    ("xtx-0b-4018-security-info.txt", "xtx-0b-4018-flash-id.txt", "ac:27:6e:a3:06:b0",
     HardwareVerdict.OK, "0b 40 18"),
    ("macronix-c2-2018-security-info.txt", "macronix-c2-2018-flash-id.txt", "ac:27:6e:a2:f5:d0",
     HardwareVerdict.OK, "c2 20 18"),
])
def test_real_boards(security, flash, mac, verdict, flash_id):
    info, runner = inspect(sample_board(security, flash, mac), native(mac=mac))
    assert info.hardware is verdict and info.flash_id == flash_id
    assert info.security is SecurityState.BLANK and info.chip_revision == 2
    assert len(runner.calls) == 3  # read only: nothing is written


def test_gigadevice_board_passes():
    # GD25Q128 (c8 40 18), as esptool 5.6.0 showed it with the flash type and voltage lines.
    info, _ = inspect(FakeBoard())
    assert info.hardware is HardwareVerdict.OK and info.flash_id == "c8 40 18"
    assert board.hardware_advice(info) is None


@pytest.mark.parametrize("maker, device, starts", [
    (0x0B, 0x4018, True), (0xC2, 0x2018, True), (0xC8, 0x4018, True),   # XTX, Macronix, GD
    (0x20, 0x7018, False),                                               # XM25QH128A
    (0x20, 0x4018, True), (0x20, 0x4013, True), (0x20, 0x4020, True),    # strict list, 0x40
    (0x20, 0x4012, False), (0x20, 0x4021, False),
    (0x20, 0x4117, True), (0x20, 0x4120, True), (0x20, 0x4116, False),   # 0x41
    (0x20, 0x5015, True), (0x20, 0x5016, True), (0x20, 0x5017, False),   # 0x50
    (0x20, 0x6018, False),
])
def test_flash_chip_rule_matches_the_bootloader(maker, device, starts):
    # ESP-IDF 5.5.3 bootloader_flash.c is_xmc_chip_strict, on esptool's printed numbers.
    chip = board.FlashChip(maker, device, 16, False)
    assert board.flash_chip_starts(chip) is starts
    assert board.is_xmc_chip_strict(maker, device) is (maker == 0x20 and starts)


@pytest.mark.parametrize("warning", [True, False])
def test_esptool_xmc_warning_is_decisive(warning):
    # 20 40 18 is on the strict list; esptool's startup warning still refuses it.
    output = support.connect_banner(MAC) + support.flash_id_output("20", "4018", xmc=warning)
    info, _ = inspect(FakeBoard(outputs={"flash-id": output}))
    expected = HardwareVerdict.FLASH_CHIP_UNSUPPORTED if warning else HardwareVerdict.OK
    assert info.hardware is expected and info.flash_id == "20 40 18"


def test_flash_chip_parsing_of_the_real_xmc_output():
    chip = board.parse_flash_chip(support.board_sample("xmc-20-7018-flash-id.txt"))
    assert (chip.maker, chip.device, chip.size_mb, chip.octal) == (0x20, 0x7018, 16, False)
    assert chip.xmc_startup_needed and chip.xmc_startup_failed and chip.jedec_id == "20 70 18"


@pytest.mark.parametrize("change, detail", [
    (dict(psram="Embedded PSRAM 8MB (AP_1v8)"), "extra memory 8 MB, type AP_1v8"),
    (dict(psram="Embedded PSRAM 16MB (AP_3v3)"), "extra memory 16 MB, type AP_3v3"),
    (dict(psram="Embedded PSRAM 8MB ()"), "extra memory 8 MB, type not given"),
    (dict(psram="Unknown Embedded PSRAM (AP_3v3)"),
     "extra memory of unknown size, type not given"),
    (dict(chip="ESP32-S3 (QFN56) (revision v1.0)"), "chip revision v1.0"),
])
def test_unsupported_board_types(change, detail):
    info, _ = inspect(FakeBoard(**change))
    assert info.hardware is HardwareVerdict.BOARD_TYPE_UNSUPPORTED
    assert info.hardware_detail == detail
    advice = board.hardware_advice(info)
    assert advice.code == "BOARD_TYPE_UNSUPPORTED" and detail in advice.what_happened
    assert not advice.can_retry


def test_octal_flash_from_esptool_or_the_efuse_summary():
    output = support.connect_banner(MAC) + support.flash_id_output("c8", "4018", kind="octal")
    info, _ = inspect(FakeBoard(outputs={"flash-id": output}))
    assert info.hardware is HardwareVerdict.BOARD_TYPE_UNSUPPORTED
    # esptool versions without the flash-type line: espefuse's FLASH_TYPE says it.
    without = "\n".join(line for line in (support.connect_banner(MAC) + support.FLASH_ID_16MB)
                         .splitlines() if not line.startswith("Flash type"))
    fake = FakeBoard(outputs={"flash-id": without})
    fake.summary["FLASH_TYPE"]["value"] = "8 data lines"
    info, _ = inspect(fake)
    assert info.hardware is HardwareVerdict.BOARD_TYPE_UNSUPPORTED
    fake = FakeBoard(outputs={"flash-id": without})
    assert inspect(fake)[0].hardware is HardwareVerdict.OK


def test_xmc_board_with_a_locked_key_is_refused_for_resume():
    # The two 3 Nov 2026 boards: key burned, never started.
    info, _ = inspect(sample_board("xmc-20-7018-security-info.txt", "xmc-20-7018-flash-id.txt",
                                   XMC_MAC, efuse_state="key-burned"), native(mac=XMC_MAC))
    assert info.security is SecurityState.KEY_BURNED_NOT_STARTED
    assert info.hardware is HardwareVerdict.FLASH_CHIP_UNSUPPORTED
    advice = board.hardware_advice(info)
    assert advice.code == "FLASH_CHIP_UNSUPPORTED" and not advice.can_retry
    assert "earlier setup locked" in advice.what_happened and "20 70 18" in advice.what_happened
    assert "Do not try to set it up again." in advice.what_to_do
    assert not any("seller" in step for step in advice.what_to_do)


def test_new_xmc_board_advice_and_board_refused_detail():
    info, _ = inspect(sample_board("xmc-20-7018-security-info.txt", "xmc-20-7018-flash-id.txt",
                                   XMC_MAC), native(mac=XMC_MAC))
    advice = board.hardware_advice(info)
    assert advice.title == "This board's memory chip cannot run our software"
    assert advice.what_to_do[0] == "Put this board aside. It is not broken."
    assert any("20 70 18" in step and "seller" in step for step in advice.what_to_do)
    assert board.refusal_detail(info, advice) == {
        "reason": "FLASH_CHIP_UNSUPPORTED", "flash_id": "20 70 18",
        "chip": "ESP32-S3 (QFN56) (revision v0.2)",
        "features": "Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB (AP_3v3)"}


def test_secure_download_mode_board_is_not_gated():
    # A finished board has started with its memory chip; it cannot show its hardware.
    info, _ = inspect(FakeBoard(efuse_state="sdm", sdm=True))
    assert info.hardware is HardwareVerdict.NOT_CHECKED and board.hardware_advice(info) is None


def test_wrong_chip_is_reported_not_raised():
    fake = FakeBoard()
    fake.failures["get-security-info"] = Failure(2, support.WRONG_CHIP)
    info, _ = inspect(fake)
    assert info.hardware is HardwareVerdict.WRONG_CHIP and info.chip == "ESP32-C3"


@pytest.mark.parametrize("output, advice", [
    (support.FAILED_TO_CONNECT, texts.AUTO_DOWNLOAD_FAILED),
    (support.PORT_BUSY, texts.PORT_BUSY),
    (support.PORT_GONE, texts.BOARD_DISCONNECTED),
    (support.SERIAL_NOISE, texts.CONNECTION_LOST),
])
def test_connection_failures_become_plain_advice(output, advice):
    fake = FakeBoard()
    fake.failures["get-security-info"] = Failure(2, output)
    with pytest.raises(InstallerError) as caught:
        inspect(fake)
    assert caught.value.advice is advice


def test_bridge_socket_is_refused_without_touching_the_board():
    runner = FakeRunner(FakeBoard())
    bridge = UsbCandidate("COM3", UsbKind.BRIDGE, 0x10C4, 0xEA60, None, "CP2102")
    with pytest.raises(InstallerError) as caught:
        BoardInspector(runner).inspect(bridge, CancelToken())
    assert caught.value.advice is texts.WRONG_SOCKET and runner.calls == []


def test_a_board_running_other_software_is_never_inspected():
    # 303A:4001: only the board's own USB-Serial/JTAG socket is ever read or written.
    runner = FakeRunner(FakeBoard())
    other = UsbCandidate("COM8", UsbKind.OTHER, 0x303A, 0x4001, None, "USB Serial Device (COM8)")
    with pytest.raises(InstallerError):
        BoardInspector(runner).inspect(other, CancelToken())
    assert runner.calls == []


def test_efuse_mac_mismatch_means_another_board():
    fake = FakeBoard()
    fake.summary["MAC"]["value"] = f"{support.OTHER_MAC} (OK)"
    with pytest.raises(InstallerError) as caught:
        inspect(fake)
    assert caught.value.advice is texts.BOARD_SWAPPED


def test_cancel_during_inspection():
    cancel = CancelToken()
    runner = FakeRunner(FakeBoard(), before=lambda call: cancel.cancel())
    with pytest.raises(InstallerError) as caught:
        BoardInspector(runner).inspect(native(), cancel)
    assert caught.value.advice is texts.CANCELLED
