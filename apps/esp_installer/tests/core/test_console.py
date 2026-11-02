"""Console parsing (firmware and ESP-IDF format strings) and the first-boot watcher."""
from __future__ import annotations

import sys

import pytest

from sl_installer.core import console, texts
from sl_installer.core.api import (BootOutcome, CancelToken, ConsoleKind, Family, HikSettings,
                                   ReplugPhase)
from sl_installer.core.console import BootWatcher, ConsoleParser, LineReader, judge

from . import support
from .support import MAC, FakeClock, FakeUsb, SerialScript

G = "\x1b[0;32m"
N = "\x1b[0m"


@pytest.mark.parametrize("line, kind, data", [
    ("ESP-ROM:esp32s3-20210327", ConsoleKind.BOOT_BANNER, {"rom": "esp32s3-20210327"}),
    ("rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)", ConsoleKind.BOOT_BANNER,
     {"reset_code": "1", "reset_reason": "POWERON", "boot_mode": "8"}),
    ("waiting for download", ConsoleKind.BOOT_BANNER, {"download_mode": "1"}),
    ("I (31) boot: ESP-IDF v5.5.3 2nd stage bootloader", ConsoleKind.BOOT_BANNER,
     {"idf": "v5.5.3"}),
    ("I (95) boot: Loaded app from partition at offset 0x2a0000", ConsoleKind.BOOT_BANNER,
     {"offset": "2a0000", "slot": "ota_0"}),
    ("I (95) boot: Loaded app from partition at offset 0x20000", ConsoleKind.BOOT_BANNER,
     {"offset": "20000", "slot": "factory"}),
    ("I (60) secure_boot_v2: enabling secure boot v2...", ConsoleKind.SECURE_BOOT_FIRST_BOOT, {}),
    ("I (900) boot: Secure boot permanently enabled", ConsoleKind.SECURE_BOOT_FIRST_BOOT, {}),
    ("I (240) esp_psram: Found 8MB PSRAM device", ConsoleKind.PSRAM_OK, {"size_mb": "8"}),
    ("I (1300) esp_psram: SPI SRAM memory test OK", ConsoleKind.PSRAM_OK, {"test": "ok"}),
    ("E (310) octal_psram: PSRAM chip is not connected, or wrong PSRAM line mode",
     ConsoleKind.PSRAM_FAILED, {}),
    ("E (1250) esp_psram: SPI SRAM memory test fail. 3/262144 writes failed, first @ 3C010000",
     ConsoleKind.PSRAM_FAILED, {}),
    ("E (20) cpu_start: Failed to init external RAM!", ConsoleKind.PSRAM_FAILED, {}),
    ("W (1500) nvs: Failed to read NVS security cfg: [0x1a0f] (ESP_ERR_HMAC_KEY_GENERATION_FAILED)",
     ConsoleKind.NVS_KEY_DANGER, {}),
    ("I (1510) nvs: Generating NVS encr-keys...", ConsoleKind.NVS_KEY_DANGER, {}),
    ("E (1520) zone_lite: Encrypted NVS initialization failed (ESP_ERR_NVS_CORRUPT_KEY_PART); "
     "preserving provisioning data and staying inert", ConsoleKind.NVS_FAILED, {}),
    ("E (1530) zone_config: No encrypted per-device provisioning namespace; firmware remains "
     "inert", ConsoleKind.SETTINGS_MISSING, {}),
    ("E (1540) zone_lite: Encrypted per-device provisioning is required; network startup is "
     "blocked", ConsoleKind.SETTINGS_MISSING, {}),
    ("I (1600) zone_config: Loaded per-device provisioning for zone=LF-ZONE-GUJRAT-01",
     ConsoleKind.PROVISIONING_LOADED, {"zone_id": "LF-ZONE-GUJRAT-01"}),
    ("I (1700) zone_lite: Zone Lite starting zone=LF-ZONE-GUJRAT-01 device_id=1",
     ConsoleKind.APP_STARTED, {"zone_id": "LF-ZONE-GUJRAT-01", "device_id": "1"}),
    ("I (2900) zone_lite: Storage ready; loaded 0 known event UIDs", ConsoleKind.STORAGE_READY,
     {"known_events": "0"}),
    ("E (2000) zone_lite: Could not mount SPIFFS storage: ESP_FAIL", ConsoleKind.STORAGE_FAILED,
     {"error": "ESP_FAIL"}),
    ("E (1990) SPIFFS: mount failed, -10025", ConsoleKind.STORAGE_FAILED, {"error": "-10025"}),
    ("E (1980) storage_repair: private mount failed -10025; nothing repaired",
     ConsoleKind.STORAGE_FAILED, {"error": "-10025"}),
    ("I (3000) zone_lite: Connecting to Wi-Fi SSID RE7", ConsoleKind.WIFI_CONNECTING,
     {"ssid": "RE7"}),
    ("I (3000) zone_lite: Connecting to Wi-Fi SSID ", ConsoleKind.WIFI_CONNECTING, {"ssid": ""}),
    ("W (6000) zone_lite: Retrying Wi-Fi connection (3)", ConsoleKind.WIFI_RETRY,
     {"attempt": "3"}),
    ("I (7000) zone_lite: Wi-Fi connected with IP 192.168.1.44", ConsoleKind.WIFI_CONNECTED,
     {"ip": "192.168.1.44"}),
    ("W (123000) wifi_setup: Wi-Fi setup access point enabled (recovery mode)",
     ConsoleKind.SETUP_AP_OPEN, {"mode": "recovery", "ssid": "SLIC ATTENDANCE-3D31"}),
    ("E (5000) wifi_setup: Wi-Fi setup portal is disabled because the protected build secret "
     "was not supplied", ConsoleKind.OTHER, {"signal": "portal_disabled"}),
    ("I (9000) add_connector: ADD automatic onboarding completed for connector 1b2c3d4e…",
     ConsoleKind.ADD_CONNECTED, {"stage": "onboarded"}),
    ("I (9100) add_connector: ADD live control channel connected", ConsoleKind.ADD_CONNECTED,
     {"stage": "live"}),
    ("W (9000) add_connector: ADD onboarding failed transport=ESP_ERR_HTTP_CONNECT status=0",
     ConsoleKind.ADD_FAILED, {"transport": "ESP_ERR_HTTP_CONNECT", "status": "0"}),
    ("W (9000) add_connector: Trusted time unavailable; delaying ADD onboarding",
     ConsoleKind.ADD_FAILED, {"reason": "time"}),
    ("I (12000) zone_lite: Selected ZKT device 192.168.1.20:4370 serial=PGB1261200074 name=G3 "
     "platform=ZMM220 time=2026-11-01 users=40 records=900 device_id=1",
     ConsoleKind.TERMINAL_FOUND, {"ip": "192.168.1.20", "port": "4370"}),
    ("I (13000) zone_lite: ZKT live attendance events registered", ConsoleKind.TERMINAL_FOUND,
     {"live": "1"}),
    ("I (11000) zone_lite: ZKT CMD_CONNECT response code=6001 session=4466 reply=0 data_len=8",
     ConsoleKind.TERMINAL_SECURE_PULL, {}),
    ("W (11000) zone_lite: ZKT CMD_CONNECT response code=2005 session=1 reply=0 data_len=0",
     ConsoleKind.OTHER, {}),
    ("W (11001) zone_lite: ZKT auth failed with response code 2001",
     ConsoleKind.TERMINAL_AUTH_FAILED, {"code": "2001"}),
    ("W (11002) zone_lite: 192.168.1.20:4370 answered TCP but failed ZKT auth",
     ConsoleKind.TERMINAL_AUTH_FAILED, {"ip": "192.168.1.20", "port": "4370"}),
    ("W (11003) zone_lite: No authenticated ZKT device found on port 4370 (failure 2, "
     "backoff=10000 ms)", ConsoleKind.TERMINAL_UNREACHABLE, {"port": "4370"}),
    ("I (20000) hikvision: Poll OK; reason=ok; stage=checkpoint_commit; http=200; failures=0; "
     "terminal=192.168.1.2; duration_ms=310; cursor=91; queued=0; storage=verified; "
     "source_worker=running", ConsoleKind.TERMINAL_FOUND, {"ip": "192.168.1.2"}),
    ("I (20000) hikvision: Poll retrying; reason=authentication_rejected; stage=identity; "
     "http=401; failures=3; terminal=192.168.1.2; duration_ms=40; cursor=0; queued=0; "
     "storage=verified; source_worker=running", ConsoleKind.TERMINAL_AUTH_FAILED,
     {"reason": "authentication_rejected", "stage": "identity", "http": "401"}),
    ("I (20000) hikvision: Poll retrying; reason=network_timeout_or_unreachable; stage=identity; "
     "http=0; failures=3; terminal=192.168.1.2; duration_ms=40; cursor=0; queued=0; "
     "storage=verified; source_worker=running", ConsoleKind.TERMINAL_UNREACHABLE,
     {"reason": "network_timeout_or_unreachable", "stage": "identity", "http": "0"}),
    ("Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.",
     ConsoleKind.PANIC, {"core": "0", "reason": "LoadProhibited"}),
    ("E BOD: Brownout detector was triggered", ConsoleKind.PANIC, {"reason": "brownout"}),
    ("Rebooting...", ConsoleKind.PANIC, {"reason": "rebooting"}),
])
def test_firmware_lines(line, kind, data):
    event = ConsoleParser(MAC).parse(line, 1.5)
    assert event.kind is kind, event
    assert event.data == data
    assert event.at == 1.5


def test_colours_and_carriage_returns_are_removed():
    event = ConsoleParser().parse(f"{G}I (1700) zone_lite: Zone Lite starting zone=Z-1 "
                                  f"device_id=7{N}\r")
    assert event.kind is ConsoleKind.APP_STARTED and event.data["device_id"] == "7"
    assert "\x1b" not in event.raw
    assert ConsoleParser().parse("   ") is None


def test_app_version_carries_the_project():
    parser = ConsoleParser()
    first = parser.parse("I (380) app_init: Project name:     zone_lite_hikvision")
    second = parser.parse("I (381) app_init: App version:      3.1.1")
    assert first.kind is ConsoleKind.OTHER and first.data == {"project": "zone_lite_hikvision"}
    assert second.kind is ConsoleKind.APP_VERSION
    assert second.data == {"version": "3.1.1", "project": "zone_lite_hikvision"}


def test_family_mismatch_abort_sequence():
    parser = ConsoleParser()
    lines = ["ESP_ERROR_CHECK failed: esp_err_t 0x103 (ESP_ERR_INVALID_STATE) at 0x4200b5a2",
             'file: "./main/zone_lite.c" line 11265', "func: app_main",
             "expression: zone_config_init()", "abort() was called at PC 0x4037ce4b on core 0"]
    kinds = [parser.parse(line).kind for line in lines]
    assert kinds == [ConsoleKind.PANIC, ConsoleKind.OTHER, ConsoleKind.OTHER,
                     ConsoleKind.FAMILY_MISMATCH, ConsoleKind.PANIC]
    other = ConsoleParser()
    other.parse("ESP_ERROR_CHECK failed: esp_err_t 0x1101 (ESP_ERR_NVS_NOT_ENOUGH_SPACE) at 0x4")
    assert other.parse("expression: zone_config_init()").kind is ConsoleKind.NVS_FAILED


def test_raw_lines_are_redacted():
    event = ConsoleParser().parse("I (1) zone_lite: debug password=test-wifi-pass")
    assert "test-wifi-pass" not in event.raw


def test_line_reader_handles_partial_lines():
    reader = LineReader()
    assert reader.feed(b"I (1) a: one\r\nI (2) b: tw") == ["I (1) a: one"]
    assert reader.feed(b"o\n") == ["I (2) b: two"]
    assert reader.feed(b"\xffpartial") == []
    assert reader.flush() == ["�partial"]


def test_line_state_never_passes_through_reset(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert console.default_line_state() == (False, False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert console.default_line_state() == (True, True)


def test_open_serial_sets_lines_before_opening(monkeypatch):
    import serial
    events = []

    class Recorder:
        def __init__(self):
            events.append("created")

        def __setattr__(self, name, value):
            events.append((name, value))
            object.__setattr__(self, name, value)

        def open(self):
            events.append("open")
    monkeypatch.setattr(serial, "Serial", Recorder)
    console.open_serial("COM5", (False, False))
    assert events.index(("dtr", False)) < events.index("open")
    assert events.index(("rts", False)) < events.index("open")
    assert ("baudrate", 115200) in events


# ------------------------------------------------------------- first boot

GOOD_BOOT = [
    "ESP-ROM:esp32s3-20210327", "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)",
    "I (30) boot: ESP-IDF v5.5.3 2nd stage bootloader",
    "I (60) secure_boot_v2: enabling secure boot v2...",
    "I (95) boot: Loaded app from partition at offset 0x2a0000",
    "I (240) esp_psram: Found 8MB PSRAM device", "I (1300) esp_psram: SPI SRAM memory test OK",
    "I (380) app_init: Project name:     zone_lite", "I (381) app_init: App version:      2.9.1",
    "I (1600) zone_config: Loaded per-device provisioning for zone=LF-ZONE-GUJRAT-01",
    "I (1700) zone_lite: Zone Lite starting zone=LF-ZONE-GUJRAT-01 device_id=1",
    "I (2900) zone_lite: Storage ready; loaded 0 known event UIDs",
    "I (3000) zone_lite: Connecting to Wi-Fi SSID RE7",
]


def watcher_with(script_steps, *, mac=MAC, drop_unread=False):
    clock = FakeClock()
    usb = FakeUsb(mac=mac)
    script = SerialScript(clock, usb, drop_unread=drop_unread)
    for moment, action, value in script_steps:
        script.at(clock.now + moment, action, value)
    clock.hooks.append(script.tick)
    return BootWatcher(usb, clock, opener=script.opener), script, clock


def first_boot(watcher, timeout=120.0, cancel=None, config=None):
    phases, events = [], []
    report = watcher.wait_first_boot(MAC, config or support.zkt_config(), phases.append,
                                     events.append, cancel or CancelToken(), timeout_s=timeout)
    return report, phases, events


def test_good_first_boot():
    watcher, script, _clock = watcher_with([
        (1.0, "unplug", None), (4.0, "plug", None), (4.5, "print", GOOD_BOOT)])
    report, phases, events = first_boot(watcher)
    assert phases == [ReplugPhase.WAITING_UNPLUG, ReplugPhase.WAITING_REPLUG,
                      ReplugPhase.WATCHING]
    assert report.outcome is BootOutcome.OK
    assert (report.zone_id_seen, report.device_id_seen, report.app_version_seen) == (
        "LF-ZONE-GUJRAT-01", "1", "2.9.1")
    assert report.advice.code == "BOOT_OK"
    assert any(e.kind is ConsoleKind.SECURE_BOOT_FIRST_BOOT for e in events)
    assert script.opened  # opened right after the replug


def test_wrong_settings():
    lines = [line.replace("GUJRAT-01", "GUJRAT-02") for line in GOOD_BOOT]
    watcher, _script, _clock = watcher_with([(1, "unplug", None), (2, "plug", None),
                                             (2.2, "print", lines)])
    report, _p, _e = first_boot(watcher)
    assert report.outcome is BootOutcome.WRONG_SETTINGS
    assert "LF-ZONE-GUJRAT-02" in report.advice.what_happened


def test_security_key_generated_by_the_board_is_fatal():
    lines = GOOD_BOOT[:9] + ["W (1500) nvs: Failed to read NVS security cfg: [0x1a0f] (X)",
                             "I (1510) nvs: Generating NVS encr-keys..."]
    watcher, _s, _c = watcher_with([(1, "unplug", None), (2, "plug", None), (2.2, "print", lines)])
    report, _p, _e = first_boot(watcher)
    assert report.outcome is BootOutcome.SECURITY_KEY_PROBLEM
    assert report.advice is texts.SECURITY_KEY_LOST and not report.advice.can_retry


def test_settings_unreadable():
    lines = GOOD_BOOT[:9] + ["E (1520) zone_lite: Encrypted NVS initialization failed (X); "
                             "preserving provisioning data and staying inert"]
    watcher, _s, _c = watcher_with([(1, "unplug", None), (2, "plug", None), (2.2, "print", lines)])
    assert first_boot(watcher)[0].advice is texts.SETTINGS_UNREADABLE


def test_family_mismatch_loop():
    abort = ["ESP_ERROR_CHECK failed: esp_err_t 0x103 (ESP_ERR_INVALID_STATE) at 0x4200b5a2",
             "expression: zone_config_init()", "abort() was called at PC 0x4037ce4b on core 0"]
    watcher, _s, _c = watcher_with([(1, "unplug", None), (2, "plug", None),
                                    (2.2, "print", GOOD_BOOT[:9] + abort)])
    report = first_boot(watcher)[0]
    assert report.outcome is BootOutcome.FAMILY_MISMATCH


def test_storage_problem():
    lines = GOOD_BOOT[:11] + ["E (1990) SPIFFS: mount failed, -10025",
                              "E (2000) zone_lite: Could not mount SPIFFS storage: ESP_FAIL"]
    watcher, _s, _c = watcher_with([(1, "unplug", None), (2, "plug", None), (2.2, "print", lines)])
    assert first_boot(watcher)[0].outcome is BootOutcome.STORAGE_PROBLEM


def test_ok_without_storage_line_after_a_while():
    watcher, _s, clock = watcher_with([(1, "unplug", None), (2, "plug", None),
                                       (2.2, "print", GOOD_BOOT[:11])])
    report = first_boot(watcher)[0]
    assert report.outcome is BootOutcome.OK


def test_faulty_board_restart_loop():
    steps = [(1, "unplug", None)]
    for cycle in range(5):
        steps += [(3 + cycle * 10.3, "plug", None), (3 + cycle * 10.3 + 9.4, "unplug", None)]
    watcher, script, _c = watcher_with(steps)
    report = first_boot(watcher)[0]
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP
    assert report.advice.code == "RESTART_LOOP_FAULTY"
    assert "10 seconds" in report.advice.what_happened
    assert len(script.opened) >= 3


def test_fast_restart_loop_after_the_first_start_names_try_again():
    steps = [(1, "unplug", None)]
    for cycle in range(5):
        steps += [(3 + cycle * 3.0, "plug", None), (3 + cycle * 3.0 + 2.4, "unplug", None)]
    watcher, _script, _c = watcher_with(steps)
    report = first_boot(watcher)[0]
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP
    assert report.advice.code == "RESTART_LOOP_FAST"
    # A power problem first, never a FAULTY label; the first-start screen offers Try again.
    assert report.advice.what_to_do[-1] == "Then click Try again."
    assert "FAULTY" not in " ".join(report.advice.what_to_do)


def test_psram_failure_is_reported_as_faulty():
    lines = GOOD_BOOT[:6] + ["E (310) octal_psram: PSRAM chip is not connected, or wrong PSRAM "
                             "line mode", "E (20) cpu_start: Failed to init external RAM!"]
    watcher, _s, _c = watcher_with([(1, "unplug", None), (2, "plug", None), (2.2, "print", lines)])
    report = first_boot(watcher)[0]
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP and report.advice is texts.PSRAM_FAILED


def test_silent_board():
    watcher, _s, _c = watcher_with([(1, "unplug", None), (2, "plug", None)])
    report = first_boot(watcher, timeout=30)[0]
    assert report.outcome is BootOutcome.NO_OUTPUT and report.advice is texts.NO_OUTPUT


def test_board_left_in_download_mode():
    watcher, _s, _c = watcher_with([(1, "unplug", None), (2, "plug", None), (2.2, "print", [
        "ESP-ROM:esp32s3-20210327", "rst:0x1 (POWERON),boot:0x23 (DOWNLOAD(USB/UART0))",
        "waiting for download"])])
    report = first_boot(watcher, timeout=30)[0]
    assert report.advice is texts.DOWNLOAD_MODE_STUCK


def test_not_unplugged():
    watcher, _s, _c = watcher_with([])
    report, phases, _e = first_boot(watcher)
    assert report.outcome is BootOutcome.NOT_REPLUGGED and phases == [ReplugPhase.WAITING_UNPLUG]


def test_not_plugged_back():
    watcher, _s, _c = watcher_with([(1, "unplug", None)])
    report, phases, _e = first_boot(watcher)
    assert report.outcome is BootOutcome.NOT_REPLUGGED
    assert phases[-1] is ReplugPhase.WAITING_REPLUG


def test_cancel_while_waiting():
    cancel = CancelToken()
    watcher, _s, clock = watcher_with([])
    clock.hooks.append(lambda now: cancel.cancel() if now > 1003 else None)
    report = first_boot(watcher, cancel=cancel)[0]
    assert report.outcome is BootOutcome.CANCELLED


def test_port_held_by_another_program():
    watcher, script, clock = watcher_with([(1, "unplug", None), (2, "plug", None)])
    script.busy_until = clock.now + 10_000
    report, _p, events = first_boot(watcher, timeout=20)
    assert report.advice is texts.PORT_BUSY
    assert any(e.data.get("signal") == "port_busy" for e in events)


def test_watch_console_follows_a_replug():
    cancel = CancelToken()
    watcher, script, clock = watcher_with([
        (0.5, "print", ["I (1) zone_lite: Connecting to Wi-Fi SSID RE7"]),
        (2.0, "unplug", None), (3.0, "plug", None),
        (3.5, "print", ["I (1) zone_lite: Wi-Fi connected with IP 10.0.0.9"])])
    clock.hooks.append(lambda now: cancel.cancel() if now > 1006 else None)
    seen = []
    watcher.watch_console(support.FakeUsb().candidate(), seen.append, cancel)
    kinds = [e.kind for e in seen]
    assert ConsoleKind.WIFI_CONNECTING in kinds and ConsoleKind.WIFI_CONNECTED in kinds
    assert len(script.opened) == 2


def test_judge_needs_two_restarts_for_a_loop():
    watch = console._Watch(started=0.0, events=[], resets=1, reset_times=(10.0,))
    assert judge(watch, support.zkt_config(), 15.0, 120.0, final=False) is None


# ESP-IDF 5.5.3 restarts the S3 after a panic with a CPU reset that keeps USB-Serial/JTAG
# up, so the port never reopens: the console alone shows the loop.
PANIC_RESTART = [
    "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.",
    "Backtrace: 0x4200b5a2:0x3fcebd80 0x4037ce4b:0x3fcebda0",
    "ELF file SHA256: 7d3a1c9e2b4f5a60",
    "Rebooting...",
    "ESP-ROM:esp32s3-20210327",
    "rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)",
    "I (30) boot: ESP-IDF v5.5.3 2nd stage bootloader",
    "I (95) boot: Loaded app from partition at offset 0x2a0000",
]
APP_START = "I (1700) zone_lite: Zone Lite starting zone=LF-ZONE-GUJRAT-01 device_id=1"


def crash_every(seconds, *, first, then, cycles=9):
    """A watch where the board prints ``first`` and then crashes every ``seconds``."""
    steps = [(1, "unplug", None), (2, "plug", None), (2.2, "print", first)]
    for cycle in range(cycles):
        steps.append((2.5 + seconds * (cycle + 1), "print", PANIC_RESTART + then))
    return watcher_with(steps)


def test_crash_loop_after_the_start_is_never_a_good_start():
    watcher, script, _clock = crash_every(3.0, first=GOOD_BOOT[:11], then=GOOD_BOOT[5:11])
    report = first_boot(watcher)[0]
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP
    assert report.advice is texts.CRASH_LOOP
    assert len(script.opened) == 1  # the port stayed open the whole time


def test_crash_loop_before_the_start_is_not_a_silent_board():
    watcher, script, clock = crash_every(3.0, first=GOOD_BOOT[:9], then=[])
    started = clock.now
    report = first_boot(watcher)[0]
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP
    assert report.advice is texts.CRASH_LOOP
    assert clock.now - started < 60  # said as soon as it showed, not after 120 s
    assert len(script.opened) == 1


def test_one_crash_after_the_start_is_reported_when_the_watch_ends():
    watcher, _script, _clock = watcher_with([
        (1, "unplug", None), (2, "plug", None), (2.2, "print", GOOD_BOOT[:11]),
        (5.0, "print", ["abort() was called at PC 0x4037ce4b on core 0", "Rebooting..."])])
    report = first_boot(watcher, timeout=40)[0]
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP
    assert report.advice is texts.CRASH_LOOP


def test_task_watchdog_warning_alone_is_still_a_good_start():
    # The firmware leaves the task watchdog's panic off: it only warns.
    warning = ("E (9000) task_wdt: Task watchdog got triggered. The following tasks/users "
               "did not reset the watchdog in time:")
    watcher, _script, _clock = watcher_with([
        (1, "unplug", None), (2, "plug", None), (2.2, "print", GOOD_BOOT[:11]),
        (4.0, "print", [warning]), (9.0, "print", [warning])])
    assert first_boot(watcher)[0].outcome is BootOutcome.OK


def test_brownout_restart_is_a_power_problem():
    watcher, _script, _clock = watcher_with([
        (1, "unplug", None), (2, "plug", None), (2.2, "print", GOOD_BOOT[:9]),
        (4.0, "print", ["Brownout detector was triggered", "ESP-ROM:esp32s3-20210327",
                        "rst:0xf (BROWNOUT_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)"])])
    report = first_boot(watcher)[0]
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP
    assert report.advice is texts.POWER_PROBLEM


def test_restarts_are_counted_from_the_console():
    parser = ConsoleParser(MAC)
    events = [parser.parse(line) for line in GOOD_BOOT[:2] + PANIC_RESTART + PANIC_RESTART]
    assert console.restarts_seen(events) == 2
    assert console.restarts_seen(events, port_reopens=3) == 3
    assert console.crash_loop(events)
    one = [parser.parse(line) for line in GOOD_BOOT[:2] + PANIC_RESTART]
    assert console.restarts_seen(one) == 1 and not console.crash_loop(one)
    # Plugging the board in again (a power-on) is not the board restarting itself.
    replugged = [parser.parse(line) for line in GOOD_BOOT[:2] * 3 + PANIC_RESTART[:1]]
    assert console.restarts_seen(replugged) == 0 and not console.crash_loop(replugged)


def test_first_boot_keeps_the_pc_awake(monkeypatch):
    from sl_installer.core import keepawake
    calls = []
    monkeypatch.setattr(keepawake, "_set_state", lambda flags: calls.append(flags) or True)
    watcher, _script, clock = watcher_with([
        (1.0, "unplug", None), (4.0, "plug", None), (4.5, "print", GOOD_BOOT)])
    clock.hooks.append(lambda now: calls.append("tick") if len(calls) < 2 else None)
    assert first_boot(watcher)[0].outcome is BootOutcome.OK
    assert calls[0] == keepawake.AWAKE and calls[-1] == keepawake.RELEASED
    assert calls.count(keepawake.AWAKE) == 1 and "tick" in calls[1:-1]


# ------------------------------------------ start line printed before the port opened

# The board prints from power-on, but the computer lists its port again only once it has
# enumerated it, about 2 s later. USB-Serial/JTAG drops what nobody reads (SerialScript's
# drop_unread), so the start line, under 2 s after power-on, is gone by then.
def start_missed(before_open, after_open, *, then=()):
    """A replug where the board prints ``before_open`` before its port is listed again."""
    return watcher_with([(1.0, "unplug", None), (4.0, "print", before_open),
                         (6.0, "plug", None), (6.5, "print", after_open), *then],
                        drop_unread=True)


# HIK 3.1.1 starts its factory copy; its project name differs, its log tags do not.
HIK_BOOT = [line.replace("0x2a0000", "0x20000")
            .replace("Project name:     zone_lite", "Project name:     zone_lite_hikvision")
            .replace("App version:      2.9.1", "App version:      3.1.1") for line in GOOD_BOOT]
HIK_POLL = ("I (21000) hikvision: Poll retrying; reason=http_error; stage=identity; http=500; "
            "failures=1; terminal=192.168.1.20; duration_ms=40; cursor=0; queued=0; "
            "storage=verified; source_worker=running")


def hik_config():
    return support.zkt_config(family=Family.HIKVISION, zkt=None,
                              bundle_id="hikvision-3.1.1-dc7d9c3-77620195",
                              hik=HikSettings(host="192.168.1.20", password="test-hik-pass",
                                              expected_serial="TEST-SERIAL-0001"))


def test_start_line_lost_before_the_port_opened_is_still_a_good_start():
    watcher, script, _clock = start_missed(GOOD_BOOT[:11], GOOD_BOOT[11:])
    report, _phases, events = first_boot(watcher)
    assert GOOD_BOOT[10] in script.dropped  # "Zone Lite starting zone=... device_id=..."
    assert ConsoleKind.APP_STARTED not in {event.kind for event in events}
    assert report.outcome is BootOutcome.OK and report.advice.code == "BOOT_OK"
    # Only the start line names the zone and device, so nothing is claimed without it.
    assert (report.zone_id_seen, report.device_id_seen, report.app_version_seen) == (
        None, None, None)
    assert report.advice.what_happened == "The board started and is working."


def test_hikvision_start_lost_and_storage_too_is_good_after_the_storage_wait():
    # The port opened later still: the first line the app reads is a poll of the machine.
    before = HIK_BOOT + ["I (6000) zone_lite: Wi-Fi connected with IP 192.168.1.44"]
    watcher, script, clock = start_missed(before, [HIK_POLL])
    begun = clock.now
    report = first_boot(watcher, config=hik_config())[0]
    assert any("Zone Lite starting" in line for line in script.dropped)
    assert report.outcome is BootOutcome.OK and report.zone_id_seen is None
    assert report.advice.what_happened == "The board started and is working."
    # With no storage line, the board is watched as long as after a start line it saw.
    waited = clock.now - (begun + 6.5)
    assert console.STORAGE_WAIT_S <= waited < 60


def test_crash_after_a_lost_start_line_is_never_a_good_start():
    # The board ran (its Wi-Fi line came), crashed and started again, this time in view:
    # the crash counts from the first line that showed it running.
    crash = ["abort() was called at PC 0x4037ce4b on core 0", "Rebooting..."]
    watcher, _script, _clock = start_missed(GOOD_BOOT[:12], GOOD_BOOT[12:], then=[
        (9.0, "print", crash), (10.0, "print", PANIC_RESTART[4:] + GOOD_BOOT[5:12])])
    report = first_boot(watcher, timeout=40)[0]
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP
    assert report.advice is texts.CRASH_LOOP


def test_without_the_start_line_a_crash_before_the_first_program_line_counts():
    # When the start line is lost, its time is unknown: a crash seen before the first line
    # of the program may still have come after the program started.
    parser = ConsoleParser(MAC)
    lines = ["abort() was called at PC 0x4037ce4b on core 0", "Rebooting...",
             *GOOD_BOOT[:10], *GOOD_BOOT[11:]]
    watch = console._Watch(started=0.0, events=[parser.parse(line, 1.0 + index / 10)
                                                for index, line in enumerate(lines)])
    assert ConsoleKind.APP_STARTED not in {event.kind for event in watch.events}
    assert judge(watch, support.zkt_config(), 30.0, 120.0, final=False) is None
    report = judge(watch, support.zkt_config(), 121.0, 120.0, final=True)
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP and report.advice is texts.CRASH_LOOP


def test_hikvision_storage_failure_after_a_lost_start_line():
    lines = HIK_BOOT[:11] + ["E (2000) zone_lite: Could not mount SPIFFS storage: ESP_FAIL"]
    watcher, _script, _clock = start_missed(lines[:11], lines[11:])
    report = first_boot(watcher, config=hik_config())[0]
    assert report.outcome is BootOutcome.STORAGE_PROBLEM


def test_without_the_start_line_only_the_settings_line_names_the_zone():
    parser = ConsoleParser(MAC)

    def watch(*lines):
        return console._Watch(started=0.0, events=[parser.parse(line, 1.0) for line in lines])

    report = judge(watch(GOOD_BOOT[9], GOOD_BOOT[11]), support.zkt_config(), 2.0, 120.0,
                   final=False)
    assert report.outcome is BootOutcome.OK
    assert (report.zone_id_seen, report.device_id_seen) == ("LF-ZONE-GUJRAT-01", None)
    assert report.advice.what_happened == "The board started with zone LF-ZONE-GUJRAT-01."
    other = GOOD_BOOT[9].replace("GUJRAT-01", "GUJRAT-02")
    report = judge(watch(other, GOOD_BOOT[11]), support.zkt_config(), 2.0, 120.0, final=False)
    assert report.outcome is BootOutcome.WRONG_SETTINGS
    assert "LF-ZONE-GUJRAT-02" in report.advice.what_happened


@pytest.mark.parametrize("line, running", [
    (GOOD_BOOT[8], False),   # the version line comes before the program starts
    (GOOD_BOOT[9], False),   # after the settings line the program can still stop
    ("I (380) app_init: Project name:     zone_lite", False),
    (GOOD_BOOT[11], True),
    ("E (1990) SPIFFS: mount failed, -10025", True),
    ("W (123000) wifi_setup: Wi-Fi setup access point enabled (recovery mode)", True),
    ("E (5000) wifi_setup: Wi-Fi setup portal is disabled because the protected build secret "
     "was not supplied", True),
    ("I (9000) wifi_setup: Validated and atomically saved a new Wi-Fi network", True),
    ("W (9000) add_connector: Trusted time unavailable; delaying ADD onboarding", True),
    ("W (11003) zone_lite: No authenticated ZKT device found on port 4370 (failure 2, "
     "backoff=10000 ms)", True),
    (HIK_POLL, True),
])
def test_lines_that_show_the_program_running(line, running):
    assert console._after_start(ConsoleParser(MAC).parse(line)) is running


@pytest.mark.parametrize("lines", [
    GOOD_BOOT[:9],  # it never got past the start of its program
    # Start-up lines the app has no rule for, printed before the program starts.
    ["I (1170) cpu_start: Multicore app", "I (1190) cpu_start: Pro cpu start user code"],
])
def test_board_that_sent_lines_but_no_start_is_not_called_silent(lines):
    watcher, _s, _c = watcher_with([(1, "unplug", None), (2, "plug", None), (2.2, "print", lines)])
    report = first_boot(watcher, timeout=30)[0]
    assert report.outcome is BootOutcome.NO_OUTPUT and report.advice is texts.START_NOT_SEEN
    assert "faulty" not in " ".join(report.advice.what_to_do)
