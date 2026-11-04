"""Start-up diagnosis scenarios from the 1.1.0 review (4 Nov 2026), and the two
PSRAM-faulty boards of that day (fixtures/boards/psram-fault-*)."""
from __future__ import annotations

from sl_installer.core import texts
from sl_installer.core.api import BootOutcome, CancelToken, ConsoleKind
from sl_installer.core.console import BootWatcher, ConsoleParser, startup_refusal

from . import support
from .support import MAC, FakeClock, FakeUsb, SerialScript

ROM = ["ESP-ROM:esp32s3-20210327", "Build:Mar 27 2021",
       "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)", "SPIWP:0xee",
       "mode:DIO, clock div:1", "load:0x3fce2980,len:0x2d94", "entry 0x403c8924"]
BOOTLOADER = [
    "I (29) boot: ESP-IDF v5.5.3 2nd stage bootloader",
    "I (29) boot: compile time Nov  2 2026 04:52:02",
    "I (29) boot: Multicore bootloader",
    "I (30) boot: chip revision: v0.2",
    "I (30) boot: efuse block revision: v1.4",
    "I (31) boot.esp32s3: Boot SPI Speed : 80MHz",
    "I (31) boot.esp32s3: SPI Mode       : DIO",
    "I (31) boot.esp32s3: SPI Flash Size : 16MB",
    "I (32) boot: Enabling RNG early entropy source...",
    "I (32) boot: Partition Table:",
    "I (33) boot: End of partition table",
]
SB_FIRST = [
    "I (195) secure_boot_v2: enabling secure boot v2...",
    "I (195) efuse: Batch mode of writing fields is enabled",
    "I (196) secure_boot_v2: Secure boot digests absent, generating..",
    "I (300) secure_boot_v2: Digests successfully calculated, 1 valid signatures (image offset 0x0)",
    "I (300) secure_boot_v2: 1 signature block(s) found appended to the bootloader.",
    "I (300) secure_boot_v2: Burning public key hash to eFuse",
    "I (300) efuse: Writing EFUSE_BLK_KEY1 with purpose 9",
    "I (400) secure_boot_v2: 1 signature block(s) found appended to the app.",
    "I (400) secure_boot_v2: Application key(0) matches with bootloader key(0).",
    "I (400) secure_boot_v2: blowing secure boot efuse...",
    "I (400) secure_boot: Enabling Security download mode...",
    "I (401) efuse: BURN BLOCK5 - OK (write block == read block)",
    "I (401) efuse: BURN BLOCK0 - OK (all write block bits are set)",
    "I (401) efuse: Batch mode. Prepared fields are committed",
    "I (402) secure_boot_v2: Secure boot permanently enabled",
]
APP = [
    "I (403) boot: Disabling RNG early entropy source...",
    "I (404) octal_psram: vendor id    : 0x0d (AP)",
    "I (405) esp_psram: Found 8MB PSRAM device",
    "I (405) cpu_start: Multicore app",
    "I (838) esp_psram: SPI SRAM memory test OK",
    "I (847) cpu_start: Pro cpu start user code",
    "I (847) app_init: Project name:     zone_lite",
    "I (847) app_init: App version:      2.8.1",
    "I (848) efuse_init: Chip rev:         v0.2",
    "I (849) spi_flash: detected chip: generic",
    "W (849) spi_flash: Detected size(16384k) larger than the size in the binary image header(4096k). Using the size in the binary image header.",
    "I (850) nvs_sec_provider: NVS Encryption - Registering HMAC-based scheme...",
    "I (851) main_task: Calling app_main()",
    "I (900) nvs: NVS partition \"nvs\" is encrypted.",
    "I (1600) zone_config: Loaded per-device provisioning for zone=LF-ZONE-GUJRAT-01",
    "I (1700) zone_lite: Zone Lite starting zone=LF-ZONE-GUJRAT-01 device_id=1",
    # The predecessor check of the other copy, from the running program.
    "I (1750) esp_image: segment 0: paddr=00520020 vaddr=3c020020 size=0a798h ( 42904) map",
    "E (1760) esp_image: Image hash failed - image is corrupt",
    "W (1800) zone_lite: something recoverable",
    "E (1810) zone_lite: something recoverable but an error",
    "I (2900) zone_lite: Storage ready; loaded 0 known event UIDs",
    "I (3000) zone_lite: Connecting to Wi-Fi SSID RE7",
]


def zkt_new_board():
    return (ROM + BOOTLOADER + [
        "I (34) esp_image: segment 0: paddr=002a0020 vaddr=3c0a0020 size=1fa14h (129556) map",
        "I (121) esp_image: Verifying image signature...",
        "I (121) secure_boot_v2: Verifying with RSA-PSS...",
        "I (124) secure_boot_v2: Signature verified successfully!",
        "I (194) boot: Loaded app from partition at offset 0x2a0000"] + SB_FIRST + APP)


def watcher_with(steps):
    clock = FakeClock()
    usb = FakeUsb(mac=MAC)
    script = SerialScript(clock, usb)
    for moment, action, value in steps:
        script.at(clock.now + moment, action, value)
    clock.hooks.append(script.tick)
    return BootWatcher(usb, clock, opener=script.opener), clock


def first(lines, timeout=120.0):
    watcher, _clock = watcher_with([(1, "unplug", None), (2, "plug", None), (2.2, "print", lines)])
    return watcher.wait_first_boot(MAC, support.zkt_config(), lambda p: None, lambda e: None,
                                   CancelToken(), timeout_s=timeout)


def test_healthy_new_zkt_board_is_ok():
    report = first(zkt_new_board())
    assert report.outcome is BootOutcome.OK, report.advice


def test_healthy_hik_factory_is_ok():
    lines = ROM + BOOTLOADER + ["I (29) boot: Defaulting to factory image",
                                "I (194) boot: Loaded app from partition at offset 0x20000"] + \
        SB_FIRST + [line.replace("2.8.1", "3.1.4").replace("zone_lite\n", "x") for line in APP]
    report = first(lines)
    assert report.outcome is BootOutcome.OK, report.advice


def test_healthy_port_opened_halfway_is_ok():
    report = first(zkt_new_board()[30:])
    assert report.outcome is BootOutcome.OK, report.advice


def test_diagnosis_healthy_has_no_refusal():
    parser = ConsoleParser(MAC)
    events = [e for e in (parser.parse(line, 0.1) for line in zkt_new_board()) if e is not None]
    assert startup_refusal(events) is None
    assert not any(e.kind is ConsoleKind.BOOTLOADER_FAILED for e in events), [
        e.raw for e in events if e.kind is ConsoleKind.BOOTLOADER_FAILED]


def test_secure_boot_failure_after_efuse_lines_is_classified():
    # secure_boot.c:385 after the efuse batch commit; boot tag is next (bootloader_utility.c:696)
    parser = ConsoleParser(MAC)
    lines = ROM + BOOTLOADER + ["I (194) boot: Loaded app from partition at offset 0x2a0000",
                                "I (195) secure_boot_v2: enabling secure boot v2...",
                                "I (195) efuse: Batch mode of writing fields is enabled",
                                "E (401) efuse: BURN BLOCK0 - ERROR (written bits != read bits)",
                                "E (402) secure_boot_v2: Error programming security eFuses (err=0x1).",
                                "E (402) boot: Secure Boot v2 failed (-1)",
                                "E (402) boot: OTA app partition slot 0 is not bootable"]
    events = [parser.parse(line, 0.1) for line in lines]
    kinds = [(e.raw, e.kind.value) for e in events if e and e.raw.startswith("E ")]
    assert kinds[1][1] == "bootloader_failed", kinds


def test_one_bad_start_then_a_good_one_is_not_refused():
    # "A later start that went on clears it": while watching, one refused start is not enough.
    bad = ROM + BOOTLOADER + ["I (34) boot: Defaulting to factory image",
                              "E (40) esp_image: Checksum failed. Calculated 0x12 read 0x34",
                              "E (41) boot: Factory app partition is not bootable",
                              "E (42) boot: No bootable app partitions in the partition table"]
    good = [line.replace("POWERON", "RTC_SW_SYS_RST") for line in zkt_new_board()]
    report = first(bad + good)
    # Diagnosis over the same lines says the board is fine:
    parser = ConsoleParser(MAC)
    events = [e for e in (parser.parse(line, 0.1) for line in bad + good) if e is not None]
    assert startup_refusal(events) is None  # Check a board agrees over the same lines
    assert report.outcome is not BootOutcome.STARTUP_REFUSED


def test_nonfatal_error_then_a_good_restart_is_not_refused():
    bad = ROM + BOOTLOADER + ["E (40) boot_comm: Image requires efuse blk rev >= v1.3, but chip is v1.2"]
    good = [line.replace("POWERON", "RTC_SW_SYS_RST") for line in zkt_new_board()]
    report = first(bad + good)
    assert report.outcome is not BootOutcome.STARTUP_REFUSED


def test_one_xmc_retry_then_a_good_start_is_not_refused():
    # A listed XMC chip (passes esptool's gate) whose startup flow fails once after power-on;
    # bootloader_reset() retries it and the next start is fine.
    bad = ROM + ["I (29) boot: ESP-IDF v5.5.3 2nd stage bootloader",
                 "I (29) bootloader_flash: XM25QHxxC startup flow",
                 "E (35) bootloader_flash: XMC flash startup fail",
                 "E (37) boot.esp32s3: failed when running XMC startup flow, reboot!"]
    good = [line.replace("POWERON", "RTC_SW_SYS_RST") for line in zkt_new_board()]
    report = first(bad + good)
    parser = ConsoleParser(MAC)
    events = [e for e in (parser.parse(line, 0.1) for line in bad + good) if e is not None]
    assert startup_refusal(events) is None  # Check a board agrees over the same lines
    assert report.outcome is not BootOutcome.STARTUP_REFUSED


def test_first_boot_secure_boot_key_mismatch_quotes_the_cause():
    def attempt(offset, slot_words):
        return [f"I (194) boot: Loaded app from partition at offset {offset}",
                "I (195) secure_boot_v2: enabling secure boot v2...",
                "I (195) efuse: Batch mode of writing fields is enabled",
                "I (196) secure_boot_v2: Secure boot digests absent, generating..",
                "I (300) secure_boot_v2: 1 signature block(s) found appended to the bootloader.",
                "I (300) secure_boot_v2: Burning public key hash to eFuse",
                "I (400) secure_boot_v2: 1 signature block(s) found appended to the app.",
                "E (400) secure_boot_v2: No application key digest matches the bootloader key digest.",
                "I (400) efuse: Batch mode of writing fields is cancelled",
                "E (401) boot: Secure Boot v2 failed (-1)",
                f"E (401) boot: {slot_words} is not bootable"]
    lines = (ROM + BOOTLOADER + attempt("0x2a0000", "OTA app partition slot 0")
             + attempt("0x20000", "Factory app partition")
             + attempt("0x520000", "OTA app partition slot 1")
             + ["E (402) boot: No bootable app partitions in the partition table"])
    report = first(lines)
    assert "No application key digest" in (report.board_said or "")


def _sample_lines(name):
    return support.board_sample(name).splitlines()


def test_psram_fault_that_hangs_is_a_faulty_board():
    # ac:27:6e:a4:e5:e8: every start stops after "Multicore app" (the PSRAM test), then panics.
    report = first(_sample_lines("psram-fault-hang-a4e5e8-console.txt"))
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP
    assert report.advice is texts.STARTUP_CRASH and not report.advice.can_retry


def test_psram_fault_that_crashes_before_the_program_is_a_faulty_board():
    # ac:27:6e:a4:57:88: the PSRAM test passed once, then every start crashed before app_main.
    report = first(_sample_lines("psram-fault-crash-a45788-console.txt"))
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP
    assert report.advice is texts.STARTUP_CRASH


def test_a_program_that_crashes_after_it_started_keeps_the_crash_advice():
    panic = ["Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.",
             "Backtrace: 0x4038289e:0x3fceb0a0 0x40376ab2:0x3fceb0d0", "Rebooting..."]
    start = [line.replace("POWERON", "RTC_SW_CPU_RST") for line in zkt_new_board()[:-6]]
    report = first(start + panic + start + panic + start + panic)
    assert report.outcome is BootOutcome.FAULTY_RESTART_LOOP
    assert report.advice is texts.CRASH_LOOP
