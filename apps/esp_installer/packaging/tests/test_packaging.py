"""Tests for the release tooling: field defaults, icon, locks, the Mac app and
disk image, and the entry point."""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
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
    # HTTPS needs the machine's own certificate, which the defaults cannot carry.
    ({"hik_transport": "https"}, "hik_transport"),
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


def test_the_app_refuses_https_in_the_defaults_file(tmp_path):
    from sl_installer.config import load_field_defaults

    target = tmp_path / "field_defaults.json"
    target.write_text(json.dumps(dict(GOOD_DEFAULTS, hik_transport="https")), encoding="utf-8")
    with pytest.raises(ValueError, match="hik_transport"):
        load_field_defaults(target)


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

def _png_size(data):
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_mac_icon_has_every_iconset_size(tmp_path):
    pytest.importorskip("PySide6")
    path = tmp_path / "app.icns"
    build.write_icns(build._square_logo(), path)
    data = path.read_bytes()
    assert data[:4] == b"icns" and struct.unpack(">I", data[4:8])[0] == len(data)
    offset, found = 8, []
    while offset < len(data):
        kind, length = data[offset:offset + 4].decode("ascii"), struct.unpack(
            ">I", data[offset + 4:offset + 8])[0]
        found.append((kind, _png_size(data[offset + 8:offset + length])))
        offset += length
    assert offset == len(data)
    assert found == [(kind, (pixels, pixels)) for kind, pixels in build.ICNS_IMAGES]
    # A standard .iconset: 16, 32, 128, 256 and 512 points at 1x and 2x.
    assert sorted(pixels for _, pixels in build.ICNS_IMAGES) == [
        16, 32, 32, 64, 128, 256, 256, 512, 512, 1024]


def test_mac_icon_is_a_rounded_white_square_with_room_around_it():
    pytest.importorskip("PySide6")
    icon = build._mac_icon(build._square_logo(), 256)
    assert icon.pixelColor(2, 2).alpha() == 0  # transparent corner of the canvas
    assert 0 < icon.pixelColor(36, 36).alpha() < 128  # shadow beside the rounded corner
    top = icon.pixelColor(128, 34)  # inside the body, above the logo
    assert (top.alpha(), top.name()) == (255, "#ffffff")

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


def _release(name, files, requires=()):
    return {"info": {"name": name, "requires_dist": list(requires)},
            "urls": [{"packagetype": "sdist" if file.endswith(".tar.gz") else "bdist_wheel",
                      "filename": file, "url": f"https://files.example/{file}"}
                     for file in files]}


@pytest.fixture
def no_wheel_metadata(monkeypatch):
    """Wheels without PEP 658 metadata: the release's requires_dist counts."""
    def refuse(url, timeout):
        raise urllib.error.HTTPError(url, 404, "no metadata", None, None)
    monkeypatch.setattr(update_locks.urllib.request, "urlopen", refuse)


def test_a_lock_needs_a_wheel_for_each_target_unless_source_only(no_wheel_metadata):
    linux_only = _release("foo", ["foo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl",
                                  "foo-1.0.tar.gz"])
    for target in update_locks.TARGETS:
        assert update_locks.target_requirements(linux_only, target) is None
    source_only = _release("esptool", ["esptool-5.1.0.tar.gz"], ["pyserial>=3.3"])
    for target in update_locks.TARGETS:
        assert update_locks.target_requirements(source_only, target) == ["pyserial>=3.3"]
    windows_only = _release("foo", ["foo-1.0-cp312-cp312-win_amd64.whl"], ["bar"])
    assert update_locks.target_requirements(windows_only, update_locks.WINDOWS) == ["bar"]
    assert update_locks.target_requirements(windows_only, update_locks.MACOS) is None


@pytest.mark.parametrize("wheel, fits", [
    ("foo-1.0-cp312-cp312-macosx_11_0_arm64.whl", True),
    ("foo-1.0-cp312-cp312-macosx_12_0_arm64.whl", True),
    ("foo-1.0-cp39-abi3-macosx_12_0_universal2.whl", True),
    ("foo-1.0-py3-none-macosx_10_13_universal2.whl", True),
    ("foo-1.0-py3-none-any.whl", True),
    # Built for a newer macOS than the app promises (LSMinimumSystemVersion).
    ("foo-1.0-cp312-cp312-macosx_14_0_arm64.whl", False),
    ("foo-1.0-cp312-cp312-macosx_10_13_x86_64.whl", False),
    ("foo-1.0-cp311-cp311-macosx_11_0_arm64.whl", False),
])
def test_a_mac_wheel_must_fit_apple_silicon_and_the_oldest_macos(no_wheel_metadata, wheel, fits):
    found = update_locks.target_requirements(_release("foo", [wheel]), update_locks.MACOS)
    assert (found is not None) is fits


def test_each_target_needs_its_own_dependencies_locked(no_wheel_metadata, tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("pyinstaller==6.22.3 \\\n    --hash=sha256:" + "a" * 64 + "\n",
                    encoding="utf-8")
    releases = {"pyinstaller": _release("pyinstaller", [
        "pyinstaller-6.22.3-py3-none-win_amd64.whl",
        "pyinstaller-6.22.3-py3-none-macosx_10_13_universal2.whl"], [
        'macholib>=1.8; sys_platform == "darwin"',
        'pefile!=2024.8.26,>=2022.5.30; sys_platform == "win32"'])}
    windows = update_locks.check_target([lock], releases, update_locks.WINDOWS)
    macos = update_locks.check_target([lock], releases, update_locks.MACOS)
    assert len(windows) == 1 and "pefile" in windows[0] and "Windows" in windows[0]
    assert len(macos) == 1 and "macholib" in macos[0] and "macOS" in macos[0]


def test_the_mac_app_and_the_locks_agree_on_the_oldest_macos():
    assert build.MACOS_MINIMUM == ".".join(str(part) for part in update_locks.MACOS_MINIMUM)


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
    for name in ("colorama", "pefile", "pywin32-ctypes", "macholib", "setuptools", "truststore",
                 "certifi"):
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


@pytest.mark.parametrize("url", [
    "http://localhost.attacker.example", "http://127.0.0.1.attacker.example",
    "http://127.0.0.1.attacker.example:80", "http://localhost@attacker.example",
    "http://127.0.0.1@evil.example", "http://127.0.0.1@evil.example/",
    "https://admin@add.example", "http://evil.example", "ftp://127.0.0.1",
    "https://add.example:99999", "https://",
])
def test_a_server_must_use_https_unless_it_is_this_computer(url, tmp_path, capsys):
    from sl_installer.config import load_config

    # The host that counts is the one httpx would reach, not how the text starts.
    with pytest.raises(ValueError):
        load_config(url, tmp_path)
    with pytest.raises(SystemExit) as stop:
        entry.main(["--server", url, "--self-test"])
    assert stop.value.code == 2 and "--server" in capsys.readouterr().err


@pytest.mark.parametrize("url, kept", [
    (None, "https://autoattn.slichealth.com"),
    ("https://staging.example.test/", "https://staging.example.test"),
    ("https://add.test", "https://add.test"),
    ("https://127.0.0.1:9", "https://127.0.0.1:9"),
    ("http://127.0.0.1:8096", "http://127.0.0.1:8096"),
    ("http://LOCALHOST:8096/", "http://LOCALHOST:8096"),
    ("http://[::1]:8096", "http://[::1]:8096"),
])
def test_good_server_addresses_are_kept(url, kept, tmp_path):
    from sl_installer.config import load_config

    assert load_config(url, tmp_path).server_url == kept


# ------------------------------------------------------------------ notices

NOTICE_VERSIONS = {"qt": "6.9.2", "pyside6-essentials": "6.9.2", "esptool": "5.1.0",
                   "pyinstaller": "6.22.0"}


def test_notices_give_the_flasher_stubs_true_licence():
    import notices

    text = notices._summary("1.2.3", NOTICE_VERSIONS, platform="win32")
    words = " ".join(text.split())
    assert "a small part of the app hands the arguments over to esptool" in words
    assert 'esptool\'s source code is in "licenses\\esptool-5.1.0.tar.gz"' in words
    stub = words[words.index("esptool flasher stub (esptool-legacy-flasher-stub v1.8.0)"):]
    assert '"_internal\\esptool\\targets\\stub_flasher\\1"' in stub
    assert notices.STUB_SOURCE in stub and "head office can supply a copy on request" in stub
    assert stub.index("GPL-2.0-or-later") < stub.index("stub_flasher\\2") \
        < stub.index("Apache-2.0 or MIT")
    assert "runs only esptool's code" not in words and "complete source" not in words
    assert max(len(line) for line in text.splitlines()) <= len(notices.RULE)


def test_mac_notices_name_the_folders_inside_the_app():
    import notices

    text = notices._summary("1.2.3", NOTICE_VERSIONS, platform="darwin")
    words = " ".join(text.split())
    assert "_internal" not in text and "\\" not in text and ".exe" not in text
    assert "the macOS build of Python" in words
    assert ('"Contents/Frameworks/PySide6" and "Contents/Frameworks/shiboken6" folders'
            in words)
    assert 'is in "Contents/Resources/licenses/esptool-5.1.0.tar.gz" inside the app' in words
    stub = words[words.index("esptool flasher stub"):]
    assert '"Contents/Resources/esptool/targets/stub_flasher/1"' in stub
    assert stub.index("GPL-2.0-or-later") < stub.index("stub_flasher/2") \
        < stub.index("Apache-2.0 or MIT")
    assert 'the start-up program in "Contents/MacOS"' in words
    assert max(len(line) for line in text.splitlines()) <= len(notices.RULE)
    # The same summary apart from where the files are.
    windows = notices._summary("1.2.3", NOTICE_VERSIONS, platform="win32")
    assert len(text.splitlines()) == len(windows.splitlines()) + 1


def test_the_notice_matches_the_stub_esptool_loads():
    from importlib import metadata

    try:
        esptool = metadata.distribution("esptool")
    except metadata.PackageNotFoundError:
        pytest.skip("esptool is not installed here")
    import notices

    readme = Path(esptool.locate_file("esptool/targets/stub_flasher/1/README.md"))
    words = " ".join(readme.read_text(encoding="utf-8").split())
    assert "General Public License Version 2 or later" in words
    assert notices.STUB_SOURCE in words
    # Stub 1 is esptool's default, and the app never sets ESPTOOL_STUB_VERSION.
    source = Path(esptool.locate_file("esptool/__init__.py")).read_text(encoding="utf-8")
    assert 'os.environ.get("ESPTOOL_STUB_VERSION", "1")' in source


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


def test_setup_waits_for_the_mutex_the_window_holds():
    """Setup and the uninstaller find the open app by its mutex, and never force it closed."""
    settings = {}
    for line in (PACKAGING / "installer.iss").read_text(encoding="utf-8").splitlines():
        name, equals, value = line.partition("=")
        if equals and name in ("AppMutex", "CloseApplications"):
            settings.setdefault(name, []).append(value.strip())
    assert settings["AppMutex"] == [entry.APP_MUTEX]
    assert settings["CloseApplications"] == ["yes"]  # "force" could stop a board mid-write


# -------------------------------------------------------------------- macOS

def _macho(cpu, minimum=None, old_style=False):
    """A thin 64-bit Mach-O header with one version load command."""
    commands = b""
    if minimum is not None:
        version = (minimum[0] << 16) | (minimum[1] << 8)
        if old_style:
            commands = struct.pack("<IIII", build.LC_VERSION_MIN_MACOSX, 16, version, 0)
        else:
            commands = struct.pack("<IIIIII", build.LC_BUILD_VERSION, 24,
                                   build.PLATFORM_MACOS, version, 0, 0)
    count = 1 if commands else 0
    return struct.pack("<IIIIIIII", 0xFEEDFACF, cpu, 0, 6, count, len(commands), 0, 0) + commands


ARM64, X86_64 = 0x0100000C, 0x01000007


def _fat(*slices):
    header = struct.pack(">II", 0xCAFEBABE, len(slices))
    offset = 8 + 20 * len(slices)
    entries, body = b"", b""
    for cpu, data in slices:
        entries += struct.pack(">IIIII", cpu, 0, offset + len(body), len(data), 0)
        body += data
    return header + entries + body


def test_mach_o_slices_and_their_oldest_macos():
    assert build.macho_slices(_macho(ARM64, (12, 0))) == {"arm64": (12, 0)}
    assert build.macho_slices(_macho(X86_64, (10, 13), old_style=True)) == {"x86_64": (10, 13)}
    assert build.macho_slices(_macho(ARM64)) == {"arm64": None}
    universal = _fat((X86_64, _macho(X86_64, (10, 9))), (ARM64, _macho(ARM64, (11, 0))))
    assert build.macho_slices(universal) == {"x86_64": (10, 9), "arm64": (11, 0)}
    assert build.macho_slices(b"#!/bin/sh\n") is None
    # A Java class file starts with the same four bytes as a universal binary.
    assert build.macho_slices(b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 64) is None


def test_every_program_file_must_run_on_apple_silicon_and_the_oldest_macos(tmp_path):
    app = tmp_path / "App.app"
    (app / "Contents" / "Frameworks").mkdir(parents=True)
    good = app / "Contents" / "Frameworks" / "good.dylib"
    good.write_bytes(_macho(ARM64, (12, 0)))
    (app / "Contents" / "Frameworks" / "universal.so").write_bytes(
        _fat((X86_64, _macho(X86_64, (10, 9))), (ARM64, _macho(ARM64, (11, 0)))))
    (app / "Contents" / "Frameworks" / "notes.txt").write_text("not a program", encoding="utf-8")
    try:  # a link is not a program file of its own
        (app / "Contents" / "link.dylib").symlink_to(good)
    except OSError:
        pass  # Windows without the right to make links
    build.check_macos_binaries(app)

    (app / "Contents" / "Frameworks" / "newer.dylib").write_bytes(_macho(ARM64, (13, 0)))
    (app / "Contents" / "Frameworks" / "intel.so").write_bytes(_macho(X86_64, (10, 9)))
    with pytest.raises(SystemExit) as stop:
        build.check_macos_binaries(app)
    text = str(stop.value)
    assert "Contents/Frameworks/newer.dylib needs macOS 13.0" in text.replace("\\", "/")
    assert "intel.so has no Apple Silicon code (x86_64)" in text
    assert "good.dylib" not in text and "universal.so" not in text


def test_info_plist_names_the_app_for_apple_silicon():
    info = build.macos_info("1.1.0")
    assert info["CFBundleIdentifier"] == "com.statelife.deviceinstaller"
    assert info["CFBundleName"] == info["CFBundleDisplayName"] == build.APP_NAME
    assert info["CFBundleShortVersionString"] == info["CFBundleVersion"] == "1.1.0"
    assert info["LSMinimumSystemVersion"] == build.MACOS_MINIMUM == "12.0"
    assert info["NSHighResolutionCapable"] is True
    assert info["LSArchitecturePriority"] == ["arm64"]
    plistlib.dumps(info)  # only types a property list can hold


def test_the_spec_reads_the_info_plist_build_wrote(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "BUILD", tmp_path)
    build.write_macos_info("1.1.0")
    written = json.loads((tmp_path / "macos-info.json").read_text(encoding="utf-8"))
    assert written == build.macos_info("1.1.0")
    spec = (PACKAGING / "sl_installer.spec").read_text(encoding="utf-8")
    assert 'BUILD / "macos-info.json"' in spec and 'target_arch="arm64"' in spec
    assert 'icon=str(BUILD / "branding" / "app.icns")' in spec


def _write_plist(app, info):
    (app / "Contents").mkdir(parents=True, exist_ok=True)
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)


def test_the_built_info_plist_is_checked(tmp_path, monkeypatch):
    app = tmp_path / f"{build.APP_NAME}.app"
    monkeypatch.setattr(build, "MAC_APP", app)
    _write_plist(app, dict(build.macos_info("1.1.0"), CFBundleExecutable=build.APP_NAME))
    build.check_info_plist("1.1.0")
    _write_plist(app, dict(build.macos_info("1.1.0"), CFBundleExecutable=build.APP_NAME,
                           LSMinimumSystemVersion="11.0"))
    with pytest.raises(SystemExit, match="LSMinimumSystemVersion"):
        build.check_info_plist("1.1.0")
    with pytest.raises(SystemExit, match="CFBundleShortVersionString"):
        build.check_info_plist("1.1.1")


@pytest.mark.parametrize("release, name", [
    (True, "StateLife-Device-Installer-1.1.0-macOS-arm64.dmg"),
    (False, "StateLife-Device-Installer-1.1.0-macOS-arm64-TEST.dmg"),
])
def test_disk_image_names(release, name):
    assert build.dmg_name("1.1.0", release) == name
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name)  # ADD's installer app store accepts it


def _record_runs(monkeypatch, codes=()):
    """Record every command build.py runs; hand out the given exit codes first."""
    runs, pending = [], list(codes)

    def run(command, check=False, env=None, **kwargs):
        runs.append((list(command), env))
        code = pending.pop(0) if pending else 0
        if check and code:
            raise subprocess.CalledProcessError(code, command)
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(build.subprocess, "run", run)
    monkeypatch.setattr(build.time, "sleep", lambda seconds: None)
    return runs


def test_the_finished_app_is_signed_ad_hoc_without_the_secrets(secrets, monkeypatch, tmp_path):
    secrets()
    monkeypatch.setattr(build, "MAC_APP", tmp_path / "App.app")
    runs = _record_runs(monkeypatch)
    build.sign_app()
    app = str(tmp_path / "App.app")
    assert [command for command, _ in runs] == [
        ["codesign", "--force", "--sign", "-", app],
        ["codesign", "--verify", "--deep", "--strict", app]]
    for _, env in runs:
        assert build.DEFAULTS_ENV not in env and build.SETUP_PASSWORD_ENV not in env


def test_the_disk_image_holds_the_app_and_a_link_to_applications(monkeypatch, tmp_path):
    monkeypatch.setattr(build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(build, "DIST", tmp_path / "dist")
    monkeypatch.setattr(build, "MAC_APP", tmp_path / "dist" / f"{build.APP_NAME}.app")
    staging = tmp_path / "build" / "dmg"
    seen = {}

    def look(command, env):
        if command[:2] == ["hdiutil", "create"]:
            seen["link"] = os.readlink(staging / "Applications")
    runs = _record_runs(monkeypatch, codes=[0, 1, 0])  # hdiutil is busy once
    recorded = build.subprocess.run

    def run(command, check=False, env=None, **kwargs):
        look(list(command), env)
        return recorded(command, check=check, env=env, **kwargs)

    monkeypatch.setattr(build.subprocess, "run", run)
    dmg = build.build_dmg("1.1.0", release=False)
    assert dmg == tmp_path / "dist" / "StateLife-Device-Installer-1.1.0-macOS-arm64-TEST.dmg"
    commands = [command for command, _ in runs]
    assert commands[0] == ["ditto", str(build.MAC_APP), str(staging / build.MAC_APP.name)]
    create = ["hdiutil", "create", "-volname", f"{build.APP_NAME} 1.1.0 TEST", "-srcfolder",
              str(staging), "-fs", "HFS+", "-format", "UDZO", "-imagekey", "zlib-level=9",
              "-ov", str(dmg)]
    assert commands[1:] == [create, create, ["hdiutil", "verify", "-quiet", str(dmg)]]
    assert seen["link"] == "/Applications"
    assert not staging.exists()


def test_the_disk_image_step_gives_up_after_three_tries(monkeypatch, tmp_path):
    monkeypatch.setattr(build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(build, "DIST", tmp_path / "dist")
    runs = _record_runs(monkeypatch, codes=[0, 1, 1, 1])
    with pytest.raises(SystemExit, match="hdiutil could not make"):
        build.build_dmg("1.1.0", release=True)
    assert [command[:2] for command, _ in runs] == [["ditto", str(build.MAC_APP)]] + [
        ["hdiutil", "create"]] * 3


def test_the_mac_app_must_hold_everything_it_loads(monkeypatch, tmp_path):
    app = tmp_path / f"{build.APP_NAME}.app"
    monkeypatch.setattr(build, "MAC_APP", app)
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(SystemExit) as stop:
        build.check_app_folder(release=False)
    missing = [line.strip() for line in str(stop.value).splitlines()[1:]]
    monkeypatch.undo()
    for name in ("Contents/MacOS/State Life Device Installer", "Contents/Info.plist",
                 "Contents/Resources/EULA.txt", "Contents/Resources/THIRD-PARTY-NOTICES.txt",
                 "Contents/Resources/app.icns",
                 "Contents/Frameworks/PySide6/Qt/plugins/platforms/libqcocoa.dylib",
                 "Contents/Frameworks/esptool/targets/stub_flasher/1/esp32s3.json"):
        assert str(Path(name)) in missing
    for name in missing:
        (app / name).parent.mkdir(parents=True, exist_ok=True)
        (app / name).write_bytes(b"")
    monkeypatch.setattr(build, "MAC_APP", app)
    monkeypatch.setattr(sys, "platform", "darwin")
    build.check_app_folder(release=False)
    (app / "Contents" / "Frameworks" / "sl_installer" / "resources" /
     build.FIELD_DEFAULTS.name).write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="TEST build must not contain"):
        build.check_app_folder(release=False)


@pytest.mark.skipif(sys.platform == "win32", reason="the Mac smoke test is a bash script")
def test_the_mac_smoke_test_is_an_executable_bash_script():
    script = PACKAGING / "smoke_test_macos.sh"
    assert os.access(script, os.X_OK)
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(script)], check=True)
