"""Whole installs against a simulated board: order, secrets, failures and resume."""
from __future__ import annotations

import os
import stat
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from sl_installer.core import texts
from sl_installer.core.api import (BoardInfo, Capabilities, CancelToken, DeviceRecord, Family,
                                   HardwareVerdict, HikSettings, InstallerError, InstallMode,
                                   SecurityState, StepId)
from sl_installer.core.installer import Installer, cleanup_stale_work

from . import support
from .support import MAC, PORT, FakeBoard, FakeClock, FakeRunner, FakeServer, FakeUsb, Failure

FACTORY = 0x20000


def blank_board(**changes) -> BoardInfo:
    info = BoardInfo(PORT, MAC, "ESP32-S3", 16, 8, HardwareVerdict.OK, SecurityState.BLANK,
                     False, {})
    return replace(info, **changes)


class Rig:
    def __init__(self, tmp_path: Path, store, bundle, fake: FakeBoard, **server_args) -> None:
        self.fake = fake
        self.runner = FakeRunner(fake)
        self.server = FakeServer(store, bundle, **server_args)
        self.usb = FakeUsb()
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
    assert rig.fake.burned_key is None
    assert rig.server.requests[0].mode is InstallMode.RESUME


def test_resume_refuses_a_board_whose_key_is_not_locked(tmp_path, store, zkt_bundle):
    rig = Rig(tmp_path, store, zkt_bundle, FakeBoard())  # the board is actually blank
    with pytest.raises(InstallerError) as caught:
        rig.run(InstallMode.RESUME, blank_board(security=SecurityState.KEY_BURNED_NOT_STARTED))
    assert caught.value.advice is texts.MODE_NOT_ALLOWED
    assert "clear start area" not in rig.purposes()


def test_change_settings_writes_settings_and_storage_only(tmp_path, store):
    rig = Rig(tmp_path, store, None, FakeBoard(efuse_state="sdm", sdm=True))
    board = blank_board(security=SecurityState.SECURED_STATE_LIFE, secure_download_mode=True,
                        hardware=HardwareVerdict.NOT_CHECKED, flash_size_mb=None, psram_mb=None)
    summary = rig.run(InstallMode.CHANGE_SETTINGS, board)
    assert rig.server.fetches == 0
    write = rig.runner.calls[1]
    assert write.args[8:16] == ("--no-stub", "write-flash", "--flash-size", "16MB",
                                "--flash-mode", "keep", "--flash-freq", "keep")
    assert sorted(rig.fake.flash) == [0x11000, 0x7A0000]
    assert rig.fake.flash[0x11000] == rig.server.nvs
    assert rig.fake.flash[0x7A0000] == support.storage_image()
    assert summary.bundle.label == "Settings only"
    assert rig.kinds()[-2:] == ["SETTINGS_WRITTEN", "COMPLETED"]


def test_change_settings_needs_no_board_software(tmp_path, store):
    rig = Rig(tmp_path, store, None, FakeBoard(efuse_state="sdm", sdm=True))
    board = blank_board(security=SecurityState.SECURED_STATE_LIFE, secure_download_mode=True,
                        hardware=HardwareVerdict.NOT_CHECKED, flash_size_mb=None, psram_mb=None)
    rig.run(InstallMode.CHANGE_SETTINGS, board, support.zkt_config(bundle_id=""))
    assert sorted(rig.fake.flash) == [0x11000, 0x7A0000]
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


@pytest.mark.parametrize("mode, board, advice", [
    (InstallMode.NEW_BOARD, blank_board(security=SecurityState.SECURED_STATE_LIFE),
     texts.ALREADY_SET_UP),
    (InstallMode.RESUME, blank_board(), texts.MODE_NOT_ALLOWED),
    (InstallMode.CHANGE_SETTINGS, blank_board(), texts.MODE_NOT_ALLOWED),
    (InstallMode.NEW_BOARD, blank_board(hardware=HardwareVerdict.WRONG_PSRAM),
     texts.MODE_NOT_ALLOWED),
    (InstallMode.NEW_BOARD, blank_board(security=SecurityState.SECURED_OTHER),
     texts.MODE_NOT_ALLOWED),
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
    assert sorted(rig.fake.flash) == [0x11000, 0x7A0000]
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
