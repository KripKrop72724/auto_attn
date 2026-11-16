"""Pictures of every screen and state, made with the fake services.

``capture_all(out_dir)`` walks each screen through the real controller with a
scripted fake story (core/fakes.py), holding the story at the right moment for
in-between states, and saves PNGs at 1280x800 and 1024x700. Animations are
frozen so every run gives the same pictures. Used by ``--screenshots DIR``,
by CI, and by the UI tests.
"""

from __future__ import annotations

import os
import threading
import time
import zipfile
from datetime import datetime
from typing import Callable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QApplication

from ..core import api, fakes, texts
from ..core.api import ConsoleKind, Family, ReplugPhase
from . import pages
from .app import ensure_app
from .pages.light_guide import LightGuidePage
from .widgets import motion
from .window import MainWindow

SIZES: Tuple[QSize, ...] = (QSize(1280, 800), QSize(1024, 700))
PACE = 0.002
TIMEOUT_S = 15.0


class SceneError(RuntimeError):
    pass


# ================================================================ board test
#
# A scripted board test (api.BoardTestServiceProto) for the screens: the pictures and the
# UI tests use it to show every result without a board. Each story is what the engine
# (core/boardtest.py) reports for that board.

BOARD_TEST_STORIES: Tuple[str, ...] = (
    "pass",
    "memory_errors",
    "memory_start_fault",
    "flash_chip_refused",
    "no_start",
    "locked_memory",
    "locked_settings",
    "resume_pass",
)

_TEST_SCRIPT: Tuple[Tuple[str, float, str], ...] = (
    ("check", 0.02, "Reading the board's security settings"),
    ("write", 0.08, "Copying the test program"),
    ("start", 0.15, "Starting the test program"),
    ("psram", 0.25, "Testing the extra memory (round 1 of 2)"),
    ("psram", 0.40, "Testing the extra memory (round 2 of 2)"),
    ("iram", 0.50, "Testing the chip's own memory"),
    ("flash", 0.62, "Testing the memory chip"),
    ("cpu", 0.75, "Testing the processor"),
    ("wifi", 0.85, "Looking for Wi-Fi networks"),
    ("cleanup", 0.93, "Removing the test program"),
    ("done", 1.0, "Finished"),
)
_OK_TITLES = {
    "psram": "The extra memory works",
    "iram": "The chip's own memory works",
    "flash": "The memory chip works",
    "cpu": "The processor works",
    "wifi": "The Wi-Fi radio works",
}
_OK_DETAIL = {
    "psram": "8 MB found, 7.9 MB tested in 2 rounds of 10 patterns, 0 errors, 41 MB/s",
    "iram": "312 KB tested, 0 errors",
    "flash": "chip 20 40 18, 16 MB, read twice and 14 blocks written, 0 errors",
    "cpu": "both cores gave checksum 0x5a17c3e9, 41 C",
    "wifi": "6 networks, best -48 dBm",
}


def _sltest(what: str, **fields: object) -> str:
    """A test program line, as the board prints it (the protocol of SPEC section A)."""
    return " ".join(["SLTEST", what] + [f"{key}={value}" for key, value in fields.items()])


def _finding(code: str, stage: str, result: str, title: str, detail: str,
             proof: Sequence[str] = ()) -> object:
    return api.TestFinding(code, stage, result, title, detail, tuple(proof))


def _ok(stage: str) -> object:
    line = _sltest("RESULT", stage=stage, result="ok", code="OK", errors=0, ms=900)
    return _finding("OK", stage, "ok", _OK_TITLES[stage], _OK_DETAIL[stage], (line,))


class ScriptedBoardTest:
    """``story`` decides the result; ``hold(stage)`` stops the story at that stage (or at
    "unplug" / "replug" for the replug steps) until ``release``."""

    def __init__(self, story: str = "pass", pace: float = PACE) -> None:
        if story not in BOARD_TEST_STORIES:
            raise ValueError(f"unknown board test story {story!r}")
        self.story = story
        self.pace = pace
        self.calls: List[Tuple[str, str]] = []
        self._held: dict = {}
        self._lock = threading.Lock()

    def hold(self, name: str) -> None:
        with self._lock:
            self._held[name] = threading.Event()

    def release(self, name: str) -> None:
        with self._lock:
            event = self._held.pop(name, None)
        if event is not None:
            event.set()

    def release_all(self) -> None:
        for name in list(self._held):
            self.release(name)

    def _pause(self, name: str, cancel: api.CancelToken) -> bool:
        with self._lock:
            event = self._held.get(name)
        if event is not None:
            while not event.is_set():
                if cancel.wait(0.005):
                    return True
            return cancel.cancelled
        return cancel.wait(self.pace)

    # -------------------------------------------------------------- service

    def run_test(self, board, on_progress, on_phase, on_event, cancel) -> object:
        self.calls.append(("run_test", board.mac))
        started = _now()
        if self.story == "flash_chip_refused" or board.hardware not in (
            api.HardwareVerdict.OK, api.HardwareVerdict.NOT_CHECKED
        ):
            finding = _finding(
                "FLASH_CHIP_UNSUPPORTED", "check", "fail",
                "The board's memory chip cannot run State Life's software",
                "Board check refused the board before any test: "
                f"{board.hardware_detail or 'memory chip 20 70 18'} (XMC flash chips stop the "
                "start-up program of the secured software)",
                ("Detected flash chip ID 0x207018",),
            )
            return self._report(board, "failed", (finding,), started, erased=True,
                                advice=texts.FLASH_CHIP_UNSUPPORTED)
        lines = [_sltest("BEGIN", proto=1, fw="1.0.0", mac=board.mac, chip_rev="v0.2",
                         cores=2, idf="v5.5.3", flash_id="204018", flash_size=16777216,
                         psram_size=8388608)]
        if self.story == "no_start":
            for stage, fraction, detail in _TEST_SCRIPT[:3]:
                on_progress(api.BoardTestProgress(stage, fraction, detail))
                if self._pause(stage, cancel):
                    return self._cancelled(board, started)
            for phase, moment in ((ReplugPhase.WAITING_UNPLUG, "unplug"),
                                  (ReplugPhase.WAITING_REPLUG, "replug"),
                                  (ReplugPhase.WATCHING, "watch")):
                on_phase(phase)
                if self._pause(moment, cancel):
                    return self._cancelled(board, started)
            finding = _finding(
                "NO_START", "startup", "warn", "The test program did not start",
                "No SLTEST BEGIN and no boot output within 12 s, before and after one replug",
            )
            return self._report(board, "incomplete", (finding,), started, erased=True,
                                advice=texts.NO_START)
        findings: List[object] = []
        for stage, fraction, detail in _TEST_SCRIPT:
            on_progress(api.BoardTestProgress(stage, fraction, detail))
            if stage == "start":
                on_event(_self_test_event(lines[0]))
            if stage in _OK_TITLES and not any(
                getattr(item, "stage", "") == stage for item in findings
            ):
                if stage == "psram" and self.story == "memory_errors":
                    err = _sltest("ERR", stage="psram", test="pattern_55", addr="0x3c812340",
                                  want="0x55555555", got="0x55515555")
                    result = _sltest("RESULT", stage="psram", result="fail",
                                     code="PSRAM_DATA_ERRORS", detail="pattern_errors",
                                     errors=1204, ms=5210)
                    on_event(_self_test_event(err))
                    on_event(_self_test_event(result))
                    findings.append(_finding(
                        "PSRAM_DATA_ERRORS", "psram", "fail",
                        "The board's extra memory gives wrong data",
                        "Extra memory pattern test: 1,204 errors, first at 0x3c812340 "
                        "(wrote 0x55555555, read 0x55515555)",
                        (err, result),
                    ))
                elif fraction >= 0.4 or stage != "psram":
                    finding = _ok(stage)
                    on_event(_self_test_event(finding.proof[0]))
                    findings.append(finding)
            if stage == "cleanup":
                failed = [f.stage for f in findings if f.result == "fail"] or ["none"]
                on_event(_self_test_event(_sltest(
                    "END", result="fail" if failed != ["none"] else "ok",
                    failed=",".join(failed), crc="1c291ca3")))
                on_event(_self_test_event(_sltest("CLEAN", result="ok")))
            if stage != "done" and self._pause(stage, cancel):
                return self._cancelled(board, started)
        if self.story == "resume_pass":
            return self._report(board, "passed", tuple(findings), started, erased=True,
                                kind="locked_board", keys_burned=True, recovery="RESUME")
        verdict = "failed" if any(f.result == "fail" for f in findings) else "passed"
        advice = texts.PSRAM_DATA_ERRORS if verdict == "failed" else None
        return self._report(board, verdict, tuple(findings), started, erased=True,
                            advice=advice)

    def check_failed_board(self, board, on_phase, on_event, cancel, seconds=90.0) -> object:
        self.calls.append(("check_failed_board", board.mac))
        started = _now()
        for phase, moment in ((ReplugPhase.WAITING_UNPLUG, "unplug"),
                              (ReplugPhase.WAITING_REPLUG, "replug"),
                              (ReplugPhase.WATCHING, "watch")):
            on_phase(phase)
            if self._pause(moment, cancel):
                return self._cancelled(board, started, kind="locked_board")
        if self.story == "locked_settings":
            lines = (
                ("I (1012) app_init: Project name:     zone_lite", ConsoleKind.BOOT_BANNER),
                ("E (1490) zone_config: no per-device settings found",
                 ConsoleKind.SETTINGS_MISSING),
            )
            for raw, kind in lines:
                on_event(api.ConsoleEvent(kind, raw, {}))
            finding = _finding(
                "SETTINGS_MISSING", "startup", "fail", "The board has no settings",
                "Board software started; zone_config found no per-device settings",
                tuple(raw for raw, _kind in lines),
            )
            return self._report(board, "incomplete", (finding,), started, erased=None,
                                kind="locked_board", keys_burned=True,
                                recovery="FIX_SETTINGS", sdm=True)
        lines = (
            ("I (238) octal_psram: vendor id    : 0x0d (AP)", ConsoleKind.OTHER),
            ("I (239) octal_psram: density      : 0x03 (64 Mbit)", ConsoleKind.OTHER),
            ("E (262) esp_psram: PSRAM chip is not connected, or wrong PSRAM line mode",
             ConsoleKind.PSRAM_FAILED),
            ("Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.",
             ConsoleKind.PANIC),
        )
        for raw, kind in lines:
            on_event(api.ConsoleEvent(kind, raw, {}))
        finding = _finding(
            "PSRAM_STARTUP_FAULT", "startup", "fail",
            "The board's extra memory fails when it starts",
            "Extra memory start-up: vendor AP, 64 Mbit, then a crash before the board "
            "software ran (3 restarts in 40 s)",
            tuple(raw for raw, _kind in lines),
        )
        return self._report(board, "failed", (finding,), started, erased=None,
                            kind="locked_board", keys_burned=True, sdm=True,
                            advice=texts.PSRAM_STARTUP_FAULT)

    # -------------------------------------------------------------- reports

    def _cancelled(self, board, started: str, kind: str = "vendor_test") -> object:
        finding = _finding("CANCELLED", "check", "info", "You stopped the test",
                           "Cancelled by the person; the test program was removed")
        return self._report(board, "incomplete", (finding,), started,
                            erased=None if kind == "locked_board" else True, kind=kind)

    @staticmethod
    def _report(board, verdict: str, findings, started: str, *, erased,
                kind: str = "vendor_test", keys_burned: bool = False,
                recovery: Optional[str] = None, sdm: bool = False, advice=None) -> object:
        efuses = {} if sdm else {"WR_DIS": {"value": 0}, "SECURE_BOOT_EN": {"value": False}}
        return api.BoardTestReport(
            mac=board.mac,
            verdict=api.BoardTestVerdict(verdict),
            kind=kind,
            started_at=started,
            finished_at=_now(),
            board=board,
            findings=tuple(findings),
            measurements={"psram_size": 8388608, "psram_mb_s": 41, "flash_id": "204018",
                          "networks": 6, "best_rssi": -48, "temp_c": 41, "chip_rev": "v0.2"},
            efuse_before=dict(efuses),
            efuse_after=dict(efuses),
            efuses_unchanged=None if sdm else True,
            erased=erased,
            keys_burned=keys_burned,
            console_log=None,
            tool_log=(),
            image_sha256={"board-test.bin": "0" * 64},
            end_crc_ok=None if sdm else True,
            recovery=recovery,
            advice=advice,
        )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _self_test_event(raw: str) -> api.ConsoleEvent:
    parts = raw.split(" ")
    data = {"sltest": parts[1]}
    for part in parts[2:]:
        name, _sep, value = part.partition("=")
        data[name] = value
    return api.ConsoleEvent(ConsoleKind.SELF_TEST, raw, data)


def scripted_save_board_test(folder: Callable[[], str]):
    """records.save_board_test for services whose records cannot write certificates."""

    def save(report, tester: str, kind: Optional[str] = None) -> Tuple[str, str]:
        mac = str(getattr(report, "mac", "")).replace(":", "")
        stem = os.path.join(folder(), f"board-test-{mac}-{kind or 'default'}")
        with open(stem + ".pdf", "wb") as handle:
            handle.write(b"%PDF-1.4\n% scripted certificate for " + tester.encode() + b"\n")
        with zipfile.ZipFile(stem + ".zip", "w") as archive:
            archive.writestr("certificate.txt", f"Scripted certificate, tested by {tester}\n")
        return stem + ".pdf", stem + ".zip"

    return save


def use_scripted_board_test(services: api.Services, story: str = "pass",
                            pace: float = PACE) -> ScriptedBoardTest:
    """Put a scripted board test (and, when needed, certificate saving) into ``services``."""
    tester = ScriptedBoardTest(story, pace)
    services.board_test = tester
    records = services.records
    world = getattr(records, "world", None)
    if world is not None and not callable(getattr(type(records), "save_board_test", None)):
        records.save_board_test = scripted_save_board_test(world.folder)
    return tester



class Driver:
    """Drives one window through a fake story, the way a person would click."""

    def __init__(self, scenario: str, size: QSize) -> None:
        self.services = fakes.make_fake_services(scenario, pace=PACE)
        self.world = fakes.fake_world(self.services)
        self.window = MainWindow(self.services)
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.window.resize(size)
        self.window.show()
        self.controller = self.window.controller
        self._held: List[str] = []
        self.board_tester: Optional[ScriptedBoardTest] = None
        if getattr(self.services, "board_test", None) is None:
            self.board_tester = use_scripted_board_test(self.services, "pass")

    # ---------------------------------------------------------------- basics

    def page(self, page_id: str):
        return self.window.pages[page_id]

    def wait(self, condition: Callable[[], bool], what: str, timeout: float = TIMEOUT_S) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            QApplication.processEvents()
            if condition():
                return
            time.sleep(0.003)
        raise SceneError(f"screenshot scene did not reach: {what}")

    def settle(self, rounds: int = 6) -> None:
        for _ in range(rounds):
            QApplication.processEvents()
            time.sleep(0.004)

    def hold(self, moment: str) -> None:
        self.world.hold(moment)
        self._held.append(moment)

    def act(self, page_id: str, name: str) -> None:
        self.page(page_id).action.emit(name)

    def scripted(self, story: str, *held: str) -> ScriptedBoardTest:
        """This window's board test tells ``story``, stopping at each of ``held``."""
        self.board_tester = use_scripted_board_test(self.services, story)
        for moment in held:
            self.board_tester.hold(moment)
        return self.board_tester

    def close(self) -> None:
        for moment in self._held:
            self.world.release(moment)
        if self.board_tester is not None:
            self.board_tester.release_all()
        self.controller.shutdown()
        self.window.hide()
        if not self.controller.runner.wait(10000):
            raise SceneError("background work did not stop")
        self.window.deleteLater()
        # Outside an event loop, deleteLater only happens when asked for.
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.world.cleanup()

    def save(self, path: str) -> str:
        self.settle()
        pixmap = self.window.grab()
        if pixmap.isNull() or not pixmap.save(path, "PNG"):
            raise SceneError(f"could not save {path}")
        return path

    # ----------------------------------------------------------------- steps

    def connectivity(self) -> None:
        self.controller.check_connectivity()
        self.wait(lambda: not self.controller.running("connectivity"), "connectivity checked")

    def sign_in_page(self) -> None:
        self.controller.show(pages.SIGN_IN)
        self.connectivity()

    def try_sign_in(self, password: str = "test-admin-pass") -> None:
        page = self.page(pages.SIGN_IN)
        page.username.setText("admin")
        page.password.setText(password)
        self.act(pages.SIGN_IN, "sign_in")

    def signed_in(self) -> None:
        self.sign_in_page()
        self.try_sign_in()
        self.wait(lambda: self.controller.current == pages.HOME, "home")
        self.wait(lambda: not self.controller.running("capabilities"), "capabilities")

    def details(self, family: Family) -> None:
        self.signed_in()
        self.act(pages.HOME, "setup")
        self.act(pages.MACHINE, family.value)
        self.wait(
            lambda: not self.controller.running("bundles") and not self.controller.running("zones"),
            "details loaded",
        )

    def fill_zkt(self, zone: str = "LF-ZONE-BLD9-04") -> None:
        self.details(Family.ZKT)
        self.page(pages.DETAILS).form.zone_id.setText(zone)

    def fill_hik(self, zone: str = "LF-ZONE-JHELUM-01") -> None:
        self.details(Family.HIKVISION)
        form = self.page(pages.DETAILS).form
        form.zone_id.setText(zone)
        form.hik_host.setText("192.168.1.20")
        form.hik_password.setText("test-machine-pass")
        form.hik_model.setCurrentIndex(form.hik_model.findData("ds-k1t321mfwx"))
        form.hik_serial.setText("TEST-SERIAL-0001")

    def connect(self, family: Family = Family.ZKT) -> None:
        self.fill_zkt() if family is Family.ZKT else self.fill_hik()
        self.act(pages.DETAILS, "next")
        self.wait(lambda: self.controller.current == pages.CONNECT, "connect")

    def board_found(self, family: Family = Family.ZKT) -> None:
        self.connect(family)
        self.wait(lambda: self.page(pages.CONNECT).primary_button.isEnabled(), "board found")

    def checked(self, family: Family = Family.ZKT) -> None:
        self.board_found(family)
        self.act(pages.CONNECT, "next")
        self.wait(lambda: self.page(pages.CHECKING).result.isVisible(), "check result")

    def confirm(self, family: Family = Family.ZKT) -> None:
        self.checked(family)
        self.act(pages.CHECKING, "next")
        self.wait(lambda: self.controller.current == pages.CONFIRM, "confirm")

    def install(self, family: Family = Family.ZKT) -> None:
        self.confirm(family)
        self.page(pages.CONFIRM).understand.setChecked(True)
        self.act(pages.CONFIRM, "start")
        self.wait(lambda: self.controller.current == pages.INSTALLING, "installing")

    def first_start(self, family: Family = Family.ZKT) -> None:
        self.install(family)
        self.wait(lambda: self.controller.current == pages.FIRST_START, "first start")

    def done(self, family: Family = Family.ZKT) -> None:
        self.first_start(family)
        self.wait(
            lambda: self.page(pages.FIRST_START).primary_button.isVisible(), "first start result"
        )
        self.act(pages.FIRST_START, "next")
        self.wait(lambda: self.controller.current == pages.DONE, "done")

    def board_test(self, choice: str = "board_test") -> None:
        """Home's Test a new board (or ``failed_board``), up to the test screen."""
        self.signed_in()
        self.act(pages.HOME, choice)
        self.wait(lambda: self.page(pages.CONNECT).primary_button.isEnabled(), "board found")
        self.act(pages.CONNECT, "next")
        self.wait(lambda: self.controller.current == pages.BOARD_TEST, "board test")

    def board_test_result(self, choice: str = "board_test") -> None:
        self.board_test(choice)
        self.wait(lambda: self.controller.current == pages.BOARD_TEST_RESULT, "test result")
        page = self.page(pages.BOARD_TEST_RESULT)
        self.wait(
            lambda: page.saved.isVisible() and not self.controller.running("certificate"),
            "certificate saved",
        )
        self.settle()
        page.scroll_to_top()

    def check_board(self) -> None:
        self.signed_in()
        self.act(pages.HOME, "check")
        self.wait(lambda: self.page(pages.CONNECT).primary_button.isEnabled(), "board found")
        self.act(pages.CONNECT, "next")
        self.wait(
            lambda: (
                self.controller.current == pages.CHECK_BOARD
                and not self.controller.running("check")
            ),
            "board checked",
        )


# ====================================================================== scenes


def _sign_in(d: Driver) -> None:
    d.sign_in_page()


def _sign_in_failed(d: Driver) -> None:
    d.sign_in_page()
    d.try_sign_in("wrong-pass")
    d.wait(lambda: d.page(pages.SIGN_IN).advice.isVisible(), "sign-in advice")


def _home(d: Driver) -> None:
    d.signed_in()


def _machine(d: Driver) -> None:
    d.signed_in()
    d.act(pages.HOME, "setup")


def _details_zkt(d: Driver) -> None:
    d.fill_zkt()


def _details_zkt_advanced(d: Driver) -> None:
    d.fill_zkt()
    page = d.page(pages.DETAILS)
    page.form.advanced.set_expanded(True)
    d.settle()
    page.scroll.verticalScrollBar().setValue(page.scroll.verticalScrollBar().maximum())


def _details_errors(d: Driver) -> None:
    d.details(Family.ZKT)
    page = d.page(pages.DETAILS)
    page.form.zone_id.setText("LF ZONE 9")
    page.form.wifi_password.setText("short")
    d.act(pages.DETAILS, "next")
    d.settle()
    page.scroll.verticalScrollBar().setValue(0)


def _details_hik(d: Driver) -> None:
    d.fill_hik()


def _details_hik_machine(d: Driver) -> None:
    d.fill_hik()
    page = d.page(pages.DETAILS)
    d.settle()
    page.scroll.ensureWidgetVisible(page.form.software_card, 20, 20)


def _details_hik_advanced(d: Driver) -> None:
    d.fill_hik()
    page = d.page(pages.DETAILS)
    page.form.advanced.set_expanded(True)
    d.settle()
    page.scroll.verticalScrollBar().setValue(page.scroll.verticalScrollBar().maximum())


def _connect_waiting(d: Driver) -> None:
    d.hold("no_board_tips")
    d.connect()
    d.settle()


def _connect_tips(d: Driver) -> None:
    d.hold("board_found")
    d.connect()
    d.wait(lambda: d.page(pages.CONNECT).problem.isVisible(), "no-board tips")


def _connect_problem(d: Driver) -> None:
    d.connect()
    d.wait(lambda: d.page(pages.CONNECT).problem.isVisible(), "connect problem")


def _connect_setup_mode(d: Driver) -> None:
    d.hold("setup_mode")  # the board keeps running its other software
    _connect_problem(d)


def _connect_found(d: Driver) -> None:
    d.board_found()


def _checking_progress(d: Driver) -> None:
    d.hold("lookup")
    d.board_found()
    d.act(pages.CONNECT, "next")
    d.wait(lambda: d.page(pages.CHECKING).facts_card.isVisible(), "board read")


def _checked(d: Driver) -> None:
    d.checked()


def _checked_hik(d: Driver) -> None:
    d.checked(Family.HIKVISION)


def _confirm(d: Driver) -> None:
    d.confirm()


def _installing(d: Driver) -> None:
    d.hold("step_copy_firmware")
    d.install()
    page = d.page(pages.INSTALLING)
    d.wait(lambda: page.current is api.StepId.COPY_FIRMWARE, "copying")
    page.update_progress(api.StepProgress(api.StepId.COPY_FIRMWARE, 0.42, "2.6 of 6.2 MB"))


def _install_failed(d: Driver) -> None:
    d.install()
    d.wait(lambda: d.page(pages.INSTALLING).problem.isVisible(), "install problem")


def _first_start_unplug(d: Driver) -> None:
    d.hold("unplug")
    d.first_start()
    d.wait(lambda: d.page(pages.FIRST_START).phase is ReplugPhase.WAITING_UNPLUG, "unplug")


def _first_start_wait(d: Driver) -> None:
    d.hold("replug")
    d.first_start()
    d.wait(lambda: d.page(pages.FIRST_START).phase is ReplugPhase.WAITING_REPLUG, "wait")


def _first_start_replug(d: Driver) -> None:
    _first_start_wait(d)
    d.page(pages.FIRST_START).start_countdown(0)


def _first_start_watching(d: Driver) -> None:
    d.hold("boot_line")
    d.first_start()
    page = d.page(pages.FIRST_START)
    d.wait(lambda: page.phase is ReplugPhase.WATCHING, "watching")
    config = d.controller.session.config
    for event in fakes.first_boot_lines(config, config.zone_id, new_board=True)[:5]:
        page.add_event(event)
    page.reveal(page.watch)


def _first_start_again(d: Driver) -> None:
    # The first start passed: the same steps once more, for the restart check.
    d.hold("restart_unplug")
    d.first_start()
    page = d.page(pages.FIRST_START)
    d.wait(lambda: page.again and page.phase is ReplugPhase.WAITING_UNPLUG, "unplug again")


def _first_start_result(d: Driver) -> None:
    # The last card: both starts passed, or the one that did not.
    d.first_start()
    page = d.page(pages.FIRST_START)
    d.wait(
        lambda: page.result.isVisible() and not d.controller.running("boot"), "first start result"
    )


def _first_start_hotspot(d: Driver) -> None:
    # The board opened its own Wi-Fi while it was watched: its name and password show.
    _first_start_result(d)
    page = d.page(pages.FIRST_START)
    d.wait(lambda: not page.hotspot_card.isHidden(), "hotspot card")
    d.settle()
    page.scroll.ensureWidgetVisible(page.hotspot_card, 20, 20)


def _done(d: Driver) -> None:
    d.done()


def _done_hik(d: Driver) -> None:
    d.done(Family.HIKVISION)


def _done_saved(d: Driver) -> None:
    d.done()
    d.act(pages.DONE, "save_record")
    d.wait(lambda: d.page(pages.DONE).saved.isVisible(), "record saved")


def _check_board(d: Driver) -> None:
    d.check_board()
    d.wait(lambda: len(d.page(pages.CHECK_BOARD).console.shown_lines) >= 6, "console lines")
    d.wait(lambda: "HOTSPOT_OPEN" in d.page(pages.CHECK_BOARD).shown_codes(), "wifi advice")


def _check_board_hotspot(d: Driver) -> None:
    _check_board(d)
    page = d.page(pages.CHECK_BOARD)
    d.wait(lambda: page.hotspot_steps.expanded, "hotspot steps")
    d.settle()
    page.scroll.ensureWidgetVisible(page.hotspot_card, 20, 20)


def _check_board_refused(d: Driver) -> None:
    # A board the software cannot start: the advice comes first, with what was measured.
    d.check_board()
    d.wait(lambda: bool(d.page(pages.CHECK_BOARD).shown_codes()), "advice")


def _check_board_settled(d: Driver) -> None:
    d.check_board()
    d.wait(lambda: d.page(pages.CHECK_BOARD).fix_blocked.isVisible(), "why Fix is not offered")


def _fix_settings(d: Driver) -> None:
    d.check_board()
    d.act(pages.CHECK_BOARD, "fix")
    d.wait(
        lambda: d.controller.current == pages.FIX_SETTINGS and not d.controller.running("bundles"),
        "fix settings",
    )


def _fix_settings_machine(d: Driver) -> None:
    _fix_settings(d)
    page = d.page(pages.FIX_SETTINGS)
    d.settle()
    page.scroll.ensureWidgetVisible(page.form.machine_card, 20, 20)


def _fix_settings_confirm(d: Driver) -> None:
    _fix_settings(d)
    page = d.page(pages.FIX_SETTINGS)
    page.confirm_edit.setText("LF-ZONE-BLD9-02")
    page.form.zkt_comm_key.setText("12345")  # typed from the attendance machine
    d.act(pages.FIX_SETTINGS, "next")
    d.wait(lambda: d.controller.current == pages.CONFIRM, "confirm new settings")


def _light_guide(d: Driver) -> None:
    d.signed_in()
    d.act(pages.HOME, "lights")


def _light_guide_colour(d: Driver) -> None:
    _light_guide(d)
    page: LightGuidePage = d.page(pages.LIGHT_GUIDE)
    page.choose_rhythm("slow")


def _light_guide_answer(d: Driver) -> None:
    _light_guide_colour(d)
    page: LightGuidePage = d.page(pages.LIGHT_GUIDE)
    page.choose_state("ZKT_FAILURE")


def _light_guide_hotspot(d: Driver) -> None:
    _light_guide_colour(d)
    page: LightGuidePage = d.page(pages.LIGHT_GUIDE)
    page.choose_state("WIFI_CONNECTING")
    d.settle()
    page.scroll.ensureWidgetVisible(page.answer, 20, 20)


def _board_test_running(d: Driver) -> None:
    d.hold("test_flash")
    d.board_test()
    page = d.page(pages.BOARD_TEST)
    d.wait(lambda: page.stage == "flash", "testing the memory chip")


def _board_test_replug(d: Driver) -> None:
    # The test program did not start by itself: the engine asks for one replug.
    d.scripted("no_start", "replug")
    d.board_test()
    page = d.page(pages.BOARD_TEST)
    d.wait(lambda: page.phase is ReplugPhase.WAITING_REPLUG, "replug steps")
    page.start_countdown(0)
    d.settle()
    page.reveal(page.replug_card)


def _board_test_result(choice: str = "board_test") -> Callable[[Driver], None]:
    def scene(d: Driver) -> None:
        d.board_test_result(choice)

    return scene


def _board_test_not_finished(d: Driver) -> None:
    d.scripted("no_start")
    d.board_test_result()


def _board_test_details(d: Driver) -> None:
    # Below the result: the tests one by one, and the board's facts.
    d.board_test_result()
    page = d.page(pages.BOARD_TEST_RESULT)
    page.scroll.verticalScrollBar().setValue(page.scroll.verticalScrollBar().maximum())


def _board_test_locked(d: Driver) -> None:
    d.board_test()
    d.wait(lambda: d.page(pages.BOARD_TEST).problem.isVisible(), "locked board advice")


def _failed_board_never_set_up(d: Driver) -> None:
    d.board_test("failed_board")
    d.wait(lambda: d.page(pages.BOARD_TEST).problem.isVisible(), "never set up advice")


def _failed_board_replug(d: Driver) -> None:
    d.hold("test_unplug")
    d.board_test("failed_board")
    page = d.page(pages.BOARD_TEST)
    d.wait(lambda: page.phase is ReplugPhase.WAITING_UNPLUG, "unplug step")


def _failed_board_watching(d: Driver) -> None:
    d.hold("console_line")
    d.board_test("failed_board")
    page = d.page(pages.BOARD_TEST)
    d.wait(lambda: page.phase is ReplugPhase.WATCHING, "watching")
    d.settle()
    page.reveal(page.watch)


def _setup_board_test(d: Driver) -> None:
    d.hold("test_wifi")
    d.checked()
    d.act(pages.CHECKING, "next")
    page = d.page(pages.BOARD_TEST)
    d.wait(lambda: page.stage == "wifi", "testing the Wi-Fi radio")


def _setup_board_test_failed(d: Driver) -> None:
    d.checked()
    d.act(pages.CHECKING, "next")
    d.wait(lambda: d.controller.current == pages.BOARD_TEST_RESULT, "test result")
    page = d.page(pages.BOARD_TEST_RESULT)
    d.wait(lambda: page.saved.isVisible() and not d.controller.running("certificate"), "saved")
    d.settle()
    page.scroll_to_top()


def _help(d: Driver) -> None:
    d.connect()
    d.act(pages.CONNECT, "help")


Scene = Tuple[str, str, Callable[[Driver], None]]

SCENES: Tuple[Scene, ...] = (
    ("sign-in", "happy_zkt", _sign_in),
    ("sign-in-wrong-password", "login_wrong_password", _sign_in_failed),
    ("sign-in-paused", "login_paused", _sign_in_failed),
    ("sign-in-no-internet", "no_internet", _sign_in_failed),
    ("sign-in-server-down", "server_down", _sign_in_failed),
    ("home", "happy_zkt", _home),
    ("home-setup-switched-off", "installer_off", _home),
    ("machine-type", "happy_zkt", _machine),
    ("details-zkt", "happy_zkt", _details_zkt),
    ("details-zkt-advanced", "happy_zkt", _details_zkt_advanced),
    ("details-mistakes", "happy_zkt", _details_errors),
    ("details-hik", "happy_hik", _details_hik),
    ("details-hik-machine", "happy_hik", _details_hik_machine),
    ("details-hik-advanced", "happy_hik", _details_hik_advanced),
    ("connect-looking", "no_board_then_found", _connect_waiting),
    ("connect-no-board-tips", "no_board_then_found", _connect_tips),
    ("connect-wrong-socket", "wrong_socket", _connect_problem),
    ("connect-setup-mode", "other_usb_mode", _connect_setup_mode),
    ("connect-several-boards", "several_boards", _connect_problem),
    ("connect-faulty-board", "faulty_restart_loop", _connect_problem),
    ("connect-fast-restart-loop", "fast_restart_loop", _connect_problem),
    ("connect-found", "happy_zkt", _connect_found),
    ("checking-in-progress", "happy_zkt", _checking_progress),
    ("checking-new-board", "happy_zkt", _checked),
    ("checking-resume", "resume_after_key", _checked),
    ("checking-already-set-up", "secured_board_fix_settings", _checked),
    ("checking-press-boot", "manual_boot_mode", _checked),
    ("checking-locked-by-others", "secured_other", _checked),
    ("checking-family-mismatch", "family_mismatch", _checked),
    ("checking-settings-not-allowed", "settings_change_not_allowed", _checked),
    ("checking-memory-chip-unsupported", "flash_chip_unsupported", _checked),
    ("checking-memory-chip-locked-board", "flash_chip_key_burned", _checked),
    ("checking-board-type-unsupported", "board_type_unsupported", _checked),
    ("confirm", "happy_zkt", _confirm),
    ("installing", "happy_zkt", _installing),
    ("installing-error-before-key", "install_error_before_key", _install_failed),
    ("installing-error-after-key", "install_error_after_key", _install_failed),
    ("first-start-unplug", "happy_zkt", _first_start_unplug),
    ("first-start-wait", "happy_zkt", _first_start_wait),
    ("first-start-replug", "happy_zkt", _first_start_replug),
    ("first-start-watching", "happy_zkt", _first_start_watching),
    ("first-start-again", "happy_zkt", _first_start_again),
    ("first-start-ok", "happy_zkt", _first_start_result),
    ("first-start-wrong-zone", "first_boot_wrong_zone", _first_start_result),
    ("first-start-settings-lost", "settings_lost_on_restart", _first_start_result),
    ("first-start-startup-refused", "startup_refused", _first_start_result),
    ("first-start-hotspot-open", "hotspot_at_start", _first_start_hotspot),
    ("done-zkt", "happy_zkt", _done),
    ("done-hik", "happy_hik", _done_hik),
    ("done-record-saved", "happy_zkt", _done_saved),
    ("check-board", "secured_board_fix_settings", _check_board),
    ("check-board-hotspot", "secured_board_fix_settings", _check_board_hotspot),
    ("check-board-head-office-unreachable", "lookup_fails", _check_board_settled),
    ("check-board-settings-not-allowed", "settings_change_not_allowed", _check_board_settled),
    ("check-board-memory-chip-unsupported", "flash_chip_key_burned", _check_board_refused),
    ("fix-settings", "secured_board_fix_settings", _fix_settings),
    ("fix-settings-comm-key", "secured_board_fix_settings", _fix_settings_machine),
    ("fix-settings-hik", "secured_hik_fix_settings", _fix_settings_machine),
    ("fix-settings-zone-not-writable", "zone_not_writable", _fix_settings),
    ("fix-settings-confirm", "secured_board_fix_settings", _fix_settings_confirm),
    ("light-guide", "happy_zkt", _light_guide),
    ("light-guide-colour", "happy_zkt", _light_guide_colour),
    ("light-guide-answer", "happy_zkt", _light_guide_answer),
    ("light-guide-hotspot", "happy_zkt", _light_guide_hotspot),
    ("help", "happy_zkt", _help),
    # The board test (1.2.0): Test a new board, Check a failed board, and the test that
    # comes before every setup.
    ("board-test-running", "board_test_pass", _board_test_running),
    ("board-test-replug", "happy_zkt", _board_test_replug),
    ("board-test-passed", "board_test_pass", _board_test_result()),
    ("board-test-memory-fault", "board_test_psram_errors", _board_test_result()),
    ("board-test-memory-fault-details", "board_test_psram_errors", _board_test_details),
    ("board-test-memory-start-fault", "board_test_psram_hang", _board_test_result()),
    ("board-test-memory-chip-refused", "board_test_xmc_refused", _board_test_result()),
    ("board-test-not-finished", "happy_zkt", _board_test_not_finished),
    ("board-test-locked-board", "secured_board_fix_settings", _board_test_locked),
    ("failed-board-never-set-up", "happy_zkt", _failed_board_never_set_up),
    ("failed-board-replug", "failed_board_psram", _failed_board_replug),
    ("failed-board-watching", "failed_board_psram", _failed_board_watching),
    ("failed-board-memory-fault", "failed_board_psram", _board_test_result("failed_board")),
    ("failed-board-settings-missing", "failed_board_settings",
     _board_test_result("failed_board")),
    ("failed-board-finish-setup", "failed_board_resume", _board_test_result("failed_board")),
    ("setup-board-test", "board_test_pass", _setup_board_test),
    ("setup-board-test-failed", "board_test_psram_errors", _setup_board_test_failed),
)


def capture(
    out_dir: str, scenes: Sequence[Scene] = SCENES, sizes: Sequence[QSize] = SIZES
) -> List[str]:
    ensure_app()
    os.makedirs(out_dir, exist_ok=True)
    was_frozen = motion.frozen()
    motion.freeze()
    saved: List[str] = []
    try:
        for size in sizes:
            for index, (name, scenario, scene) in enumerate(scenes, start=1):
                driver: Optional[Driver] = Driver(scenario, size)
                try:
                    scene(driver)
                    path = os.path.join(
                        out_dir, f"{index:02d}-{name}-{size.width()}x{size.height()}.png"
                    )
                    saved.append(driver.save(path))
                finally:
                    driver.close()
                    driver = None
    finally:
        if not was_frozen:
            motion.unfreeze()
    return saved


def capture_all(out_dir: str) -> List[str]:
    """Every screen and state at 1280x800 and 1024x700; returns the PNG paths."""
    return capture(out_dir)
