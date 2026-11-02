"""The real app against head office's real installer API, end to end.

The window, the engine and the ADD client are the shipped app, as in
test_ui_real_engine.py. Head office is ADD itself: its FastAPI app (zk_add.web)
with the real admin sign-in, CSRF token and password step-up, the installer
routes, its bundle store and a file-backed SQLite database, reached in process
through Starlette's test transport. Only the board, its port and the protected
provisioner (real envelopes, as in tests/unit/test_installer_api.py) are
simulated. These tests catch places where the app and the server disagree about
the API, which neither side's own tests can see.

They need ADD's server packages (esp-installer-dev:local has them) and are
skipped where those are missing, such as the Windows release build.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

import httpx
import pytest

REPO = Path(__file__).resolve().parents[4]
BACKEND = REPO / "apps" / "add_backend"
if BACKEND.is_dir() and str(BACKEND) not in sys.path:
    # This checkout's ADD, ahead of any copy installed in the environment.
    sys.path.insert(0, str(BACKEND))
pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
zk_add = pytest.importorskip("zk_add")
if BACKEND not in Path(zk_add.__file__).resolve().parents:
    pytest.skip("ADD was imported from another checkout", allow_module_level=True)

from cryptography.hazmat.primitives import serialization  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from zk_add import installer_api, provisioning_api, web  # noqa: E402
from zk_add.db import Base, create_database_engine  # noqa: E402
from zk_add.models import AuditEvent  # noqa: E402
from zk_add.provisioning_api import FixedWindowLimiter  # noqa: E402
from zk_add.security import hash_admin_password  # noqa: E402
from zk_add.service import onboard_connector  # noqa: E402
from zk_add.settings import settings  # noqa: E402
from zk_add.time_utils import utc_now  # noqa: E402

from sl_installer.core import api, texts  # noqa: E402
from sl_installer.core.add_client import AddClient, LoginThrottle  # noqa: E402
from sl_installer.core.api import (  # noqa: E402
    Family,
    FieldDefaults,
    InstallerError,
    InstallMode,
    PackageRequest,
    ReplugPhase,
    ZktSettings,
)
from sl_installer.core.board import BoardInspector  # noqa: E402
from sl_installer.core.bundles import BundleStore  # noqa: E402
from sl_installer.core.clock import Clock  # noqa: E402
from sl_installer.core.console import BootWatcher  # noqa: E402
from sl_installer.core.diagnosis import Diagnoser  # noqa: E402
from sl_installer.core.envelope import OneTimeKey  # noqa: E402
from sl_installer.core.installer import Installer  # noqa: E402
from sl_installer.core.records import Records  # noqa: E402
from sl_installer.core.usb import UsbWatcher  # noqa: E402
from sl_installer.ui import pages  # noqa: E402
from sl_installer.ui.window import MainWindow  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it loads
    spec.loader.exec_module(module)
    return module


HERE = Path(__file__).resolve().parent
# The board, port and console simulations of the real-engine tests (tests/ is not a package).
real_engine = _load("sl_installer_real_engine_tests", HERE / "test_ui_real_engine.py")
support = real_engine.support

ADMIN = "StateHealthAdmin"
ADMIN_PASSWORD = "test-only-admin-sign-in"
INTERNAL_TOKEN = "test-only-internal-token-0123456789abcdef"
MAC = support.MAC
ZONE = real_engine.ZONE
SITE_ZONE = "LF-ZONE-BLD9-02"
# ADD's words for a board that may be locked with an older security root (review B1/F1).
SETTINGS_REFUSED = installer_api.SETTINGS_CHANGE_REFUSED
TIMEOUT_MS = 30000


class Provisioner:
    """The protected provisioner's v2 endpoint: real envelopes for the app's one-time key."""

    def __init__(self) -> None:
        self.nvs = bytes(range(256)) * (0x6000 // 256)
        self.hmac_key = bytes(range(100, 132))
        self.builds: List[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"ok": True})
        payload = json.loads(request.content)
        self.builds.append(payload)
        config = payload["configuration"]
        package = support.make_package(
            payload["request_id"],
            payload["target_mac"],
            payload["recipient_public_key"],
            family=config["firmware_family"],
            zone_id=config["zone_id"],
            device_id=config["device_id"],
            nvs=self.nvs,
            hmac_key=self.hmac_key,
        )
        return httpx.Response(
            201,
            json={
                "provisioning_manifest": package.provisioning_manifest,
                "provisioning_ciphertext_b64": base64.b64encode(
                    package.provisioning_ciphertext
                ).decode(),
                "hmac_ciphertext_b64": base64.b64encode(package.hmac_ciphertext).decode(),
            },
        )


class HeadOffice:
    """ADD with one admin, an empty bundle store and the simulated provisioner."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.engine = create_database_engine(f"sqlite:///{tmp_path / 'add.db'}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(provisioning_api, "SessionLocal", self.sessions)
        monkeypatch.setattr(web, "SessionLocal", self.sessions)
        self.key = support.signing_key()  # the vault key's stand-in, for both sides
        pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.store = tmp_path / "installer-bundles"
        self.store.mkdir()
        for name, value in {
            "admin_username": ADMIN,
            "admin_password_hash": hash_admin_password(ADMIN_PASSWORD),
            "admin_cookie_secure": False,
            "provisioning_enabled": True,
            "installer_enabled": True,
            "provisioning_internal_token": INTERNAL_TOKEN,
            "provisioning_worker_url": "http://add-provisioner.test",
            "firmware_signing_public_key_pem_b64": base64.b64encode(pem).decode(),
            "installer_bundle_store_path": str(self.store),
            "installer_min_app_version": "1.0.0",
        }.items():
            monkeypatch.setattr(settings, name, value)
        monkeypatch.setattr(installer_api, "package_limiter", FixedWindowLimiter(30, 600))
        monkeypatch.setattr(installer_api, "event_limiter", FixedWindowLimiter(240, 600))
        self.provisioner = Provisioner()
        monkeypatch.setattr(
            installer_api,
            "_provisioner_client",
            lambda: httpx.Client(
                transport=httpx.MockTransport(self.provisioner.handler), timeout=5
            ),
        )
        installer_api.bundle_store.clear()
        self.http = TestClient(web.app)

    @property
    def transport(self) -> httpx.BaseTransport:
        return self.http._transport  # the app's own HTTP client goes in process to web.app

    def publish(self, bundle) -> None:
        """A bundle as publish-installer-bundle.ps1 leaves it in the store."""
        directory = self.store / bundle.bundle_id
        shutil.copytree(bundle.directory, directory)
        (directory / "manifest.json").write_text(json.dumps(bundle.manifest), encoding="utf-8")
        (directory / "manifest.sig").write_text(bundle.signature, encoding="ascii")

    def add_connector(
        self, mac: str, *, zone_id: str, seen: bool, device_id: str = "1", family: str = "zkt"
    ) -> None:
        """A board ADD knows; ``seen`` when it has been online (its own stream opened)."""
        with self.sessions() as db:
            connector, _token, _created = onboard_connector(
                db,
                hardware_id=mac,
                zone_id=zone_id,
                zone_name=zone_id,
                device_id=device_id,
                firmware_version="2.7.8",
                expected_serial=None,
                actor="test",
                ip_address=None,
                firmware_family=family,
            )
            if seen:
                connector.last_seen_at = utc_now() - timedelta(minutes=5)
            db.commit()

    def audits(self, action: Optional[str] = None) -> List[AuditEvent]:
        with self.sessions() as db:
            statement = select(AuditEvent).order_by(AuditEvent.id)
            if action:
                statement = statement.where(AuditEvent.action == action)
            return list(db.scalars(statement))

    def close(self) -> None:
        installer_api.bundle_store.clear()
        self.engine.dispose()


class Rig:
    """The shipped app on a simulated board, talking to the real head office."""

    def __init__(self, qtbot, tmp_path: Path, office: HeadOffice, board) -> None:
        self.qtbot = qtbot
        self.office = office
        self.board = board
        self.ports = real_engine.Ports(MAC)
        self.console = real_engine.Console(self.ports, real_engine.boot_lines(ZONE))
        self.runner = support.FakeRunner(board)
        clock = Clock()
        usb = UsbWatcher(lister=self.ports, clock=clock, tree_macs=dict)
        store = BundleStore(
            tmp_path / "app-bundles", support.verifier(office.key), storage_gz=support.STORAGE_GZ
        )
        self.server = AddClient(
            "http://testserver",
            app_version="1.0.0",
            store=store,
            transport=office.transport,
            throttle=LoginThrottle(),
        )
        work = tmp_path / "work"
        self.services = api.Services(
            server=self.server,
            connectivity=real_engine.Online(),
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

    def wait_page(self, page_id: str) -> None:
        self.wait(lambda: self.controller.current == page_id)

    def click_primary(self, page_id: str) -> None:
        button = self.page(page_id).primary_button
        assert button.isVisible() and button.isEnabled(), f"{page_id}: main button not available"
        button.click()

    def sign_in(self) -> None:
        self.controller.start()
        page = self.page(pages.SIGN_IN)
        page.username.setText(ADMIN)
        page.password.setText(ADMIN_PASSWORD)
        self.click_primary(pages.SIGN_IN)
        self.wait_page(pages.HOME)

    def board_found(self) -> None:
        self.wait_page(pages.CONNECT)
        self.wait(lambda: self.page(pages.CONNECT).primary_button.isEnabled())
        self.click_primary(pages.CONNECT)

    def to_checking(self, zone: str = ZONE) -> None:
        """Set up a new board: ZKTeco, the recommended software, ``zone``; then the check."""
        self.page(pages.HOME).action.emit("setup")
        self.wait_page(pages.MACHINE)
        self.page(pages.MACHINE).zkt_card.click()
        self.wait_page(pages.DETAILS)
        form = self.page(pages.DETAILS).form
        self.wait(lambda: bool(form.selected_bundle_id()))
        form.zone_id.setText(zone)
        self.click_primary(pages.DETAILS)
        self.board_found()
        checking = self.page(pages.CHECKING)
        self.wait(lambda: checking.result.isVisible())

    def to_check_board(self) -> None:
        self.page(pages.HOME).action.emit("check")
        self.board_found()
        self.wait_page(pages.CHECK_BOARD)
        self.wait(lambda: not self.controller.running("check"))

    def confirm_and_install(self) -> None:
        self.wait_page(pages.CONFIRM)
        confirm = self.page(pages.CONFIRM)
        if confirm.understand.isVisible():
            confirm.understand.setChecked(True)
        self.click_primary(pages.CONFIRM)
        self.wait_page(pages.INSTALLING)

    def replug(self, lines: List[str]) -> None:
        first_start = self.page(pages.FIRST_START)
        self.wait_page(pages.FIRST_START)
        self.wait(lambda: first_start.phase is ReplugPhase.WAITING_UNPLUG)
        self.console.lines = lines
        self.ports.plugged = False
        self.wait(lambda: first_start.phase is ReplugPhase.WAITING_REPLUG)
        self.ports.plugged = True
        self.wait(lambda: first_start.result.isVisible())

    def package_request(self, mode: InstallMode, **config) -> PackageRequest:
        return PackageRequest(
            "sli-e2e-0123456789abcdef",
            MAC,
            mode,
            support.zkt_config(**config),
            OneTimeKey().public_key_b64,
            "1.0.0",
        )

    def close(self) -> None:
        self.controller.session.installing = False
        self.controller.shutdown()
        self.window.hide()
        assert self.controller.runner.wait(10000), "background work did not stop"
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def office(tmp_path, monkeypatch):
    head_office = HeadOffice(tmp_path, monkeypatch)
    yield head_office
    head_office.close()


@pytest.fixture
def rig(qtbot, tmp_path, office):
    made: List[Rig] = []

    def make(board) -> Rig:
        item = Rig(qtbot, tmp_path, office, board)
        made.append(item)
        return item

    yield make
    for item in made:
        item.close()


def zkt_bundle(tmp_path: Path, office: HeadOffice):
    return support.build_bundle(tmp_path / "built", Family.ZKT, office.key)


def events(office: HeadOffice) -> List[str]:
    return [
        row.action
        for row in office.audits()
        if row.action.startswith("INSTALLER_") and row.action != "INSTALLER_PACKAGE_ISSUED"
    ]


# ------------------------------------------------------------------ tests


def test_new_board_package_and_events_from_the_real_head_office(rig, office, tmp_path):
    bundle = zkt_bundle(tmp_path, office)
    office.publish(bundle)
    r = rig(support.FakeBoard())
    r.sign_in()
    r.to_checking()
    assert r.page(pages.CHECKING).result.advice.code == "NEW_BOARD"
    r.click_primary(pages.CHECKING)
    r.confirm_and_install()
    r.replug(real_engine.boot_lines(ZONE))
    first_start = r.page(pages.FIRST_START)
    assert first_start.result.advice.code == "FIRST_START_OK", first_start.result.advice
    r.click_primary(pages.FIRST_START)
    r.wait_page(pages.DONE)

    # The board got head office's software, the provisioner's settings and its key.
    assert r.board.flash[0x0] == bundle.files["bootloader-signed.bin"]
    assert r.board.flash[0x11000] == office.provisioner.nvs
    assert r.board.burned_key == office.provisioner.hmac_key
    (issued,) = office.audits("INSTALLER_PACKAGE_ISSUED")
    assert issued.target_id == MAC and issued.actor == ADMIN
    assert issued.after["mode"] == "new_board" and issued.after["bundle_id"] == bundle.bundle_id
    r.wait(lambda: events(office)[-1:] == ["INSTALLER_BOOT_OK"])
    kinds = events(office)
    assert kinds[0] == "INSTALLER_STARTED" and "INSTALLER_KEY_LOCKED" in kinds
    assert kinds[-2:] == ["INSTALLER_COMPLETED", "INSTALLER_BOOT_OK"]
    assert len({row.request_id for row in office.audits() if row.action in kinds}) == 1
    for row in office.audits():
        assert "test-wifi-pass" not in json.dumps(row.after or {})

    # Once the board has been online, this app may still change its settings: it set it up.
    office.add_connector(MAC, zone_id=ZONE, seen=True)
    record = r.server.device(MAC)
    assert record.known and record.ever_online and record.settings_change_allowed


def test_new_hikvision_board_package_from_the_real_head_office(rig, office, tmp_path):
    bundle = support.build_bundle(tmp_path / "built", Family.HIKVISION, office.key)
    office.publish(bundle)
    r = rig(support.FakeBoard())
    r.sign_in()
    r.page(pages.HOME).action.emit("setup")
    r.wait_page(pages.MACHINE)
    r.page(pages.MACHINE).hik_card.click()
    r.wait_page(pages.DETAILS)
    form = r.page(pages.DETAILS).form
    r.wait(lambda: form.selected_bundle_id() == bundle.bundle_id)
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
    lines = [
        line.replace("0x2a0000", "0x20000").replace(
            "Project name:     zone_lite", "Project name:     zone_lite_hikvision"
        )
        for line in real_engine.boot_lines("LF-ZONE-JHELUM-01", version="3.1.1")
    ]
    r.replug(lines)
    first_start = r.page(pages.FIRST_START)
    assert first_start.result.advice.code == "FIRST_START_OK", first_start.result.advice

    # ADD took the app's Hikvision settings as they are, and passed them on.
    (issued,) = office.audits("INSTALLER_PACKAGE_ISSUED")
    assert issued.after["firmware_family"] == "hikvision"
    assert issued.after["bundle_id"] == bundle.bundle_id
    (build,) = office.provisioner.builds
    config = build["configuration"]
    assert (config["hik_host"], config["hik_expected_serial"], config["zkt_comm_key"]) == (
        "192.168.1.20",
        "TEST-SERIAL-0001",
        0,
    )
    assert config["hik_profile"] == api.HIK_MODELS[0].profile
    assert config["hik_source_epoch"].startswith("lf-zone-jhelum-01-")
    assert r.board.burned_key == office.provisioner.hmac_key


def test_online_board_set_up_before_the_app_gets_no_new_settings(rig, office):
    office.add_connector(MAC, zone_id=SITE_ZONE, seen=True)
    r = rig(support.FakeBoard(efuse_state="sdm", sdm=True))
    r.sign_in()
    record = r.server.device(MAC)
    assert record.known and record.ever_online
    assert not record.settings_change_allowed and record.settings_change_reason == SETTINGS_REFUSED

    # Check a board: Fix settings is not offered, and head office's reason is shown.
    r.to_check_board()
    check = r.page(pages.CHECK_BOARD)
    r.wait(lambda: check.fix_blocked.isVisible())
    assert not check.primary_button.isVisible()
    blocked = check.fix_blocked.advice
    assert blocked.code == "SETTINGS_CHANGE_NOT_ALLOWED" and not blocked.can_retry
    assert blocked.what_happened == SETTINGS_REFUSED
    assert check.fix_blocked.buttons == []  # nothing to try again: only head office can help

    # Head office refuses the package itself too, and the app says so in the same words.
    with pytest.raises(InstallerError) as caught:
        r.server.create_package(
            r.package_request(
                InstallMode.CHANGE_SETTINGS, bundle_id="", zone_id=SITE_ZONE, zone_name=SITE_ZONE
            )
        )
    advice = caught.value.advice
    assert advice is texts.SETTINGS_CHANGE_NOT_SUPPORTED and not advice.can_retry
    assert advice.title == blocked.title and advice.what_to_do == blocked.what_to_do
    assert office.provisioner.builds == [] and office.audits("INSTALLER_PACKAGE_ISSUED") == []


def test_never_online_board_gets_new_settings_without_published_software(rig, office):
    # Nothing is published: a settings change writes no board software (review F3).
    office.add_connector(MAC, zone_id=SITE_ZONE, seen=False)
    r = rig(support.FakeBoard(efuse_state="sdm", sdm=True))
    r.sign_in()
    record = r.server.device(MAC)
    assert record.known and not record.ever_online and record.settings_change_allowed
    r.to_check_board()
    check = r.page(pages.CHECK_BOARD)
    r.wait(lambda: check.primary_button.isVisible() and check.primary_button.isEnabled())
    assert check.primary_button.text() == "Fix settings"
    r.click_primary(pages.CHECK_BOARD)

    r.wait_page(pages.FIX_SETTINGS)
    fix = r.page(pages.FIX_SETTINGS)
    assert not fix.warning_card.isVisible()  # never online: no punches to lose
    assert fix.form.zone_id.text() == SITE_ZONE  # head office's
    fix.form.zkt_comm_key.setText("12345")  # from the attendance machine
    r.wait(lambda: fix.primary_button.isEnabled())
    r.click_primary(pages.FIX_SETTINGS)
    r.confirm_and_install()
    r.replug(real_engine.boot_lines(SITE_ZONE))
    first_start = r.page(pages.FIRST_START)
    assert first_start.result.advice.code == "FIRST_START_OK", first_start.result.advice

    assert r.board.flash[0x11000] == office.provisioner.nvs and r.board.burned_key is None
    (issued,) = office.audits("INSTALLER_PACKAGE_ISSUED")
    assert issued.after["mode"] == "change_settings" and issued.after["bundle_id"] is None
    (build,) = office.provisioner.builds
    assert build["configuration"]["zkt_comm_key"] == 12345


def test_failure_after_the_burn_tells_head_office_and_the_setup_can_finish(rig, office, tmp_path):
    office.publish(zkt_bundle(tmp_path, office))
    r = rig(support.FakeBoard())
    # The start-up program copy breaks once, after the key is locked (cable pulled).
    r.board.failures["copy start-up program"] = support.Failure(2, support.SERIAL_NOISE)
    r.sign_in()
    r.to_checking()
    r.click_primary(pages.CHECKING)
    r.confirm_and_install()
    installing = r.page(pages.INSTALLING)
    r.wait(lambda: installing.problem.isVisible())
    assert installing.problem.advice.code == "RESUME_NEEDED"

    # Head office stores that the board is now locked; the audit row keeps the flag.
    r.wait(lambda: bool(office.audits("INSTALLER_FAILED")))
    (failed,) = office.audits("INSTALLER_FAILED")
    assert failed.after["lock_burned"] is True, failed.after
    assert "[REDACTED]" not in json.dumps(failed.after)

    # Head office has never seen this board online, so it lets this app finish it.
    r.click_primary(pages.INSTALLING)
    r.wait_page(pages.CHECKING)
    checking = r.page(pages.CHECKING)
    r.wait(lambda: checking.result.isVisible())
    assert checking.result.advice.code == "RESUME", checking.result.advice
    r.click_primary(pages.CHECKING)
    r.confirm_and_install()
    r.replug(real_engine.boot_lines(ZONE))
    assert r.page(pages.FIRST_START).result.advice.code == "FIRST_START_OK"
    modes = [row.after["mode"] for row in office.audits("INSTALLER_PACKAGE_ISSUED")]
    assert modes == ["new_board", "resume"]


def test_locked_board_head_office_has_seen_online_is_not_finished(rig, office, tmp_path):
    # Key locked but never started, yet head office has seen it working at a site: not a
    # setup that stopped, but a board set up some other way (review F1).
    office.publish(zkt_bundle(tmp_path, office))
    office.add_connector(MAC, zone_id=ZONE, seen=True)
    r = rig(support.FakeBoard(efuse_state="key-burned"))
    r.sign_in()
    r.to_checking()
    checking = r.page(pages.CHECKING)
    advice = checking.result.advice
    assert advice.code == "SETTINGS_CHANGE_NOT_ALLOWED" and not advice.can_retry, advice
    assert advice.what_happened == SETTINGS_REFUSED
    assert not checking.primary_button.isVisible()  # no "Finish the setup"
    assert checking.secondary_button.text() == "Use another board"

    with pytest.raises(InstallerError) as caught:
        r.server.create_package(r.package_request(InstallMode.RESUME, zone_id=ZONE, zone_name=ZONE))
    assert caught.value.advice is texts.SETTINGS_CHANGE_NOT_SUPPORTED
    assert office.provisioner.builds == [] and r.board.flash == {}


def _known_as_hikvision(office, monkeypatch, tmp_path):
    office.publish(zkt_bundle(tmp_path, office))
    office.add_connector(MAC, zone_id=ZONE, seen=False, family="hikvision")


def _device_number_taken(office, monkeypatch, tmp_path):
    office.publish(zkt_bundle(tmp_path, office))
    office.add_connector(support.OTHER_MAC, zone_id=ZONE, seen=False)


def _app_too_old(office, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "installer_min_app_version", "9.0.0")


def _provisioner_down(office, monkeypatch, tmp_path):
    office.publish(zkt_bundle(tmp_path, office))
    office.provisioner.handler = lambda request: (
        httpx.Response(200, json={"ok": True})
        if request.url.path == "/health/ready"
        else httpx.Response(500)
    )


def _nothing_published(office, monkeypatch, tmp_path):
    pass


@pytest.mark.parametrize(
    "arrange, code, words",
    [
        (_known_as_hikvision, "FAMILY_MISMATCH", "Hikvision"),
        (_device_number_taken, "DEVICE_ID_IN_USE", "Use device number 2 instead"),
        (_app_too_old, "APP_TOO_OLD", "newer version"),
        (_provisioner_down, "PROVISIONER_UNAVAILABLE", "cannot make board settings"),
        (_nothing_published, "NOT_FOUND", "could not find"),
    ],
)
def test_head_office_refusals_are_worded_by_the_app(
    rig, office, monkeypatch, tmp_path, arrange, code, words
):
    arrange(office, monkeypatch, tmp_path)
    r = rig(support.FakeBoard())
    r.server.login(ADMIN, ADMIN_PASSWORD)
    with pytest.raises(InstallerError) as caught:
        r.server.create_package(
            r.package_request(InstallMode.NEW_BOARD, zone_id=ZONE, zone_name=ZONE)
        )
    advice = caught.value.advice
    assert advice.code == code, advice
    assert words in " ".join((advice.what_happened,) + advice.what_to_do), advice
    assert office.audits("INSTALLER_PACKAGE_ISSUED") == []


@pytest.mark.parametrize(
    "change, words",
    [
        ({"zkt": ZktSettings(4370, 1979, "8.8.8.8")}, "machine address"),
        ({"device_id": "device 1!"}, "device number"),
    ],
)
def test_head_office_names_a_wrong_setting_as_the_form_does(rig, office, change, words):
    # ADD's message is shown word for word under the app's own title (review E15).
    r = rig(support.FakeBoard())
    r.server.login(ADMIN, ADMIN_PASSWORD)
    with pytest.raises(InstallerError) as caught:
        r.server.create_package(r.package_request(InstallMode.NEW_BOARD, **change))
    advice = caught.value.advice
    assert advice.code.startswith("INVALID_FIELD:"), advice
    assert words in advice.title and words in advice.what_happened.lower(), advice


def test_head_office_rate_limit_says_how_long_to_wait(rig, office, monkeypatch):
    monkeypatch.setattr(installer_api, "package_limiter", FixedWindowLimiter(1, 600))
    r = rig(support.FakeBoard())
    r.server.login(ADMIN, ADMIN_PASSWORD)
    request = r.package_request(InstallMode.NEW_BOARD)
    with pytest.raises(InstallerError):
        r.server.create_package(request)  # counted, then refused: nothing is published
    with pytest.raises(InstallerError) as caught:
        r.server.create_package(request)
    advice = caught.value.advice
    assert advice.code == "RATE_LIMITED"
    assert "Wait 10 minutes." in advice.what_to_do, advice.what_to_do
