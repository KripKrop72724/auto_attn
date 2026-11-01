"""The real screens on the real engine, with only the board, its port and head office simulated.

Below the window everything is the shipped engine: the USB watcher, the board
inspector, the installer (bundle gate, one-time envelope, flash plan), the
first-start watcher, diagnosis and records. The board answers esptool and
espefuse the way an ESP32-S3 does (tests/core/support.py), its USB port is a
listed port the test plugs and unplugs, and head office is the engine tests'
fake with the real envelope encryption. The UI tests' scripted fakes stand in
for all of this elsewhere, so these tests catch places where the two disagree.
"""

from __future__ import annotations

import importlib.util
import logging
import socket
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import List, Tuple

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from sl_installer.config import load_config
from sl_installer.core import api
from sl_installer.core.api import (
    BundleInfo,
    ConnectivityStatus,
    Family,
    FieldDefaults,
    InstallMode,
    ReplugPhase,
)
from sl_installer.core.board import BoardInspector
from sl_installer.core.bundles import BundleStore
from sl_installer.core.clock import Clock
from sl_installer.core.console import BootWatcher
from sl_installer.core.diagnosis import Diagnoser
from sl_installer.core.factory import make_services
from sl_installer.core.installer import Installer
from sl_installer.core.records import Records
from sl_installer.core.usb import UsbWatcher
from sl_installer.ui import pages
from sl_installer.ui.window import MainWindow


def _load_support():
    """tests/core/support.py, loaded by path (tests/ is not a package)."""
    path = Path(__file__).resolve().parents[1] / "core" / "support.py"
    spec = importlib.util.spec_from_file_location("sl_installer_core_test_support", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it loads
    spec.loader.exec_module(module)
    return module


support = _load_support()

ZONE = "LF-ZONE-BLD9-04"
TIMEOUT_MS = 30000


def boot_lines(zone: str, device: str = "1", version: str = "2.9.1") -> List[str]:
    """A first start as the firmware and ESP-IDF print it."""
    return [
        "ESP-ROM:esp32s3-20210327",
        "rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)",
        "I (30) boot: ESP-IDF v5.5.3 2nd stage bootloader",
        "I (60) secure_boot_v2: enabling secure boot v2...",
        "I (95) boot: Loaded app from partition at offset 0x2a0000",
        "I (240) esp_psram: Found 8MB PSRAM device",
        "I (380) app_init: Project name:     zone_lite",
        f"I (381) app_init: App version:      {version}",
        f"I (1600) zone_config: Loaded per-device provisioning for zone={zone}",
        f"I (1700) zone_lite: Zone Lite starting zone={zone} device_id={device}",
        "I (2900) zone_lite: Storage ready; loaded 0 known event UIDs",
        "I (3000) zone_lite: Connecting to Wi-Fi SSID RE7",
    ]


class Ports:
    """The computer's serial ports: the board's own USB port while it is plugged in."""

    def __init__(self, mac: str) -> None:
        self.plugged = True
        self.info = SimpleNamespace(
            device="/dev/ttyACM0",
            vid=0x303A,
            pid=0x1001,
            serial_number=mac.upper(),
            description="USB JTAG/serial debug unit",
        )

    def __call__(self):
        return [self.info] if self.plugged else []


class Console:
    """What the board prints each time its port is opened."""

    def __init__(self, ports: Ports, lines: List[str]) -> None:
        self.ports = ports
        self.lines = lines
        self.opened: List[str] = []
        self.streams: List["_Stream"] = []

    def open(self, port: str):
        if not self.ports.plugged:
            raise FileNotFoundError(2, "No such file or directory")
        self.opened.append(port)
        stream = _Stream(self.ports, ("\n".join(self.lines) + "\n").encode())
        self.streams.append(stream)
        return stream

    @property
    def held(self) -> bool:
        """True while the app holds the port open (esptool could not open it on Windows)."""
        return any(not stream.closed for stream in self.streams)


class _Stream:
    def __init__(self, ports: Ports, data: bytes) -> None:
        self.ports = ports
        self.data = data
        self.closed = False

    def read(self, size: int) -> bytes:
        if not self.ports.plugged:
            raise OSError("device disconnected")
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk

    def close(self) -> None:
        self.closed = True


class HeadOffice(support.FakeServer):
    """The engine tests' head office, offering its one signed bundle."""

    def __init__(self, store: BundleStore, bundle) -> None:
        super().__init__(store, bundle)
        manifest = bundle.manifest
        self.info = BundleInfo(
            bundle.bundle_id,
            Family(manifest["firmware_family"]),
            manifest["label"],
            manifest["version"],
            True,
            manifest["release_state"],
        )

    def bundles(self, family=None):
        return [self.info] if family in (None, self.info.family) else []


class Online:
    def check(self) -> ConnectivityStatus:
        return ConnectivityStatus(True, True)


class Rig:
    def __init__(self, qtbot, tmp_path: Path, board, family: Family = Family.ZKT) -> None:
        self.qtbot = qtbot
        key = support.signing_key()
        self.bundle = support.build_bundle(tmp_path, family, key)
        store = BundleStore(
            tmp_path / "bundles", support.verifier(key), storage_gz=support.STORAGE_GZ
        )
        self.board = board
        self.ports = Ports(support.MAC)
        self.console = Console(self.ports, boot_lines(ZONE))
        # For each esptool/espefuse call: did it run on the GUI thread, was the port held?
        self.tool_calls: List[Tuple[bool, bool]] = []
        self.runner = support.FakeRunner(
            board, before=lambda _call: self.tool_calls.append((on_gui_thread(), self.console.held))
        )
        clock = Clock()
        usb = UsbWatcher(lister=self.ports, clock=clock, tree_macs=dict)
        self.server = HeadOffice(store, self.bundle)
        work = tmp_path / "work"
        self.work = work
        self.services = api.Services(
            server=self.server,
            connectivity=Online(),
            usb=usb,
            boards=BoardInspector(self.runner, temp_root=work),
            installer=Installer(
                runner=self.runner,
                server=self.server,
                usb=usb,
                store=store,
                app_version="1.0.0",
                clock=clock,
                temp_root=work,
            ),
            boot=BootWatcher(usb, clock, opener=self.console.open, placeholder_ssids=("RE7",)),
            diagnosis=Diagnoser(("RE7",)),
            records=Records(tmp_path / "records", tmp_path / "logs", "1.0.0"),
            defaults=FieldDefaults(
                wifi_password="test-wifi-pass", setup_ap_password="test-hotspot-pass"
            ),
            app_version="1.0.0",
        )
        self.window = MainWindow(self.services)
        self.window.resize(1280, 800)
        self.window.show()
        self.controller = self.window.controller

    def page(self, page_id: str):
        return self.window.pages[page_id]

    def wait(self, condition, timeout: int = TIMEOUT_MS) -> None:
        self.qtbot.waitUntil(condition, timeout=timeout)

    def wait_page(self, page_id: str, timeout: int = TIMEOUT_MS) -> None:
        self.wait(lambda: self.controller.current == page_id, timeout)

    def click_primary(self, page_id: str) -> None:
        button = self.page(page_id).primary_button
        assert button.isVisible() and button.isEnabled(), f"{page_id}: main button not available"
        button.click()

    def sign_in(self) -> None:
        self.controller.start()
        page = self.page(pages.SIGN_IN)
        page.username.setText("admin")
        page.password.setText("test-admin-pass")
        self.click_primary(pages.SIGN_IN)
        self.wait_page(pages.HOME)

    def board_found(self) -> None:
        self.wait_page(pages.CONNECT)
        self.wait(lambda: self.page(pages.CONNECT).primary_button.isEnabled())
        self.click_primary(pages.CONNECT)

    def confirm_and_install(self) -> None:
        self.wait_page(pages.CONFIRM)
        confirm = self.page(pages.CONFIRM)
        if confirm.understand.isVisible():
            confirm.understand.setChecked(True)
        self.click_primary(pages.CONFIRM)
        self.wait_page(pages.INSTALLING)

    def replug(self, lines: List[str]) -> None:
        """Unplug when the screen asks, wait, plug back in; the board then prints ``lines``."""
        first_start = self.page(pages.FIRST_START)
        self.wait_page(pages.FIRST_START)
        self.wait(lambda: first_start.phase is ReplugPhase.WAITING_UNPLUG)
        self.console.lines = lines
        self.ports.plugged = False
        self.wait(lambda: first_start.phase is ReplugPhase.WAITING_REPLUG)
        self.ports.plugged = True
        self.wait(lambda: first_start.result.isVisible())

    def close(self) -> None:
        self.controller.session.installing = False
        self.controller.shutdown()
        self.window.hide()
        assert self.controller.runner.wait(10000), "background work did not stop"
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def on_gui_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


@pytest.fixture
def rig(qtbot, tmp_path):
    made: List[Rig] = []

    def make(board, family: Family = Family.ZKT) -> Rig:
        item = Rig(qtbot, tmp_path, board, family)
        made.append(item)
        return item

    yield make
    for item in made:
        item.close()


COMMANDS = (
    "get-security-info",
    "flash-id",
    "erase-region",
    "write-flash",
    "verify-flash",
    "summary",
    "burn-key",
)


def command_of(args) -> str:
    return next(arg for arg in args if arg in COMMANDS)


def esptool_args(rig: Rig, command: str):
    return [seen.call.args for seen in rig.runner.seen if command in seen.call.args]


def test_new_zkt_board_end_to_end_on_the_real_engine(rig):
    r = rig(support.FakeBoard())
    r.sign_in()
    r.page(pages.HOME).action.emit("setup")
    r.wait_page(pages.MACHINE)
    r.page(pages.MACHINE).zkt_card.click()
    r.wait_page(pages.DETAILS)
    form = r.page(pages.DETAILS).form
    r.wait(lambda: form.selected_bundle_id() == r.bundle.bundle_id)
    form.zone_id.setText(ZONE)
    r.click_primary(pages.DETAILS)

    r.board_found()  # the real USB watcher found the board by its USB serial number
    checking = r.page(pages.CHECKING)
    r.wait(lambda: checking.result.isVisible())
    assert checking.result.advice.code == "NEW_BOARD", checking.result.advice
    r.click_primary(pages.CHECKING)

    r.confirm_and_install()
    r.replug(boot_lines(ZONE))
    first_start = r.page(pages.FIRST_START)
    assert first_start.result.advice.code == "FIRST_START_OK", first_start.result.advice
    r.click_primary(pages.FIRST_START)
    r.wait_page(pages.DONE)
    done = r.page(pages.DONE)
    assert done.credentials.values["hotspot"].text() == "SLIC ATTENDANCE-3D31"
    assert done.credentials.values["password"].text() == "test-hotspot-pass"
    done.secondary_button.click()  # Save a record, through the real records service
    r.wait(lambda: done.saved.isVisible())
    record = Path(r.controller.session.record_path).read_text(encoding="utf-8")
    assert ZONE in record and "test-wifi-pass" not in record
    r.window.open_help(done.help_topic)
    r.window.help_panel.save_button.click()  # Help > Save a help file
    r.wait(lambda: r.window.help_panel.saved.isVisible())
    with zipfile.ZipFile(r.window.help_panel._saved_path) as archive:
        names = archive.namelist()
        packed = b"".join(archive.read(name) for name in names)
    assert any(name.startswith("records/") for name in names)
    assert b"test-wifi-pass" not in packed and r.server.hmac_key.hex().encode() not in packed

    # The board got the bundle, its own key, and the start-up program last.
    board, bundle = r.board, r.bundle
    assert board.burned_key == r.server.hmac_key
    assert board.flash[0x0] == bundle.files["bootloader-signed.bin"]
    assert board.flash[0x11000] == r.server.nvs
    writes = esptool_args(r, "write-flash")
    assert [args[-2] for args in writes] == ["0x7a0000", "0x0"]  # everything else, then 0x0
    commands = [command_of(seen.call.args) for seen in r.runner.seen]
    burn = commands.index("burn-key")
    assert commands[burn - 2 : burn + 4] == [
        "verify-flash",
        "summary",
        "burn-key",
        "summary",
        "write-flash",
        "verify-flash",
    ]
    assert commands[-2:] == ["write-flash", "verify-flash"]
    for seen in r.runner.seen:
        args = seen.call.args
        assert not {"hard-reset", "soft-reset", "watchdog-reset"} & set(args)
        if seen.call.tool == "esptool":
            assert args[args.index("--after") + 1] == "no-reset"
    # The key file existed only for the burn.
    burn_seen = next(seen for seen in r.runner.seen if "burn-key" in seen.call.args)
    assert burn_seen.key_file_bytes == r.server.hmac_key and not burn_seen.cancellable
    key_file = Path(burn_seen.call.args[burn_seen.call.args.index("BLOCK_KEY0") + 1])
    assert not key_file.exists() and not list(r.work.glob("sl-install-*"))

    # Head office heard every milestone, then how the first start went.
    kinds = [event[2] for event in r.server.events]
    assert kinds[0] == "STARTED" and "KEY_LOCKED" in kinds
    r.wait(lambda: [event[2] for event in r.server.events][-1:] == ["BOOT_OK"])
    assert [event[2] for event in r.server.events][-2:] == ["COMPLETED", "BOOT_OK"]
    assert len({event[0] for event in r.server.events}) == 1  # one request number throughout
    assert r.tool_calls and not any(gui for gui, _held in r.tool_calls), "ran on the GUI thread"
    assert not any(held for _gui, held in r.tool_calls)  # the port was free for every call


def test_new_hikvision_board_end_to_end_on_the_real_engine(rig):
    r = rig(support.FakeBoard(), Family.HIKVISION)
    r.sign_in()
    r.page(pages.HOME).action.emit("setup")
    r.wait_page(pages.MACHINE)
    r.page(pages.MACHINE).hik_card.click()
    r.wait_page(pages.DETAILS)
    form = r.page(pages.DETAILS).form
    r.wait(lambda: form.selected_bundle_id() == r.bundle.bundle_id)
    form.zone_id.setText("LF-ZONE-JHELUM-01")
    form.hik_host.setText("192.168.1.20")
    form.hik_password.setText("test-machine-pass")
    form.hik_serial.setText("TEST-SERIAL-0001")
    r.click_primary(pages.DETAILS)
    r.board_found()
    checking = r.page(pages.CHECKING)
    r.wait(lambda: checking.result.isVisible())
    assert checking.result.advice.code == "NEW_BOARD", checking.result.advice
    r.click_primary(pages.CHECKING)
    r.confirm_and_install()
    # The Hikvision build starts its factory copy; its project name differs, its log tags do not.
    lines = [
        line.replace("0x2a0000", "0x20000").replace(
            "Project name:     zone_lite", "Project name:     zone_lite_hikvision"
        )
        for line in boot_lines("LF-ZONE-JHELUM-01", version="3.1.1")
    ]
    r.replug(lines)
    first_start = r.page(pages.FIRST_START)
    assert first_start.result.advice.code == "FIRST_START_OK", first_start.result.advice

    # What the screens typed reached head office as the engine completed it.
    (request,) = r.server.requests
    hik = request.config.hik
    assert request.config.family is Family.HIKVISION and request.config.zkt is None
    assert (hik.host, hik.expected_serial, hik.transport, hik.port) == (
        "192.168.1.20",
        "TEST-SERIAL-0001",
        "http_digest",
        80,
    )
    assert hik.profile == api.HIK_MODELS[0].profile  # the tested model, chosen by default
    assert hik.source_epoch.startswith("lf-zone-jhelum-01-")
    # Hikvision boards start the factory copy: no ota_0 or ota_1, start-up program last.
    written = [arg for args in esptool_args(r, "write-flash") for arg in args]
    assert "0x20000" in written and "0x2a0000" not in written and "0x520000" not in written
    assert [args[-2] for args in esptool_args(r, "write-flash")][-1] == "0x0"
    assert r.board.burned_key == r.server.hmac_key


def test_failure_after_the_key_is_finished_without_a_second_burn(rig):
    r = rig(support.FakeBoard())
    # The start-up program copy breaks once, after the key is locked (cable pulled).
    r.board.failures["copy start-up program"] = support.Failure(2, support.SERIAL_NOISE)
    r.sign_in()
    r.page(pages.HOME).action.emit("setup")
    r.wait_page(pages.MACHINE)
    r.page(pages.MACHINE).zkt_card.click()
    r.wait_page(pages.DETAILS)
    form = r.page(pages.DETAILS).form
    r.wait(lambda: form.selected_bundle_id() == r.bundle.bundle_id)
    form.zone_id.setText(ZONE)
    r.click_primary(pages.DETAILS)
    r.board_found()
    checking = r.page(pages.CHECKING)
    r.wait(lambda: checking.result.isVisible())
    r.click_primary(pages.CHECKING)
    r.confirm_and_install()

    installing = r.page(pages.INSTALLING)
    r.wait(lambda: installing.problem.isVisible())
    advice = installing.problem.advice
    assert advice.code == "RESUME_NEEDED" and r.board.burned_key == r.server.hmac_key
    assert installing.primary_button.text() == "Finish the setup"
    assert advice.what_to_do[-1] == "Click Finish the setup."
    r.click_primary(pages.INSTALLING)

    r.wait_page(pages.CHECKING)
    r.wait(lambda: checking.result.isVisible())
    assert checking.result.advice.code == "RESUME", checking.result.advice
    r.click_primary(pages.CHECKING)
    r.confirm_and_install()
    assert r.controller.session.mode is InstallMode.RESUME
    r.replug(boot_lines(ZONE))
    first_start = r.page(pages.FIRST_START)
    assert first_start.result.advice.code == "FIRST_START_OK", first_start.result.advice

    assert len(esptool_args(r, "burn-key")) == 1  # the resume did not burn again
    assert r.board.flash[0x0] == r.bundle.files["bootloader-signed.bin"]
    commands = [command_of(seen.call.args) for seen in r.runner.seen]
    assert commands[-2:] == ["write-flash", "verify-flash"]  # the start-up program, last
    kinds = [event[2] for event in r.server.events]
    assert "FAILED" in kinds and kinds[-2:] == ["COMPLETED", "BOOT_OK"]


def test_secured_board_settings_change_on_the_real_engine(rig):
    r = rig(support.FakeBoard(efuse_state="sdm", sdm=True))
    r.console.lines = boot_lines("LF-ZONE-BLD9-02")
    r.sign_in()
    r.page(pages.HOME).action.emit("check")
    r.board_found()
    r.wait_page(pages.CHECK_BOARD)
    check = r.page(pages.CHECK_BOARD)
    r.wait(lambda: check.primary_button.isVisible() and check.primary_button.isEnabled())
    assert check.primary_button.text() == "Fix settings"
    # The live console runs on the real watcher; leaving the screen must free the port.
    r.wait(lambda: bool(r.console.opened))
    calls_before_fix = len(r.tool_calls)
    r.click_primary(pages.CHECK_BOARD)

    r.wait_page(pages.FIX_SETTINGS)
    fix = r.page(pages.FIX_SETTINGS)
    r.wait(lambda: not r.controller.running("bundles"))
    fix.form.zone_id.setText(ZONE)
    fix.form.zkt_comm_key.setText("12345")  # from the attendance machine, never a default
    r.click_primary(pages.FIX_SETTINGS)
    r.confirm_and_install()
    assert r.controller.session.mode is InstallMode.CHANGE_SETTINGS
    r.replug(boot_lines(ZONE))
    first_start = r.page(pages.FIRST_START)
    assert first_start.result.advice.code == "FIRST_START_OK", first_start.result.advice

    # Secure Download Mode: one blind ROM write of the settings and empty storage, no key.
    assert r.board.burned_key is None
    assert esptool_args(r, "burn-key") == [] and esptool_args(r, "erase-region") == []
    (write,) = esptool_args(r, "write-flash")
    assert "--no-stub" in write and write[-4::2] == ("0x11000", "0x7a0000")
    assert r.board.flash[0x11000] == r.server.nvs
    assert not any(held for _gui, held in r.tool_calls[calls_before_fix:])
    (request,) = r.server.requests
    assert request.config.zkt.comm_key == 12345


@pytest.fixture
def restore_logging():
    logger = logging.getLogger("sl_installer")
    handlers, propagate, level = list(logger.handlers), logger.propagate, logger.level
    yield
    for handler in list(logger.handlers):
        if handler not in handlers:
            logger.removeHandler(handler)
            handler.close()
    logger.propagate, logger.level = propagate, level


def test_real_services_start_offline_without_touching_usb(
    qtbot, tmp_path, monkeypatch, restore_logging
):
    touched: List[str] = []

    def refuse(name: str):
        def blocked(*_args, **_kwargs):
            touched.append(name)
            raise OSError(f"{name} is blocked in this test")

        return blocked

    monkeypatch.setattr(socket, "getaddrinfo", refuse("network"))
    monkeypatch.setattr(socket, "create_connection", refuse("network"))
    monkeypatch.setattr(socket.socket, "connect", refuse("network"))
    import serial
    from serial.tools import list_ports

    monkeypatch.setattr(list_ports, "comports", lambda *_a, **_k: touched.append("usb") or [])
    monkeypatch.setattr(serial.Serial, "open", refuse("serial"))

    services = make_services(load_config(server_url="https://127.0.0.1:9", data_dir=tmp_path))
    window = MainWindow(services)
    window.show()
    qtbot.wait(100)
    assert touched == []  # building the engine and the window touched no network or USB

    window.controller.start()
    bar = window.top_bar
    qtbot.waitUntil(lambda: bar.internet_chip.text == "No internet", timeout=10000)
    assert bar.server_chip.text == "Head office not answering"
    assert window.controller.current == pages.SIGN_IN
    assert set(touched) == {"network"}  # only the connection check tried, and was refused
    window.close()
    assert window.controller.runner.wait(10000)
