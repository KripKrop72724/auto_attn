"""Tests for the release tooling: field defaults, icon, locks and the entry point."""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGING))

import build  # noqa: E402  (packaging/build.py)
import update_locks  # noqa: E402

from sl_installer import __main__ as entry  # noqa: E402

GOOD_DEFAULTS = {
    "wifi_ssid": "RE7",
    "wifi_password": "test-wifi-pass",
    "zkt_port": 4370,
    "zkt_comm_key": 4242,
    "zkt_preferred_ip": "0.0.0.0",
    "device_id": "1",
    "hik_port": 80,
    "hik_transport": "http_digest",
    "hik_username": "admin",
}
SETUP_PASSWORD = "test-hotspot-pass"


@pytest.fixture
def secrets(monkeypatch):
    def set_secrets(values=GOOD_DEFAULTS, password=SETUP_PASSWORD):
        raw = values if isinstance(values, str) else json.dumps(values)
        monkeypatch.setenv(build.DEFAULTS_ENV, raw)
        monkeypatch.setenv(build.SETUP_PASSWORD_ENV, password)
    monkeypatch.delenv(build.DEFAULTS_ENV, raising=False)
    monkeypatch.delenv(build.SETUP_PASSWORD_ENV, raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return set_secrets


# ------------------------------------------------------------ field defaults

def test_without_secrets_it_is_a_test_build(secrets):
    assert build.field_defaults(require_release=False) is None


def test_release_needs_the_secrets(secrets):
    with pytest.raises(SystemExit, match="A release needs"):
        build.field_defaults(require_release=True)


def test_release_values_come_from_the_secrets(secrets):
    secrets()
    values = build.field_defaults(require_release=True)
    assert values == dict(GOOD_DEFAULTS, setup_ap_password=SETUP_PASSWORD)


def test_one_secret_alone_is_refused(secrets, monkeypatch):
    secrets()
    monkeypatch.delenv(build.SETUP_PASSWORD_ENV)
    with pytest.raises(SystemExit, match="or neither"):
        build.field_defaults(require_release=False)


@pytest.mark.parametrize("change, message", [
    ({"extra": 1}, "unknown keys"),
    ({"zkt_port": "4370"}, "wrong value type"),
    ({"zkt_port": True}, "wrong value type"),
    ({"wifi_password": "short"}, "wifi_password"),
    ({"zkt_comm_key": -1}, "zkt_comm_key"),
    ({"zkt_preferred_ip": "8.8.8.8"}, "zkt_preferred_ip"),
    ({"device_id": "bad id"}, "device_id"),
    ({"hik_transport": "telnet"}, "hik_transport"),
    ({"hik_port": 0}, "hik_port"),
])
def test_bad_defaults_are_refused_without_showing_values(secrets, change, message):
    secrets(dict(GOOD_DEFAULTS, **change))
    with pytest.raises(SystemExit) as stop:
        build.field_defaults(require_release=True)
    text = str(stop.value)
    assert message in text
    for secret in ("test-wifi-pass", "4242", SETUP_PASSWORD, "short"):
        assert secret not in text


def test_missing_key_and_bad_json_are_refused(secrets):
    secrets({key: value for key, value in GOOD_DEFAULTS.items() if key != "hik_username"})
    with pytest.raises(SystemExit, match="missing keys"):
        build.field_defaults(require_release=True)
    secrets('{"wifi_ssid": ')
    with pytest.raises(SystemExit, match="not valid JSON"):
        build.field_defaults(require_release=True)


def test_hotspot_password_must_suit_wifi(secrets):
    secrets(password="1234567")
    with pytest.raises(SystemExit, match=build.SETUP_PASSWORD_ENV):
        build.field_defaults(require_release=True)


def test_values_are_masked_on_github_only(secrets, monkeypatch, capsys):
    secrets()
    build.field_defaults(require_release=True)
    assert capsys.readouterr().out == ""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    build.field_defaults(require_release=True)
    masks = capsys.readouterr().out.splitlines()
    assert masks == ["::add-mask::test-wifi-pass", "::add-mask::4242",
                     f"::add-mask::{SETUP_PASSWORD}"]


def test_the_app_reads_the_written_file_as_a_release(secrets, tmp_path, monkeypatch):
    from sl_installer.config import load_field_defaults

    secrets()
    target = tmp_path / "field_defaults.json"
    monkeypatch.setattr(build, "FIELD_DEFAULTS", target)
    assert build.write_field_defaults(require_release=True) is True
    defaults = load_field_defaults(target)
    assert defaults.is_test_build is False
    assert defaults.wifi_password == "test-wifi-pass" and defaults.zkt_comm_key == 4242
    assert defaults.setup_ap_password == SETUP_PASSWORD


def test_a_test_build_removes_any_old_file(secrets, tmp_path, monkeypatch):
    from sl_installer.config import load_field_defaults

    target = tmp_path / "field_defaults.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(build, "FIELD_DEFAULTS", target)
    assert build.write_field_defaults(require_release=False) is False
    assert not target.exists()
    assert load_field_defaults(target).is_test_build is True


def test_version_resource_is_filled_in(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "BUILD", tmp_path)
    build.write_version_info("1.2.3", release=False)
    text = (tmp_path / "version-info.txt").read_text(encoding="utf-8")
    assert "filevers=(1, 2, 3, 0)" in text and "'FileVersion', '1.2.3'" in text
    assert "'OriginalFilename', 'State Life Device Installer.exe'" in text
    assert "TEST BUILD" in text and "$" not in text
    # PyInstaller's own parser imports Windows-only modules.
    versioninfo = pytest.importorskip("PyInstaller.utils.win32.versioninfo", exc_type=ImportError)
    info = versioninfo.load_version_info_from_text_file(str(tmp_path / "version-info.txt"))
    assert info.ffi.fileVersionMS == (1 << 16) | 2


# --------------------------------------------------------------------- icon

def test_icon_has_every_size(tmp_path):
    pytest.importorskip("PySide6")
    path = tmp_path / "app.ico"
    build.write_icon(build._square_logo(), path)
    data = path.read_bytes()
    _, kind, count = struct.unpack("<HHH", data[:6])
    assert kind == 1 and count == len(build.ICON_SIZES)
    for index, size in enumerate(build.ICON_SIZES):
        width, height, _, _, planes, bits, length, offset = struct.unpack(
            "<BBBBHHII", data[6 + 16 * index:22 + 16 * index])
        assert (width or 256, height or 256, planes, bits) == (size, size, 1, 32)
        blob = data[offset:offset + length]
        if size >= 256:
            assert blob.startswith(b"\x89PNG")
        else:
            header_size, dib_width, dib_height = struct.unpack("<Iii", blob[:12])
            assert (header_size, dib_width, dib_height) == (40, size, size * 2)
            assert length == 40 + size * size * 4 + ((size + 31) // 32) * 4 * size


# -------------------------------------------------------------------- locks

def test_hashes_are_added_in_pip_compile_layout(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("#\n# header\n#\nfoo==1.0 ; python_version >= \"3.10\"\n    # via bar\n",
                    encoding="utf-8")
    releases = {"foo": {"urls": [{"digests": {"sha256": "b" * 64}},
                                 {"digests": {"sha256": "a" * 64}}]}}
    update_locks.add_hashes(lock, releases)
    assert lock.read_text(encoding="utf-8").splitlines()[3:] == [
        'foo==1.0 ; python_version >= "3.10" \\',
        f"    --hash=sha256:{'a' * 64} \\",
        f"    --hash=sha256:{'b' * 64}",
        "    # via bar",
    ]
    assert list(update_locks.requirement_lines(lock)) == ['foo==1.0 ; python_version >= "3.10"']


def test_a_lock_needs_a_windows_wheel_unless_source_only():
    linux_only = {"info": {"name": "foo", "requires_dist": []}, "urls": [
        {"packagetype": "bdist_wheel", "filename": "foo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl"},
        {"packagetype": "sdist", "filename": "foo-1.0.tar.gz"}]}
    assert update_locks.windows_requirements(linux_only) is None
    source_only = {"info": {"name": "esptool", "requires_dist": ["pyserial>=3.3"]},
                   "urls": [{"packagetype": "sdist", "filename": "esptool-5.1.0.tar.gz"}]}
    assert update_locks.windows_requirements(source_only) == ["pyserial>=3.3"]


def test_committed_locks_pin_the_agreed_versions():
    app = PACKAGING.parent
    pins = dict(update_locks.pin_of(line)
                for name in ("requirements.lock", "requirements-build.lock")
                for line in update_locks.requirement_lines(app / name))
    assert pins["pyside6-essentials"] == "6.9.2"
    assert pins["esptool"] == "5.1.0"
    assert pins["pyserial"] == "3.5"
    assert pins["httpx"] == "0.28.1"
    assert pins["bitstring"].startswith("4.")  # espefuse needs bitstring.BitStream
    assert pins["pyinstaller"].startswith("6.")
    for name in ("colorama", "pefile", "pywin32-ctypes", "setuptools", "truststore", "certifi"):
        assert name in pins
    for name in ("requirements.lock", "requirements-build.lock"):
        lines = (app / name).read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line and not line[0].isspace() and not line.startswith("#"):
                assert lines[index + 1].startswith("    --hash=sha256:"), line


# -------------------------------------------------------------- entry point

def test_tool_mode_hands_over_to_the_engine(monkeypatch):
    from sl_installer.core import esptool_runner

    seen = []
    monkeypatch.setattr(esptool_runner, "dispatch_tool", lambda args: seen.append(args) or 7)
    assert entry.main(["--tool", "espefuse", "--chip", "esp32s3", "summary"]) == 7
    assert seen == [["espefuse", "--chip", "esp32s3", "summary"]]


def test_tool_mode_reports_a_crash_instead_of_raising(monkeypatch, capsys):
    from sl_installer.core import esptool_runner

    def crash(args):
        raise RuntimeError("tool exploded")

    monkeypatch.setattr(esptool_runner, "dispatch_tool", crash)
    assert entry.main(["--tool", "esptool", "version"]) == 1
    assert "tool exploded" in capsys.readouterr().err


def test_self_test_reports_problems(monkeypatch, capsys):
    import types

    import sl_installer.core

    fake = types.ModuleType("sl_installer.core.selftest")
    seen = []
    fake.run = lambda cfg: seen.append(cfg.server_url) or ["the signing key is missing"]
    monkeypatch.setitem(sys.modules, "sl_installer.core.selftest", fake)
    monkeypatch.setattr(sl_installer.core, "selftest", fake, raising=False)
    monkeypatch.setattr(entry, "_smoke_test_window", lambda: None)
    built = []
    monkeypatch.setattr(entry, "_smoke_test_services", built.append)
    assert entry.main(["--self-test", "--server", "https://add.test"]) == 1
    out = capsys.readouterr().out
    assert "PROBLEM: the signing key is missing" in out and "Self-test failed" in out
    assert seen == ["https://add.test"] and built == ["https://add.test"]
    fake.run = lambda cfg: []
    assert entry.main(["--self-test"]) == 0
    assert "Self-test passed." in capsys.readouterr().out


def test_self_test_builds_the_real_services_without_starting_them(monkeypatch, tmp_path,
                                                                  capsys):
    import logging

    from sl_installer import config as app_config

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv(app_config.DATA_DIR_ENV, str(tmp_path))
    logger = logging.getLogger("sl_installer")
    handlers, propagate = list(logger.handlers), logger.propagate
    try:
        entry._smoke_test_services("https://127.0.0.1:9")
    finally:
        for handler in [h for h in logger.handlers if h not in handlers]:
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = propagate
    assert "Real services built." in capsys.readouterr().out
    assert (tmp_path / "logs" / "installer.log").is_file()


def test_a_second_window_says_the_app_is_open_and_stops(monkeypatch, tmp_path):
    import sl_installer.core.factory
    import sl_installer.ui.app
    from sl_installer import config as app_config
    from sl_installer.core.single_instance import LOCK_FILE_NAME, InstanceLock

    monkeypatch.setenv(app_config.DATA_DIR_ENV, str(tmp_path))
    built, shown = [], []
    monkeypatch.setattr(sl_installer.core.factory, "make_services",
                        lambda cfg: built.append(cfg) or "services")
    monkeypatch.setattr(entry, "_already_open", lambda: shown.append(True) or 0)

    def run_gui(services):
        # While the window runs, the lock is held: a second start would be refused.
        assert not InstanceLock(tmp_path / "state" / LOCK_FILE_NAME).acquire()
        return 0

    monkeypatch.setattr(sl_installer.ui.app, "run_gui", run_gui)
    assert entry.main([]) == 0 and len(built) == 1 and shown == []

    first_window = InstanceLock(tmp_path / "state" / LOCK_FILE_NAME)
    assert first_window.acquire()  # released when the window above closed
    try:
        assert entry.main([]) == 0
        assert shown == [True] and len(built) == 1  # no services, so no shared log file
    finally:
        first_window.release()


def test_modes_cannot_be_combined_and_version_prints(capsys):
    with pytest.raises(SystemExit) as stop:
        entry.main(["--self-test", "--screenshots", "shots"])
    assert stop.value.code == 2
    with pytest.raises(SystemExit) as stop:
        entry.main(["--server", "http://add.example", "--self-test"])
    assert stop.value.code == 2 and "--server" in capsys.readouterr().err
    with pytest.raises(SystemExit) as stop:
        entry.main(["--version"])
    assert stop.value.code == 0
    from sl_installer import __version__

    assert capsys.readouterr().out.strip() == __version__
