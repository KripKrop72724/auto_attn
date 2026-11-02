"""Diagnosis rules: what the person is told, most important first."""
from __future__ import annotations

import pytest

from sl_installer.core import texts
from sl_installer.core.api import (BoardInfo, DeviceRecord, HardwareVerdict, PresenceReport,
                                   PresenceState, SecurityState, Severity, UsbCandidate, UsbKind)
from sl_installer.core.console import ConsoleParser
from sl_installer.core.diagnosis import Diagnoser, likely_light

from .support import MAC

ONE = PresenceReport(PresenceState.ONE_BOARD, board=UsbCandidate(
    "COM5", UsbKind.NATIVE, 0x303A, 0x1001, MAC))


def events(*lines):
    parser = ConsoleParser(MAC)
    return [parser.parse(line, float(i)) for i, line in enumerate(lines)]


def codes(advice):
    return [item.code for item in advice]


def diagnose(presence=ONE, board=None, device=None, lines=()):
    return Diagnoser(("RE7",)).diagnose(board, presence, device, events(*lines))


def secured(**changes):
    values = dict(port="COM5", mac=MAC, chip="ESP32-S3", flash_size_mb=None, psram_mb=None,
                  hardware=HardwareVerdict.NOT_CHECKED, security=SecurityState.SECURED_STATE_LIFE,
                  secure_download_mode=True, evidence={})
    values.update(changes)
    return BoardInfo(**values)


def test_restart_loop_is_the_first_thing_said():
    presence = PresenceReport(PresenceState.RESTART_LOOP, board=ONE.board, loop_period_s=10.3)
    advice = diagnose(presence, lines=["I (1) zone_lite: Connecting to Wi-Fi SSID RE7"])
    assert advice[0].code == "RESTART_LOOP_FAULTY" and advice[0].illustration == "faulty_board"
    fast = diagnose(PresenceReport(PresenceState.RESTART_LOOP, loop_period_s=2.0))
    assert fast[0].code == "RESTART_LOOP_FAST"
    # Connect and Check a board show this with their Check again button, never FAULTY (E5).
    assert fast[0].what_to_do[-1] == "Then click Check again."
    assert "FAULTY" not in " ".join(fast[0].what_to_do)


@pytest.mark.parametrize("presence, code", [
    (PresenceReport(PresenceState.WRONG_SOCKET), "WRONG_SOCKET"),
    (PresenceReport(PresenceState.SEVERAL_BOARDS), "SEVERAL_BOARDS"),
    (PresenceReport(PresenceState.NO_BOARD, seconds_in_state=3), "NO_BOARD"),
    (PresenceReport(PresenceState.NO_BOARD, seconds_in_state=15), "NO_BOARD_TIPS"),
])
def test_presence(presence, code):
    assert codes(diagnose(presence))[0] == code


def test_charge_only_cable_is_mentioned_after_15_seconds():
    advice = diagnose(PresenceReport(PresenceState.NO_BOARD, seconds_in_state=20))[0]
    assert any("only charge" in step for step in advice.what_to_do)


@pytest.mark.parametrize("board, code", [
    (secured(security=SecurityState.SECURED_OTHER), "SECURED_OTHER"),
    (secured(security=SecurityState.UNSECURED_USED), "UNSECURED_USED"),
    (secured(hardware=HardwareVerdict.WRONG_PSRAM, security=SecurityState.BLANK), "WRONG_PSRAM"),
    (secured(hardware=HardwareVerdict.WRONG_CHIP, security=SecurityState.UNKNOWN), "WRONG_CHIP"),
    (secured(security=SecurityState.UNKNOWN, evidence={"note": "FIRST_START_UNFINISHED"}),
     "FIRST_START_UNFINISHED"),
    (secured(security=SecurityState.KEY_BURNED_NOT_STARTED), "UNFINISHED_SETUP"),
])
def test_board_states(board, code):
    assert code in codes(diagnose(board=board))


def test_lost_security_key_outranks_everything_on_the_console():
    advice = diagnose(lines=["I (1) zone_lite: Connecting to Wi-Fi SSID RE7",
                             "I (1510) nvs: Generating NVS encr-keys..."])
    assert advice[0] is texts.SECURITY_KEY_LOST


def test_placeholder_wifi_points_to_the_hotspot():
    advice = diagnose(lines=["I (3000) zone_lite: Connecting to Wi-Fi SSID RE7",
                             "W (6000) zone_lite: Retrying Wi-Fi connection (1)"])
    wifi = next(a for a in advice if a.code == "WIFI_PLACEHOLDER")
    assert wifi.illustration == "hotspot" and wifi.severity is Severity.INFO
    assert any("SLIC ATTENDANCE-3D31" in step for step in wifi.what_to_do)
    assert any("192.168.254.1" in step for step in wifi.what_to_do)


def test_real_wifi_that_keeps_failing():
    lines = ["I (3000) zone_lite: Connecting to Wi-Fi SSID SiteNet"] + [
        f"W (6000) zone_lite: Retrying Wi-Fi connection ({i})" for i in range(1, 5)]
    assert "WIFI_NOT_JOINING" in codes(diagnose(lines=lines))
    assert "WIFI_NOT_JOINING" not in codes(diagnose(lines=lines[:2]))


def test_hotspot_open():
    advice = diagnose(lines=["I (3000) zone_lite: Connecting to Wi-Fi SSID SiteNet",
                             "W (123000) wifi_setup: Wi-Fi setup access point enabled "
                             "(recovery mode)"])
    hotspot = next(a for a in advice if a.code == "HOTSPOT_OPEN")
    assert "SLIC ATTENDANCE-3D31" in hotspot.what_happened


def test_wifi_connected_clears_wifi_advice():
    advice = diagnose(lines=["I (3000) zone_lite: Connecting to Wi-Fi SSID RE7",
                             "I (7000) zone_lite: Wi-Fi connected with IP 192.168.1.44"])
    assert not {"WIFI_PLACEHOLDER", "WIFI_NOT_JOINING"} & set(codes(advice))


@pytest.mark.parametrize("line, code", [
    ("W (9000) add_connector: ADD onboarding failed transport=ESP_ERR_HTTP_CONNECT status=0",
     "HEAD_OFFICE_UNREACHABLE"),
    ("W (9000) add_connector: ADD onboarding failed transport=ESP_OK status=401",
     "HEAD_OFFICE_REJECTED"),
    ("W (9000) add_connector: Trusted time unavailable; delaying ADD onboarding",
     "TIME_SERVICE_BLOCKED"),
    ("I (9100) add_connector: ADD live control channel connected", "HEAD_OFFICE_CONNECTED"),
])
def test_head_office(line, code):
    assert code in codes(diagnose(lines=["I (7000) zone_lite: Wi-Fi connected with IP 10.0.0.2",
                                         line]))


def test_comm_key_wrong():
    advice = diagnose(lines=[
        "I (381) app_init: App version:      2.7.8",
        "W (11000) zone_lite: ZKT CMD_CONNECT response code=2005 session=1 reply=0 data_len=0",
        "W (11001) zone_lite: ZKT auth failed with response code 2001",
        "W (11002) zone_lite: 192.168.1.20:4370 answered TCP but failed ZKT auth"])
    assert "COMM_KEY_WRONG" in codes(advice)


def test_secure_pull_terminal_needs_280_on_older_firmware():
    lines = ["I (380) app_init: Project name:     zone_lite",
             "I (381) app_init: App version:      2.7.8",
             "I (11000) zone_lite: ZKT CMD_CONNECT response code=6001 session=4466 reply=0 "
             "data_len=8",
             "W (11001) zone_lite: ZKT auth failed with response code 6001",
             "W (11002) zone_lite: 192.168.1.43:4370 answered TCP but failed ZKT auth"]
    assert "NEEDS_ZKT_280" in codes(diagnose(lines=lines))
    newer = [line.replace("2.7.8", "2.8.0") for line in lines]
    newer.insert(3, "W (11001) zone_lite: Secure-pull handshake failed at authentication "
                    "(code=2001, ok)")
    assert "COMM_KEY_WRONG" in codes(diagnose(lines=newer))
    assert "NEEDS_ZKT_280" not in codes(diagnose(lines=newer))


def test_terminal_found_later_wins():
    advice = diagnose(lines=[
        "W (11003) zone_lite: No authenticated ZKT device found on port 4370 (failure 1, "
        "backoff=10000 ms)",
        "I (12000) zone_lite: Selected ZKT device 192.168.1.20:4370 serial=X name=G3",
        "I (13000) zone_lite: ZKT live attendance events registered"])
    assert "TERMINAL_CONNECTED" in codes(advice) and "TERMINAL_NOT_FOUND" not in codes(advice)


@pytest.mark.parametrize("reason, code", [
    ("authentication_rejected", "HIK_PASSWORD_WRONG"),
    ("network_timeout_or_unreachable", "HIK_NOT_REACHABLE"),
    ("invalid_configuration", "HIK_SETTINGS_INCOMPLETE"),
])
def test_hikvision_terminal(reason, code):
    line = (f"I (20000) hikvision: Poll retrying; reason={reason}; stage=identity; http=401; "
            "failures=3; terminal=192.168.1.2; duration_ms=40; cursor=0; queued=0; "
            "storage=verified; source_worker=running")
    assert code in codes(diagnose(lines=["I (380) app_init: Project name:     zone_lite_hikvision",
                                         line]))


def test_storage_and_wrong_start_copy():
    advice = diagnose(lines=["I (95) boot: Loaded app from partition at offset 0x20000",
                             "I (380) app_init: Project name:     zone_lite",
                             "I (381) app_init: App version:      2.9.1",
                             "E (2000) zone_lite: Could not mount SPIFFS storage: ESP_FAIL"])
    assert {"STORAGE_PROBLEM", "WRONG_START_COPY"} <= set(codes(advice))
    hik = diagnose(lines=["I (95) boot: Loaded app from partition at offset 0x20000",
                          "I (380) app_init: Project name:     zone_lite_hikvision"])
    assert "WRONG_START_COPY" not in codes(hik)


def test_power_and_crash_loops():
    assert "POWER_PROBLEM" in codes(diagnose(lines=["E BOD: Brownout detector was triggered"]))
    crash = "Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled."
    assert "CRASH_LOOP" in codes(diagnose(lines=[crash, crash]))
    assert "CRASH_LOOP" not in codes(diagnose(lines=[crash]))


def test_crash_loop_rule_is_the_first_starts_rule():
    # The task watchdog only warns in this firmware: repeated warnings are no crash loop.
    warning = "E (9000) task_wdt: Task watchdog got triggered. The following tasks did not"
    assert "CRASH_LOOP" not in codes(diagnose(lines=[warning, warning, warning]))
    # Two panic restarts are, even when the crash lines themselves were not caught.
    restart = ["Rebooting...", "ESP-ROM:esp32s3-20210327",
               "rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)"]
    assert "CRASH_LOOP" in codes(diagnose(lines=restart * 2))
    assert "CRASH_LOOP" not in codes(diagnose(lines=restart))


def test_port_busy_signal_and_test_firmware():
    from sl_installer.core.api import ConsoleEvent, ConsoleKind
    busy = ConsoleEvent(ConsoleKind.OTHER, "port busy", {"signal": "port_busy"})
    advice = Diagnoser().diagnose(None, ONE, None, [busy])
    assert advice[0] is texts.PORT_BUSY
    assert "TEST_FIRMWARE" in codes(diagnose(lines=[
        "E (5000) wifi_setup: Wi-Fi setup portal is disabled because the protected build "
        "secret was not supplied"]))


@pytest.mark.parametrize("device, code", [
    (DeviceRecord(known=True, mac=MAC, online=True), "BOARD_ONLINE"),
    (DeviceRecord(known=True, mac=MAC, online=False, ever_online=True,
                  status_text="Offline since 10:42"), "BOARD_OFFLINE"),
])
def test_head_office_record(device, code):
    advice = diagnose(device=device)
    found = next(a for a in advice if a.code == code)
    if code == "BOARD_OFFLINE":
        assert found.what_happened == "Offline since 10:42"


def test_secured_board_never_seen_by_head_office():
    advice = diagnose(board=secured(), device=DeviceRecord(known=False, mac=MAC))
    assert "BOARD_NOT_SEEN" in codes(advice)


def test_order_errors_then_warnings_then_info_then_success():
    advice = diagnose(board=secured(security=SecurityState.UNSECURED_USED),
                      device=DeviceRecord(known=True, mac=MAC, online=True),
                      lines=["I (3000) zone_lite: Connecting to Wi-Fi SSID RE7",
                             "W (9000) add_connector: Trusted time unavailable; delaying ADD"])
    ranks = [{"error": 0, "warning": 1, "info": 2, "success": 3}[a.severity.value]
             for a in advice]
    assert ranks == sorted(ranks)
    assert len(codes(advice)) == len(set(codes(advice)))


def test_light_hint():
    assert likely_light(events("I (3000) zone_lite: Connecting to Wi-Fi SSID RE7")).key == \
        "WIFI_CONNECTING"
    assert likely_light(events("E (1540) zone_lite: Encrypted per-device provisioning is "
                               "required; network startup is blocked")).key == "FATAL"
    assert likely_light(events("I (7000) zone_lite: Wi-Fi connected with IP 1.2.3.4",
                               "I (13000) zone_lite: ZKT live attendance events registered")
                        ).key == "HEALTHY"
    hint = next(a for a in diagnose(lines=["I (3000) zone_lite: Connecting to Wi-Fi SSID RE7"])
                if a.code.startswith("LED_HINT"))
    assert "blue" in hint.title and hint.severity is Severity.INFO
