"""Pictures of every screen and state, made with the fake services.

``capture_all(out_dir)`` walks each screen through the real controller with a
scripted fake story (core/fakes.py), holding the story at the right moment for
in-between states, and saves PNGs at 1280x800 and 1024x700. Animations are
frozen so every run gives the same pictures. Used by ``--screenshots DIR``,
by CI, and by the UI tests.
"""

from __future__ import annotations

import os
import time
from typing import Callable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QApplication

from ..core import api, fakes
from ..core.api import Family, ReplugPhase
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

    def close(self) -> None:
        for moment in self._held:
            self.world.release(moment)
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


def _first_start_result(d: Driver) -> None:
    d.first_start()
    d.wait(lambda: d.page(pages.FIRST_START).result.isVisible(), "first start result")


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
    ("confirm", "happy_zkt", _confirm),
    ("installing", "happy_zkt", _installing),
    ("installing-error-before-key", "install_error_before_key", _install_failed),
    ("installing-error-after-key", "install_error_after_key", _install_failed),
    ("first-start-unplug", "happy_zkt", _first_start_unplug),
    ("first-start-wait", "happy_zkt", _first_start_wait),
    ("first-start-replug", "happy_zkt", _first_start_replug),
    ("first-start-watching", "happy_zkt", _first_start_watching),
    ("first-start-ok", "happy_zkt", _first_start_result),
    ("first-start-wrong-zone", "first_boot_wrong_zone", _first_start_result),
    ("done-zkt", "happy_zkt", _done),
    ("done-hik", "happy_hik", _done_hik),
    ("done-record-saved", "happy_zkt", _done_saved),
    ("check-board", "secured_board_fix_settings", _check_board),
    ("check-board-hotspot", "secured_board_fix_settings", _check_board_hotspot),
    ("check-board-head-office-unreachable", "lookup_fails", _check_board_settled),
    ("check-board-settings-not-allowed", "settings_change_not_allowed", _check_board_settled),
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
