"""The wizard: which screen shows, and the engine work behind each one.

Every engine call runs on a worker (ui/runner.py); results and engine
callbacks come back to the GUI thread through queued signals, so the window
never freezes. Each kind of background work is an ``Operation``: leaving a
screen, going Back, signing out or closing the window cancels its token, and
anything that arrives late from it is ignored.

Cancelling an install is allowed only before the step that cannot be undone
(the security key for a new board; for the other modes, the step that writes
what the board needs to start). After that the window will not close.
"""

from __future__ import annotations

import enum
import functools
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from ..core import api, texts
from ..core.api import (
    Advice,
    BootOutcome,
    BundleInfo,
    Family,
    InstallMode,
    PresenceState,
    SecurityState,
    Severity,
    StepId,
)
from ..core.installer import report_boot, report_restart
from . import naming, wording
from .pages import (
    CHECK_BOARD,
    CHECKING,
    CONFIRM,
    CONNECT,
    DETAILS,
    DONE,
    FIRST_START,
    FIX_SETTINGS,
    HOME,
    INSTALLING,
    LIGHT_GUIDE,
    MACHINE,
    SIGN_IN,
)
from .pages.checking import LOOKUP, READ
from .pages.settings_form import FormValues, bundle_words, model_for, to_config
from .runner import Operation, Runner

log = logging.getLogger(__name__)

CONNECTIVITY_INTERVAL_MS = 15000
# Head office can switch board setup on or off at any time: look again this often, and
# on every connectivity check while Home says setup cannot be used (review E7).
CAPABILITIES_REFRESH_S = 180.0
NO_BOARD_TIPS_AFTER_S = 15.0
# A board running other software is gone this long at most while it restarts (RST, or
# its own software): its setup-mode steps stay up meanwhile instead of flashing away.
SETUP_MODE_GAP_S = 3.0
FIRST_BOOT_TIMEOUT_S = 120.0
DIAGNOSE_DELAY_MS = 700

# Advice about the board's Wi-Fi, which tells the person to use the board's own Wi-Fi.
WIFI_ADVICE = frozenset(
    {"HOTSPOT_OPEN", "WIFI_PLACEHOLDER", "WIFI_NOT_JOINING", "LED_HINT:WIFI_CONNECTING"}
)


class Flow(str, enum.Enum):
    SETUP = "setup"
    CHECK = "check"


SETUP_STEPS = ("Machine", "Details", "Connect", "Check", "Confirm", "Install", "Start", "Done")
_SETUP_INDEX = {
    MACHINE: 0,
    DETAILS: 1,
    CONNECT: 2,
    CHECKING: 3,
    CONFIRM: 4,
    INSTALLING: 5,
    FIRST_START: 6,
    DONE: 7,
}
FIX_STEPS = ("Connect", "Check", "Settings", "Confirm", "Write", "Start", "Done")
_FIX_INDEX = {
    CONNECT: 0,
    CHECKING: 1,
    CHECK_BOARD: 1,
    FIX_SETTINGS: 2,
    CONFIRM: 3,
    INSTALLING: 4,
    FIRST_START: 5,
    DONE: 6,
}

# The first step that must not be interrupted, per mode.
CANCEL_LIMIT = {
    InstallMode.NEW_BOARD: StepId.LOCK_KEY,
    InstallMode.RESUME: StepId.COPY_STARTUP,
    InstallMode.CHANGE_SETTINGS: StepId.WRITE_SETTINGS,
}


def cancel_allowed(mode: InstallMode, current: Optional[StepId]) -> bool:
    steps = api.steps_for(mode)
    limit = steps.index(CANCEL_LIMIT[mode])
    index = steps.index(current) if current in steps else 0
    return index < limit


def advice_of(error: BaseException) -> Advice:
    if isinstance(error, api.InstallerError):
        return error.advice
    return wording.unexpected()


@dataclass
class Session:
    """Everything about the board being handled right now."""

    flow: Flow = Flow.SETUP
    fixing: bool = False
    family: Optional[Family] = None
    values: Optional[FormValues] = None
    bundles: Tuple[BundleInfo, ...] = ()
    bundle: Optional[BundleInfo] = None
    candidate: Optional[api.UsbCandidate] = None
    presence: Optional[api.PresenceReport] = None
    board: Optional[api.BoardInfo] = None
    record: Optional[api.DeviceRecord] = None
    mode: Optional[InstallMode] = None
    config: Optional[api.InstallConfig] = None
    summary: Optional[api.InstallSummary] = None
    boot: Optional[api.BootReport] = None  # the first start
    restart: Optional[api.BootReport] = None  # the restart check, after a good first start
    install_step: Optional[StepId] = None
    installing: bool = False
    user_cancelled: bool = False
    fix_loaded: bool = False
    form_ready: bool = False
    record_path: Optional[str] = None
    light_return: str = HOME
    events: List[api.ConsoleEvent] = field(default_factory=list)


@dataclass(frozen=True)
class _CheckOutcome:
    board: api.BoardInfo
    record: Optional[api.DeviceRecord]
    lookup_error: Optional[BaseException]
    advice: Tuple[Advice, ...]
    candidate: Optional[api.UsbCandidate] = None  # the port the board was read on


class Controller(QObject):
    page_changed = Signal(str)

    def __init__(self, services: api.Services, window) -> None:
        super().__init__(window)
        self.services = services
        self.window = window
        self.runner = Runner(self)
        self.session = Session()
        self.user: Optional[str] = None
        self.capabilities: Optional[api.Capabilities] = None
        self.zones: List[api.ZoneSummary] = []
        self.current = ""
        self._came_from = ""
        self._ops: Dict[str, Operation] = {}
        self._faulty_reported: Set[str] = set()
        self._presence_state: Optional[PresenceState] = None
        self._presence_fast = False
        self._loop_board: Optional[api.UsbCandidate] = None  # in a fast restart loop
        self._capabilities_at: Optional[float] = None  # time.monotonic() of the last load
        self._connectivity: Optional[api.ConnectivityStatus] = None
        # Check a board keeps its console open while the light guide is shown (E8).
        self._keep_board_watch = False
        self._poll = QTimer(self)
        self._poll.setInterval(CONNECTIVITY_INTERVAL_MS)
        self._poll.timeout.connect(self.check_connectivity)
        self._diagnose_timer = QTimer(self)
        self._diagnose_timer.setSingleShot(True)
        self._diagnose_timer.setInterval(DIAGNOSE_DELAY_MS)
        self._diagnose_timer.timeout.connect(self._diagnose_check_board)
        for page_id, page in window.pages.items():
            page.action.connect(functools.partial(self._on_action, page_id))
        window.top_bar.sign_out_clicked.connect(self.sign_out)
        window.help_panel.save_help_file.connect(self._save_help_file)
        window.help_panel.open_folder.connect(self._open_folder)

    # ================================================================ basics

    def page(self, page_id: str):
        return self.window.pages[page_id]

    def start(self) -> None:
        """Show the sign-in screen and start watching the internet."""
        self.show(SIGN_IN)
        self.check_connectivity()
        self._poll.start()

    def shutdown(self) -> None:
        self._poll.stop()
        self._diagnose_timer.stop()
        for operation in list(self._ops.values()):
            operation.stop()
        self._ops.clear()

    def close_blocker(self) -> Optional[str]:
        """Words for why the window must stay open, or None."""
        if (
            self.session.installing
            and self.session.mode is not None
            and not cancel_allowed(self.session.mode, self.session.install_step)
        ):
            return (
                "The board is being set up. Closing the app now could damage the board. "
                "Please wait until the app says it is finished."
            )
        return None

    def close_question(self) -> Optional[str]:
        """Words to ask before the window closes, or None.

        While the restart check runs, the board has started once and nothing stops a
        person in a hurry from taking it away: closing the app then skips the check that
        shows it keeps its settings. The check can take minutes, so this only asks.
        """
        boot = self.session.boot
        if self.running("boot") and boot is not None and boot.outcome is BootOutcome.OK:
            return (
                "The app has not finished checking this board. If you close the app now, "
                "do not install the board at a site."
            )
        return None

    def _begin(self, kind: str) -> Operation:
        old = self._ops.get(kind)
        if old is not None:
            old.stop()
        operation = Operation(kind)
        self._ops[kind] = operation
        return operation

    def _stop(self, *kinds: str) -> None:
        for kind in kinds:
            operation = self._ops.pop(kind, None)
            if operation is not None:
                operation.stop()

    def running(self, kind: str) -> bool:
        operation = self._ops.get(kind)
        return operation is not None and operation.current

    def _run(
        self,
        kind: str,
        work: Callable[[Operation], object],
        on_done: Optional[Callable[[object], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> Operation:
        """Start ``work(operation)`` on a worker as the current ``kind`` operation."""
        operation = self._begin(kind)

        def finished(callback, value):
            if self._ops.get(kind) is operation:
                del self._ops[kind]
            if callback is not None:
                callback(value)

        self.runner.start(
            lambda: work(operation),
            functools.partial(finished, on_done),
            functools.partial(finished, on_error),
            operation,
        )
        return operation

    # ============================================================ navigation

    def show(self, page_id: str) -> None:
        came_from = self.current
        if self.current and self.current != page_id:
            self._leave(self.current, page_id)
        self._came_from = came_from
        self.current = page_id
        self.window.show_page(page_id)
        self._update_steps()
        self._update_sign_out()
        enter = getattr(self, f"_enter_{page_id}", None)
        if enter is not None:
            enter()
        self.page_changed.emit(page_id)

    def _leave(self, page_id: str, target: str = "") -> None:
        if page_id == CONNECT:
            self._stop("presence", "presence_advice")
        elif page_id == CHECKING:
            self._stop("check")
        elif page_id == CHECK_BOARD:
            self._diagnose_timer.stop()
            # The light guide is a quick look-up while the board keeps talking: its console
            # stays open, so coming back neither resets the board nor loses what it said.
            self._keep_board_watch = target == LIGHT_GUIDE and not self.running("check")
            self._stop("check", "diagnose", "lookup")
            if not self._keep_board_watch:
                self._stop("console")
        elif page_id == LIGHT_GUIDE:
            if target != CHECK_BOARD:
                self._keep_board_watch = False
                self._diagnose_timer.stop()
                self._stop("console", "diagnose")
        elif page_id == FIRST_START:
            self._stop("boot")
        elif page_id == FIX_SETTINGS:
            self._stop("zones", "lookup")
        elif page_id == DETAILS:
            self._stop("bundles", "zones")

    def back(self) -> None:
        target = self._back_target()
        if target is not None:
            if self.current == FIX_SETTINGS:
                # Back on the way to the board check: no longer changing settings, so the
                # steps (and Back from there) are the check's own again.
                self.session.fixing = False
            self.show(target)

    def _back_target(self) -> Optional[str]:
        session, page = self.session, self.current
        if page == MACHINE:
            return HOME
        if page == DETAILS:
            return MACHINE
        if page == CONNECT:
            return DETAILS if session.flow is Flow.SETUP and not session.fixing else HOME
        if page == CHECKING:
            return CONNECT
        if page == CONFIRM:
            return FIX_SETTINGS if session.fixing else DETAILS
        if page == FIX_SETTINGS:
            return CHECK_BOARD if session.flow is Flow.CHECK else CHECKING
        if page == CHECK_BOARD:
            return HOME
        if page == LIGHT_GUIDE:
            return session.light_return
        return None

    def _update_steps(self) -> None:
        page, session = self.current, self.session
        if session.fixing and page in _FIX_INDEX:
            index = len(FIX_STEPS) if page == DONE else _FIX_INDEX[page]  # Done: every step ticked
            self.window.set_steps(FIX_STEPS, index)
        elif session.flow is Flow.SETUP and page in _SETUP_INDEX:
            index = len(SETUP_STEPS) if page == DONE else _SETUP_INDEX[page]
            self.window.set_steps(SETUP_STEPS, index)
        else:
            self.window.set_steps(None)

    def _update_sign_out(self) -> None:
        busy = self.session.installing or self.running("boot")
        self.window.top_bar.set_sign_out_enabled(not busy)

    def _on_action(self, page_id: str, name: str) -> None:
        if page_id != self.current:
            return
        if name == "help":
            self.window.open_help(self.page(page_id).help_topic)
            return
        handler = getattr(self, f"_{page_id}_{name}", None)
        if handler is not None:
            handler()
        elif name == "back":
            self.back()
        elif name == "home":
            self.go_home()
        else:
            log.warning("no handler for %s/%s", page_id, name)

    def go_home(self) -> None:
        self.session = Session()
        self.show(HOME)

    # ========================================================== connectivity

    def check_connectivity(self) -> None:
        if self.running("connectivity"):
            return
        self._run(
            "connectivity",
            lambda _op: self.services.connectivity.check(),
            self._connectivity_checked,
            self._connectivity_failed,
        )

    def _connectivity_checked(self, status: object) -> None:
        if not isinstance(status, api.ConnectivityStatus):
            status = api.ConnectivityStatus(False, False)
        self._connectivity = status
        self.window.top_bar.set_connectivity(status)
        words = wording.network_words(status)
        self.page(SIGN_IN).set_network(words)
        self.page(HOME).set_network(words)
        # Look again often while Home says setup cannot be used, and now and then anyway.
        on_notice = self.current == HOME and self.home_notice() is not None
        self._refresh_capabilities(0.0 if on_notice else CAPABILITIES_REFRESH_S)

    def _connectivity_failed(self, error: BaseException) -> None:
        self._connectivity_checked(api.ConnectivityStatus(False, False))

    # =============================================================== sign in

    def _sign_in_sign_in(self) -> None:
        page = self.page(SIGN_IN)
        if self.running("login") or not page.check_filled():
            return
        username, password = page.credentials()
        page.set_busy(True)
        page.show_advice(None)
        self._run(
            "login",
            lambda _op: self.services.server.login(username, password),
            functools.partial(self._signed_in, username),
            self._sign_in_failed,
        )

    def _signed_in(self, username: str, result: object) -> None:
        page = self.page(SIGN_IN)
        self.user = result if isinstance(result, str) and result else username
        page.set_busy(False)
        page.clear_password()
        self.window.top_bar.set_user(self.user)
        self._load_capabilities()
        self._load_zones()
        self.go_home()

    def _sign_in_failed(self, error: BaseException) -> None:
        page = self.page(SIGN_IN)
        page.set_busy(False)
        advice = advice_of(error)
        status = self._connectivity
        if advice.code == "SECURE_CONNECTION_FAILED" and status is not None and not status.internet:
            # The internet check failed too, for example on a network that wants its own
            # sign-in page first: say that, not "check the date and time" (E14).
            advice = wording.network_problem(status)
        page.show_advice(advice)
        page.password.setFocus()
        self.check_connectivity()

    def sign_out(self) -> None:
        if self.session.installing:
            return
        self.shutdown()
        self._poll.start()
        user_was = self.user
        self.user = None
        self.capabilities = None
        self._capabilities_at = None
        self.zones = []
        self.session = Session()
        self.window.top_bar.set_user(None)
        self.page(SIGN_IN).reset()
        if user_was:
            self.runner.start(
                self.services.server.logout,
                on_error=lambda error: log.info("sign-out did not reach the server: %s", error),
            )
        self.show(SIGN_IN)

    def _load_capabilities(self) -> None:
        self._run(
            "capabilities",
            lambda _op: self.services.server.capabilities(),
            self._capabilities_loaded,
            lambda error: log.info("capabilities not loaded: %s", error),
        )

    def _capabilities_loaded(self, capabilities: object) -> None:
        if isinstance(capabilities, api.Capabilities):
            self.capabilities = capabilities
            self._capabilities_at = time.monotonic()
            if self.current == HOME:
                self._draw_home()

    def _refresh_capabilities(self, max_age_s: float = 0.0) -> None:
        """Ask head office again, unless the last answer is younger than ``max_age_s``."""
        if not self.user or self.running("capabilities"):
            return
        loaded = self._capabilities_at
        if max_age_s and loaded is not None and time.monotonic() - loaded < max_age_s:
            return
        self._load_capabilities()

    def home_notice(self) -> Optional[Advice]:
        caps = self.capabilities
        if caps is None:
            return None
        if naming.app_is_too_old(self.services.app_version, caps.min_app_version):
            return wording.app_too_old(caps.min_app_version)
        if not caps.enabled:
            return wording.installer_off()
        if not caps.provisioner_ready:
            return wording.settings_service_down()
        return None

    def _load_zones(self, then: Optional[Callable[[], None]] = None) -> None:
        def loaded(zones: object) -> None:
            self.zones = list(zones or ())
            for page_id in (DETAILS, FIX_SETTINGS):
                self.page(page_id).form.set_zones(self.zones)
            if then is not None:
                then()

        self._run(
            "zones",
            lambda _op: tuple(self.services.server.zones()),
            loaded,
            lambda error: log.info("zones not loaded: %s", error),
        )

    # ================================================================== home

    def _enter_home(self) -> None:
        self._draw_home()
        # Head office may have switched setup on (or off) since the last look.
        self._refresh_capabilities()

    def _draw_home(self) -> None:
        notice = self.home_notice()
        page = self.page(HOME)
        page.set_notice(notice, recheck=notice is not None and notice.code != "APP_TOO_OLD")
        page.set_network(wording.network_words(self._connectivity))
        blocked = notice is not None and notice.code in ("APP_TOO_OLD", "INSTALLER_OFF")
        page.setup_card.setEnabled(not blocked)

    def _home_recheck(self) -> None:
        self._refresh_capabilities()

    def _home_setup(self) -> None:
        family = self.session.family
        self.session = Session(flow=Flow.SETUP, family=family)
        self.page(MACHINE).set_selected(None)
        self.show(MACHINE)

    def _home_check(self) -> None:
        self.session = Session(flow=Flow.CHECK)
        self.show(CONNECT)

    def _home_connect_help(self) -> None:
        self.window.open_help("connect")

    def _home_lights(self) -> None:
        self.open_light_guide(HOME)

    def open_light_guide(self, came_from: str) -> None:
        self.session.light_return = came_from
        page = self.page(LIGHT_GUIDE)
        page.reset()
        # The blue blink means the board's own Wi-Fi may be open: the answer shows its name
        # (when a board is connected) and password, not only the last screen of a setup (E1).
        board = self.session.board
        page.set_hotspot(
            naming.hotspot_name(board.mac) if board is not None else None,
            self.services.defaults.setup_ap_password,
        )
        self.show(LIGHT_GUIDE)

    # =========================================================== machine type

    def _machine_zkt(self) -> None:
        self.choose_family(Family.ZKT)

    def _machine_hikvision(self) -> None:
        self.choose_family(Family.HIKVISION)

    def choose_family(self, family: Family) -> None:
        page = self.page(DETAILS)
        if not self.session.form_ready or family is not page.form.family:
            page.form.reset(family, self.services.defaults)
            page.form.set_zones(self.zones)
            self.session.form_ready = True
        self.session.family = family
        self.page(MACHINE).set_selected(family)
        page.set_family(family)
        self.show(DETAILS)

    # ================================================================ details

    def _enter_details(self) -> None:
        self.page(DETAILS).show_problem(None)
        self._load_bundles(self.session.family or Family.ZKT)
        self._load_zones()

    def _details_reload(self) -> None:
        self._enter_details()

    def _load_bundles(self, family: Family) -> None:
        """The board software on offer, for the Details form (a settings change needs none)."""
        page = self.page(DETAILS)
        page.form.set_bundles_loading()

        def loaded(bundles: object) -> None:
            found = tuple(item for item in (bundles or ()) if item.family is family)
            self.session.bundles = found
            page.form.set_bundles(found)
            page.show_problem(None if found else wording.no_bundles(family))

        def failed(error: BaseException) -> None:
            page.form.set_bundles(())
            page.show_problem(advice_of(error))

        self._run(
            "bundles", lambda _op: tuple(self.services.server.bundles(family)), loaded, failed
        )

    def _details_next(self) -> None:
        page = self.page(DETAILS)
        if self.running("bundles"):
            page.form.bundle_field.set_error("Wait a moment. The list is still loading.")
            return
        if not page.form.validate():
            return
        values = page.form.values()
        self.session.values = values
        self.session.family = values.family
        self.session.bundle = next(
            (item for item in self.session.bundles if item.bundle_id == values.bundle_id), None
        )
        if self.session.bundle is None:
            page.show_problem(wording.no_bundles(values.family))
            return
        self.show(CONNECT)

    # ================================================================ connect

    def _enter_connect(self) -> None:
        page = self.page(CONNECT)
        page.reset()
        self._presence_state = None
        self._presence_fast = False
        self._loop_board = None
        self.session.candidate = None

        def watch(operation: Operation) -> None:
            on_report = self.runner.relay(self._on_presence, operation)
            self.services.usb.watch(on_report, operation.token, 0.3)

        self._run("presence", watch, None, self._presence_failed)

    def _on_presence(self, report: api.PresenceReport) -> None:
        page = self.page(CONNECT)
        self.session.presence = report
        state = report.state
        if state is PresenceState.ONE_BOARD and report.board is not None:
            self.session.candidate = report.board
            if self._presence_state is not PresenceState.ONE_BOARD:
                page.show_found(report.board)
            self._presence_state, self._presence_fast = state, False
            return
        self.session.candidate = None
        if state is PresenceState.NO_BOARD and report.seconds_in_state < NO_BOARD_TIPS_AFTER_S:
            if (
                self._presence_state is PresenceState.OTHER_USB_MODE
                and report.seconds_in_state < SETUP_MODE_GAP_S
            ):
                # Restarting: the steps stay up until it is back in its own software (the
                # same advice, not shown again) or in setup mode (Board found).
                return
            if self._presence_state is not None:
                page.show_waiting()
            self._presence_state, self._presence_fast = None, False
            return
        fast = wording.fast_restart_loop(report)
        if state is PresenceState.RESTART_LOOP and report.board is not None:
            if fast:
                # Not the faulty-board signature: a cable or power problem first, or a board
                # that stops at its start. Next stays grey; Check again reads it anyway,
                # which puts it into setup mode and so stops the loop (review E5).
                self._loop_board = report.board
            else:
                self._report_faulty(report)
        if state is not self._presence_state or fast is not self._presence_fast:
            self._presence_state = state
            self._presence_fast = fast
            self._explain_presence(report)

    def _explain_presence(self, report: api.PresenceReport) -> None:
        fallback = wording.presence_advice(report)
        page = self.page(CONNECT)
        fast = wording.fast_restart_loop(report)
        faulty = report.state is PresenceState.RESTART_LOOP and not fast

        def show(advices: object) -> None:
            if self._presence_state is not report.state or self._presence_fast is not fast:
                return
            chosen = next(
                (item for item in (advices or ()) if item.severity is not Severity.INFO), None
            )
            advice = chosen or fallback
            if advice is not None:
                actions = (
                    [("Check again", "check_again", "primary")]
                    if fast and self._loop_board is not None
                    else []
                )
                page.show_problem(advice, still_looking=not faulty, actions=actions)

        def failed(error: BaseException) -> None:
            show(())

        self._run(
            "presence_advice",
            lambda _op: tuple(self.services.diagnosis.diagnose(None, report, None, ())),
            show,
            failed,
        )

    def _report_faulty(self, report: api.PresenceReport) -> None:
        board = report.board
        mac = board.mac if board is not None else None
        if not mac or mac in self._faulty_reported:
            return
        self._faulty_reported.add(mac)
        detail: Dict[str, object] = {
            "loop_period_s": report.loop_period_s,
            "port": board.port,
            "app_version": self.services.app_version,
        }
        request_id = f"ui-{uuid.uuid4().hex}"
        self.runner.start(
            lambda: self.services.server.report(request_id, mac, "BOARD_FAULTY", detail),
            on_error=lambda error: log.info("faulty board not reported: %s", error),
        )

    def _presence_failed(self, error: BaseException) -> None:
        self.page(CONNECT).show_problem(advice_of(error), still_looking=False)

    def _connect_check_again(self) -> None:
        """A board in a fast restart loop: read it anyway (after a new cable or socket)."""
        board = self._loop_board
        if board is None or not self._presence_fast:
            return
        self.session.candidate = board
        # Reading it puts it into setup mode, which ends the loop: the advice on the next
        # screen must not keep saying it restarts.
        self.session.presence = api.PresenceReport(PresenceState.ONE_BOARD, (board,), board)
        self._connect_next()

    def _connect_next(self) -> None:
        if self.session.candidate is None:
            return
        if self.session.flow is Flow.CHECK and not self.session.fixing:
            self.show(CHECK_BOARD)
        else:
            self.show(CHECKING)

    # =============================================================== checking

    def _board_on_usb_now(self, candidate: api.UsbCandidate) -> api.UsbCandidate:
        """The same board as USB lists it now (on the worker): the port with its board ID,
        or the only board when the old or the new port gives no ID.

        Plugged into another socket it has a new port name, so Try again reads it there
        instead of failing on the old one. Reading is all a check does.

        A board that runs other software is back in it after a replug or a press of RST
        (Connect's setup-mode advice, core/usb.py): with no native port but such a one,
        the old port is gone, and "unplugged" would be wrong. The way on is setup mode
        by hand, then Try again.
        """
        try:
            listed = self.services.usb.snapshot().candidates
        except Exception:  # the old port may still be right
            log.info("could not list the boards again", exc_info=True)
            return candidate
        natives = [item for item in listed if item.kind is api.UsbKind.NATIVE]
        if not natives and any(item.kind is api.UsbKind.OTHER for item in listed):
            raise api.InstallerError(
                texts.AUTO_DOWNLOAD_FAILED, cause="no board in setup mode, other software on USB"
            )
        same = [item for item in natives if candidate.mac and item.mac == candidate.mac]
        if same:
            return same[0]
        if len(natives) == 1 and (candidate.mac is None or natives[0].mac is None):
            return natives[0]
        return candidate

    def _inspect_and_look_up(
        self, operation: Operation, on_board: Callable[[api.BoardInfo], None]
    ) -> _CheckOutcome:
        candidate = self._board_on_usb_now(self.session.candidate)
        presence = self.session.presence
        board = self.services.boards.inspect(candidate, operation.token)
        on_board(board)
        record, lookup_error = None, None
        try:
            record = self.services.server.device(board.mac)
        except api.InstallerError as error:
            lookup_error = error
        advice: Tuple[Advice, ...] = ()
        if presence is not None:
            try:
                advice = tuple(self.services.diagnosis.diagnose(board, presence, record, ()))
            except Exception:  # advice is a bonus here; the result still stands
                log.exception("diagnosis failed")
        return _CheckOutcome(board, record, lookup_error, advice, candidate)

    def _enter_checking(self) -> None:
        page = self.page(CHECKING)
        page.start()
        if self.session.candidate is None:
            self.show(CONNECT)
            return

        def work(operation: Operation) -> _CheckOutcome:
            return self._inspect_and_look_up(
                operation, self.runner.relay(page.board_read, operation)
            )

        self._run("check", work, self._checked, self._check_failed)

    def _check_failed(self, error: BaseException) -> None:
        advice = advice_of(error)
        primary = ("Try again", "retry") if advice.can_retry else None
        self.page(CHECKING).show_result(advice, primary=primary, failed_step=READ)

    def _checked(self, outcome: object) -> None:
        if not isinstance(outcome, _CheckOutcome):
            return
        session, page = self.session, self.page(CHECKING)
        board, record = outcome.board, outcome.record
        session.board, session.record = board, record
        session.candidate = outcome.candidate or session.candidate
        page.show_facts(board, record)
        if outcome.lookup_error is not None:
            advice = advice_of(outcome.lookup_error)
            page.show_result(
                advice,
                primary=("Try again", "retry") if advice.can_retry else None,
                failed_step=LOOKUP,
            )
            return
        refusal = wording.board_refusal(board)
        if refusal is not None:
            stop = next(
                (item for item in outcome.advice if item.severity is Severity.ERROR), refusal
            )
            retry = refusal.can_retry
            page.show_result(
                stop,
                primary=("Try again", "retry") if retry else None,
                secondary=None if retry else ("Use another board", "another"),
            )
            return
        blocked = wording.install_blocked(session.candidate)
        if blocked is not None:
            # Each way on from here writes to the board, which the engine does only on the
            # port that has the board's USB board ID (board-safety review BS1).
            session.mode = None
            page.show_result(blocked, primary=("Try again", "retry"))
            return
        security = board.security
        known_family = record.family if record is not None and record.known else None
        # The family rule holds for every board, set up or not (design section 4, rule 7):
        # head office's machine type wins, and the way on is to choose it (review E4).
        if (
            known_family is not None
            and session.family is not None
            and known_family is not session.family
        ):
            page.show_result(
                wording.family_mismatch(known_family, session.family),
                primary=(f"Choose {wording.family_name(known_family)}", "machine"),
                secondary=("Use another board", "another"),
            )
            return
        if security is SecurityState.BLANK:
            session.mode = InstallMode.NEW_BOARD
            page.show_result(
                Advice(
                    "NEW_BOARD",
                    "This is a new board",
                    "It has never been set up. It is ready for the next step.",
                    (),
                    Severity.SUCCESS,
                    "success",
                ),
                primary=("Next", "next"),
            )
        elif security is SecurityState.KEY_BURNED_NOT_STARTED:
            blocked = wording.resume_blocked(record)
            if blocked is not None:
                # Head office has seen this board working: not a setup that stopped, and
                # ADD would refuse to finish it (review F1).
                session.mode = None
                page.show_result(blocked, secondary=("Use another board", "another"))
                return
            session.mode = InstallMode.RESUME
            page.show_result(
                Advice(
                    "RESUME",
                    "The last setup of this board did not finish",
                    "The board was locked for State Life, but the setup stopped before the "
                    "end. You can finish it now.",
                    ("Click Finish the setup. The details you typed are used.",),
                    Severity.WARNING,
                    "cable_in",
                ),
                primary=("Finish the setup", "resume"),
            )
        elif security is SecurityState.SECURED_STATE_LIFE:
            blocked = wording.settings_change_blocked(record)
            if blocked is not None:
                session.mode = None
                page.show_result(blocked, secondary=("Use another board", "another"))
                return
            session.mode = InstallMode.CHANGE_SETTINGS
            known = record is not None and record.known
            where = (
                f"Head office knows it as {record.zone_id}, device number {record.device_id}."
                if known and record.zone_id
                else "Head office does not know where it is used."
            )
            page.show_result(
                Advice(
                    "ALREADY_SET_UP",
                    "This board is already set up",
                    where,
                    (
                        "To give it new settings, click Change its settings.",
                        "To set up a new board instead, go back and connect another board.",
                    ),
                    Severity.INFO,
                    "success",
                ),
                primary=("Change its settings", "fix"),
            )

    def _checking_next(self) -> None:
        self.session.fixing = False
        self.show(CONFIRM)

    def _checking_resume(self) -> None:
        self.session.fixing = False
        self.show(CONFIRM)

    def _checking_fix(self) -> None:
        if wording.settings_change_blocked(self.session.record) is None:
            self.start_fixing()

    def _checking_machine(self) -> None:
        # Head office knows this board as the other machine type: choose that one.
        self.session.fixing = False
        self.show(MACHINE)

    def _checking_retry(self) -> None:
        self._enter_checking()

    def _checking_another(self) -> None:
        self.show(CONNECT)

    # ============================================================ fix settings

    def start_fixing(self) -> None:
        self.session.fixing = True
        self.session.fix_loaded = False
        self.session.mode = InstallMode.CHANGE_SETTINGS
        self.show(FIX_SETTINGS)

    def _enter_fix_settings(self) -> None:
        session, page = self.session, self.page(FIX_SETTINGS)
        if not session.fix_loaded:
            if session.record is None:
                # Never guess "unknown board": without head office's answer the app cannot
                # tell whether punches would be lost, or whether new settings would suit
                # this board (review E2, B1). Look it up again first.
                page.show_unavailable(wording.settings_change_blocked(None), retry=True)
                self._look_up_for_fix()
                return
            blocked = wording.settings_change_blocked(session.record)
            if blocked is not None:
                page.show_unavailable(blocked)
                return
            page.load(session.record, session.board, session.family, self.services.defaults)
            page.form.set_zones(self.zones)
            values = session.values
            if (
                session.flow is Flow.SETUP
                and values is not None
                and values.family is page.chosen_family()
            ):
                # The details typed for this board in this setup are what the person wants
                # (only for the machine type head office has: E4).
                page.form.fill(values)
            session.fix_loaded = True
        # No board software is loaded or needed: a settings change writes none (review F3).

    def _look_up_for_fix(self) -> None:
        session = self.session
        board, candidate = session.board, session.candidate
        mac = board.mac if board is not None else (candidate.mac if candidate else None)
        if not mac:
            return

        def found(record: object) -> None:
            if isinstance(record, api.DeviceRecord) and self.current == FIX_SETTINGS:
                self.session.record = record
                self._enter_fix_settings()

        def failed(error: BaseException) -> None:
            if self.current == FIX_SETTINGS and self.session.record is None:
                page = self.page(FIX_SETTINGS)
                page.show_unavailable(wording.settings_change_blocked(None), retry=True)

        self._run("lookup", lambda _op: self.services.server.device(mac), found, failed)

    def _fix_settings_reload(self) -> None:
        if not self.session.fix_loaded:
            self._enter_fix_settings()
            return
        self._load_zones()

    def _fix_settings_next(self) -> None:
        page = self.page(FIX_SETTINGS)
        if not page.check():
            return
        values = page.form.values()
        self.session.values = values
        self.session.family = values.family
        self.session.mode = InstallMode.CHANGE_SETTINGS
        self.show(CONFIRM)

    # ================================================================ confirm

    def summary_rows(self, finished: bool = False) -> List[Tuple[str, str]]:
        """The details as a list of (what, value); ``finished`` words them for the Done screen."""
        session = self.session
        values, mode = session.values, session.mode
        if values is None:
            return []
        others = [zone.zone_name or zone.zone_id for zone in self.zones]
        group = naming.group_of(values.zone_name)
        rows = [
            ("Machine type", wording.family_name(values.family)),
            ("ZONE ID", values.zone_id),
            ("ZONE NAME", values.zone_name),
            ("Status page name", f"{naming.public_name(values.zone_name, others)} ({group} group)"),
            ("Device number", values.device_id),
        ]
        if values.family is Family.HIKVISION:
            rows += [
                ("Machine address", values.hik_host),
                # Must match the machine letter for letter, and head office keeps the first
                # one it sees: the last chance to catch a typo (review E9).
                ("Machine serial number", values.hik_serial),
                ("Machine model", model_for(values.hik_model_key).label),
            ]
        if mode is InstallMode.CHANGE_SETTINGS:
            version = (
                session.record.firmware_version
                if session.record and session.record.firmware_version
                else ""
            )
            rows.append(
                ("Board software", f"Stays the same ({version})" if version else "Stays the same")
            )
        elif session.bundle is not None:
            label = session.bundle.label if finished else bundle_words(session.bundle)
            rows.append(("Board software", label))
        rows.append(("Wi-Fi name", values.wifi_ssid))
        if session.board is not None:
            rows.append(("Board ID", naming.board_id(session.board.mac)))
        return rows

    def _enter_confirm(self) -> None:
        session = self.session
        # A settings change writes no board software, so it names none (review F3).
        changing = session.mode is InstallMode.CHANGE_SETTINGS
        if (
            session.values is None
            or (session.bundle is None and not changing)
            or session.mode is None
            or session.board is None
        ):
            self.go_home()
            return
        epoch = (
            session.record.hik_source_epoch
            if session.record and session.record.hik_source_epoch
            else ""
        )
        bundle_id = "" if changing or session.bundle is None else session.bundle.bundle_id
        session.config = to_config(session.values, bundle_id, epoch)
        self.page(CONFIRM).show_summary(self.summary_rows(), session.mode)

    def _confirm_start(self) -> None:
        if self.session.config is None:
            return
        self.show(INSTALLING)
        self.start_install()

    # ============================================================= installing

    def start_install(self) -> None:
        session, page = self.session, self.page(INSTALLING)
        mode, board, config = session.mode, session.board, session.config
        if mode is None or board is None or config is None:
            self.go_home()
            return
        session.install_step = None
        session.user_cancelled = False
        session.installing = True
        session.summary = None
        page.start(mode)
        page.set_cancel_allowed(True)
        self._update_sign_out()

        def work(operation: Operation) -> object:
            on_step = self.runner.relay(self._on_install_step, operation)
            return self.services.installer.run(mode, board, config, on_step, operation.token)

        self._run("install", work, self._installed, self._install_failed)

    def _on_install_step(self, progress: api.StepProgress) -> None:
        session = self.session
        session.install_step = progress.step
        page = self.page(INSTALLING)
        page.update_progress(progress)
        allowed = (
            not session.user_cancelled
            and session.mode is not None
            and cancel_allowed(session.mode, progress.step)
        )
        page.set_cancel_allowed(allowed)

    def _installing_cancel(self) -> None:
        session = self.session
        operation = self._ops.get("install")
        if (
            operation is None
            or session.mode is None
            or not cancel_allowed(session.mode, session.install_step)
        ):
            return
        session.user_cancelled = True
        operation.token.cancel()  # keep listening: the engine reports how it stopped
        page = self.page(INSTALLING)
        page.set_cancel_allowed(False)
        page.banner.set_notice(
            "Stopping", "The app is stopping safely. Please wait.", Severity.INFO
        )

    def _installed(self, summary: object) -> None:
        session = self.session
        session.installing = False
        session.summary = summary if isinstance(summary, api.InstallSummary) else None
        self.page(INSTALLING).finish_all()
        self._update_sign_out()
        self.show(FIRST_START)

    def _install_failed(self, error: BaseException) -> None:
        session, page = self.session, self.page(INSTALLING)
        session.installing = False
        self._update_sign_out()
        advice = advice_of(error)
        step = getattr(error, "step", None) or session.install_step
        irreversible = bool(getattr(error, "irreversible_done", False))
        if not isinstance(error, api.InstallerError) and session.mode is InstallMode.NEW_BOARD:
            # Unknown failure: assume the worst once the key step was reached.
            irreversible = not cancel_allowed(session.mode, session.install_step)
        if session.user_cancelled and not irreversible:
            changing = session.mode is InstallMode.CHANGE_SETTINGS
            page.show_problem(
                wording.setup_stopped(session.mode),
                [
                    ("Try again", "retry", "primary")
                    if changing
                    else ("Start again", "restart", "primary"),
                    ("Go back to the start", "home", "secondary"),
                ],
                step,
            )
            return
        details_problem = (
            advice.code.startswith("INVALID_FIELD") or advice.code == "DEVICE_ID_IN_USE"
        )
        if irreversible and advice.can_retry:
            # KEY_STATE_UNCLEAR says "check the board again": that is what this button does.
            words = (
                "Check the board again"
                if advice.code == "KEY_STATE_UNCLEAR"
                else "Finish the setup"
            )
            actions = [(words, "finish", "primary")]
            if step is StepId.PREPARE_FILES:
                # Head office refused the details before anything was written (E11).
                actions.append(("Change the details", "details", "secondary"))
        elif irreversible:
            # The key is locked but the board is not safe to finish ("Do not use this board").
            actions = [("Go back to the start", "home", "secondary")]
        elif details_problem:
            # Only a details problem is fixed on the form; everything else is not (E6).
            actions = [("Change the details", "details", "primary")]
            if advice.can_retry:
                actions.append(("Try again", "retry", "secondary"))
        elif advice.code == "FAMILY_MISMATCH":
            actions = [
                ("Choose the machine type", "machine", "primary"),
                ("Go back to the start", "home", "secondary"),
            ]
        else:
            actions = [("Try again", "retry", "primary")] if advice.can_retry else []
            actions.append(("Go back to the start", "home", "secondary"))
        page.show_problem(advice, actions, step)

    def _installing_retry(self) -> None:
        self.start_install()

    def _installing_restart(self) -> None:
        self.show(CHECKING)

    def _installing_finish(self) -> None:
        # The board now has its key: checking it again offers "Finish the setup".
        self.show(CHECKING)

    def _installing_details(self) -> None:
        self.show(FIX_SETTINGS if self.session.fixing else DETAILS)

    def _installing_machine(self) -> None:
        if self.session.flow is Flow.CHECK:
            self.show(FIX_SETTINGS)  # the machine type is chosen on that page
            return
        self.session.fixing = False
        self.show(MACHINE)

    # ============================================================ first start
    #
    # Two watched starts: the first one, then, once it passed, the restart check (one more
    # deliberate replug). Only a board that names its settings again at the second start
    # gets Next and the Done screen: on 2 Nov 2026 two new boards started well once and
    # had lost their settings at every later start.

    def _enter_first_start(self) -> None:
        session, page = self.session, self.page(FIRST_START)
        page.start()
        board, config = session.board, session.config
        if board is None or config is None:
            self.go_home()
            return
        session.boot = session.restart = None
        mac, transcript = self._watched_board()

        def work(operation: Operation) -> object:
            on_phase = self.runner.relay(page.set_phase, operation)
            on_event = self.runner.relay(page.add_event, operation)
            return self.services.boot.wait_first_boot(
                mac,
                config,
                on_phase,
                on_event,
                operation.token,
                FIRST_BOOT_TIMEOUT_S,
                transcript=transcript,
            )

        self._run("boot", work, self._booted, self._boot_failed)
        self._update_sign_out()

    def _watched_board(self) -> Tuple[str, Optional[str]]:
        """The board ID to watch, and the install whose console log keeps what it says."""
        session = self.session
        summary, board = session.summary, session.board
        mac = summary.mac if summary else (board.mac if board else "")
        return mac, summary.request_id if summary else None

    def _booted(self, report: object) -> None:
        session, page = self.session, self.page(FIRST_START)
        self._update_sign_out()
        if not isinstance(report, api.BootReport):
            self._boot_failed(RuntimeError("no boot report"))
            return
        session.boot = report
        self._report_boot(report)
        if report.outcome is BootOutcome.OK:
            # Not the end: the board must also keep its settings when it starts again.
            self._start_restart_check()
            return
        advice = report.advice or wording.boot_fallback(report, session.config)
        page.show_result(advice, self._boot_actions(report.outcome, advice))

    def _start_restart_check(self) -> None:
        """One more replug, and the second start watched the same way (BootService)."""
        session, page = self.session, self.page(FIRST_START)
        config, first = session.config, session.boot
        if config is None or first is None or session.board is None:
            self.go_home()
            return
        session.restart = None
        mac, transcript = self._watched_board()
        page.start_again(wording.restart_check(first))

        def work(operation: Operation) -> object:
            on_phase = self.runner.relay(page.set_phase, operation)
            on_event = self.runner.relay(page.add_event, operation)
            # With the first start's report: other settings than it showed were not kept.
            return self.services.boot.wait_restart(
                mac,
                config,
                on_phase,
                on_event,
                operation.token,
                FIRST_BOOT_TIMEOUT_S,
                transcript=transcript,
                first=first,
            )

        self._run("boot", work, self._restarted, self._boot_failed)
        self._update_sign_out()

    def _restarted(self, report: object) -> None:
        session, page = self.session, self.page(FIRST_START)
        self._update_sign_out()
        if not isinstance(report, api.BootReport):
            self._boot_failed(RuntimeError("no restart report"))
            return
        session.restart = report
        self._report_boot(report, restart=True)
        if report.outcome is BootOutcome.OK and session.boot is not None:
            page.show_result(
                wording.start_checks_ok(session.boot, report), [("Next", "next", "primary")]
            )
            return
        mac, _transcript = self._watched_board()
        advice = report.advice or wording.boot_fallback(report, session.config, mac)
        if report.outcome is BootOutcome.SETTINGS_LOST:
            # The advice says to send a help file to head office: it carries this record.
            self._keep_record()
        page.show_result(advice, self._boot_actions(report.outcome, advice))

    def _report_boot(self, report: api.BootReport, restart: bool = False) -> None:
        """Tell head office how a watched start went; best effort, off the GUI thread."""
        summary = self.session.summary
        if summary is None or report.outcome is BootOutcome.CANCELLED:
            return
        server = self.services.server
        send = report_restart if restart else report_boot
        self.runner.start(
            lambda: send(server, summary, report),
            on_error=lambda error: log.info("start not reported: %s", error),
        )

    def _keep_record(self) -> None:
        """Save the setup record without being asked, for a board that must not be used."""
        session = self.session
        summary, boot, restart = session.summary, session.boot, session.restart
        if summary is None or boot is None:
            return

        def kept(path: object) -> None:
            session.record_path = str(path)
            log.info("setup record kept: %s", path)

        self._run(
            "record",
            lambda _op: self.services.records.save_install_record(summary, boot, restart),
            kept,
            lambda error: log.info("setup record not kept: %s", error),
        )

    @staticmethod
    def _boot_actions(outcome: Optional[BootOutcome], advice: Advice) -> List[Tuple[str, str, str]]:
        if outcome is BootOutcome.SETTINGS_LOST:
            # Writing the settings again would last only until the next restart, so the board
            # must not go to a site: nothing to try again or fix here (the advice says so).
            return [("Go back to the start", "home", "secondary")]
        settings_fix = outcome in (
            BootOutcome.WRONG_SETTINGS,
            BootOutcome.STORAGE_PROBLEM,
            BootOutcome.FAMILY_MISMATCH,
        ) or (outcome is BootOutcome.SECURITY_KEY_PROBLEM and advice.can_retry)
        if settings_fix:
            # Watching again would show the same thing; the settings must be written again.
            # The engine's advice for these says "Click Fix settings".
            actions = [("Fix settings", "fix_settings", "primary")]
        elif advice.can_retry:
            actions = [("Try again", "retry", "primary")]
        else:
            actions = []
        actions.append(("Check the board", "check", "secondary"))
        return actions

    def _boot_failed(self, error: BaseException) -> None:
        self._update_sign_out()
        advice = advice_of(error)
        self.page(FIRST_START).show_result(advice, self._boot_actions(None, advice))

    def _first_start_next(self) -> None:
        # Done says the board is ready: only once it kept its settings through a restart.
        restart = self.session.restart
        if restart is not None and restart.outcome is BootOutcome.OK:
            self.show(DONE)

    def _first_start_retry(self) -> None:
        boot = self.session.boot
        if boot is not None and boot.outcome is BootOutcome.OK:
            # The first start passed already: only the restart check is watched again.
            self._start_restart_check()
        else:
            self._enter_first_start()

    def _first_start_fix_settings(self) -> None:
        # Checking the board again leads to "Change its settings", with these details filled in.
        self.session.fixing = False
        self.show(CHECKING)

    def _first_start_check(self) -> None:
        self.session = Session(flow=Flow.CHECK)
        self.show(CONNECT)

    # =================================================================== done

    def _enter_done(self) -> None:
        session, page = self.session, self.page(DONE)
        values = session.values
        rows = self.summary_rows(finished=True)
        mac = (
            session.summary.mac if session.summary else (session.board.mac if session.board else "")
        )
        title = (
            "The new settings are on the board"
            if session.mode is InstallMode.CHANGE_SETTINGS
            else "The board is ready"
        )
        restart = session.restart
        page.show_done(
            rows,
            mac,
            self.services.defaults.setup_ap_password,
            values is not None and values.family is Family.HIKVISION,
            title,
            kept_settings=restart is not None and restart.outcome is BootOutcome.OK,
        )

    def _done_save_record(self) -> None:
        session = self.session
        if session.summary is None:
            return
        summary, boot = session.summary, session.boot or api.BootReport(BootOutcome.OK)
        restart = session.restart
        page = self.page(DONE)
        page.set_secondary("Saving", "save_record")
        page.secondary_button.setEnabled(False)

        def done(path: object) -> None:
            page.secondary_button.setEnabled(True)
            page.set_secondary("Save a record", "save_record")
            session.record_path = str(path)
            page.show_saved(str(path))

        def failed(error: BaseException) -> None:
            page.secondary_button.setEnabled(True)
            page.set_secondary("Save a record", "save_record")
            page.show_save_problem(advice_of(error))

        self._run(
            "record",
            lambda _op: self.services.records.save_install_record(summary, boot, restart),
            done,
            failed,
        )

    def _done_open_folder(self) -> None:
        if self.session.record_path:
            self._open_folder(self.session.record_path)

    def _done_another(self) -> None:
        family = self.session.family
        self.session = Session(flow=Flow.SETUP, family=family)
        self.page(MACHINE).set_selected(family)
        self.show(MACHINE)

    # ============================================================= check board

    def _enter_check_board(self) -> None:
        keep, self._keep_board_watch = self._keep_board_watch, False
        if keep and self._came_from == LIGHT_GUIDE and self.session.candidate is not None:
            # Back from the light guide: the board kept talking, so carry on watching it
            # instead of reading it again, which would reset it (review E8).
            if not self.running("console"):
                self._watch_console()
            self._offer_fix(False)
            self._diagnose_check_board()
            return
        self._start_check_board()

    def _start_check_board(self) -> None:
        session, page = self.session, self.page(CHECK_BOARD)
        page.start()
        session.events = []
        if session.candidate is None:
            self.show(CONNECT)
            return

        def work(operation: Operation) -> _CheckOutcome:
            return self._inspect_and_look_up(
                operation, self.runner.relay(page.show_board, operation)
            )

        self._run("check", work, self._board_checked, self._board_check_failed)

    def _board_checked(self, outcome: object) -> None:
        if not isinstance(outcome, _CheckOutcome):
            return
        session, page = self.session, self.page(CHECK_BOARD)
        session.board, session.record = outcome.board, outcome.record
        session.candidate = outcome.candidate or session.candidate
        page.show_board(outcome.board)
        if outcome.lookup_error is not None:
            page.show_record_problem(advice_of(outcome.lookup_error))
        elif outcome.record is not None:
            page.show_record(outcome.record)
        if session.record is not None and session.record.family is not None:
            session.family = session.record.family
        self._show_check_advice(outcome.advice)
        self._offer_fix(outcome.lookup_error is not None)
        self._watch_console()

    def _offer_fix(self, lookup_failed: bool) -> None:
        """Fix settings, only for a set-up board that head office knows and allows (E2, B1)."""
        session, page = self.session, self.page(CHECK_BOARD)
        board = session.board
        if board is None or board.security is not SecurityState.SECURED_STATE_LIFE:
            page.set_fix_available(False)
            return
        unreadable = wording.install_blocked(session.candidate)
        if unreadable is not None:
            # The page's own Check again reads the board again, after a socket change too.
            page.show_fix_blocked(unreadable)
            return
        blocked = wording.settings_change_blocked(session.record, lookup_failed)
        if blocked is None:
            page.set_fix_available(True)
        else:
            page.show_fix_blocked(blocked, retry=lookup_failed or session.record is None)

    def _check_board_lookup(self) -> None:
        """Try again: ask head office about this board, without reading the board again."""
        session = self.session
        board = session.board
        if board is None or self.running("lookup"):
            return
        page = self.page(CHECK_BOARD)
        page.fix_blocked.hide()
        page.show_record_lookup()

        def found(record: object) -> None:
            if not isinstance(record, api.DeviceRecord):
                return
            session.record = record
            page.show_record(record)
            if record.family is not None:
                session.family = record.family
            self._offer_fix(False)
            self._diagnose_check_board()

        def failed(error: BaseException) -> None:
            page.show_record_problem(advice_of(error))
            self._offer_fix(True)

        self._run("lookup", lambda _op: self.services.server.device(board.mac), found, failed)

    def _show_check_advice(self, advices: Sequence[Advice]) -> None:
        """The advice list, plus the board's own Wi-Fi when the board has one (E1)."""
        session, page = self.session, self.page(CHECK_BOARD)
        board = session.board
        listed = list(advices)
        refusal = wording.board_refusal(board) if board is not None else None
        if refusal is not None and not any(item.severity is Severity.ERROR for item in listed):
            listed.insert(0, refusal)
        page.show_advice_list(listed)
        about_wifi = any(item.code in WIFI_ADVICE for item in listed)
        mac = (
            board.mac
            if board is not None
            else (session.candidate.mac if session.candidate else None)
        )
        set_up = board is not None and board.security is SecurityState.SECURED_STATE_LIFE
        if mac and (set_up or about_wifi):
            page.show_hotspot(
                naming.hotspot_name(mac),
                self.services.defaults.setup_ap_password,
                open_steps=about_wifi,
            )
        else:
            page.hide_hotspot()

    def _board_check_failed(self, error: BaseException) -> None:
        advice = advice_of(error)
        self.page(CHECK_BOARD).show_problem(advice, retry=advice.can_retry)
        self._watch_console()

    def _watch_console(self) -> None:
        candidate = self.session.candidate
        if candidate is None:
            return

        def work(operation: Operation) -> None:
            on_event = self.runner.relay(self._on_check_event, operation)
            self.services.boot.watch_console(candidate, on_event, operation.token)

        self._run("console", work, None, lambda error: log.info("console watch ended: %s", error))

    def _on_check_event(self, event: api.ConsoleEvent) -> None:
        self.session.events.append(event)
        self.session.events = self.session.events[-200:]
        self.page(CHECK_BOARD).add_event(event)
        if not self._diagnose_timer.isActive():
            self._diagnose_timer.start()

    def _diagnose_check_board(self) -> None:
        session = self.session
        if self.current != CHECK_BOARD or session.presence is None:
            return
        board, record, presence = session.board, session.record, session.presence
        events = tuple(session.events)

        def shown(advices: object) -> None:
            if self.current == CHECK_BOARD:
                self._show_check_advice(tuple(advices or ()))

        self._run(
            "diagnose",
            lambda _op: tuple(self.services.diagnosis.diagnose(board, presence, record, events)),
            shown,
            lambda error: log.info("diagnosis failed: %s", error),
        )

    def _check_board_fix(self) -> None:
        session = self.session
        if (
            session.board is not None
            and session.board.security is SecurityState.SECURED_STATE_LIFE
            and not self.running("lookup")
            and wording.settings_change_blocked(session.record) is None
            and wording.install_blocked(session.candidate) is None
        ):
            self.start_fixing()

    def _check_board_lights(self) -> None:
        self.open_light_guide(CHECK_BOARD)

    def _check_board_recheck(self) -> None:
        self._stop("console", "diagnose", "lookup")
        self._start_check_board()

    # ============================================================ light guide

    def _light_guide_restart(self) -> None:
        page = self.page(LIGHT_GUIDE)
        page.reset()
        page.scroll_to_top()

    # ================================================================== files

    def _save_help_file(self) -> None:
        panel = self.window.help_panel
        panel.set_saving()
        self._run(
            "help_file",
            lambda _op: self.services.records.save_help_file(),
            lambda path: panel.show_saved(str(path)),
            lambda error: panel.show_save_problem(advice_of(error)),
        )

    def _open_folder(self, path: str) -> None:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
