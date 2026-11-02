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
    (dict(psram="Embedded PSRAM 2MB (AP_3v3)"), HardwareVerdict.WRONG_PSRAM),
    (dict(psram=""), HardwareVerdict.WRONG_PSRAM),
])
def test_hardware_gate(change, verdict):
    info, _ = inspect(FakeBoard(**change))
    assert info.hardware is verdict


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
