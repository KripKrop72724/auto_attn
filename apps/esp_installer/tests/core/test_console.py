"""Console parsing (firmware and ESP-IDF format strings) and the first-boot watcher."""
from __future__ import annotations

import sys

import pytest

from sl_installer.core import console, texts
from sl_installer.core.api import (BootOutcome, BootReport, CancelToken, ConsoleKind, Family,
                                   HikSettings, ReplugPhase)
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


def watcher_with(script_steps, *, mac=MAC, drop_unread=False, console_logs=None):
    clock = FakeClock()
    usb = FakeUsb(mac=mac)
    script = SerialScript(clock, usb, drop_unread=drop_unread)
    for moment, action, value in script_steps:
        script.at(clock.now + moment, action, value)
    clock.hooks.append(script.tick)
    return (BootWatcher(usb, clock, opener=script.opener, console_logs=console_logs), script,
            clock)


def first_boot(watcher, timeout=120.0, cancel=None, config=None, transcript=None):
    phases, events = [], []
    report = watcher.wait_first_boot(MAC, config or support.zkt_config(), phases.append,
                                     events.append, cancel or CancelToken(), timeout_s=timeout,
                                     transcript=transcript)
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


def test_crash_loop_before_the_program_starts_is_a_faulty_board():
    # The start reached ESP-IDF's own start-up and crashed before our program: the board.
    watcher, script, clock = crash_every(3.0, first=GOOD_BOOT[:9], then=[])
    started = clock.now
    report = first_boot(watcher)[0]
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP
    assert report.advice is texts.STARTUP_CRASH and not report.advice.can_retry
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


# ----------------------------------------------- the restart check (the second start)

# The next start of a board that kept its settings: Secure Boot is on already.
RESTART_BOOT = [line for line in GOOD_BOOT if "secure_boot" not in line]
# 2 Nov 2026, two new ZKT 2.7.8 boards: a good first start, then at the next start this.
SETTINGS_GONE = RESTART_BOOT[:8] + [
    "E (1530) zone_config: No encrypted per-device provisioning namespace; firmware remains "
    "inert",
    "E (1540) zone_lite: Encrypted per-device provisioning is required; network startup is "
    "blocked",
]


def second_start(watcher, timeout=120.0, cancel=None, config=None, transcript=None,
                 first=None):
    phases, events = [], []
    report = watcher.wait_restart(MAC, config or support.zkt_config(), phases.append,
                                  events.append, cancel or CancelToken(), timeout_s=timeout,
                                  transcript=transcript, first=first)
    return report, phases, events


# A first start that showed this setup's settings: zone, device and version.
FIRST_SEEN = BootReport(BootOutcome.OK, "LF-ZONE-GUJRAT-01", "1", "2.9.1")


def lost_advice(report, changed=False):
    """The lost-settings advice, with the board ID for its label."""
    assert report.outcome is BootOutcome.SETTINGS_LOST
    assert report.advice == texts.settings_lost_on_restart(MAC, changed=changed)
    assert report.advice.code == "SETTINGS_LOST_ON_RESTART" and not report.advice.can_retry
    assert any("AC:27:6E:A4:3D:30" in step for step in report.advice.what_to_do)
    return report.advice


def watch_of(*lines):
    parser = ConsoleParser(MAC)
    return console._Watch(started=0.0, events=[parser.parse(line, 1.0) for line in lines])


def test_restart_check_passes_when_the_board_names_its_settings_again():
    watcher, script, _clock = watcher_with([
        (1.0, "unplug", None), (4.0, "plug", None), (4.5, "print", RESTART_BOOT)])
    report, phases, _events = second_start(watcher)
    # The same replug as the first start.
    assert phases == [ReplugPhase.WAITING_UNPLUG, ReplugPhase.WAITING_REPLUG,
                      ReplugPhase.WATCHING]
    assert report.outcome is BootOutcome.OK
    assert (report.zone_id_seen, report.device_id_seen, report.app_version_seen) == (
        "LF-ZONE-GUJRAT-01", "1", "2.9.1")
    assert report.advice.code == "RESTART_OK"
    assert report.advice.title == "The board kept its settings"
    assert script.opened


def test_settings_gone_at_the_next_start_are_lost_not_unreadable():
    watcher, _script, _clock = watcher_with([
        (1.0, "unplug", None), (4.0, "plug", None), (4.5, "print", SETTINGS_GONE)])
    report, _phases, events = second_start(watcher)
    assert ConsoleKind.SETTINGS_MISSING in {event.kind for event in events}
    lost_advice(report)
    # The first start keeps its own verdict for the same lines.
    assert judge(watch_of(*SETTINGS_GONE), support.zkt_config(), 2.0, 120.0,
                 final=False).advice is texts.SETTINGS_UNREADABLE


@pytest.mark.parametrize("line", [
    "E (1520) zone_lite: Encrypted NVS initialization failed (ESP_ERR_NVS_CORRUPT_KEY_PART); "
    "preserving provisioning data and staying inert",
    "E (1525) zone_config: Could not open encrypted provisioning namespace: ESP_FAIL",
])
def test_unreadable_settings_at_the_next_start_are_lost(line):
    report = judge(watch_of(*RESTART_BOOT[:8], line), support.zkt_config(), 2.0, 120.0,
                   final=False, restart=True, board_id=MAC)
    lost_advice(report)
    # Unreadable settings can make the board restart again and again: no "solid red".
    assert "red" not in report.advice.what_happened


def test_the_settings_line_can_be_followed_by_settings_missing():
    # zone_config reads the namespace (the settings line), then app_main may still stop:
    # "provisioning is required" when the board's provisioned flag is gone.
    loaded = RESTART_BOOT[8]
    assert judge(watch_of(loaded), support.zkt_config(), 2.0, 120.0, final=False,
                 restart=True) is None  # watch on: the settings line alone is not enough
    report = judge(watch_of(loaded, SETTINGS_GONE[-1]), support.zkt_config(), 2.0, 120.0,
                   final=False, restart=True)
    assert report.outcome is BootOutcome.SETTINGS_LOST
    # Time up with the settings line only: the first start's words for a start not seen.
    report = judge(watch_of(loaded), support.zkt_config(), 121.0, 120.0, final=True,
                   restart=True)
    assert report.outcome is BootOutcome.NO_OUTPUT and report.advice is texts.START_NOT_SEEN


@pytest.mark.parametrize("lines, zone, device", [
    (RESTART_BOOT[8:], "LF-ZONE-GUJRAT-01", "1"),          # settings line and start line
    ([RESTART_BOOT[8], *RESTART_BOOT[10:]], "LF-ZONE-GUJRAT-01", None),  # settings line only
    (RESTART_BOOT[9:], "LF-ZONE-GUJRAT-01", "1"),          # start line only
])
def test_restart_check_takes_either_line_that_names_the_settings(lines, zone, device):
    report = judge(watch_of(*lines), support.zkt_config(), 2.0, 120.0, final=False,
                   restart=True)
    assert report.outcome is BootOutcome.OK and report.advice.code == "RESTART_OK"
    assert (report.zone_id_seen, report.device_id_seen) == (zone, device)


def test_restart_check_without_either_line_is_not_a_success():
    # Both lines came before the port opened: running, but nothing shows which settings.
    running = RESTART_BOOT[10:]  # storage and Wi-Fi only
    first = judge(watch_of(*running), support.zkt_config(), 2.0, 120.0, final=False)
    assert first.outcome is BootOutcome.OK  # the first start passes on these
    report = judge(watch_of(*running), support.zkt_config(), 2.0, 120.0, final=False,
                   restart=True)
    assert report.outcome is BootOutcome.SETTINGS_NOT_SEEN
    assert report.advice is texts.SETTINGS_NOT_SEEN and report.advice.can_retry


def test_restart_check_with_the_lines_lost_before_the_port_opened():
    lines = RESTART_BOOT
    watcher, script, _clock = start_missed(lines[:10], lines[10:])
    report = second_start(watcher)[0]
    assert lines[8] in script.dropped and lines[9] in script.dropped
    assert report.outcome is BootOutcome.SETTINGS_NOT_SEEN


def test_restart_check_names_other_settings_as_the_first_start_does():
    lines = [line.replace("GUJRAT-01", "GUJRAT-02") for line in RESTART_BOOT]
    watcher, _script, _clock = watcher_with([(1, "unplug", None), (2, "plug", None),
                                             (2.2, "print", lines)])
    report = second_start(watcher)[0]
    assert report.outcome is BootOutcome.WRONG_SETTINGS
    assert "LF-ZONE-GUJRAT-02" in report.advice.what_happened
    # Nor can a first start that showed neither line tell what the board had then.
    missed = BootReport(BootOutcome.OK, None, None, "2.9.1")
    report = judge(watch_of(*lines[8:]), support.zkt_config(), 2.0, 120.0, final=False,
                   restart=True, first=missed, board_id=MAC)
    assert report.outcome is BootOutcome.WRONG_SETTINGS


def test_other_settings_than_at_the_first_start_were_not_kept():
    # The first start showed this setup's zone; the next start names another one. Fix
    # settings would last only until the next restart, as with lost settings.
    lines = [line.replace("GUJRAT-01", "GUJRAT-02") for line in RESTART_BOOT]
    watcher, _script, _clock = watcher_with([(1, "unplug", None), (2, "plug", None),
                                             (2.2, "print", lines)])
    report = second_start(watcher, first=FIRST_SEEN)[0]
    advice = lost_advice(report, changed=True)
    assert "other settings" in advice.what_happened
    assert report.zone_id_seen == "LF-ZONE-GUJRAT-02"
    config = support.zkt_config()
    # The settings line alone, also with the zone gone from the settings.
    for line in (RESTART_BOOT[8].replace("GUJRAT-01", "GUJRAT-02"),
                 "I (1600) zone_config: Loaded per-device provisioning for zone="):
        lost_advice(judge(watch_of(line), config, 2.0, 120.0, final=False, restart=True,
                          first=FIRST_SEEN, board_id=MAC), changed=True)
    # Another device number, when the first start showed the device.
    other_device = RESTART_BOOT[9].replace("device_id=1", "device_id=2")
    lost_advice(judge(watch_of(other_device, *RESTART_BOOT[10:]), config, 2.0, 120.0,
                      final=False, restart=True, first=FIRST_SEEN, board_id=MAC),
                changed=True)
    # A first start that showed only the zone cannot tell about the device.
    zone_only = BootReport(BootOutcome.OK, "LF-ZONE-GUJRAT-01", None, "2.9.1")
    report = judge(watch_of(other_device, *RESTART_BOOT[10:]), config, 2.0, 120.0,
                   final=False, restart=True, first=zone_only, board_id=MAC)
    assert report.outcome is BootOutcome.WRONG_SETTINGS
    # The same settings again pass, also when given the first start.
    report = judge(watch_of(*RESTART_BOOT[8:]), config, 2.0, 120.0, final=False,
                   restart=True, first=FIRST_SEEN, board_id=MAC)
    assert report.outcome is BootOutcome.OK and report.advice.code == "RESTART_OK"


def test_restart_check_judged_when_its_time_is_up():
    # The watch can end before the storage line (a short bench --boot-seconds, or a first
    # program line late in the watch): the restart rule holds then too.
    wifi = RESTART_BOOT[11]
    assert "Connecting to Wi-Fi" in wifi
    watcher, _script, _clock = watcher_with([
        (1.0, "unplug", None), (4.0, "plug", None), (4.5, "print", RESTART_BOOT[:8] + [wifi])])
    report = second_start(watcher, timeout=10)[0]
    assert report.outcome is BootOutcome.SETTINGS_NOT_SEEN
    assert report.advice is texts.SETTINGS_NOT_SEEN
    watcher, _script, _clock = watcher_with([
        (1.0, "unplug", None), (4.0, "plug", None), (4.5, "print", RESTART_BOOT[:9] + [wifi])])
    report = second_start(watcher, timeout=10)[0]
    assert report.outcome is BootOutcome.OK and report.advice.code == "RESTART_OK"
    assert report.zone_id_seen == "LF-ZONE-GUJRAT-01"


@pytest.mark.parametrize("steps, timeout, outcome, advice", [
    ([(1, "unplug", None), (2, "plug", None)], 30, BootOutcome.NO_OUTPUT, texts.NO_OUTPUT),
    ([], 120, BootOutcome.NOT_REPLUGGED, texts.NOT_REPLUGGED),
    ([(1, "unplug", None)], 120, BootOutcome.NOT_REPLUGGED, texts.NOT_REPLUGGED),
], ids=["silent", "not-unplugged", "not-plugged-back"])
def test_restart_check_reuses_the_first_start_advice(steps, timeout, outcome, advice):
    watcher, _script, _clock = watcher_with(steps)
    report = second_start(watcher, timeout=timeout)[0]
    assert report.outcome is outcome and report.advice is advice


def test_crash_loop_at_the_restart_check_reuses_the_first_start_advice():
    watcher, _script, _clock = crash_every(3.0, first=RESTART_BOOT[:10], then=RESTART_BOOT[4:10])
    report = second_start(watcher)[0]
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP and report.advice is texts.CRASH_LOOP


def test_restart_check_keeps_the_pc_awake(monkeypatch):
    from sl_installer.core import keepawake
    calls = []
    monkeypatch.setattr(keepawake, "_set_state", lambda flags: calls.append(flags) or True)
    watcher, _script, _clock = watcher_with([
        (1.0, "unplug", None), (4.0, "plug", None), (4.5, "print", RESTART_BOOT)])
    assert second_start(watcher)[0].outcome is BootOutcome.OK
    assert calls == [keepawake.AWAKE, keepawake.RELEASED]


# ------------------------------------------------------- the install's console log

def test_each_watched_start_is_kept_in_the_installs_console_log(tmp_path):
    from sl_installer.core.logging_setup import register_secret
    register_secret("test-wifi-pass")
    # Before the storage line, at which the first start's verdict ends the watch.
    secret_lines = ["I (1800) zone_lite: debug password=test-wifi-pass",
                    "I (1810) zone_lite: joining test-wifi-pass now"]
    watcher, _script, _clock = watcher_with([
        (1.0, "unplug", None), (4.0, "plug", None),
        (4.5, "print", [G + GOOD_BOOT[0] + N] + GOOD_BOOT[1:11] + secret_lines + GOOD_BOOT[11:]),
        (60.0, "unplug", None), (63.0, "plug", None), (63.5, "print", SETTINGS_GONE)],
        console_logs=tmp_path)
    first = first_boot(watcher, transcript="sli-0123456789abcdef")[0]
    again = second_start(watcher, transcript="sli-0123456789abcdef")[0]
    assert (first.outcome, again.outcome) == (BootOutcome.OK, BootOutcome.SETTINGS_LOST)
    (path,) = tmp_path.iterdir()
    assert path.name == "console-ac276ea43d30-sli-0123456789abcdef.log"
    text = path.read_text(encoding="utf-8")
    first_part, second_part = text.split("=== Second start, after a restart, ")
    assert first_part.startswith("=== First start, ")
    assert "# the board was unplugged" in first_part and "the port" in first_part
    # Every line as the board printed it, without colours, in order.
    assert "ESP-ROM:esp32s3-20210327" in first_part and "\x1b" not in text
    assert first_part.index("Loaded per-device provisioning") < first_part.index("Storage ready")
    assert "=== Result: ok (BOOT_OK) ===" in first_part
    assert "No encrypted per-device provisioning namespace" in second_part
    assert "Loaded per-device provisioning" not in second_part
    assert "=== Result: settings_lost (SETTINGS_LOST_ON_RESTART) ===" in second_part
    # Redacted as installer.log is: registered secrets and secret-looking names.
    assert "test-wifi-pass" not in text
    assert "password=[redacted]" in text and "joining [redacted] now" in text


def test_no_console_log_without_a_log_folder_or_an_install(tmp_path):
    steps = [(1.0, "unplug", None), (4.0, "plug", None), (4.5, "print", GOOD_BOOT)]
    watcher, _script, _clock = watcher_with(steps)
    assert first_boot(watcher, transcript="sli-1")[0].outcome is BootOutcome.OK
    watcher, _script, _clock = watcher_with(steps, console_logs=tmp_path)
    assert first_boot(watcher)[0].outcome is BootOutcome.OK
    assert list(tmp_path.iterdir()) == []


def test_a_console_log_problem_never_stops_the_watch(tmp_path):
    blocked = tmp_path / "not-a-folder"
    blocked.write_text("", encoding="utf-8")
    watcher, _script, _clock = watcher_with(
        [(1.0, "unplug", None), (4.0, "plug", None), (4.5, "print", GOOD_BOOT)],
        console_logs=blocked)
    assert first_boot(watcher, transcript="sli-1")[0].outcome is BootOutcome.OK


def test_console_log_stops_growing_at_its_size(tmp_path):
    log = console.ConsoleLog(tmp_path / "console-x.log", FakeClock(), max_bytes=4000)
    for section in range(3):
        log.begin(f"Start {section}")
        for index in range(400):
            log.line(f"I ({index}) zone_lite: a line the board printed, number {index}")
        log.end(BootReport(BootOutcome.OK))
    text = (tmp_path / "console-x.log").read_text(encoding="utf-8")
    size = (tmp_path / "console-x.log").stat().st_size
    assert 4000 - 200 < size < 4000 + 1000
    assert text.count("this file is full") == 3 and text.count("=== Result: ok (-) ===") == 3
    assert "number 0" in text and "number 399" not in text


def test_old_console_logs_go_so_the_folder_stays_small(tmp_path):
    import os
    for index in range(console.CONSOLE_LOGS_KEPT + 5):
        path = tmp_path / f"console-ac276ea43d30-sli-{index:02d}.log"
        path.write_text("x\n", encoding="utf-8")
        os.utime(path, (1_000_000 + index, 1_000_000 + index))
    (tmp_path / "installer.log").write_text("kept\n", encoding="utf-8")
    watcher, _script, _clock = watcher_with(
        [(1.0, "unplug", None), (4.0, "plug", None), (4.5, "print", GOOD_BOOT)],
        console_logs=tmp_path)
    assert first_boot(watcher, transcript="sli-new")[0].outcome is BootOutcome.OK
    kept = console.console_log_files(tmp_path)
    assert len(kept) == console.CONSOLE_LOGS_KEPT
    names = {path.name for path in kept}
    assert "console-ac276ea43d30-sli-new.log" in names
    assert "console-ac276ea43d30-sli-24.log" in names and "console-ac276ea43d30-sli-05.log" \
        not in names
    assert (tmp_path / "installer.log").exists()


# ------------------------------------------- the bootloader refuses to start the program

def xmc_loop_lines():
    """The board's own lines of the real 3 Nov 2026 loop (fixtures/boards), oldest first."""
    lines = []
    for row in support.board_sample("xmc-20-7018-first-start-console.txt").splitlines():
        text = row[9:] if row[:9].strip().replace(".", "").isdigit() else ""
        if text and not text.startswith("#"):
            lines.append(text)
    return lines


@pytest.mark.parametrize("line, reason, fatal", [
    ("E (35) bootloader_flash: XMC flash startup fail", "xmc_flash", "1"),
    ("E (37) boot.esp32s3: failed when running XMC startup flow, reboot!", "xmc_flash", "1"),
    ("E (40) boot: load partition table error!", "partition_table", "1"),
    ("E (40) boot: Failed to verify partition table", "partition_table", "1"),
    ("E (41) flash_parts: partition 3 invalid magic number 0x0", "partition_table", "1"),
    ("E (60) boot: No bootable app partitions in the partition table", "no_app", "1"),
    ("E (20) boot: failed to load bootloader image header!", "bootloader_image", "1"),
    ("E (58) boot: Factory app partition is not bootable", "no_app", "0"),
    ("E (57) esp_image: Image hash failed - image is corrupt", "image", "0"),
    ("E (57) esp_image: image at 0x20000 has invalid magic byte (nothing flashed here?)",
     "image", "0"),
    ("E (50) boot_comm: chip revision check failed. Required >= v0.3, found v0.2.",
     "chip_revision", "0"),
    ("E (90) secure_boot_v2: Secure Boot V2 verification failed.", "secure_boot", "0"),
    ("E (90) boot: something else went wrong", "other", "0"),
])
def test_bootloader_failure_lines(line, reason, fatal):
    parser = ConsoleParser(MAC)
    parser.parse("ESP-ROM:esp32s3-20210327")  # the bootloader prints now
    event = parser.parse(line, 0.2)
    assert event.kind is ConsoleKind.BOOTLOADER_FAILED, event
    assert event.data == {"reason": reason, "fatal": fatal}


def test_shared_tags_count_only_while_the_bootloader_prints():
    # The running program checks other copies too (OTA): its esp_image errors are no refusal.
    line = "E (9000) esp_image: image at 0x520000 has invalid magic byte (nothing flashed here?)"
    unknown = ConsoleParser(MAC)
    assert unknown.parse(line).kind is ConsoleKind.OTHER  # the port opened halfway
    running = ConsoleParser(MAC)
    running.parse("ESP-ROM:esp32s3-20210327")
    running.parse("I (1700) zone_lite: Zone Lite starting zone=Z device_id=1")
    assert running.parse(line).kind is ConsoleKind.OTHER
    # Bootloader-only tags always count; info lines never.
    assert unknown.parse("E (35) bootloader_flash: XMC flash startup fail").kind is \
        ConsoleKind.BOOTLOADER_FAILED
    flow = unknown.parse("I (29) bootloader_flash: XM25QHxxC startup flow")
    assert flow.kind is ConsoleKind.OTHER and flow.data == {"signal": "xmc_flow"}
    assert unknown.parse("I (29) boot: Multicore bootloader").kind is ConsoleKind.OTHER
    # The bootloader's own lines showed who prints now (the ROM line came before the port).
    assert unknown.parse(line).kind is ConsoleKind.BOOTLOADER_FAILED


def test_real_xmc_loop_says_the_memory_chip_not_the_cable():
    lines = xmc_loop_lines()
    assert "E (35) bootloader_flash: XMC flash startup fail" in lines
    watcher, script, clock = watcher_with([(1, "unplug", None), (2, "plug", None),
                                           (2.2, "print", lines)])
    started = clock.now
    report, _phases, events = first_boot(watcher)
    assert report.outcome is BootOutcome.STARTUP_REFUSED
    assert report.advice.code == "FLASH_CHIP_UNSUPPORTED" and not report.advice.can_retry
    assert "memory chip" in report.advice.what_happened
    assert report.board_said == "E (35) bootloader_flash: XMC flash startup fail"
    assert clock.now - started < 10  # said at once, not after 120 s
    assert any(event.kind is ConsoleKind.BOOTLOADER_FAILED for event in events)


def test_xmc_loop_beats_the_fast_restart_loop():
    # A computer that loses the port at every restart: the loop of a power problem, but the
    # board's own words say it is the memory chip.
    xmc = ["ESP-ROM:esp32s3-20210327", "rst:0x3 (RTC_SW_SYS_RST),boot:0x28 (SPI_FAST_FLASH_BOOT)",
           "I (29) boot: ESP-IDF v5.5.3 2nd stage bootloader",
           "I (29) bootloader_flash: XM25QHxxC startup flow",
           "E (35) bootloader_flash: XMC flash startup fail",
           "E (37) boot.esp32s3: failed when running XMC startup flow, reboot!"]
    steps = [(1, "unplug", None)]
    for cycle in range(5):
        moment = 3 + cycle * 3.0
        steps += [(moment, "plug", None), (moment + 0.2, "print", xmc),
                  (moment + 2.4, "unplug", None)]
    watcher, _script, _clock = watcher_with(steps)
    report = first_boot(watcher)[0]
    assert report.outcome is BootOutcome.STARTUP_REFUSED
    assert report.advice.code == "FLASH_CHIP_UNSUPPORTED"


def test_other_refusals_quote_the_line_and_ask_for_a_help_file():
    lines = ["ESP-ROM:esp32s3-20210327", "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)",
             "I (30) boot: ESP-IDF v5.5.3 2nd stage bootloader",
             "E (57) esp_image: Image hash failed - image is corrupt",
             "E (58) boot: Factory app partition is not bootable",
             "E (60) boot: No bootable app partitions in the partition table"]
    watcher, _script, _clock = watcher_with([(1, "unplug", None), (2, "plug", None),
                                             (2.2, "print", lines)])
    report = first_boot(watcher)[0]
    assert report.outcome is BootOutcome.STARTUP_REFUSED
    advice = report.advice
    assert advice.code == "STARTUP_REFUSED" and not advice.can_retry
    # The first error of that start is the cause the others follow.
    assert '"E (57) esp_image: Image hash failed - image is corrupt"' in advice.what_happened
    assert any("Save a help file" in step for step in advice.what_to_do)
    assert report.board_said == "E (57) esp_image: Image hash failed - image is corrupt"


def test_a_refusal_the_bootloader_recovers_from_is_a_good_start():
    # ota_0 is damaged, the bootloader falls back to the factory copy and the program runs.
    lines = (GOOD_BOOT[:3] + ["E (57) esp_image: Image hash failed - image is corrupt",
                              "E (58) boot: OTA app partition slot 0 is not bootable",
                              "I (95) boot: Loaded app from partition at offset 0x20000"]
             + GOOD_BOOT[5:])
    watcher, _script, _clock = watcher_with([(1, "unplug", None), (2, "plug", None),
                                             (2.2, "print", lines)])
    assert first_boot(watcher)[0].outcome is BootOutcome.OK


def test_a_refusal_that_may_recover_waits_for_a_restart_or_the_end():
    parser = ConsoleParser(MAC)
    lines = ["ESP-ROM:esp32s3-20210327", "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)",
             "E (58) boot: Factory app partition is not bootable"]
    watch = console._Watch(started=0.0, events=[parser.parse(line, 0.2) for line in lines])
    config = support.zkt_config()
    assert judge(watch, config, 5.0, 120.0, final=False) is None
    assert judge(watch, config, 121.0, 120.0, final=True).outcome is BootOutcome.STARTUP_REFUSED
    # The bootloader started the board again: one refused start may still be a one-off.
    watch.events += [parser.parse(line, 0.4) for line in lines[:2]]
    assert judge(watch, config, 5.0, 120.0, final=False) is None
    # Refused again, and started again: two in a row, the refusal stands.
    watch.events += [parser.parse(line, 0.6) for line in lines[2:]]
    watch.events += [parser.parse(line, 0.8) for line in lines[:2]]
    assert judge(watch, config, 5.0, 120.0, final=False).outcome is BootOutcome.STARTUP_REFUSED


def test_bootloader_lines_reach_the_screen_past_the_other_line_limit():
    # Check a board shows everything the board says; ROM lines are capped, refusals not.
    cancel = CancelToken()
    lines = xmc_loop_lines() * 30
    watcher, _script, clock = watcher_with([(0.5, "print", lines)])
    clock.hooks.append(lambda now: cancel.cancel() if now > 1010 else None)
    seen = []
    watcher.watch_console(support.FakeUsb().candidate(), seen.append, cancel)
    refusals = [event for event in seen if event.kind is ConsoleKind.BOOTLOADER_FAILED]
    others = [event for event in seen if event.kind is ConsoleKind.OTHER and not event.data]
    assert len(others) == BootWatcher.MAX_OTHER_EVENTS
    assert len(refusals) == sum(1 for line in lines if line.startswith("E ("))


def test_failed_starts_keep_the_line_that_decided_them():
    lines = GOOD_BOOT[:9] + ["E (1520) zone_lite: Encrypted NVS initialization failed (X); "
                             "preserving provisioning data and staying inert"]
    watcher, _s, _c = watcher_with([(1, "unplug", None), (2, "plug", None), (2.2, "print", lines)])
    report = first_boot(watcher)[0]
    assert report.board_said == lines[-1]
    good, _s, _c = watcher_with([(1, "unplug", None), (2, "plug", None),
                                 (2.2, "print", GOOD_BOOT)])
    assert first_boot(good)[0].board_said is None
    # Without a line that decided it, the newest error line the board printed.
    silent = ["ESP-ROM:esp32s3-20210327", "E (900) zone_lite: something odd"]
    watcher, _s, _c = watcher_with([(1, "unplug", None), (2, "plug", None),
                                    (2.2, "print", silent)])
    report = first_boot(watcher, timeout=30)[0]
    assert report.outcome is BootOutcome.NO_OUTPUT and report.board_said == silent[-1]
