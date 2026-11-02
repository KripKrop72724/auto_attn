"""Scripted Services for the screen tests, the screenshots and demos.

``make_fake_services(scenario)`` returns ``api.Services`` whose parts behave
like the real engine for one story: a new ZKTeco board, a faulty board, a
wrong password, and so on (see SCENARIOS). Everything is built from api
types only, waits are short and cancellable, and nothing touches USB, files
outside a temp folder, or the network, so a run gives the same result every
time.

A test can hold the story at a named moment and let it go later::

    services = make_fake_services("no_board_then_found")
    world = fake_world(services)
    world.hold("board_found")      # the board stays missing ...
    ...
    world.release("board_found")   # ... until now

Every engine call is written to ``world.calls`` with the thread it ran on,
so tests can prove the window never calls the engine from the GUI thread.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import zipfile
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Tuple

from . import api
from .api import (
    Advice,
    BootOutcome,
    BootReport,
    BundleInfo,
    Capabilities,
    ConnectivityStatus,
    ConsoleEvent,
    ConsoleKind,
    DeviceRecord,
    Family,
    FieldDefaults,
    HardwareVerdict,
    InstallerError,
    InstallMode,
    PresenceReport,
    PresenceState,
    ReplugPhase,
    SecurityState,
    Severity,
    StepId,
    StepProgress,
    UsbCandidate,
    UsbKind,
    ZoneSummary,
)

SCENARIOS: Tuple[str, ...] = (
    "happy_zkt",
    "happy_hik",
    "no_board_then_found",
    "wrong_socket",
    "other_usb_mode",
    "several_boards",
    "faulty_restart_loop",
    "secured_board_fix_settings",
    "resume_after_key",
    "server_down",
    "no_internet",
    "login_wrong_password",
    "login_paused",
    "install_error_before_key",
    "install_error_after_key",
    "first_boot_wrong_zone",
    # extra stories for screenshots and edge cases
    "manual_boot_mode",
    "secured_other",
    "family_mismatch",
    "fast_restart_loop",
    "settings_change_not_allowed",
    "lookup_fails",
    "zone_not_writable",
    "secured_hik_fix_settings",
    "installer_off",
    # Every story's board keeps its settings at the restart check; this one does not.
    "settings_lost_on_restart",
)

# Boards that are already set up and locked for State Life in these stories.
_SECURED_STORIES = (
    "secured_board_fix_settings",
    "settings_change_not_allowed",
    "lookup_fails",
    "zone_not_writable",
    "secured_hik_fix_settings",
)

BOARD_MAC = "ac:27:6e:a4:3d:30"
OTHER_MAC = "ac:27:6e:a4:51:9c"
STARTED_AT = "2026-11-01T10:00:00Z"
FINISHED_AT = "2026-11-01T10:03:12Z"

# Placeholders only: the real field defaults are injected at release-build time.
TEST_DEFAULTS = FieldDefaults(
    wifi_ssid="RE7",
    wifi_password="test-wifi-pass",
    zkt_port=4370,
    zkt_comm_key=0,
    zkt_preferred_ip="0.0.0.0",
    device_id="1",
    hik_port=80,
    hik_transport="http_digest",
    hik_username="admin",
    setup_ap_password="test-hotspot-pass",
    is_test_build=True,
)

BUNDLES: Tuple[BundleInfo, ...] = (
    BundleInfo(
        "zkt-2.7.8-b8d0ca7e",
        Family.ZKT,
        "ZKT 2.7.8",
        "2.7.8",
        True,
        "AVAILABLE",
        "Production release for ZKTeco machines.",
        6_291_456,
    ),
    BundleInfo(
        "zkt-2.7.7-4c1e9a20",
        Family.ZKT,
        "ZKT 2.7.7",
        "2.7.7",
        False,
        "AVAILABLE",
        "The release before 2.7.8.",
        6_225_920,
    ),
    BundleInfo(
        "hikvision-3.1.1-dc7d9c3-21ac81b2",
        Family.HIKVISION,
        "Hikvision 3.1.1",
        "3.1.1",
        True,
        "FIELD_BASELINE",
        "Field build for Hikvision machines.",
        5_767_168,
    ),
)

ZONES: Tuple[ZoneSummary, ...] = (
    ZoneSummary("LF-ZONE-BLD9-01", "LF-ZONE-BLD9-01", ("1",), (Family.ZKT,)),
    ZoneSummary("LF-ZONE-BLD9-02", "LF-ZONE-BLD9-02", ("1",), (Family.ZKT,)),
    ZoneSummary("LF-ZONE-BLD9-03", "LF-ZONE-BLD9-03", ("1",), (Family.HIKVISION,)),
    ZoneSummary("ZONE-LAHORE-01", "ZONE-LAHORE-01", ("1", "2"), (Family.ZKT,)),
    ZoneSummary("ZONE-MULTAN-01", "ZONE-MULTAN-01", ("1",), (Family.ZKT,)),
    ZoneSummary("ZONE-SLICTOWER-13FL", "ZONE-SLICTOWER-13FL", ("1",), (Family.ZKT,)),
)

NATIVE = UsbCandidate("COM5", UsbKind.NATIVE, 0x303A, 0x1001, BOARD_MAC, "USB Serial Device (COM5)")
NATIVE_OTHER = UsbCandidate(
    "COM6", UsbKind.NATIVE, 0x303A, 0x1001, OTHER_MAC, "USB Serial Device (COM6)"
)
BRIDGE = UsbCandidate(
    "COM7", UsbKind.BRIDGE, 0x1A86, 0x55D3, None, "USB-Enhanced-SERIAL CH343 (COM7)"
)
# A new board running its maker's demo: Espressif's USB defaults, no board ID.
OTHER_USB = UsbCandidate("COM8", UsbKind.OTHER, 0x303A, 0x4001, None, "USB Serial Device (COM8)")


@dataclass(frozen=True)
class Call:
    name: str
    thread: str
    on_main_thread: bool


def _advice(
    code: str,
    title: str,
    what: str,
    todo: Sequence[str] = (),
    severity: Severity = Severity.ERROR,
    picture: Optional[str] = None,
    can_retry: bool = True,
) -> Advice:
    return Advice(code, title, what, tuple(todo), severity, picture, can_retry)


CANCELLED = _advice(
    "CANCELLED",
    "Stopped",
    "The work was stopped before it finished.",
    ("You can start again.",),
    Severity.INFO,
)


class FakeWorld:
    """The shared state of one scripted story."""

    def __init__(self, scenario: str, pace: float) -> None:
        self.scenario = scenario
        self.pace = pace
        self.calls: List[Call] = []
        self.reports: List[Tuple[str, str, str, Dict[str, object]]] = []
        # The install each watched start was asked to keep the console lines of.
        self.transcripts: List[Optional[str]] = []
        # The first start's report each restart check was given.
        self.first_starts: List[Optional[BootReport]] = []
        self.login_attempts = 0
        self.inspections = 0
        self.install_runs = 0
        self.lookups = 0
        self.signed_in: Optional[str] = None
        self.board_state = {
            "resume_after_key": SecurityState.KEY_BURNED_NOT_STARTED,
            "secured_other": SecurityState.SECURED_OTHER,
            **{story: SecurityState.SECURED_STATE_LIFE for story in _SECURED_STORIES},
        }.get(scenario, SecurityState.BLANK)
        # What head office says about setting up boards; a test may change it.
        self.capabilities = Capabilities(
            enabled=scenario != "installer_off",
            provisioner_ready=True,
            min_app_version="1.0.0",
            server_time=STARTED_AT,
        )
        self._lock = threading.Lock()
        self._held: Dict[str, threading.Event] = {}
        self._folder: Optional[str] = None

    # ------------------------------------------------------------- record

    def note(self, name: str) -> None:
        thread = threading.current_thread()
        with self._lock:
            self.calls.append(Call(name, thread.name, thread is threading.main_thread()))

    def called(self, name: str) -> int:
        with self._lock:
            return sum(1 for call in self.calls if call.name == name)

    def calls_on_main_thread(self) -> List[str]:
        with self._lock:
            return [call.name for call in self.calls if call.on_main_thread]

    # ------------------------------------------------------------- timing

    def hold(self, name: str) -> None:
        """Make the story wait at ``name`` until ``release(name)``."""
        with self._lock:
            self._held[name] = threading.Event()

    def release(self, name: str) -> None:
        with self._lock:
            event = self._held.get(name)
        if event is not None:
            event.set()

    def pause(self, name: str, cancel: api.CancelToken, steps: float = 1.0) -> bool:
        """Wait ``steps`` paces, or while ``name`` is held. True if cancelled meanwhile."""
        with self._lock:
            event = self._held.get(name)
        if event is not None:
            while not event.is_set():
                if cancel.wait(0.005):
                    return True
            return cancel.cancelled
        return cancel.wait(self.pace * steps)

    def folder(self) -> str:
        with self._lock:
            if self._folder is None:
                self._folder = tempfile.mkdtemp(prefix="sl-installer-fake-")
            return self._folder

    def cleanup(self) -> None:
        """Remove the temp folder the fake records were written to."""
        with self._lock:
            folder, self._folder = self._folder, None
        if folder:
            shutil.rmtree(folder, ignore_errors=True)

    # ----------------------------------------------------------- scenario

    @property
    def offline(self) -> bool:
        return self.scenario in ("server_down", "no_internet")

    def server_error(self) -> InstallerError:
        if self.scenario == "no_internet":
            return InstallerError(
                _advice(
                    "NO_INTERNET",
                    "This computer has no internet",
                    "The app needs the internet to reach head office.",
                    ("Connect this computer to Wi-Fi or to a network cable.", "Then try again."),
                    Severity.ERROR,
                    "no_internet",
                )
            )
        return InstallerError(
            _advice(
                "SERVER_UNREACHABLE",
                "Head office is not answering",
                "The internet works, but the head office server did not answer.",
                ("Wait a few minutes and try again.", "If it keeps happening, call head office."),
                Severity.ERROR,
                "head_office",
            )
        )


# ====================================================================== server


class FakeServer:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    @property
    def signed_in_user(self) -> Optional[str]:
        return self.world.signed_in

    def login(self, username: str, password: str) -> str:
        world = self.world
        world.note("server.login")
        world.login_attempts += 1
        world.pause("login", api.CancelToken(), 4)
        if world.offline:
            raise world.server_error()
        if world.scenario == "login_paused" or (
            world.scenario == "login_wrong_password" and world.login_attempts >= 3
        ):
            raise InstallerError(
                _advice(
                    "LOGIN_PAUSED",
                    "Sign-in is paused for 10 minutes",
                    "The password was wrong 3 times. The app waits, so the dashboard account is "
                    "not locked.",
                    (
                        "Wait 10 minutes.",
                        "Check the name and password with head office.",
                        "Then try again.",
                    ),
                    Severity.WARNING,
                    None,
                    can_retry=False,
                )
            )
        if world.scenario == "login_wrong_password":
            left = 3 - world.login_attempts
            raise InstallerError(
                _advice(
                    "WRONG_PASSWORD",
                    "The name or password is not right",
                    "Head office did not accept this name and password.",
                    (
                        "Check the name and the password, then try again.",
                        "Capital letters must match.",
                        f"You have {left} more {'try' if left == 1 else 'tries'} before sign-in "
                        "pauses.",
                    ),
                    Severity.ERROR,
                )
            )
        world.signed_in = username
        return username

    def logout(self) -> None:
        self.world.note("server.logout")
        self.world.signed_in = None

    def capabilities(self) -> Capabilities:
        self.world.note("server.capabilities")
        if self.world.offline:
            raise self.world.server_error()
        return self.world.capabilities

    def bundles(self, family: Optional[Family] = None) -> Sequence[BundleInfo]:
        self.world.note("server.bundles")
        self.world.pause("bundles", api.CancelToken(), 2)
        if self.world.offline:
            raise self.world.server_error()
        return tuple(item for item in BUNDLES if family is None or item.family is family)

    def fetch_bundle(
        self, bundle_id: str, on_bytes: Optional[api.BytesProgressFn], cancel: api.CancelToken
    ) -> api.LocalBundle:
        self.world.note("server.fetch_bundle")
        info = bundle_by_id(bundle_id)
        if on_bytes is not None:
            on_bytes(info.download_bytes, info.download_bytes)
        return api.LocalBundle(info, self.world.folder(), {"bundle_id": bundle_id})

    def device(self, mac: str) -> DeviceRecord:
        world = self.world
        world.note("server.device")
        world.lookups += 1
        world.pause("lookup", api.CancelToken(), 2)
        if world.offline or (world.scenario == "lookup_fails" and world.lookups == 1):
            # lookup_fails: head office does not answer the first look-up, then does.
            raise world.server_error()
        if world.scenario in ("secured_board_fix_settings", "lookup_fails"):
            return DeviceRecord(
                known=True,
                mac=mac,
                zone_id="LF-ZONE-BLD9-02",
                zone_name="LF-ZONE-BLD9-02",
                device_id="1",
                family=Family.ZKT,
                firmware_version="2.7.8",
                online=False,
                last_seen_at="2026-11-01T05:42:00Z",
                ever_online=True,
                public_name="Building 9 · Device 2",
                group="Life",
                status_text="Offline since 10:42 am",
            )
        if world.scenario == "settings_change_not_allowed":
            # Locked before this app, possibly with another security root (review B1).
            return DeviceRecord(
                known=True,
                mac=mac,
                zone_id="ZONE-SLICTOWER-13FL",
                zone_name="ZONE-SLICTOWER-13FL",
                device_id="1",
                family=Family.ZKT,
                firmware_version="2.4.12",
                online=False,
                ever_online=True,
                public_name="SLIC Tower · 13th Floor",
                group="Health",
                status_text="Offline since 26 Oct",
                settings_change_allowed=False,
                # ADD's own sentence (installer_api SETTINGS_CHANGE_REFUSED).
                settings_change_reason=(
                    "This board was set up before this app. Only head office can change its "
                    "settings."
                ),
            )
        if world.scenario == "zone_not_writable":
            return DeviceRecord(
                known=True,
                mac=mac,
                zone_id="LF-ZONE-G&PBLD8-01",
                zone_name="LF-ZONE-G&PBLD8-01",
                device_id="1",
                family=Family.ZKT,
                firmware_version="2.7.8",
                online=True,
                ever_online=True,
                public_name="Group & Pension Building 8",
                group="Life",
                status_text="Online",
            )
        if world.scenario == "secured_hik_fix_settings":
            return DeviceRecord(
                known=True,
                mac=mac,
                zone_id="LF-ZONE-JHELUM-01",
                zone_name="LF-ZONE-JHELUM-01",
                device_id="1",
                family=Family.HIKVISION,
                firmware_version="3.1.1",
                online=True,
                ever_online=True,
                public_name="Jhelum",
                group="Life",
                status_text="Online",
                hik_source_epoch="lf-zone-jhelum-01-20261030",
            )
        if world.scenario == "family_mismatch":
            return DeviceRecord(
                known=True,
                mac=mac,
                zone_id="LF-ZONE-BLD9-03",
                zone_name="LF-ZONE-BLD9-03",
                device_id="1",
                family=Family.HIKVISION,
                firmware_version="3.1.1",
                online=False,
                ever_online=True,
                public_name="Building 9 · Device 3",
                group="Life",
                status_text="Offline since yesterday",
                hik_source_epoch="lf-zone-bld9-03-20261030",
            )
        return DeviceRecord(known=False, mac=mac)

    def zones(self) -> Sequence[ZoneSummary]:
        self.world.note("server.zones")
        if self.world.offline:
            raise self.world.server_error()
        return ZONES

    def create_package(self, request: api.PackageRequest) -> api.EncryptedPackage:
        self.world.note("server.create_package")
        return api.EncryptedPackage(request.request_id, {"schema": "fake"}, b"", b"")

    def report(self, request_id: str, mac: str, kind: str, detail: Dict[str, object]) -> None:
        self.world.note("server.report")
        self.world.reports.append((request_id, mac, kind, dict(detail)))


def bundle_by_id(bundle_id: str) -> BundleInfo:
    for bundle in BUNDLES:
        if bundle.bundle_id == bundle_id:
            return bundle
    return BUNDLES[0]


class FakeConnectivity:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def check(self) -> ConnectivityStatus:
        self.world.note("connectivity.check")
        # The same words as core/connectivity.py.
        if self.world.scenario == "no_internet":
            return ConnectivityStatus(
                False, False, "This computer is not connected to the internet."
            )
        if self.world.scenario == "server_down":
            return ConnectivityStatus(
                True, False, "The internet works, but head office is not answering."
            )
        return ConnectivityStatus(True, True, "Connected to head office.")


# ========================================================================= usb


def _report(
    state: PresenceState, seconds: float = 0.0, loop: Optional[float] = None
) -> PresenceReport:
    if state is PresenceState.ONE_BOARD:
        return PresenceReport(state, (NATIVE,), NATIVE, None, seconds)
    if state is PresenceState.WRONG_SOCKET:
        return PresenceReport(state, (BRIDGE,), None, None, seconds)
    if state is PresenceState.OTHER_USB_MODE:
        return PresenceReport(state, (OTHER_USB,), None, None, seconds)
    if state is PresenceState.SEVERAL_BOARDS:
        return PresenceReport(state, (NATIVE, NATIVE_OTHER), None, None, seconds)
    if state is PresenceState.RESTART_LOOP:
        return PresenceReport(state, (NATIVE,), NATIVE, loop, seconds)
    return PresenceReport(PresenceState.NO_BOARD, (), None, None, seconds)


class FakeUsb:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def _script(self) -> List[Tuple[PresenceReport, str, float]]:
        """(report, moment name, paces to wait before it)."""
        scenario = self.world.scenario
        if scenario == "no_board_then_found":
            return [
                (_report(PresenceState.NO_BOARD, 0.0), "", 0),
                (_report(PresenceState.NO_BOARD, 16.0), "no_board_tips", 4),
                (_report(PresenceState.ONE_BOARD, 0.5), "board_found", 30),
            ]
        if scenario == "wrong_socket":
            return [(_report(PresenceState.WRONG_SOCKET, 1.0), "board_found", 3)]
        if scenario == "other_usb_mode":
            # BOOT held through RST: the board restarts and comes back in setup mode.
            return [
                (_report(PresenceState.OTHER_USB_MODE, 1.0), "", 0),
                (_report(PresenceState.NO_BOARD, 0.0), "setup_mode", 30),
                (_report(PresenceState.ONE_BOARD, 0.5), "board_found", 3),
            ]
        if scenario == "several_boards":
            return [(_report(PresenceState.SEVERAL_BOARDS, 1.0), "board_found", 3)]
        if scenario == "faulty_restart_loop":
            return [
                (_report(PresenceState.ONE_BOARD, 0.5), "board_found", 3),
                (_report(PresenceState.RESTART_LOOP, 31.0, 10.3), "restart_loop", 6),
            ]
        if scenario == "fast_restart_loop":
            # Every 3.4 s: a weak cable or socket, or a board that stops at its start.
            return [
                (_report(PresenceState.ONE_BOARD, 0.5), "board_found", 3),
                (_report(PresenceState.RESTART_LOOP, 12.0, 3.4), "restart_loop", 6),
            ]
        return [
            (_report(PresenceState.NO_BOARD, 0.0), "", 0),
            (_report(PresenceState.ONE_BOARD, 0.5), "board_found", 3),
        ]

    def snapshot(self) -> PresenceReport:
        self.world.note("usb.snapshot")
        return self._script()[-1][0]

    def watch(
        self, on_report: api.PresenceFn, cancel: api.CancelToken, interval_s: float = 0.3
    ) -> None:
        self.world.note("usb.watch")
        script = self._script()
        for report, moment, paces in script:
            if paces and self.world.pause(moment, cancel, paces):
                return
            if cancel.cancelled:
                return
            on_report(report)
        last = script[-1][0]
        # Like the real watcher: keep reporting the state until cancelled.
        while not cancel.wait(max(self.world.pace * 5, 0.01)):
            last = replace(last, seconds_in_state=last.seconds_in_state + self.world.pace * 5)
            on_report(last)


# ====================================================================== boards


class FakeBoards:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def inspect(self, candidate: UsbCandidate, cancel: api.CancelToken) -> api.BoardInfo:
        world = self.world
        world.note("boards.inspect")
        world.inspections += 1
        if world.pause("inspect", cancel, 4):
            raise InstallerError(CANCELLED)
        if world.scenario == "manual_boot_mode" and world.inspections == 1:
            raise InstallerError(
                _advice(
                    "SETUP_MODE_FAILED",
                    "The board did not answer",
                    "The app could not switch the board into setup mode by itself.",
                    (
                        "Hold down the BOOT button on the board.",
                        "While you hold it, press and let go of the RST button.",
                        "Let go of the BOOT button.",
                        "Click Try again.",
                    ),
                    Severity.WARNING,
                    "boot_button",
                ),
                cause="fake: no sync reply",
            )
        state = world.board_state
        mac = candidate.mac or BOARD_MAC
        if state in (SecurityState.SECURED_STATE_LIFE, SecurityState.SECURED_OTHER):
            return api.BoardInfo(
                candidate.port,
                mac,
                "ESP32-S3",
                None,
                None,
                HardwareVerdict.NOT_CHECKED,
                state,
                True,
                {"flags": "0x000000c5"},
            )
        return api.BoardInfo(
            candidate.port,
            mac,
            "ESP32-S3",
            16,
            8,
            HardwareVerdict.OK,
            state,
            False,
            {
                "key_purpose_0": "HMAC_UP"
                if state is SecurityState.KEY_BURNED_NOT_STARTED
                else "USER"
            },
        )


# =================================================================== installer


class FakeInstaller:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def run(
        self,
        mode: InstallMode,
        board: api.BoardInfo,
        config: api.InstallConfig,
        on_step: api.ProgressFn,
        cancel: api.CancelToken,
    ) -> api.InstallSummary:
        world = self.world
        world.note("installer.run")
        world.install_runs += 1
        first_run = world.install_runs == 1
        steps = api.steps_for(mode)
        key_steps = (StepId.LOCK_KEY, StepId.COPY_STARTUP, StepId.WRITE_SETTINGS)
        for step in steps:
            # Like the real engine: a cancel is honoured only before the key step.
            token = cancel if step not in key_steps else api.CancelToken()
            for fraction in (0.0, 0.35, 0.7, 1.0):
                if token.cancelled:
                    raise InstallerError(CANCELLED, step=step)
                if (
                    world.scenario == "install_error_before_key"
                    and first_run
                    and step is StepId.COPY_FIRMWARE
                    and fraction >= 0.7
                ):
                    raise InstallerError(
                        _advice(
                            "COPY_FAILED",
                            "The copy stopped",
                            "The connection to the board broke while copying. Nothing was locked, "
                            "so the "
                            "board is safe.",
                            (
                                "Check that the cable is pushed in at both ends.",
                                "Use a socket on the computer itself, not a hub.",
                                "Click Try again.",
                            ),
                            Severity.ERROR,
                            "cable",
                        ),
                        step=step,
                        cause="fake: serial stream stopped",
                    )
                if (
                    world.scenario == "install_error_after_key"
                    and first_run
                    and step is StepId.COPY_STARTUP
                    and fraction >= 0.35
                ):
                    world.board_state = SecurityState.KEY_BURNED_NOT_STARTED
                    raise InstallerError(
                        _advice(
                            "STARTUP_COPY_FAILED",
                            "The setup stopped before the end",
                            "The board is already locked for State Life, but its start-up program "
                            "was not "
                            "copied. This board can still be finished.",
                            (
                                "Check that the cable is pushed in at both ends.",
                                "Click Finish the setup.",
                            ),
                            Severity.ERROR,
                            "cable",
                            # Like the engine's resume advice: finishing is still possible.
                            can_retry=True,
                        ),
                        step=step,
                        irreversible_done=True,
                        cause="fake: write failed",
                    )
                on_step(StepProgress(step, fraction, _detail(step, fraction)))
                if world.pause(f"step_{step.value}", token, 1):
                    raise InstallerError(CANCELLED, step=step)
        if mode is not InstallMode.CHANGE_SETTINGS:
            world.board_state = SecurityState.SECURED_STATE_LIFE
        return api.InstallSummary(
            f"fake-request-{world.install_runs:04d}",
            mode,
            board.mac,
            config,
            bundle_by_id(config.bundle_id),
            STARTED_AT,
            FINISHED_AT,
        )


def _detail(step: StepId, fraction: float) -> str:
    if step is StepId.COPY_FIRMWARE:
        return f"{6.2 * fraction:.1f} of 6.2 MB"
    if step is StepId.PREPARE_FILES:
        return "Checking the files" if fraction < 1 else ""
    return ""


# ======================================================================== boot


def _line(kind: ConsoleKind, raw: str, at: float, **data: str) -> ConsoleEvent:
    return ConsoleEvent(kind, raw, dict(data), at)


def first_boot_lines(
    config: api.InstallConfig, zone_id: str, new_board: bool
) -> List[ConsoleEvent]:
    hik = config.family is Family.HIKVISION
    version = bundle_by_id(config.bundle_id).version
    project = "zone_lite_hikvision" if hik else "zone_lite"
    lines = [
        _line(ConsoleKind.BOOT_BANNER, "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)", 0.1)
    ]
    if new_board:
        lines.append(
            _line(
                ConsoleKind.SECURE_BOOT_FIRST_BOOT,
                "I (312) secure_boot_v2: enabling secure boot v2...",
                0.3,
            )
        )
    lines += [
        _line(ConsoleKind.PSRAM_OK, "I (402) esp_psram: Found 8MB PSRAM device", 0.4),
        _line(
            ConsoleKind.APP_VERSION,
            f"I (520) app_init: App version:      {version}",
            0.5,
            version=version,
            project=project,
        ),
        _line(
            ConsoleKind.PROVISIONING_LOADED,
            f"I (902) zone_config: Loaded per-device provisioning for zone={zone_id}",
            0.9,
            zone_id=zone_id,
        ),
        _line(
            ConsoleKind.APP_STARTED,
            f"I (910) zone_lite: Zone Lite starting zone={zone_id} device_id={config.device_id}",
            0.9,
            zone_id=zone_id,
            device_id=config.device_id,
        ),
        _line(
            ConsoleKind.STORAGE_READY,
            "I (1220) zone_lite: Storage ready; loaded 0 known event UIDs",
            1.2,
        ),
        _line(
            ConsoleKind.WIFI_CONNECTING,
            f"I (1400) zone_lite: Connecting to Wi-Fi SSID {config.wifi_ssid}",
            1.4,
            ssid=config.wifi_ssid,
        ),
    ]
    return lines


def settings_lost_lines(config: api.InstallConfig) -> List[ConsoleEvent]:
    """The next start of the 2 Nov 2026 boards: no settings, so the board stays still."""
    lines = first_boot_lines(config, config.zone_id, new_board=False)[:3]
    lines += [
        _line(
            ConsoleKind.SETTINGS_MISSING,
            "E (902) zone_config: No encrypted per-device provisioning namespace; firmware "
            "remains inert",
            0.9,
        ),
        _line(
            ConsoleKind.SETTINGS_MISSING,
            "E (905) zone_lite: Encrypted per-device provisioning is required; network startup "
            "is blocked",
            0.9,
        ),
    ]
    return lines


class FakeBoot:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def wait_restart(
        self,
        mac: str,
        expect: api.InstallConfig,
        on_phase: api.PhaseFn,
        on_event: api.EventFn,
        cancel: api.CancelToken,
        timeout_s: float = 120.0,
        transcript: Optional[str] = None,
        first: Optional[BootReport] = None,
    ) -> BootReport:
        """The restart check: one more replug; the board names its settings again."""
        world = self.world
        world.note("boot.wait_restart")
        world.transcripts.append(transcript)
        world.first_starts.append(first)
        on_phase(ReplugPhase.WAITING_UNPLUG)
        if world.pause("restart_unplug", cancel, 6):
            return BootReport(BootOutcome.CANCELLED)
        on_phase(ReplugPhase.WAITING_REPLUG)
        if world.pause("restart_replug", cancel, 6):
            return BootReport(BootOutcome.CANCELLED)
        on_phase(ReplugPhase.WATCHING)
        lost = world.scenario == "settings_lost_on_restart"
        if lost:
            lines = settings_lost_lines(expect)
        else:
            lines = first_boot_lines(expect, expect.zone_id, new_board=False)
        for event in lines:
            if world.pause("restart_line", cancel, 1):
                return BootReport(BootOutcome.CANCELLED, events=tuple(lines))
            on_event(event)
        version = bundle_by_id(expect.bundle_id).version
        if lost:
            # No advice: the screens then use the engine's own words for this outcome.
            return BootReport(BootOutcome.SETTINGS_LOST, None, None, version, tuple(lines))
        return BootReport(
            BootOutcome.OK, expect.zone_id, expect.device_id, version, tuple(lines)
        )

    def wait_first_boot(
        self,
        mac: str,
        expect: api.InstallConfig,
        on_phase: api.PhaseFn,
        on_event: api.EventFn,
        cancel: api.CancelToken,
        timeout_s: float = 120.0,
        transcript: Optional[str] = None,
    ) -> BootReport:
        world = self.world
        world.note("boot.wait_first_boot")
        world.transcripts.append(transcript)
        on_phase(ReplugPhase.WAITING_UNPLUG)
        if world.pause("unplug", cancel, 6):
            return BootReport(BootOutcome.CANCELLED)
        on_phase(ReplugPhase.WAITING_REPLUG)
        if world.pause("replug", cancel, 6):
            return BootReport(BootOutcome.CANCELLED)
        on_phase(ReplugPhase.WATCHING)
        wrong = world.scenario == "first_boot_wrong_zone"
        zone_seen = "ZONE-MULTAN-01" if wrong else expect.zone_id
        lines = first_boot_lines(
            expect, zone_seen, new_board=world.scenario not in _SECURED_STORIES
        )
        for event in lines:
            if world.pause("boot_line", cancel, 1):
                return BootReport(BootOutcome.CANCELLED, events=tuple(lines))
            on_event(event)
        version = bundle_by_id(expect.bundle_id).version
        if wrong:
            return BootReport(
                BootOutcome.WRONG_SETTINGS,
                zone_seen,
                expect.device_id,
                version,
                tuple(lines),
                _advice(
                    "BOOT_WRONG_ZONE",
                    "The board started with other settings",
                    f"It started for {zone_seen}, not {expect.zone_id}.",
                    (
                        "Click Fix settings to write the right settings.",
                        "If it happens again, call head office.",
                    ),
                    Severity.ERROR,
                    "replug",
                ),
            )
        return BootReport(BootOutcome.OK, zone_seen, expect.device_id, version, tuple(lines))

    def watch_console(
        self, candidate: UsbCandidate, on_event: api.EventFn, cancel: api.CancelToken
    ) -> None:
        world = self.world
        world.note("boot.watch_console")
        if world.scenario == "secured_board_fix_settings":
            hotspot = "SLIC ATTENDANCE-3D31"
            script = [
                _line(
                    ConsoleKind.BOOT_BANNER, "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)", 0.1
                ),
                _line(
                    ConsoleKind.APP_VERSION,
                    "I (520) app_init: App version:      2.7.8",
                    0.5,
                    version="2.7.8",
                ),
                _line(
                    ConsoleKind.PROVISIONING_LOADED,
                    "I (902) zone_config: Loaded per-device provisioning for zone=LF-ZONE-BLD9-02",
                    0.9,
                    zone_id="LF-ZONE-BLD9-02",
                ),
                _line(
                    ConsoleKind.STORAGE_READY,
                    "I (1220) zone_lite: Storage ready; loaded 214 known event UIDs",
                    1.2,
                ),
                _line(
                    ConsoleKind.WIFI_CONNECTING,
                    "I (1400) zone_lite: Connecting to Wi-Fi SSID RE7",
                    1.4,
                    ssid="RE7",
                ),
                _line(
                    ConsoleKind.WIFI_RETRY, "I (4402) zone_lite: Retrying Wi-Fi connection (1)", 4.4
                ),
                _line(
                    ConsoleKind.WIFI_RETRY, "I (7405) zone_lite: Retrying Wi-Fi connection (2)", 7.4
                ),
                _line(
                    ConsoleKind.SETUP_AP_OPEN,
                    "W (121400) wifi_setup: Wi-Fi setup access point enabled (recovery mode)",
                    121.4,
                    ssid=hotspot,
                ),
            ]
            for event in script:
                if world.pause("console_line", cancel, 1):
                    return
                on_event(event)
        cancel.wait(3600 * 24)


# =================================================================== diagnosis

_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2, Severity.SUCCESS: 3}
_WIFI_KINDS = (
    ConsoleKind.WIFI_CONNECTING,
    ConsoleKind.WIFI_RETRY,
    ConsoleKind.WIFI_CONNECTED,
    ConsoleKind.SETUP_AP_OPEN,
)


class FakeDiagnosis:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def diagnose(
        self,
        board: Optional[api.BoardInfo],
        presence: PresenceReport,
        device: Optional[DeviceRecord],
        events: Sequence[ConsoleEvent],
    ) -> Sequence[Advice]:
        self.world.note("diagnosis.diagnose")
        found: List[Advice] = []
        state = presence.state
        if state is PresenceState.NO_BOARD and presence.seconds_in_state >= 15:
            found.append(
                _advice(
                    "USB_NO_BOARD",
                    "The board is not showing up",
                    "This computer cannot see a board yet.",
                    (
                        "Use the cable that came with the board. Some phone cables only charge.",
                        "Plug it into the socket marked USB on the board.",
                        "Plug the other end straight into this computer, not into a hub.",
                        "Try another socket on the computer.",
                        "Some new boards only show up in setup mode. Hold down the BOOT button. "
                        "While you hold it, press and release the RST button. Then let go of "
                        "BOOT.",
                        "If nothing changes after 3 tries, put the board aside and use another.",
                    ),
                    Severity.WARNING,
                    "cable",
                )
            )
        elif state is PresenceState.WRONG_SOCKET:
            found.append(
                _advice(
                    "USB_WRONG_SOCKET",
                    "The cable is in the wrong socket",
                    "The board has two sockets. The cable is in the one marked COM.",
                    (
                        "Unplug the cable from the board.",
                        "Plug it into the other socket, the one marked USB.",
                    ),
                    Severity.WARNING,
                    "usb_socket",
                )
            )
        elif state is PresenceState.OTHER_USB_MODE:
            found.append(
                _advice(
                    "USB_SETUP_MODE_NEEDED",
                    "Put the board into setup mode",
                    "The board is running other software, so the app cannot set it up yet. "
                    "This is normal for a new board.",
                    (
                        "Hold down the BOOT button on the board.",
                        "While you hold BOOT, press and release the RST button.",
                        "Let go of BOOT.",
                        "The app then finds the board by itself. When this screen says Board "
                        "found, click Next.",
                        "If you still see this message after 5 seconds, unplug the board.",
                        "Hold down BOOT while you plug the board back into the socket marked "
                        "USB. Then let go of BOOT.",
                        "If you still see this message after 3 tries, open Help. Click Save a "
                        "help file and send it to head office.",
                    ),
                    Severity.WARNING,
                    "boot_button",
                )
            )
        elif state is PresenceState.SEVERAL_BOARDS:
            found.append(
                _advice(
                    "USB_SEVERAL_BOARDS",
                    "More than one board is connected",
                    "This computer can see 2 boards. The app works with one board at a time.",
                    ("Unplug every board except the one you want to use.",),
                    Severity.WARNING,
                    "unplug",
                )
            )
        elif (
            state is PresenceState.RESTART_LOOP
            and presence.loop_period_s is not None
            and presence.loop_period_s < 6.0
        ):
            found.append(
                _advice(
                    "RESTART_LOOP_FAST",
                    "The board keeps restarting",
                    f"It restarts about every {presence.loop_period_s:.0f} seconds. Usually the "
                    "cable or the USB socket does not give it enough power.",
                    (
                        "Use a short cable straight into this computer, not a hub.",
                        "Try another USB socket on this computer.",
                        "Then click Check again.",
                    ),
                    Severity.WARNING,
                    "cable",
                )
            )
        elif state is PresenceState.RESTART_LOOP:
            found.append(
                _advice(
                    "BOARD_FAULTY",
                    "This board is faulty",
                    "It keeps switching itself off and on, about every 10 seconds. "
                    "Its light never comes on.",
                    (
                        "Unplug the board.",
                        "Put a FAULTY label on it and give it back to head office.",
                        "Connect a new board.",
                    ),
                    Severity.ERROR,
                    "faulty_board",
                    can_retry=False,
                )
            )
        if board is not None and board.security is SecurityState.SECURED_OTHER:
            found.append(
                _advice(
                    "BOARD_SECURED_OTHER",
                    "This board belongs to someone else",
                    "It was locked by another company. It cannot be set up for State Life.",
                    ("Unplug it and put it aside.", "Use another board."),
                    Severity.ERROR,
                    "faulty_board",
                    can_retry=False,
                )
            )
        if device is not None and device.known and device.online is False:
            found.append(
                _advice(
                    "DEVICE_OFFLINE",
                    "Head office has not heard from this board",
                    f"{device.status_text or 'It is offline'}.",
                    (
                        "Check that the board has power at the site.",
                        "Check that the site's internet is working.",
                    ),
                    Severity.WARNING,
                )
            )
        kinds = [event.kind for event in events]
        wifi = [event for event in events if event.kind in _WIFI_KINDS]
        if wifi and wifi[-1].kind is ConsoleKind.SETUP_AP_OPEN:
            # Like the engine: the board's own Wi-Fi is open now.
            hotspot = wifi[-1].data.get("ssid") or "the board's own Wi-Fi"
            found.append(
                _advice(
                    "HOTSPOT_OPEN",
                    "The board's own Wi-Fi is open",
                    f"The board has opened its own Wi-Fi called {hotspot}, so you can choose "
                    "the site's Wi-Fi.",
                    (
                        f"On a phone, join {hotspot}. Use the board's Wi-Fi password shown on "
                        "this screen.",
                        "Open http://192.168.254.1 in the phone's browser.",
                        "Choose the site's Wi-Fi and type its password.",
                    ),
                    Severity.INFO,
                    "hotspot",
                )
            )
        elif kinds.count(ConsoleKind.WIFI_RETRY) >= 2:
            found.append(
                _advice(
                    "WIFI_PLACEHOLDER",
                    "Choose the site Wi-Fi with a phone",
                    "The board is looking for the Wi-Fi called RE7. That name is only a "
                    "placeholder, so this is expected.",
                    (
                        "About 2 minutes after it starts, the board opens its own Wi-Fi.",
                        "On a phone, join it with the board's Wi-Fi password shown on this screen.",
                        "Choose the site's Wi-Fi and type its password.",
                    ),
                    Severity.INFO,
                    "hotspot",
                )
            )
        if ConsoleKind.TERMINAL_AUTH_FAILED in kinds:
            found.append(
                _advice(
                    "TERMINAL_KEY_WRONG",
                    "The attendance machine refused the board",
                    "The comm key on the machine is different.",
                    ("Read the comm key in the machine's menu.", "Then choose Fix settings."),
                    Severity.ERROR,
                )
            )
        return sorted(found, key=lambda item: _RANK.get(item.severity, 9))


# ===================================================================== records


class FakeRecords:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def save_install_record(
        self, summary: api.InstallSummary, boot: BootReport, restart: Optional[BootReport] = None
    ) -> str:
        self.world.note("records.save_install_record")
        config = summary.config
        path = os.path.join(
            self.world.folder(), f"board-{summary.mac.replace(':', '')}-{summary.request_id}.txt"
        )
        lines = [
            "State Life Device Installer: install record (fake)",
            f"Board ID: {summary.mac}",
            f"Machine type: {config.family.value}",
            f"ZONE ID: {config.zone_id}",
            f"ZONE NAME: {config.zone_name}",
            f"Device number: {config.device_id}",
            f"Board software: {summary.bundle.label}",
            f"First start: {boot.outcome.value}",
            f"After a restart: {restart.outcome.value if restart else 'not watched'}",
        ]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def save_help_file(self) -> str:
        self.world.note("records.save_help_file")
        path = os.path.join(self.world.folder(), "state-life-installer-help.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("README.txt", "Fake help file: no logs in fake mode.\n")
        return path


# ===================================================================== factory


def make_fake_services(scenario: str, *, pace: float = 0.01) -> api.Services:
    """Fake services for ``scenario`` (one of SCENARIOS); ``pace`` is seconds per scripted beat."""
    if scenario not in SCENARIOS:
        raise ValueError(
            f"unknown fake scenario {scenario!r}; choose one of {', '.join(SCENARIOS)}"
        )
    world = FakeWorld(scenario, pace)
    return api.Services(
        server=FakeServer(world),
        connectivity=FakeConnectivity(world),
        usb=FakeUsb(world),
        boards=FakeBoards(world),
        installer=FakeInstaller(world),
        boot=FakeBoot(world),
        diagnosis=FakeDiagnosis(world),
        records=FakeRecords(world),
        defaults=TEST_DEFAULTS,
        app_version="1.0.0",
    )


def fake_world(services: api.Services) -> FakeWorld:
    """The shared state behind fake services, for tests."""
    world = getattr(services.server, "world", None)
    if not isinstance(world, FakeWorld):
        raise TypeError("these are not fake services")
    return world
