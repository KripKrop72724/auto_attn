"""The exact esptool/espefuse calls of each mode (design section 4)."""
from __future__ import annotations

import struct
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

from sl_installer.core import flashplan
from sl_installer.core.api import Family, InstallMode, StepId, steps_for
from sl_installer.core.flashplan import FlashRange, PlanError, Purpose

PORT = "COM7"
COMMON = ("--chip", "esp32s3", "--port", PORT, "--before", "default-reset", "--after", "no-reset")
KEEP = ("--flash-mode", "keep", "--flash-freq", "keep", "--flash-size", "keep")
EFUSE = ("--chip", "esp32s3", "--port", PORT, "--before", "default-reset")


def zkt_ranges(tmp: Path):
    sizes = {"bootloader": 0x6000, "partition-table": 0xC00, "nvs": 0x6000, "otadata": 0x2000,
             "factory": 0x190000, "ota_0": 0x190000, "ota_1": 0x150000, "storage": 0x800000}
    names = {"bootloader": "boot.bin", "partition-table": "pt.bin", "nvs": "nvs.bin",
             "otadata": "otadata.bin", "factory": "app.bin", "ota_0": "app.bin",
             "ota_1": "prev.bin", "storage": "storage.bin"}
    return [FlashRange(role, flashplan.ROLE_OFFSETS[role], str(tmp / names[role]), size)
            for role, size in sizes.items()]


def hik_ranges(tmp: Path):
    return [r for r in zkt_ranges(tmp) if r.role not in ("ota_0", "ota_1")]


def path(tmp: Path, name: str) -> str:
    return str(tmp / name)


def test_new_board_zkt_exact_calls(tmp_path):
    plan = flashplan.new_board_plan(PORT, Family.ZKT, zkt_ranges(tmp_path), tmp_path)
    rest = ("0x10000", path(tmp_path, "pt.bin"), "0x11000", path(tmp_path, "nvs.bin"),
            "0x17000", path(tmp_path, "otadata.bin"), "0x20000", path(tmp_path, "app.bin"),
            "0x2a0000", path(tmp_path, "app.bin"), "0x520000", path(tmp_path, "prev.bin"),
            "0x7a0000", path(tmp_path, "storage.bin"))
    expected = [
        (StepId.CLEAR_START_AREA, "esptool", COMMON + ("--no-stub", "get-security-info")),
        (StepId.CLEAR_START_AREA, "esptool", COMMON + ("erase-region", "0x0", "0x10000")),
        (StepId.COPY_FIRMWARE, "esptool", COMMON + ("write-flash",) + KEEP + rest),
        (StepId.CHECK_COPY, "esptool", COMMON + ("verify-flash",) + KEEP + rest),
        (StepId.LOCK_KEY, "espefuse", EFUSE + (
            "summary", "--format", "json", "--file", path(tmp_path, "key-state-before.json"))),
        (StepId.LOCK_KEY, "espefuse", EFUSE + (
            "--do-not-confirm", "burn-key", "BLOCK_KEY0", path(tmp_path, "board-key.bin"),
            "HMAC_UP")),
        (StepId.LOCK_KEY, "espefuse", EFUSE + (
            "summary", "--format", "json", "--file", path(tmp_path, "key-state-after.json"))),
        (StepId.COPY_STARTUP, "esptool", COMMON + ("write-flash",) + KEEP + (
            "0x0", path(tmp_path, "boot.bin"))),
        (StepId.COPY_STARTUP, "esptool", COMMON + ("verify-flash",) + KEEP + (
            "0x0", path(tmp_path, "boot.bin"))),
    ]
    assert [(p.step, p.call.tool, p.call.args) for p in plan.calls] == expected
    assert [p.purpose for p in plan.calls] == [
        Purpose.IDENTITY, Purpose.ERASE_START_AREA, Purpose.WRITE_FIRMWARE,
        Purpose.VERIFY_FIRMWARE, Purpose.KEY_STATE_BEFORE_BURN, Purpose.BURN_KEY,
        Purpose.KEY_STATE_AFTER_BURN, Purpose.WRITE_STARTUP, Purpose.VERIFY_STARTUP]
    assert plan.steps() == steps_for(InstallMode.NEW_BOARD)[1:]
    assert [p.irreversible for p in plan.calls].count(True) == 1
    assert plan.calls[5].key_path == path(tmp_path, "board-key.bin")


def test_new_board_hikvision_writes_factory_only_layout(tmp_path):
    plan = flashplan.new_board_plan(PORT, Family.HIKVISION, hik_ranges(tmp_path), tmp_path)
    write = plan.for_step(StepId.COPY_FIRMWARE)[0]
    assert [r.role for r in write.ranges] == ["partition-table", "nvs", "otadata", "factory",
                                              "storage"]
    assert "0x2a0000" not in write.call.args and "0x520000" not in write.call.args


def test_resume_never_burns_and_checks_the_key_first(tmp_path):
    plan = flashplan.resume_plan(PORT, Family.ZKT, zkt_ranges(tmp_path), tmp_path)
    purposes = [p.purpose for p in plan.calls]
    assert purposes == [Purpose.IDENTITY, Purpose.KEY_STATE_FOR_RESUME, Purpose.ERASE_START_AREA,
                        Purpose.WRITE_FIRMWARE, Purpose.VERIFY_FIRMWARE, Purpose.WRITE_STARTUP,
                        Purpose.VERIFY_STARTUP]
    assert all("burn-key" not in p.call.args for p in plan.calls)
    assert plan.calls[1].call.args == EFUSE + (
        "summary", "--format", "json", "--file", path(tmp_path, "key-state-resume.json"))
    assert plan.steps() == steps_for(InstallMode.RESUME)[1:]


def test_change_settings_uses_only_secure_download_mode_flags(tmp_path):
    ranges = [FlashRange("storage", 0x7A0000, path(tmp_path, "storage.bin"), 0x800000),
              FlashRange("nvs", 0x11000, path(tmp_path, "nvs.bin"), 0x6000)]
    plan = flashplan.change_settings_plan(PORT, ranges)
    assert [(p.step, p.call.tool, p.call.args) for p in plan.calls] == [
        (StepId.WRITE_SETTINGS, "esptool", COMMON + ("--no-stub", "get-security-info")),
        (StepId.WRITE_SETTINGS, "esptool", COMMON + (
            "--no-stub", "write-flash", "--flash-size", "16MB", "--flash-mode", "keep",
            "--flash-freq", "keep", "--no-compress", "0x11000", path(tmp_path, "nvs.bin"),
            "0x7a0000", path(tmp_path, "storage.bin"))),
    ]
    assert plan.steps() == steps_for(InstallMode.CHANGE_SETTINGS)[1:]
    # The ROM erases a whole range silently before writing it: allow for that.
    assert plan.calls[1].call.idle_timeout_s >= 300


@pytest.mark.parametrize("make", [flashplan.new_board_plan, flashplan.resume_plan])
def test_stub_write_of_the_storage_range_may_stay_silent_for_minutes(tmp_path, make):
    # The 8 MiB empty storage image is one compressed stub block: after its progress
    # line the stub erases and writes 8 MiB while esptool prints nothing, for up to
    # 40 s per MB (about 335 s). Killing it at 90 s failed slow boards every time.
    plan = make(PORT, Family.ZKT, zkt_ranges(tmp_path), tmp_path)
    write = plan.for_step(StepId.COPY_FIRMWARE)[0]
    assert write.purpose is Purpose.WRITE_FIRMWARE
    assert any(item.role == "storage" for item in write.ranges)
    assert write.call.idle_timeout_s >= 400
    assert write.call.timeout_s > write.call.idle_timeout_s
    # A short range keeps a short silence limit.
    startup = plan.for_step(StepId.COPY_STARTUP)[0]
    assert startup.purpose is Purpose.WRITE_STARTUP and startup.call.idle_timeout_s < 100


@pytest.mark.parametrize("mode", list(InstallMode))
def test_no_call_can_boot_the_board(tmp_path, mode):
    plan = flashplan.build_plan(mode, PORT, Family.ZKT, zkt_ranges(tmp_path) if mode is not
                                InstallMode.CHANGE_SETTINGS else
                                [r for r in zkt_ranges(tmp_path) if r.role in ("nvs", "storage")],
                                tmp_path)
    for planned in plan.calls:
        args = planned.call.args
        assert not set(args) & {"hard-reset", "soft-reset", "watchdog-reset", "no-reset-stub",
                                "run", "erase-flash", "--force", "load-ram"}
        if planned.call.tool == "esptool":
            assert args[args.index("--after") + 1] == "no-reset"
        else:
            assert "--after" not in args  # espefuse 5.1.0 has no such option
        assert args[args.index("--before") + 1] == "default-reset"
    assert flashplan.safety_problems(plan) == ()


def test_bootloader_is_written_last_after_the_burn(tmp_path):
    plan = flashplan.new_board_plan(PORT, Family.ZKT, zkt_ranges(tmp_path), tmp_path)
    writes = [i for i, p in enumerate(plan.calls) if p.purpose in (Purpose.WRITE_FIRMWARE,
                                                                   Purpose.WRITE_STARTUP)]
    burn = next(i for i, p in enumerate(plan.calls) if p.purpose is Purpose.BURN_KEY)
    erase = next(i for i, p in enumerate(plan.calls) if p.purpose is Purpose.ERASE_START_AREA)
    bootloader_write = next(i for i, p in enumerate(plan.calls)
                            if any(r.role == "bootloader" for r in p.ranges))
    assert erase < min(writes)
    assert writes[0] < burn < bootloader_write == max(writes)
    assert all(r.role != "bootloader" for r in plan.calls[writes[0]].ranges)
    verify = plan.for_step(StepId.CHECK_COPY)[0]
    assert verify.ranges == plan.calls[writes[0]].ranges


def test_safety_check_catches_reordered_or_unsafe_plans(tmp_path):
    plan = flashplan.new_board_plan(PORT, Family.ZKT, zkt_ranges(tmp_path), tmp_path)
    calls = list(plan.calls)
    swapped = replace(plan, calls=tuple(calls[:4] + calls[7:8] + calls[4:7] + calls[8:]))
    assert "the bootloader must be written after the key burn" in \
        flashplan.safety_problems(swapped)
    hard = replace(calls[2].call, args=calls[2].call.args[:7] + ("hard-reset",)
                   + calls[2].call.args[8:])
    unsafe = replace(plan, calls=tuple(calls[:2] + [replace(calls[2], call=hard)] + calls[3:]))
    assert any("no-reset" in problem for problem in flashplan.safety_problems(unsafe))
    burning_resume = replace(plan, mode=InstallMode.RESUME)
    assert "only a new board may have its key burned" in \
        flashplan.safety_problems(burning_resume)
    no_erase = replace(plan, calls=tuple(c for c in calls if c.purpose is not
                                         Purpose.ERASE_START_AREA))
    assert any("erased" in problem for problem in flashplan.safety_problems(no_erase))


def test_plan_refuses_wrong_layouts(tmp_path):
    ranges = zkt_ranges(tmp_path)
    moved = [replace(r, offset=0x8000) if r.role == "partition-table" else r for r in ranges]
    with pytest.raises(PlanError, match="production offset"):
        flashplan.new_board_plan(PORT, Family.ZKT, moved, tmp_path)
    big_boot = [replace(r, size=0x10001) if r.role == "bootloader" else r for r in ranges]
    with pytest.raises(PlanError, match="overlaps|too large"):
        flashplan.new_board_plan(PORT, Family.ZKT, big_boot, tmp_path)
    short_storage = [replace(r, size=0x100000) if r.role == "storage" else r for r in ranges]
    with pytest.raises(PlanError, match="exactly"):
        flashplan.new_board_plan(PORT, Family.ZKT, short_storage, tmp_path)
    with pytest.raises(PlanError, match="expected images"):
        flashplan.new_board_plan(PORT, Family.HIKVISION, ranges, tmp_path)
    with pytest.raises(PlanError, match="settings and storage only"):
        flashplan.change_settings_plan(PORT, ranges)


def test_otadata_selects_ota_0_like_flash_prepared():
    data = flashplan.otadata_selecting_ota_0()
    assert len(data) == 0x2000
    seq, = struct.unpack_from("<I", data, 0)
    state, crc = struct.unpack_from("<II", data, 24)
    assert (seq, state) == (1, 2)
    assert crc == zlib.crc32(struct.pack("<I", 1), 0xFFFFFFFF)
    assert data[4:24] == b"\xff" * 20 and data[32:] == b"\xff" * (0x2000 - 32)
    assert flashplan.blank_otadata() == b"\xff" * 0x2000


def test_partition_entries_reads_the_production_table():
    from .support import partition_table
    entries = flashplan.partition_entries(partition_table())
    for name, expected in flashplan.PRODUCTION_PARTITIONS.items():
        assert entries[name] == expected


def test_with_port_only_changes_the_port(tmp_path):
    plan = flashplan.new_board_plan(PORT, Family.ZKT, zkt_ranges(tmp_path), tmp_path)
    moved = flashplan.with_port(plan.calls[2], "COM9")
    assert moved.call.args[moved.call.args.index("--port") + 1] == "COM9"
    assert [a for a in moved.call.args if a != "COM9"] == \
        [a for a in plan.calls[2].call.args if a != PORT]
    assert moved.ranges == plan.calls[2].ranges
