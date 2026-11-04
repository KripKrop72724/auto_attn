"""Whole installs against a simulated board: order, secrets, failures and resume."""
from __future__ import annotations

import os
import stat
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from sl_installer.core import texts
from sl_installer.core.api import (BoardInfo, Capabilities, CancelToken, DeviceRecord, Family,
                                   HardwareVerdict, HikSettings, InstallerError, InstallMode,
                                   SecurityState, StepId)
from sl_installer.core.installer import PORT_WAIT_S, Installer, cleanup_stale_work
from sl_installer.core.usb import UsbWatcher

from . import support
from .support import (MAC, OTHER_MAC, PORT, FakeBoard, FakeClock, FakeRunner, FakeServer,
                      FakeUsb, Failure)

FACTORY = 0x20000


def blank_board(**changes) -> BoardInfo:
    info = BoardInfo(PORT, MAC, "ESP32-S3", 16, 8, HardwareVerdict.OK, SecurityState.BLANK,
                     False, {})
    return replace(info, **changes)


class Rig:
    def __init__(self, tmp_path: Path, store, bundle, fake: FakeBoard, usb=None,
                 **server_args) -> None:
        self.fake = fake
        self.runner = FakeRunner(fake)
        self.server = FakeServer(store, bundle, **server_args)
        self.usb = usb or FakeUsb()
        self.clock = FakeClock()
        self.work = tmp_path / "work"
        self.steps = []
        self.installer = Installer(runner=self.runner, server=self.server, usb=self.usb,
                                   store=store, app_version="1.0.0", clock=self.clock,
                                   temp_root=self.work, today=lambda: date(2026, 11, 1))

    def run(self, mode=InstallMode.NEW_BOARD, board=None, config=None, cancel=None):
        return self.installer.run(mode, board or blank_board(), config or support.zkt_config(),
                                  self.steps.append, cancel or CancelToken())

    def purposes(self):
        return [c.label for c in self.runner.calls]

    def kinds(self):
        return [event[2] for event in self.server.events]


@pytest.fixture
def rig(tmp_path, store, zkt_bundle):
    return Rig(tmp_path, store, zkt_bundle, FakeBoard())


def test_new_zkt_board_end_to_end(rig, zkt_bundle):
    summary = rig.run()
    assert rig.purposes() == [
        "check board identity", "clear start area", "copy firmware", "check copy",
        "read key state", "lock security key", "read key state", "copy start-up program",
        "check start-up program"]
    files = zkt_bundle.files
    flash = rig.fake.flash
    assert flash[0x0] == files["bootloader-signed.bin"]
    assert flash[0x10000] == files["partition-table.bin"]
    assert flash[0x11000] == rig.server.nvs
    assert flash[0x17000] == files["ota-data.bin"]
    assert flash[FACTORY] == flash[0x2A0000] == files["zone-lite-signed.bin"]
    assert flash[0x520000] == files["rollback-predecessor.bin"]
    assert flash[0x7A0000] == files["storage-empty.bin"]
    assert flash[0xFA0000] == b"\xff" * 0x60000  # the redo area, cleared
    assert rig.fake.erased == [(0x0, 0x10000)]
    assert rig.fake.burned_key == rig.server.hmac_key
    assert summary.mode is InstallMode.NEW_BOARD and summary.mac == MAC
    assert summary.bundle.bundle_id == zkt_bundle.bundle_id
    request = rig.server.requests[0]
    assert request.mode is InstallMode.NEW_BOARD and request.mac == MAC
    assert request.recipient_public_key_b64 and request.app_version == "1.0.0"


def test_key_file_exists_only_during_the_burn(rig):
    rig.run()
    burn = next(seen for seen in rig.runner.seen if "burn-key" in seen.call.args)
    assert burn.key_file_bytes == rig.server.hmac_key
    key_path = Path(burn.call.args[burn.call.args.index("BLOCK_KEY0") + 1])
    assert not key_path.exists()
    others = [seen for seen in rig.runner.seen if seen is not burn]
    assert all(not seen.files_present.get(str(key_path)) for seen in others)
    assert not rig.work.exists() or not any(rig.work.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_secret_files_are_private(rig):
    modes = {}

    def look(call):
        if "burn-key" in call.args:
            path = Path(call.args[call.args.index("BLOCK_KEY0") + 1])
            modes["key"] = stat.S_IMODE(path.stat().st_mode)
            modes["dir"] = stat.S_IMODE(path.parent.stat().st_mode)
            modes["settings"] = stat.S_IMODE((path.parent / "settings.bin").stat().st_mode)
    rig.runner.before = look
    rig.run()
    assert modes == {"key": 0o600, "dir": 0o700, "settings": 0o600}


def test_cancel_is_offered_only_before_the_key_step(rig):
    rig.run()
    labels = [(seen.call.label, seen.cancellable) for seen in rig.runner.seen]
    burn_index = next(i for i, seen in enumerate(rig.runner.seen) if "burn-key" in seen.call.args)
    assert all(cancellable for _label, cancellable in labels[:burn_index])
    assert not any(cancellable for _label, cancellable in labels[burn_index:])


def test_progress_and_audit_events(rig):
    rig.run()
    steps = [progress.step for progress in rig.steps]
    order = [StepId.PREPARE_FILES, StepId.CLEAR_START_AREA, StepId.COPY_FIRMWARE,
             StepId.CHECK_COPY, StepId.LOCK_KEY, StepId.COPY_STARTUP]
    assert [s for i, s in enumerate(steps) if i == 0 or steps[i - 1] is not s] == order
    for step in order:
        fractions = [p.fraction for p in rig.steps if p.step is step and p.fraction is not None]
        assert fractions == sorted(fractions) and fractions[-1] == 1.0
    copy_details = [p.detail for p in rig.steps if p.step is StepId.COPY_FIRMWARE and p.detail]
    assert copy_details and copy_details[-1].endswith("MB")
    assert rig.kinds() == ["STARTED", "START_AREA_CLEARED", "FIRMWARE_COPIED", "COPY_CHECKED",
                           "KEY_LOCKED", "STARTUP_COPIED", "COMPLETED"]
    assert "test-wifi-pass" not in repr(rig.server.events)


def test_hikvision_board_gets_profile_and_source_epoch(tmp_path, store, hik_bundle):
    rig = Rig(tmp_path, store, hik_bundle, FakeBoard())
    config = support.zkt_config(
        family=Family.HIKVISION, zone_id="LF-ZONE-JHELUM-01", zone_name="LF-ZONE-JHELUM-01",
        bundle_id=hik_bundle.bundle_id, zkt=None,
        hik=HikSettings(host="192.168.1.2", password="test-hik-pass",
                        expected_serial="DS-K1T342EFWX20220310"))
    rig.run(config=config)
    sent = rig.server.requests[0].config.hik
    assert sent.profile == "ds-k1t342efwx-v3.3.5-220310-poll5-pilot-v1"
    assert sent.source_epoch == "lf-zone-jhelum-01-20261101"
    assert 0x2A0000 not in rig.fake.flash and 0x520000 not in rig.fake.flash
    assert rig.fake.flash[0x17000] == b"\xff" * 0x2000
    assert rig.fake.flash[0xFA0000] == b"\xff" * 0x60000  # never read by 3.1.1, cleared alike


def test_hikvision_keeps_the_epoch_head_office_has(tmp_path, store, hik_bundle):
    record = DeviceRecord(known=True, mac=MAC, family=Family.HIKVISION,
                          hik_source_epoch="lf-zone-jhelum-01-20261030")
    rig = Rig(tmp_path, store, hik_bundle, FakeBoard(), record=record)
    config = support.zkt_config(
        family=Family.HIKVISION, bundle_id=hik_bundle.bundle_id, zkt=None,
        hik=HikSettings(host="192.168.1.2", password="test-hik-pass", expected_serial="X1",
                        model_key="other"))
    rig.run(config=config)
    sent = rig.server.requests[0].config.hik
    assert sent.source_epoch == "lf-zone-jhelum-01-20261030"
    assert sent.profile == "hikvision-other-unverified-v1"


def test_resume_rewrites_everything_and_never_burns(tmp_path, store, zkt_bundle):
    rig = Rig(tmp_path, store, zkt_bundle, FakeBoard(efuse_state="key-burned"))
    board = blank_board(security=SecurityState.KEY_BURNED_NOT_STARTED)
    rig.run(InstallMode.RESUME, board)
    assert all("burn-key" not in call.args for call in rig.runner.calls)
    assert rig.purposes()[:3] == ["check board identity", "read key state", "clear start area"]
    assert rig.fake.flash[0x0] == zkt_bundle.files["bootloader-signed.bin"]
    assert rig.fake.flash[0xFA0000] == b"\xff" * 0x60000
    assert rig.fake.burned_key is None
    assert rig.server.requests[0].mode is InstallMode.RESUME


def test_resume_refuses_a_board_whose_key_is_not_locked(tmp_path, store, zkt_bundle):
    rig = Rig(tmp_path, store, zkt_bundle, FakeBoard())  # the board is actually blank
    with pytest.raises(InstallerError) as caught:
        rig.run(InstallMode.RESUME, blank_board(security=SecurityState.KEY_BURNED_NOT_STARTED))
    assert caught.value.advice is texts.MODE_NOT_ALLOWED
    assert "clear start area" not in rig.purposes()


def test_change_settings_writes_settings_storage_and_the_redo_area_only(tmp_path, store):
    rig = Rig(tmp_path, store, None, FakeBoard(efuse_state="sdm", sdm=True))
    board = blank_board(security=SecurityState.SECURED_STATE_LIFE, secure_download_mode=True,
                        hardware=HardwareVerdict.NOT_CHECKED, flash_size_mb=None, psram_mb=None)
    summary = rig.run(InstallMode.CHANGE_SETTINGS, board)
    assert rig.server.fetches == 0
    write = rig.runner.calls[1]
    assert write.args[8:16] == ("--no-stub", "write-flash", "--flash-size", "16MB",
                                "--flash-mode", "keep", "--flash-freq", "keep")
    assert sorted(rig.fake.flash) == [0x11000, 0x7A0000, 0xFA0000]
    assert rig.fake.flash[0x11000] == rig.server.nvs
    assert rig.fake.flash[0x7A0000] == support.storage_image()
    assert rig.fake.flash[0xFA0000] == b"\xff" * 0x60000
    # One write: settings, storage, then the redo area last.
    assert write.args[-6::2] == ("0x11000", "0x7a0000", "0xfa0000")
    assert summary.bundle.label == "Settings only"
    assert rig.kinds()[-2:] == ["SETTINGS_WRITTEN", "COMPLETED"]


def test_change_settings_needs_no_board_software(tmp_path, store):
    rig = Rig(tmp_path, store, None, FakeBoard(efuse_state="sdm", sdm=True))
    board = blank_board(security=SecurityState.SECURED_STATE_LIFE, secure_download_mode=True,
                        hardware=HardwareVerdict.NOT_CHECKED, flash_size_mb=None, psram_mb=None)
    rig.run(InstallMode.CHANGE_SETTINGS, board, support.zkt_config(bundle_id=""))
    assert sorted(rig.fake.flash) == [0x11000, 0x7A0000, 0xFA0000]
    assert rig.server.requests[0].config.bundle_id == ""
    started = next(detail for _r, _m, kind, detail in rig.server.events if kind == "STARTED")
    assert started["bundle_id"] is None  # no unrelated board software in the audit
    with pytest.raises(InstallerError) as caught:  # a new board still needs one
        Rig(tmp_path / "b", store, None, FakeBoard()).run(
            config=support.zkt_config(bundle_id=""))
    assert caught.value.advice.code == "INVALID_FIELD:bundle_id"


def test_change_settings_refused_when_head_office_cannot_make_them(tmp_path, store):
    record = DeviceRecord(known=True, mac=MAC, family=Family.ZKT, ever_online=True,
                          settings_change_allowed=False,
                          settings_change_reason="Set up before this app.")
    rig = Rig(tmp_path, store, None, FakeBoard(efuse_state="sdm", sdm=True), record=record)
    board = blank_board(security=SecurityState.SECURED_STATE_LIFE, secure_download_mode=True)
    with pytest.raises(InstallerError) as caught:
        rig.run(InstallMode.CHANGE_SETTINGS, board)
    assert caught.value.advice is texts.SETTINGS_CHANGE_NOT_SUPPORTED
    assert not caught.value.advice.can_retry and not caught.value.irreversible_done
    assert rig.server.requests == [] and rig.runner.calls == [] and rig.fake.flash == {}


def test_the_pc_is_kept_awake_for_the_whole_install(rig, monkeypatch):
    from sl_installer.core import keepawake
    states = []
    monkeypatch.setattr(keepawake, "_set_state", lambda flags: states.append(flags) or True)
    during = []
    rig.runner.before = lambda call: during.append(states[-1] if states else None)
    rig.run()
    assert during and set(during) == {keepawake.AWAKE}
    assert states == [keepawake.AWAKE, keepawake.RELEASED]
    # A failed install lets the PC sleep again too.
    states.clear()
    rig.fake.failures["verify-flash"] = Failure(2, "A fatal error occurred: Verification failed.")
    with pytest.raises(InstallerError):
        rig.run()
    assert states == [keepawake.AWAKE, keepawake.RELEASED]


def test_change_settings_refuses_a_board_that_lost_the_fingerprint(tmp_path, store):
    fake = FakeBoard(efuse_state="sdm", sdm=True)
    fake.sdm_purposes = (9, 10, 11, 0, 0, 0, 12)
    rig = Rig(tmp_path, store, None, fake)
    board = blank_board(security=SecurityState.SECURED_STATE_LIFE, secure_download_mode=True)
    with pytest.raises(InstallerError) as caught:
        rig.run(InstallMode.CHANGE_SETTINGS, board)
    assert caught.value.advice is texts.MODE_NOT_ALLOWED and rig.fake.flash == {}


def test_failure_before_the_burn_leaves_a_safe_board(rig):
    rig.fake.failures["verify-flash"] = Failure(2, "Verification failed (digest mismatch).\n"
                                                   "A fatal error occurred: Verification failed.")
    with pytest.raises(InstallerError) as caught:
        rig.run()
    error = caught.value
    assert error.advice is texts.COPY_CHECK_FAILED and not error.irreversible_done
    assert error.step is StepId.CHECK_COPY
    assert rig.fake.burned_key is None and 0x0 not in rig.fake.flash
    assert rig.kinds()[-1] == "FAILED"


def test_failure_after_the_burn_asks_for_resume(rig):
    rig.fake.failures["copy start-up program"] = Failure(2, support.SERIAL_NOISE)
    with pytest.raises(InstallerError) as caught:
        rig.run()
    error = caught.value
    assert error.irreversible_done and error.advice.code == "RESUME_NEEDED"
    assert error.step is StepId.COPY_STARTUP
    assert any("short cable" in step for step in error.advice.what_to_do)
    assert error.advice.what_to_do[-1] == "Click Finish the setup."  # the screen's button
    # Not "key_burned": ADD stores any detail name containing "key" as [REDACTED].
    assert rig.server.events[-1][3]["lock_burned"] is True


def test_burn_that_did_not_land_is_retryable(rig):
    rig.fake.failures["burn-key"] = Failure(2, "A fatal error occurred: Burn BLOCK4 failed")
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.KEY_NOT_LOCKED and not caught.value.irreversible_done


def test_burn_that_landed_despite_an_error_needs_resume(rig):
    rig.fake.failures["burn-key"] = Failure(1, support.SERIAL_NOISE,
                                            then=lambda b: b.burn_hmac(b"\1" * 32))
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.irreversible_done and caught.value.advice.code == "RESUME_NEEDED"


def test_burn_with_unknown_outcome(rig):
    # The burn call dies and the board then stops answering, so nobody can tell.
    rig.fake.failures["burn-key"] = Failure(1, support.SERIAL_NOISE, then=lambda b: b.failures
                                            .__setitem__("summary",
                                                         Failure(2, support.FAILED_TO_CONNECT)))
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.KEY_STATE_UNCLEAR and caught.value.irreversible_done


def test_key_must_be_protected_after_the_burn(rig):
    def unprotect(call):
        if "summary" in call.args and rig.fake.burned_key is not None:
            rig.fake.summary["BLOCK_KEY0"].update(readable=True)
    rig.runner.before = unprotect
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.KEY_CHECK_FAILED and caught.value.irreversible_done
    assert 0x0 not in rig.fake.flash  # the bootloader is never written for such a board


def test_cancel_before_the_key_step(rig):
    cancel = CancelToken()
    rig.runner.before = lambda call: cancel.cancel() if call.label == "copy firmware" else None
    with pytest.raises(InstallerError) as caught:
        rig.run(cancel=cancel)
    assert caught.value.advice is texts.CANCELLED and not caught.value.irreversible_done
    assert all("burn-key" not in call.args for call in rig.runner.calls)
    assert rig.kinds()[-1] == "CANCELLED"


def test_cancel_after_the_burn_is_ignored(rig):
    cancel = CancelToken()
    rig.runner.before = lambda call: cancel.cancel() if "burn-key" in call.args else None
    summary = rig.run(cancel=cancel)
    assert summary.mode is InstallMode.NEW_BOARD and 0x0 in rig.fake.flash


def test_family_known_to_head_office_wins(tmp_path, store, zkt_bundle):
    record = DeviceRecord(known=True, mac=MAC, family=Family.HIKVISION)
    rig = Rig(tmp_path, store, zkt_bundle, FakeBoard(), record=record)
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice.code == "FAMILY_MISMATCH"
    assert "Hikvision" in caught.value.advice.what_happened
    assert rig.runner.calls == [] and rig.server.requests == []


def test_swapped_board_is_caught_before_anything_is_erased(rig):
    rig.fake.reported_mac = support.OTHER_MAC
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.BOARD_SWAPPED
    assert rig.purposes() == ["check board identity"] and rig.fake.erased == []


def test_secured_board_is_never_erased_in_new_board_mode(tmp_path, store, zkt_bundle):
    rig = Rig(tmp_path, store, zkt_bundle, FakeBoard(efuse_state="secured-not-sdm"))
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.ALREADY_SET_UP and rig.fake.erased == []


def test_board_found_again_on_a_new_port(rig):
    def move(call):
        if call.label == "check copy":
            rig.usb.port = "/dev/cu.usbmodem2101"
    rig.runner.before = move
    rig.run()
    ports = [c.args[c.args.index("--port") + 1] for c in rig.runner.calls]
    assert ports[:3] == [PORT] * 3 and set(ports[4:]) == {"/dev/cu.usbmodem2101"}


def test_board_unplugged_before_the_burn(rig):
    def unplug(call):
        if call.label == "copy firmware":
            rig.usb.present = False
    rig.runner.before = unplug
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.BOARD_DISCONNECTED and not caught.value.irreversible_done


def test_second_board_without_an_id_is_refused(rig):
    rig.usb.present = False
    rig.usb.extra_boards = 2
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.SEVERAL_BOARDS


# ------------------------------------------------ one board, by its USB board ID

def watcher_usb(serial):
    """The real UsbWatcher on one listed port whose USB serial number is ``serial[0]``.

    The computer ignores serial numbers when it is None, and the device-tree fallback
    finds nothing either, so the same socket gives every board the same port name.
    """
    port = SimpleNamespace(device="COM7", vid=0x303A, pid=0x1001,
                           description="USB Serial Device (COM7)")

    def ports():
        port.serial_number = serial[0]
        return [port]
    return UsbWatcher(ports, FakeClock(), tree_macs=dict), "COM7"


def usb_for(finder):
    """(port finder, its port, a way to make the board's USB board ID unreadable)."""
    if finder == "watcher":
        serial = [MAC.upper()]
        usb, port = watcher_usb(serial)
        return usb, port, lambda: serial.__setitem__(0, None)
    usb = FakeUsb()
    return usb, PORT, lambda: setattr(usb, "mac", None)


def leftover_board():
    """Put aside after a setup that stopped before the burn: software in, start area empty."""
    board = FakeBoard(OTHER_MAC)
    board.flash.update({0x20000: b"zone lite", 0x2A0000: b"zone lite"})
    return board


def finished_board():
    """Another State Life board, in Secure Download Mode: erase and write still work."""
    board = FakeBoard(OTHER_MAC, efuse_state="sdm", sdm=True)
    board.flash[0x0] = b"its own start-up program"
    return board


def no_id_advice(key_locked):
    """The stop without a USB board ID; once the key is locked, as the advice to finish."""
    if not key_locked:
        return texts.BOARD_ID_NOT_READABLE
    advice = texts.resume_needed(texts.BOARD_ID_NOT_READABLE)
    assert advice.what_to_do[0] == "Do not throw the board away."
    assert advice.what_to_do[-1] == "Click Finish the setup."
    return advice


# Board A is unplugged right after one call and board B goes in before the next; this
# computer gives B no USB board ID (review BS1). Nothing may then reach B.
SWAPS = [
    pytest.param("check board identity", 1, False, finished_board, id="before-the-erase"),
    pytest.param("read key state", 1, False, lambda: FakeBoard(OTHER_MAC), id="before-the-burn"),
    pytest.param("read key state", 2, True, leftover_board, id="before-the-start-up-program"),
]


@pytest.mark.parametrize("finder", ["fake", "watcher"])
@pytest.mark.parametrize("label, nth, burned, other", SWAPS)
def test_a_board_swapped_in_without_a_usb_board_id_gets_no_call(
        tmp_path, store, zkt_bundle, finder, label, nth, burned, other):
    usb, port, lose_id = usb_for(finder)
    rig = Rig(tmp_path, store, zkt_bundle, FakeBoard(), usb=usb)
    b = other()
    b_flash, ends, original = dict(b.flash), [], rig.runner.run
    rig.swapped_at = None

    def run(call, *, cancel=None, on_line=None):
        result = original(call, cancel=cancel, on_line=on_line)
        ends.append(call.label)
        if ends.count(label) == nth and call.label == label:
            rig.runner.board = b  # A unplugged, B plugged in
            lose_id()
            rig.swapped_at = len(rig.runner.calls)
        return result
    rig.runner.run = run
    with pytest.raises(InstallerError) as caught:
        rig.run(board=blank_board(port=port))
    error = caught.value
    assert error.advice == no_id_advice(burned) and error.irreversible_done is burned
    assert rig.swapped_at is not None and rig.runner.calls[rig.swapped_at:] == []  # no call
    assert b.flash == b_flash and b.erased == [] and b.burned_key is None
    assert (rig.fake.burned_key == rig.server.hmac_key) is burned and 0x0 not in rig.fake.flash
    assert rig.kinds()[-1] == "FAILED"


@pytest.mark.parametrize("finder", ["fake", "watcher"])
@pytest.mark.parametrize("mode", list(InstallMode))
def test_a_board_without_a_usb_board_id_is_never_written(tmp_path, store, zkt_bundle, finder,
                                                         mode):
    usb, port, lose_id = usb_for(finder)
    lose_id()
    fake = {InstallMode.NEW_BOARD: FakeBoard(),
            InstallMode.RESUME: FakeBoard(efuse_state="key-burned"),
            InstallMode.CHANGE_SETTINGS: FakeBoard(efuse_state="sdm", sdm=True)}[mode]
    security = {InstallMode.NEW_BOARD: SecurityState.BLANK,
                InstallMode.RESUME: SecurityState.KEY_BURNED_NOT_STARTED,
                InstallMode.CHANGE_SETTINGS: SecurityState.SECURED_STATE_LIFE}[mode]
    board = blank_board(port=port, security=security)
    if mode is InstallMode.CHANGE_SETTINGS:
        board = replace(board, secure_download_mode=True, hardware=HardwareVerdict.NOT_CHECKED)
    rig = Rig(tmp_path, store, zkt_bundle, fake, usb=usb)
    started = rig.clock.now
    with pytest.raises(InstallerError) as caught:
        rig.run(mode, board)
    locked = mode is InstallMode.RESUME  # its key is locked
    assert caught.value.advice == no_id_advice(locked)
    assert caught.value.irreversible_done is locked
    assert rig.runner.calls == [] and fake.flash == {} and fake.erased == []
    assert rig.clock.now - started >= PORT_WAIT_S  # it waited for the ID to show first


@pytest.mark.parametrize("mode", [InstallMode.NEW_BOARD, InstallMode.RESUME])
def test_a_banner_in_secure_download_mode_means_another_board(tmp_path, store, zkt_bundle,
                                                              mode):
    # Only a first start switches Secure Download Mode on, so a new or unfinished board is
    # never in it: even with its USB board ID, that answer came from another board.
    fake = FakeBoard() if mode is InstallMode.NEW_BOARD else FakeBoard(efuse_state="key-burned")
    rig = Rig(tmp_path, store, zkt_bundle, fake)
    rig.runner.before = lambda call: setattr(fake, "sdm", fake.sdm or
                                             call.label == "clear start area")
    security = (SecurityState.BLANK if mode is InstallMode.NEW_BOARD
                else SecurityState.KEY_BURNED_NOT_STARTED)
    with pytest.raises(InstallerError) as caught:
        rig.run(mode, blank_board(security=security))
    advice = caught.value.advice
    if mode is InstallMode.NEW_BOARD:
        assert advice is texts.BOARD_SWAPPED
    else:
        # The key is locked: the screen offers only Finish the setup, and says so.
        assert advice.code == "RESUME_NEEDED" and "board you started with" in advice.what_happened
        assert advice.what_to_do == ("Do not throw the board away.",
                                     "Plug in the board you started with.",
                                     "Click Finish the setup.")
    assert rig.purposes()[-1] == "clear start area"  # nothing after it
    assert caught.value.irreversible_done is (mode is InstallMode.RESUME)


def test_an_esptool_answer_without_the_board_id_stops_a_new_board(rig):
    original = rig.fake.handle

    def no_board_id(call):
        code, output = original(call)
        if call.label == "copy firmware":
            output = "\n".join(line for line in output.splitlines()
                               if not line.startswith("MAC:"))
        return code, output
    rig.fake.handle = no_board_id
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.BOARD_SWAPPED and rig.purposes()[-1] == "copy firmware"
    assert rig.fake.burned_key is None


@pytest.mark.parametrize("mode, board, advice", [
    (InstallMode.NEW_BOARD, blank_board(security=SecurityState.SECURED_STATE_LIFE),
     texts.ALREADY_SET_UP),
    (InstallMode.RESUME, blank_board(), texts.MODE_NOT_ALLOWED),
    (InstallMode.CHANGE_SETTINGS, blank_board(), texts.MODE_NOT_ALLOWED),
    (InstallMode.NEW_BOARD, blank_board(hardware=HardwareVerdict.WRONG_PSRAM),
     texts.WRONG_PSRAM),
    (InstallMode.NEW_BOARD, blank_board(security=SecurityState.SECURED_OTHER),
     texts.MODE_NOT_ALLOWED),
    # Every mode writes the redo area at the end of a 16 MB chip.
    (InstallMode.NEW_BOARD, blank_board(flash_size_mb=32), texts.MODE_NOT_ALLOWED),
])
def test_mode_must_fit_the_board(rig, mode, board, advice):
    with pytest.raises(InstallerError) as caught:
        rig.run(mode, board)
    assert caught.value.advice is advice and rig.runner.calls == []


def test_settings_are_checked_before_head_office_is_asked(rig):
    with pytest.raises(InstallerError) as caught:
        rig.run(config=support.zkt_config(zone_id="BAD ZONE"))
    assert caught.value.advice.code == "INVALID_FIELD:zone_id"
    assert caught.value.step is StepId.PREPARE_FILES  # the screen offers "Change the details"
    assert rig.server.requests == [] and rig.runner.calls == []


@pytest.mark.parametrize("caps, advice", [
    (Capabilities(False, True, "1.0.0"), texts.SERVER_DISABLED),
    (Capabilities(True, False, "1.0.0"), texts.PROVISIONER_UNAVAILABLE),
    (Capabilities(True, True, "9.0.0"), texts.APP_TOO_OLD),
])
def test_head_office_capabilities(tmp_path, store, zkt_bundle, caps, advice):
    rig = Rig(tmp_path, store, zkt_bundle, FakeBoard(), capabilities=caps)
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is advice


def test_package_for_other_settings_is_refused(rig):
    def other(request):
        return support.make_package(request.request_id, request.mac,
                                    request.recipient_public_key_b64, family="zkt",
                                    zone_id=request.config.zone_id, device_id="2",
                                    nvs=rig.server.nvs, hmac_key=rig.server.hmac_key)
    rig.server.package_override = other
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.PACKAGE_BAD and rig.runner.calls == []


def test_package_for_another_board_is_refused(rig):
    rig.server.tamper = lambda manifest: manifest["aad"].update(target_mac=support.OTHER_MAC)
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.PACKAGE_BAD


def test_unexpected_errors_still_reach_the_screen_as_advice(rig, monkeypatch):
    monkeypatch.setattr(rig.server, "device", lambda mac: 1 / 0)
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.UNEXPECTED and "ZeroDivisionError" in caught.value.cause


def test_stale_work_folders_are_wiped(tmp_path):
    old = tmp_path / "sl-install-old"
    old.mkdir()
    (old / "board-key.bin").write_bytes(b"secret" * 6)
    fresh = tmp_path / "sl-install-new"
    fresh.mkdir()
    os.utime(old, (0, 0))
    cleanup_stale_work(tmp_path)
    assert not old.exists() and fresh.exists()


def test_a_holder_of_the_instance_lock_wipes_every_work_folder(tmp_path):
    # Left by a power cut during the burn a minute ago: no install can be running now.
    fresh = tmp_path / "sl-install-fresh"
    fresh.mkdir()
    (fresh / "board-key.bin").write_bytes(b"secret" * 6)
    inspecting = tmp_path / "sl-inspect-fresh"  # not an install's folder
    inspecting.mkdir()
    cleanup_stale_work(tmp_path, max_age_s=0)
    assert not fresh.exists() and inspecting.exists()


def test_a_work_folder_dated_ahead_goes_only_with_the_instance_lock(tmp_path):
    # The computer clock was 5 hours fast at the power cut; Windows has put it right since.
    ahead = tmp_path / "sl-install-ahead"
    ahead.mkdir()
    (ahead / "board-key.bin").write_bytes(b"secret" * 6)
    later = time.time() + 5 * 3600
    os.utime(ahead, (later, later))
    cleanup_stale_work(tmp_path)  # without the lock it may be a running install's
    assert (ahead / "board-key.bin").exists()
    cleanup_stale_work(tmp_path, max_age_s=0)
    assert not ahead.exists()


def test_write_without_its_confirmation_is_not_trusted(rig):
    original = rig.fake.handle

    def no_hash(call):
        code, output = original(call)
        if call.label == "copy firmware":
            output = output.replace("Hash of data verified.", "")
        return code, output
    rig.fake.handle = no_hash
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice is texts.COPY_CHECK_FAILED and not caught.value.irreversible_done


def test_change_settings_on_a_secured_board_outside_download_security(tmp_path, store):
    rig = Rig(tmp_path, store, None, FakeBoard(efuse_state="secured-not-sdm"))
    board = blank_board(security=SecurityState.SECURED_STATE_LIFE)
    rig.run(InstallMode.CHANGE_SETTINGS, board)
    assert sorted(rig.fake.flash) == [0x11000, 0x7A0000, 0xFA0000]
    assert "--no-stub" in rig.runner.calls[1].args


def test_screen_and_audit_failures_never_break_an_install(rig, monkeypatch):
    def broken_report(*args):
        raise RuntimeError("ADD down")
    monkeypatch.setattr(rig.server, "report", broken_report)
    rig.installer.run(InstallMode.NEW_BOARD, blank_board(), support.zkt_config(),
                      lambda progress: 1 / 0, CancelToken())
    assert 0x0 in rig.fake.flash


def test_boot_and_faulty_board_reports(rig):
    from sl_installer.core.api import BootOutcome, BootReport
    from sl_installer.core.installer import report_boot, report_faulty_board
    summary = rig.run()
    report_boot(rig.server, summary, BootReport(BootOutcome.OK, "Z", "1", "2.9.1",
                                                advice=texts.boot_ok("Z", "1", "2.9.1")))
    assert rig.server.events[-1][2] == "BOOT_OK"
    assert rig.server.events[-1][3]["app_version_seen"] == "2.9.1"
    report_faulty_board(rig.server, MAC, 10.3, texts.restart_loop_faulty(10.3))
    request_id, mac, kind, detail = rig.server.events[-1]
    assert (mac, kind, detail["loop_period_s"]) == (MAC, "BOARD_FAULTY", 10.3)
    assert request_id.startswith("sli-")


@pytest.mark.parametrize("outcome, advice, kind, kept", [
    ("ok", texts.settings_kept("Z", "1", "2.9.1"), "RESTART_OK", True),
    ("settings_lost", texts.SETTINGS_LOST_ON_RESTART, "RESTART_FAILED", False),
    ("settings_not_seen", texts.SETTINGS_NOT_SEEN, "RESTART_FAILED", None),
    ("not_replugged", texts.NOT_REPLUGGED, "RESTART_FAILED", None),
])
def test_restart_check_report(rig, outcome, advice, kind, kept):
    from sl_installer.core.api import BootOutcome, BootReport
    from sl_installer.core.installer import report_restart
    summary = rig.run()
    report_restart(rig.server, summary, BootReport(BootOutcome(outcome), "Z", None, "2.9.1",
                                                   advice=advice))
    request_id, mac, sent, detail = rig.server.events[-1]
    assert (request_id, mac, sent) == (summary.request_id, MAC, kind)
    assert detail == {"outcome": outcome, "zone_id_seen": "Z", "device_id_seen": None,
                      "app_version_seen": "2.9.1", "advice": advice.code, "board_said": None,
                      "settings_kept": kept}


def test_failed_starts_carry_the_boards_own_words(rig):
    from sl_installer.core.api import BootOutcome, BootReport
    from sl_installer.core.installer import report_boot, report_restart
    summary = rig.run()
    line = "E (35) bootloader_flash: XMC flash startup fail"
    report_boot(rig.server, summary, BootReport(BootOutcome.STARTUP_REFUSED,
                                                advice=texts.STARTUP_REFUSED, board_said=line))
    assert rig.server.events[-1][2] == "BOOT_FAILED"
    assert rig.server.events[-1][3]["board_said"] == line
    # Redacted again and cut to 200 characters: ADD keeps it.
    long = "E (902) zone_lite: password=test-wifi-pass " + "x" * 400
    report_restart(rig.server, summary, BootReport(BootOutcome.SETTINGS_LOST, board_said=long))
    said = rig.server.events[-1][3]["board_said"]
    assert rig.server.events[-1][2] == "RESTART_FAILED"
    assert len(said) == 200 and "test-wifi-pass" not in said


# ------------------------------------------------- boards the software cannot start

XMC_BOARD = dict(hardware=HardwareVerdict.FLASH_CHIP_UNSUPPORTED, flash_id="20 70 18",
                 hardware_detail="memory chip 20 70 18",
                 evidence={"chip_line": "ESP32-S3 (QFN56) (revision v0.2)",
                           "features": "Wi-Fi, Embedded PSRAM 8MB (AP_3v3)"})


@pytest.mark.parametrize("mode, security", [
    (InstallMode.NEW_BOARD, SecurityState.BLANK),
    # The two 3 Nov 2026 boards: key burned, never started. Never written again.
    (InstallMode.RESUME, SecurityState.KEY_BURNED_NOT_STARTED),
])
def test_xmc_board_is_refused_before_anything_is_written(tmp_path, store, zkt_bundle, mode,
                                                         security):
    fake = FakeBoard(efuse_state="key-burned" if mode is InstallMode.RESUME else "blank")
    rig = Rig(tmp_path, store, zkt_bundle, fake)
    with pytest.raises(InstallerError) as caught:
        rig.run(mode, blank_board(security=security, **XMC_BOARD))
    advice = caught.value.advice
    assert advice.code == "FLASH_CHIP_UNSUPPORTED" and not advice.can_retry
    assert ("earlier setup locked" in advice.what_happened) is (mode is InstallMode.RESUME)
    assert rig.runner.calls == [] and rig.server.requests == [] and rig.fake.flash == {}
    assert rig.fake.burned_key is None
    (_request, mac, kind, detail), = rig.server.events
    assert (mac, kind) == (MAC, "BOARD_REFUSED")
    assert detail == {"reason": "FLASH_CHIP_UNSUPPORTED", "flash_id": "20 70 18",
                      "chip": "ESP32-S3 (QFN56) (revision v0.2)",
                      "features": "Wi-Fi, Embedded PSRAM 8MB (AP_3v3)"}


def test_board_type_is_refused_before_anything_is_written(rig):
    board = blank_board(hardware=HardwareVerdict.BOARD_TYPE_UNSUPPORTED,
                        hardware_detail="extra memory 8 MB, type AP_1v8")
    with pytest.raises(InstallerError) as caught:
        rig.run(board=board)
    assert caught.value.advice.code == "BOARD_TYPE_UNSUPPORTED"
    assert "The app found: extra memory 8 MB, type AP_1v8." in caught.value.advice.what_happened
    assert rig.runner.calls == [] and rig.kinds() == ["BOARD_REFUSED"]


def test_esptools_xmc_warning_stops_a_new_board_before_its_key(tmp_path, store, zkt_bundle):
    # A board that got past the check (an older one): the first erase shows the warning,
    # as the stub printed it on 3 Nov 2026.
    assert support.XMC_WARNING in support.board_sample("xmc-20-7018-erase-region.txt")
    rig = Rig(tmp_path, store, zkt_bundle, FakeBoard(xmc_warning=True))
    with pytest.raises(InstallerError) as caught:
        rig.run()
    assert caught.value.advice.code == "FLASH_CHIP_UNSUPPORTED"
    assert not caught.value.irreversible_done
    assert rig.purposes()[-1] == "clear start area" and rig.fake.burned_key is None
    assert rig.kinds()[-2:] == ["BOARD_REFUSED", "FAILED"]
    refused, failed = rig.server.events[-2:]
    assert refused[0] == failed[0]  # the run's request ID ties them together
    assert sorted(refused[3]) == ["chip", "features", "flash_id", "reason"]
    assert refused[3]["reason"] == "FLASH_CHIP_UNSUPPORTED"


def test_esptools_xmc_warning_stops_a_resume(tmp_path, store, zkt_bundle):
    rig = Rig(tmp_path, store, zkt_bundle, FakeBoard(efuse_state="key-burned", xmc_warning=True))
    with pytest.raises(InstallerError) as caught:
        rig.run(InstallMode.RESUME, blank_board(security=SecurityState.KEY_BURNED_NOT_STARTED))
    advice = caught.value.advice
    assert advice.code == "FLASH_CHIP_UNSUPPORTED" and caught.value.irreversible_done
    assert "Do not try to set it up again." in advice.what_to_do
    assert rig.fake.flash == {}  # stopped at the first erase


def test_the_bundles_chip_revisions_decide(rig, monkeypatch, zkt_bundle, store):
    from sl_installer.core import installer as installer_module
    from sl_installer.core.bundles import chip_revision_window
    # The test images leave the maximum unset, like an image built without one.
    fetched = store.fetch(support.FakeSource(zkt_bundle), zkt_bundle.bundle_id, None,
                          CancelToken())
    assert chip_revision_window(fetched) == (0, None)
    monkeypatch.setattr(installer_module, "chip_revision_window", lambda bundle: (0, 1))
    with pytest.raises(InstallerError) as caught:
        rig.run(board=blank_board(chip_revision=2))
    advice = caught.value.advice
    assert advice.code == "BOARD_TYPE_UNSUPPORTED" and not advice.can_retry
    assert "chip revision v0.2; this board software needs v0.0 to v0.1" in advice.what_happened
    # Before head office makes the settings and before anything is written.
    assert rig.server.requests == [] and rig.runner.calls == [] and rig.fake.flash == {}
    assert rig.kinds()[-2:] == ["BOARD_REFUSED", "FAILED"]
    monkeypatch.setattr(installer_module, "chip_revision_window", lambda bundle: (0, 99))
    rig.run(board=blank_board(chip_revision=2))
    assert rig.fake.burned_key is not None


def test_image_header_chip_revisions():
    from sl_installer.core.bundles import ImageError, header_chip_revisions
    header = bytearray(24)
    header[0] = 0xE9
    header[15:19] = (3).to_bytes(2, "little") + (99).to_bytes(2, "little")
    assert header_chip_revisions(bytes(header)) == (3, 99)
    with pytest.raises(ImageError):
        header_chip_revisions(b"\x00" * 24)


def test_change_settings_refuses_a_flash_chip_that_is_not_16_mb(tmp_path, store):
    rig = Rig(tmp_path, store, None, FakeBoard(efuse_state="secured-not-sdm", flash_mb=8))
    board = blank_board(security=SecurityState.SECURED_STATE_LIFE, flash_size_mb=8)
    with pytest.raises(InstallerError) as caught:
        rig.run(InstallMode.CHANGE_SETTINGS, board)
    assert caught.value.advice is texts.MODE_NOT_ALLOWED
    assert caught.value.cause == "change_settings on a 8 MB flash chip"
    assert rig.runner.calls == [] and rig.server.requests == [] and rig.fake.flash == {}


def test_a_changed_redo_area_image_is_made_again(tmp_path, store):
    blank = store.redo_area_image()
    blank.write_bytes(b"\x00" + blank.read_bytes()[1:])  # tampered or damaged on disk
    rig = Rig(tmp_path, store, None, FakeBoard(efuse_state="sdm", sdm=True))
    board = blank_board(security=SecurityState.SECURED_STATE_LIFE, secure_download_mode=True,
                        hardware=HardwareVerdict.NOT_CHECKED, flash_size_mb=None, psram_mb=None)
    rig.run(InstallMode.CHANGE_SETTINGS, board)
    assert rig.fake.flash[0xFA0000] == b"\xff" * 0x60000


def test_a_blank_redo_area_that_does_not_match_its_hash_stops_the_run(tmp_path, store,
                                                                    monkeypatch):
    from sl_installer.core import bundles
    monkeypatch.setattr(bundles, "blank_redo_area", lambda: b"\x00" * 0x60000)
    rig = Rig(tmp_path, store, None, FakeBoard(efuse_state="sdm", sdm=True))
    board = blank_board(security=SecurityState.SECURED_STATE_LIFE, secure_download_mode=True,
                        hardware=HardwareVerdict.NOT_CHECKED, flash_size_mb=None, psram_mb=None)
    with pytest.raises(InstallerError) as caught:
        rig.run(InstallMode.CHANGE_SETTINGS, board)
    assert caught.value.advice is texts.TOOL_MISSING
    assert caught.value.cause == "blank redo area does not match its pinned hash"
    assert rig.runner.calls == [] and rig.server.requests == [] and rig.fake.flash == {}
