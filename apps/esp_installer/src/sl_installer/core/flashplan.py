"""Exact esptool/espefuse calls for each install mode (design section 4).

The order is the safety argument, so it lives in one pure function per mode and
is unit-tested call by call:

NEW_BOARD   identity check, erase 0x0-0xFFFF, write every range except the
            bootloader (the blank redo area last), verify on the chip, check the
            key block is blank, burn the HMAC key into BLOCK_KEY0 (HMAC_UP), check
            purpose and protection, write the bootloader last, verify it.
RESUME      the same without the burn; the key must already be HMAC_UP and
            protected.
CHANGE_SETTINGS  identity check, then one write of settings (0x11000), empty
            storage (0x7A0000) and the blank redo area (0xFA0000), in that order,
            with the flags Secure Download Mode accepts.

The redo area (0xFA0000-0xFFFFFF, past the last partition) holds ZKT 2.7.6+'s
last-resort reformat records. Their journal is in NVS (key "reclaim") and they
describe blocks of the store; every mode replaces both, so every mode also
writes the area back to all 0xFF, as on a board that never ran a round, after the
settings and the storage it belonged to. Hikvision firmware never reads the
area: clearing it there too keeps one plan for both families.

Rules every call follows: esptool always gets ``--after no-reset`` (nothing may
boot a Zone Lite image before its key is burned; the only first boot is the
person's unplug and replug), ``--before default-reset`` only enters download
mode, and no call uses erase-flash, run, --force or a reset that boots the app.
espefuse 5.1.0 has no ``--after`` option and never resets after it finishes.

Layout constants come from firmware/zone_lite/tools/flash_prepared_zone_lite.py
and firmware/zone_lite/partitions.csv. The partition table is at 0x10000
(CONFIG_PARTITION_TABLE_OFFSET), so erasing 0x0-0xFFFF removes only the
bootloader: until the bootloader is written back last, no reset can start
anything.
"""
from __future__ import annotations

import enum
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple

from .api import Family, InstallMode, StepId
from .esptool_runner import ESPEFUSE, ESPTOOL, ToolCall

CHIP = "esp32s3"
FLASH_TOTAL = 0x1000000
BOOTLOADER_OFFSET = 0x0
START_AREA_SIZE = 0x10000
PARTITION_TABLE_OFFSET = 0x10000
PARTITION_TABLE_MAX = 0x1000
NVS_OFFSET = 0x11000
NVS_SIZE = 0x6000
OTADATA_OFFSET = 0x17000
OTADATA_SIZE = 0x2000
FACTORY_OFFSET = 0x20000
OTA_0_OFFSET = 0x2A0000
OTA_1_OFFSET = 0x520000
APP_SLOT_SIZE = 0x280000
STORAGE_OFFSET = 0x7A0000
STORAGE_SIZE = 0x800000
# firmware/zone_lite/main/storage_repair.c SR_REDO_OFFSET and SR_REDO_BYTES: 96 sectors
# from the end of storage to the end of the 16 MiB chip, in no partition.
REDO_AREA_OFFSET = 0xFA0000
REDO_AREA_SIZE = 0x60000
ESP_OTA_IMG_VALID = 2

ROLE_OFFSETS: Dict[str, int] = {
    "bootloader": BOOTLOADER_OFFSET,
    "partition-table": PARTITION_TABLE_OFFSET,
    "nvs": NVS_OFFSET,
    "otadata": OTADATA_OFFSET,
    "factory": FACTORY_OFFSET,
    "ota_0": OTA_0_OFFSET,
    "ota_1": OTA_1_OFFSET,
    "storage": STORAGE_OFFSET,
    "redo-area": REDO_AREA_OFFSET,
}
# Ranges the app adds to every mode's main write, never in a bundle: the settings from
# ADD ("nvs") and the blank redo area, made here (blank_redo_area), never downloaded.
APP_ROLES = ("nvs", "redo-area")
# A settings change's one write, in this order: the redo area goes after the settings
# and the storage it belonged to, so a cut write never leaves an old store without it.
CHANGE_SETTINGS_ROLES = ("nvs", "storage", "redo-area")
# Image roles each family's bundle carries (the settings come from ADD as "nvs").
# ZKT boots the release from ota_0 with a qualified predecessor in ota_1; HIK
# boots factory with blank otadata. Both need the empty storage image.
BUNDLE_ROLES: Dict[Family, Tuple[str, ...]] = {
    Family.ZKT: ("bootloader", "partition-table", "otadata", "factory", "ota_0", "ota_1",
                 "storage"),
    Family.HIKVISION: ("bootloader", "partition-table", "otadata", "factory", "storage"),
}
# (type, subtype, offset, size) the bundle's partition table must contain.
PRODUCTION_PARTITIONS: Dict[str, Tuple[int, int, int, int]] = {
    "nvs": (1, 0x02, NVS_OFFSET, NVS_SIZE),
    "otadata": (1, 0x00, OTADATA_OFFSET, OTADATA_SIZE),
    "factory": (0, 0x00, FACTORY_OFFSET, APP_SLOT_SIZE),
    "ota_0": (0, 0x10, OTA_0_OFFSET, APP_SLOT_SIZE),
    "ota_1": (0, 0x11, OTA_1_OFFSET, APP_SLOT_SIZE),
    "storage": (1, 0x82, STORAGE_OFFSET, STORAGE_SIZE),
}

KEEP_FLASH_ARGS = ("--flash-mode", "keep", "--flash-freq", "keep", "--flash-size", "keep")
# Proven on secured boards (tools/usb-wifi-rescue/repair.ps1): Secure Download Mode
# allows only ROM writes, cannot detect the flash size, and must not touch headers.
SDM_WRITE_ARGS = ("--flash-size", "16MB", "--flash-mode", "keep", "--flash-freq", "keep",
                  "--no-compress")
FORBIDDEN_ARGS = ("hard-reset", "soft-reset", "watchdog-reset", "no-reset-stub", "run",
                  "erase-flash", "--force", "load-ram", "--erase-all", "write-mem",
                  "burn-efuse", "burn-key-digest", "read-protect-efuse", "write-protect-efuse")


def otadata_selecting_ota_0() -> bytes:
    """Both otadata sectors, with one valid entry that selects ota_0.

    The bootloader boots the valid entry with the highest ota_seq, slot
    (ota_seq - 1) % 2; the CRC is ESP-IDF otatool's (flash_prepared_zone_lite.py).
    """
    seq = 1
    crc = zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF)
    entry = struct.pack("<I", seq) + b"\xff" * 20 + struct.pack("<II", ESP_OTA_IMG_VALID, crc)
    return entry + b"\xff" * (OTADATA_SIZE - len(entry))


def blank_otadata() -> bytes:
    """Erased otadata: the bootloader then starts the factory app (Hikvision)."""
    return b"\xff" * OTADATA_SIZE


def blank_redo_area() -> bytes:
    """The redo area as a board that never ran a round has it: erased."""
    return b"\xff" * REDO_AREA_SIZE


def partition_entries(table: bytes) -> Dict[str, Tuple[int, int, int, int]]:
    entries = {}
    for start in range(0, len(table) - 31, 32):
        row = table[start:start + 32]
        if row[:2] != b"\xaa\x50":
            break
        kind, subtype, offset, size = struct.unpack_from("<BBII", row, 2)
        label = row[12:28].split(b"\0", 1)[0].decode("ascii", "replace")
        entries[label] = (kind, subtype, offset, size)
    return entries


def partitions_end(table: bytes) -> int:
    """Where the last partition of ``table`` ends.

    Reads the rows partition_entries reads (up to the first that is not an entry),
    but every one of them: a repeated label there keeps only its last row.
    """
    end = 0
    for start in range(0, len(table) - 31, 32):
        row = table[start:start + 32]
        if row[:2] != b"\xaa\x50":
            break
        _kind, _subtype, offset, size = struct.unpack_from("<BBII", row, 2)
        end = max(end, offset + size)
    return end


class Purpose(str, enum.Enum):
    IDENTITY = "identity"                    # get-security-info, read-only
    KEY_STATE_BEFORE_BURN = "key_state_before_burn"
    KEY_STATE_FOR_RESUME = "key_state_for_resume"
    ERASE_START_AREA = "erase_start_area"
    WRITE_FIRMWARE = "write_firmware"
    VERIFY_FIRMWARE = "verify_firmware"
    BURN_KEY = "burn_key"                    # the only irreversible call
    KEY_STATE_AFTER_BURN = "key_state_after_burn"
    WRITE_STARTUP = "write_startup"
    VERIFY_STARTUP = "verify_startup"
    WRITE_SETTINGS = "write_settings"


@dataclass(frozen=True)
class FlashRange:
    role: str
    offset: int
    path: str
    size: int

    @property
    def end(self) -> int:
        return self.offset + self.size


@dataclass(frozen=True)
class PlannedCall:
    step: StepId
    purpose: Purpose
    call: ToolCall
    ranges: Tuple[FlashRange, ...] = ()
    summary_path: Optional[str] = None      # espefuse JSON written here
    key_path: Optional[str] = None          # BURN_KEY: file that must exist only for this call

    @property
    def irreversible(self) -> bool:
        return self.purpose is Purpose.BURN_KEY

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.ranges)


@dataclass(frozen=True)
class FlashPlan:
    mode: InstallMode
    port: str
    calls: Tuple[PlannedCall, ...]

    def steps(self) -> Tuple[StepId, ...]:
        seen = []
        for planned in self.calls:
            if planned.step not in seen:
                seen.append(planned.step)
        return tuple(seen)

    def for_step(self, step: StepId) -> Tuple[PlannedCall, ...]:
        return tuple(planned for planned in self.calls if planned.step is step)


class PlanError(ValueError):
    """The images handed to a plan break the layout; nothing was run."""


def esptool_args(port: str, *command: str, stub: bool = True) -> Tuple[str, ...]:
    args = ["--chip", CHIP, "--port", port, "--before", "default-reset", "--after", "no-reset"]
    if not stub:
        args.append("--no-stub")
    return tuple(args) + tuple(command)


def espefuse_args(port: str, *command: str, confirm_burn: bool = False) -> Tuple[str, ...]:
    args = ["--chip", CHIP, "--port", port, "--before", "default-reset"]
    if confirm_burn:
        # espefuse otherwise waits for someone to type BURN on a console nobody sees.
        args.append("--do-not-confirm")
    return tuple(args) + tuple(command)


def _pairs(ranges: Iterable[FlashRange]) -> Tuple[str, ...]:
    out = []
    for item in ranges:
        out.extend([hex(item.offset), item.path])
    return tuple(out)


def _write_timeout(ranges: Sequence[FlashRange]) -> Tuple[float, float]:
    total = sum(item.size for item in ranges)
    largest = max((item.size for item in ranges), default=0)
    # Worst case about 25 KB/s. Both writers can stay silent for a whole range: the ROM
    # erases a range before writing it, and the stub acknowledges one compressed block
    # before it erases and writes all it expands to (the 8 MiB empty storage image is a
    # single 16 KiB block). esptool itself then waits up to 40 s per MB
    # (ERASE_WRITE_TIMEOUT_PER_MB), hence the idle budget for the largest range.
    timeout = 180.0 + total / 25_000
    idle = 90.0 + 40.0 * largest / 0x100000
    return timeout, idle


def _verify_timeout(ranges: Sequence[FlashRange]) -> Tuple[float, float]:
    total = sum(item.size for item in ranges)
    return 120.0 + total / 200_000, 90.0 + 20.0 * total / 0x100000


def _identity(step: StepId, port: str) -> PlannedCall:
    return PlannedCall(step, Purpose.IDENTITY, ToolCall(
        ESPTOOL, esptool_args(port, "get-security-info", stub=False), "check board identity",
        timeout_s=60.0, idle_timeout_s=45.0))


def _key_state(step: StepId, purpose: Purpose, port: str, path: Path) -> PlannedCall:
    return PlannedCall(step, purpose, ToolCall(
        ESPEFUSE, espefuse_args(port, "summary", "--format", "json", "--file", str(path)),
        "read key state", timeout_s=90.0, idle_timeout_s=60.0), summary_path=str(path))


def _erase_start(step: StepId, port: str) -> PlannedCall:
    return PlannedCall(step, Purpose.ERASE_START_AREA, ToolCall(
        ESPTOOL, esptool_args(port, "erase-region", hex(BOOTLOADER_OFFSET), hex(START_AREA_SIZE)),
        "clear start area", timeout_s=90.0, idle_timeout_s=60.0))


def _write(step: StepId, purpose: Purpose, port: str, ranges: Sequence[FlashRange],
           label: str) -> PlannedCall:
    timeout, idle = _write_timeout(ranges)
    return PlannedCall(step, purpose, ToolCall(
        ESPTOOL, esptool_args(port, "write-flash", *KEEP_FLASH_ARGS, *_pairs(ranges)), label,
        timeout_s=timeout, idle_timeout_s=idle), ranges=tuple(ranges))


def _verify(step: StepId, purpose: Purpose, port: str, ranges: Sequence[FlashRange],
            label: str) -> PlannedCall:
    timeout, idle = _verify_timeout(ranges)
    return PlannedCall(step, purpose, ToolCall(
        ESPTOOL, esptool_args(port, "verify-flash", *KEEP_FLASH_ARGS, *_pairs(ranges)), label,
        timeout_s=timeout, idle_timeout_s=idle), ranges=tuple(ranges))


def _check_ranges(ranges: Sequence[FlashRange]) -> None:
    ordered = sorted(ranges, key=lambda item: item.offset)
    end = 0
    for item in ordered:
        if item.size <= 0:
            raise PlanError(f"{item.role} is empty")
        if item.offset != ROLE_OFFSETS.get(item.role, -1):
            raise PlanError(f"{item.role} is not at its production offset")
        if item.offset < end:
            raise PlanError(f"{item.role} overlaps the range before it")
        if item.end > FLASH_TOTAL:
            raise PlanError(f"{item.role} runs past the end of the 16 MB chip")
        if item.end > REDO_AREA_OFFSET and item.role != "redo-area":
            raise PlanError(f"{item.role} reaches the redo area")
        end = item.end
    limits = {"bootloader": START_AREA_SIZE, "partition-table": PARTITION_TABLE_MAX,
              "factory": APP_SLOT_SIZE, "ota_0": APP_SLOT_SIZE, "ota_1": APP_SLOT_SIZE}
    exact = {"nvs": NVS_SIZE, "otadata": OTADATA_SIZE, "storage": STORAGE_SIZE,
             "redo-area": REDO_AREA_SIZE}
    for item in ordered:
        if item.role in limits and item.size > limits[item.role]:
            raise PlanError(f"{item.role} is too large for its slot")
        if item.role in exact and item.size != exact[item.role]:
            raise PlanError(f"{item.role} must be exactly {exact[item.role]:#x} bytes")


def _split(family: Family, ranges: Sequence[FlashRange]
           ) -> Tuple[FlashRange, Tuple[FlashRange, ...]]:
    roles = sorted(item.role for item in ranges)
    wanted = sorted(BUNDLE_ROLES[family] + APP_ROLES)
    if roles != wanted:
        raise PlanError(f"expected images {wanted}, got {roles}")
    _check_ranges(ranges)
    bootloader = next(item for item in ranges if item.role == "bootloader")
    rest = tuple(sorted((item for item in ranges if item.role != "bootloader"),
                        key=lambda item: item.offset))
    return bootloader, rest


def new_board_plan(port: str, family: Family, ranges: Sequence[FlashRange],
                   work_dir: Path) -> FlashPlan:
    bootloader, rest = _split(family, ranges)
    key_path = str(work_dir / "board-key.bin")
    calls = (
        _identity(StepId.CLEAR_START_AREA, port),
        _erase_start(StepId.CLEAR_START_AREA, port),
        _write(StepId.COPY_FIRMWARE, Purpose.WRITE_FIRMWARE, port, rest, "copy firmware"),
        _verify(StepId.CHECK_COPY, Purpose.VERIFY_FIRMWARE, port, rest, "check copy"),
        _key_state(StepId.LOCK_KEY, Purpose.KEY_STATE_BEFORE_BURN, port,
                   work_dir / "key-state-before.json"),
        PlannedCall(StepId.LOCK_KEY, Purpose.BURN_KEY, ToolCall(
            ESPEFUSE, espefuse_args(port, "burn-key", "BLOCK_KEY0", key_path, "HMAC_UP",
                                    confirm_burn=True),
            "lock security key", timeout_s=120.0, idle_timeout_s=90.0), key_path=key_path),
        _key_state(StepId.LOCK_KEY, Purpose.KEY_STATE_AFTER_BURN, port,
                   work_dir / "key-state-after.json"),
        _write(StepId.COPY_STARTUP, Purpose.WRITE_STARTUP, port, (bootloader,),
               "copy start-up program"),
        _verify(StepId.COPY_STARTUP, Purpose.VERIFY_STARTUP, port, (bootloader,),
                "check start-up program"),
    )
    return _checked(FlashPlan(InstallMode.NEW_BOARD, port, calls))


def resume_plan(port: str, family: Family, ranges: Sequence[FlashRange],
                work_dir: Path) -> FlashPlan:
    bootloader, rest = _split(family, ranges)
    calls = (
        _identity(StepId.CLEAR_START_AREA, port),
        _key_state(StepId.CLEAR_START_AREA, Purpose.KEY_STATE_FOR_RESUME, port,
                   work_dir / "key-state-resume.json"),
        _erase_start(StepId.CLEAR_START_AREA, port),
        _write(StepId.COPY_FIRMWARE, Purpose.WRITE_FIRMWARE, port, rest, "copy firmware"),
        _verify(StepId.CHECK_COPY, Purpose.VERIFY_FIRMWARE, port, rest, "check copy"),
        _write(StepId.COPY_STARTUP, Purpose.WRITE_STARTUP, port, (bootloader,),
               "copy start-up program"),
        _verify(StepId.COPY_STARTUP, Purpose.VERIFY_STARTUP, port, (bootloader,),
                "check start-up program"),
    )
    return _checked(FlashPlan(InstallMode.RESUME, port, calls))


def change_settings_plan(port: str, ranges: Sequence[FlashRange]) -> FlashPlan:
    roles = sorted(item.role for item in ranges)
    if roles != sorted(CHANGE_SETTINGS_ROLES):
        raise PlanError("settings change writes settings, storage and the redo area only, "
                        f"got {roles}")
    _check_ranges(ranges)
    ordered = tuple(sorted(ranges, key=lambda item: item.offset))
    timeout, idle = _write_timeout(ordered)
    calls = (
        _identity(StepId.WRITE_SETTINGS, port),
        PlannedCall(StepId.WRITE_SETTINGS, Purpose.WRITE_SETTINGS, ToolCall(
            ESPTOOL, esptool_args(port, "write-flash", *SDM_WRITE_ARGS, *_pairs(ordered),
                                  stub=False),
            "write new settings", timeout_s=timeout, idle_timeout_s=idle), ranges=ordered),
    )
    return _checked(FlashPlan(InstallMode.CHANGE_SETTINGS, port, calls))


def build_plan(mode: InstallMode, port: str, family: Family, ranges: Sequence[FlashRange],
               work_dir: Path) -> FlashPlan:
    if mode is InstallMode.NEW_BOARD:
        return new_board_plan(port, family, ranges, work_dir)
    if mode is InstallMode.RESUME:
        return resume_plan(port, family, ranges, work_dir)
    return change_settings_plan(port, ranges)


def safety_problems(plan: FlashPlan) -> Tuple[str, ...]:
    """Every way ``plan`` breaks the section 4 rules; empty when it is safe."""
    problems = []
    burns = [i for i, planned in enumerate(plan.calls) if planned.purpose is Purpose.BURN_KEY]
    if plan.mode is not InstallMode.NEW_BOARD and burns:
        problems.append("only a new board may have its key burned")
    if plan.mode is InstallMode.NEW_BOARD and len(burns) != 1:
        problems.append("a new board needs exactly one key burn")
    bootloader_writes = [i for i, planned in enumerate(plan.calls)
                         if any(item.role == "bootloader" for item in planned.ranges)
                         and planned.purpose in (Purpose.WRITE_STARTUP, Purpose.WRITE_FIRMWARE)]
    for index, planned in enumerate(plan.calls):
        args = planned.call.args
        if any(arg in FORBIDDEN_ARGS for arg in args):
            problems.append(f"{planned.call.label}: forbidden argument")
        if planned.call.tool == ESPTOOL:
            if "--after" not in args or args[args.index("--after") + 1] != "no-reset":
                problems.append(f"{planned.call.label}: esptool must use --after no-reset")
        if "--before" in args and args[args.index("--before") + 1] not in (
                "default-reset", "no-reset"):
            problems.append(f"{planned.call.label}: unexpected --before")
        if planned.purpose is Purpose.WRITE_FIRMWARE and burns and index > burns[0]:
            problems.append("firmware must be written before the key burn")
        if planned.call.tool == ESPEFUSE and "--after" in args:
            problems.append("espefuse has no --after option")
        for item in planned.ranges:
            if item.end > FLASH_TOTAL:
                problems.append(f"{planned.call.label}: {item.role} runs past the 16 MB chip")
            if item.end > REDO_AREA_OFFSET and (item.role, item.offset, item.size) != (
                    "redo-area", REDO_AREA_OFFSET, REDO_AREA_SIZE):
                problems.append(f"{planned.call.label}: only the redo area may be written "
                                "past 0xFA0000")
    # Every mode replaces the settings and the store, so its one main write clears their
    # redo records too, once and after them (NEW_BOARD and RESUME still write the
    # bootloader later, on its own).
    main = [planned for planned in plan.calls
            if planned.purpose in (Purpose.WRITE_FIRMWARE, Purpose.WRITE_SETTINGS)]
    if (len(main) != 1 or not main[0].ranges
            or [item.role for item in main[0].ranges].count("redo-area") != 1
            or main[0].ranges[-1].role != "redo-area"):
        problems.append("the redo area must be cleared once, as the last range of the main write")
    if plan.mode in (InstallMode.NEW_BOARD, InstallMode.RESUME):
        erase = [i for i, planned in enumerate(plan.calls)
                 if planned.purpose is Purpose.ERASE_START_AREA]
        writes = [i for i, planned in enumerate(plan.calls) if planned.ranges]
        if not erase or (writes and erase[0] > min(writes)):
            problems.append("the start area must be erased before anything is written")
        if len(bootloader_writes) != 1 or bootloader_writes[0] != max(
                i for i, planned in enumerate(plan.calls)
                if planned.purpose in (Purpose.WRITE_FIRMWARE, Purpose.WRITE_STARTUP)):
            problems.append("the bootloader must be written last")
        if burns and bootloader_writes and bootloader_writes[0] < burns[0]:
            problems.append("the bootloader must be written after the key burn")
        last = plan.calls[-1]
        if last.purpose is not Purpose.VERIFY_STARTUP:
            problems.append("the plan must end by verifying the bootloader")
        verifies = [planned for planned in plan.calls
                    if planned.purpose is Purpose.VERIFY_FIRMWARE]
        if len(verifies) != 1 or not main or verifies[0].ranges != main[0].ranges:
            problems.append("every copied range must be checked on the chip")
    else:
        for planned in plan.calls:
            if planned.call.tool == ESPEFUSE:
                problems.append("a settings change must not touch the key")
            if planned.ranges and "--no-stub" not in planned.call.args:
                problems.append("Secure Download Mode writes need --no-stub")
            roles = tuple(item.role for item in planned.ranges)
            if planned.ranges and roles != CHANGE_SETTINGS_ROLES:
                problems.append("a settings change writes settings, storage and the redo area "
                                "only, in that order")
    return tuple(problems)


def _checked(plan: FlashPlan) -> FlashPlan:
    problems = safety_problems(plan)
    if problems:
        raise PlanError("; ".join(problems))
    return plan


def with_port(planned: PlannedCall, port: str) -> PlannedCall:
    """The same call on another port name (the board re-enumerated)."""
    args = list(planned.call.args)
    args[args.index("--port") + 1] = port
    call = ToolCall(planned.call.tool, tuple(args), planned.call.label,
                    planned.call.timeout_s, planned.call.idle_timeout_s)
    return PlannedCall(planned.step, planned.purpose, call, planned.ranges,
                       planned.summary_path, planned.key_path)
