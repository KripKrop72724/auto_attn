"""Config, field defaults, self-test, factory wiring and the bench tool's guard."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sl_installer import __main__ as entry
from sl_installer import config as app_config
from sl_installer.config import AppConfig, load_config, load_field_defaults
from sl_installer.core import bench, selftest, texts
from sl_installer.core.api import (BootOutcome, BootReport, FieldDefaults, HardwareVerdict,
                                   PresenceReport, PresenceState, SecurityState, Services)
from sl_installer.core.esptool_runner import ToolResult, ToolRunner
from sl_installer.core.factory import make_services
from sl_installer.core.single_instance import LOCK_FILE_NAME, InstanceLock

from . import support


def test_data_dir_per_platform(monkeypatch, tmp_path):
    monkeypatch.delenv(app_config.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\field\AppData\Local")
    assert str(app_config.default_data_dir()).replace("/", "\\").endswith(
        r"Local\StateLife\DeviceInstaller")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert app_config.default_data_dir() == (Path.home() / "Library" / "Application Support"
                                             / "StateLife" / "DeviceInstaller")
    monkeypatch.setenv(app_config.DATA_DIR_ENV, str(tmp_path))
    assert app_config.default_data_dir() == tmp_path


def test_server_must_be_https(tmp_path):
    assert load_config(data_dir=tmp_path).server_url == "https://autoattn.slichealth.com"
    assert load_config("https://staging.example.test/", tmp_path).server_url == \
        "https://staging.example.test"
    with pytest.raises(ValueError):
        load_config("http://autoattn.slichealth.com", tmp_path)


def test_resources_are_where_the_engine_expects_them(tmp_path):
    cfg = load_config(data_dir=tmp_path)
    assert cfg.signing_key_path.is_file() and cfg.storage_image_path.is_file()
    assert cfg.work_dir == tmp_path / "work"


def test_field_defaults_test_build_and_release(tmp_path):
    assert load_field_defaults(tmp_path / "missing.json") == FieldDefaults()
    release = tmp_path / "field_defaults.json"
    release.write_text(json.dumps({"wifi_password": "test-wifi-pass", "zkt_comm_key": 1979,
                                   "setup_ap_password": "test-hotspot-pass"}), encoding="utf-8")
    defaults = load_field_defaults(release)
    assert not defaults.is_test_build and defaults.zkt_comm_key == 1979
    assert defaults.wifi_ssid == "RE7" and defaults.setup_ap_password == "test-hotspot-pass"


@pytest.mark.parametrize("content", [
    "not json", "[]", json.dumps({"unknown": 1}), json.dumps({"zkt_comm_key": "1979"}),
    json.dumps({"zkt_port": 0}), json.dumps({"wifi_password": 5}),
])
def test_broken_release_defaults_are_refused(tmp_path, content):
    path = tmp_path / "field_defaults.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_field_defaults(path)


class ToolsOK(ToolRunner):
    def __init__(self, outputs=None):
        self.outputs = outputs or {"esptool": "esptool v5.1.0\n5.1.0",
                                   "espefuse": "Usage: espefuse [OPTIONS]\n espefuse v5.1.0"}
        self.calls = []

    def run(self, call, *, cancel=None, on_line=None):
        self.calls.append(call)
        output = self.outputs.get(call.tool)
        return ToolResult(call, 0 if output else 1, output or "ImportError: BitStream", 0.1)


def test_self_test_passes_with_working_tools(tmp_path):
    cfg = load_config(data_dir=tmp_path)
    runner = ToolsOK()
    assert selftest.run(cfg, runner) == []
    assert [c.args for c in runner.calls] == [("version",), ("--help",)]


def test_self_test_reports_a_broken_espefuse(tmp_path):
    cfg = load_config(data_dir=tmp_path)
    problems = selftest.run(cfg, ToolsOK({"esptool": "5.1.0"}))
    assert len(problems) == 1 and "espefuse" in problems[0]


def test_self_test_reports_missing_resources(tmp_path):
    resources = tmp_path / "res"
    resources.mkdir()
    cfg = AppConfig("https://autoattn.slichealth.com", tmp_path / "data", resources, False, "1")
    problems = selftest.run(cfg, ToolsOK())
    assert any("signing key" in p for p in problems)
    assert any("storage image" in p for p in problems)


def test_factory_builds_every_service(tmp_path):
    services = make_services(load_config(data_dir=tmp_path))
    assert isinstance(services, Services)
    for name in ("server", "connectivity", "usb", "boards", "installer", "boot", "diagnosis",
                 "records"):
        assert getattr(services, name) is not None
    assert services.defaults.is_test_build or services.defaults.wifi_password
    assert (tmp_path / "logs" / "installer.log").exists()
    assert hasattr(services.usb, "snapshot") and hasattr(services.usb, "watch")
    import logging
    logging.getLogger("sl_installer").propagate = True


def test_services_without_the_instance_lock_keep_a_running_install_s_files(tmp_path):
    import logging

    running = tmp_path / "work" / "sl-install-running"
    running.mkdir(parents=True)
    (running / "board-key.bin").write_bytes(b"secret" * 6)
    try:
        make_services(load_config(data_dir=tmp_path))  # as the self-test builds them
    finally:
        logging.getLogger("sl_installer").propagate = True
    assert (running / "board-key.bin").exists()


def test_the_window_wipes_every_work_folder_and_holds_the_app_mutex(monkeypatch, tmp_path):
    import sl_installer.core.factory
    import sl_installer.ui.app

    monkeypatch.setenv(app_config.DATA_DIR_ENV, str(tmp_path))
    crashed = tmp_path / "work" / "sl-install-crashed"  # a power cut during the burn
    crashed.mkdir(parents=True)
    (crashed / "board-key.bin").write_bytes(b"secret" * 6)
    mutex = []
    monkeypatch.setattr(sl_installer.core.factory, "make_services", lambda cfg: "services")
    monkeypatch.setattr(entry, "_hold_app_mutex", lambda: mutex.append("held") or 1234)
    monkeypatch.setattr(entry, "_release_app_mutex", lambda handle: mutex.append(handle))

    def run_gui(services):
        assert not InstanceLock(tmp_path / "state" / LOCK_FILE_NAME).acquire()
        assert not crashed.exists() and mutex == ["held"]
        return 0

    monkeypatch.setattr(sl_installer.ui.app, "run_gui", run_gui)
    assert entry.main([]) == 0
    assert mutex == ["held", 1234]  # closed again when the window closed


def test_only_the_window_holds_the_app_mutex(monkeypatch, tmp_path):
    from sl_installer.core import esptool_runner

    held = []
    monkeypatch.setattr(entry, "_hold_app_mutex", lambda: held.append(1))
    monkeypatch.setattr(esptool_runner, "dispatch_tool", lambda args: 0)
    monkeypatch.setattr(entry, "_self_test", lambda server: 0)
    monkeypatch.setattr(entry, "_screenshots", lambda directory: 0)
    assert entry.main(["--tool", "esptool", "version"]) == 0
    assert entry.main(["--self-test"]) == 0
    assert entry.main(["--screenshots", str(tmp_path)]) == 0
    assert held == []


def test_the_app_mutex_is_made_and_closed_through_kernel32(monkeypatch):
    import ctypes

    pytest.importorskip("ctypes.wintypes")
    if sys.platform != "win32":
        assert entry._hold_app_mutex() is None  # Windows only
    calls = []

    class Function:
        def __init__(self, result):
            self.result = result

        def __call__(self, *args):
            calls.append(args)
            return self.result

    class Kernel32:
        CreateMutexW = Function(4321)
        CloseHandle = Function(1)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, use_last_error: Kernel32(),
                        raising=False)
    handle = entry._hold_app_mutex()
    entry._release_app_mutex(handle)
    assert handle == 4321 and calls == [(None, False, entry.APP_MUTEX), (4321,)]


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows named mutex")
def test_setup_sees_the_app_mutex_only_while_the_window_holds_it():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def setup_sees_it() -> bool:  # what Inno Setup does for AppMutex
        found = kernel32.OpenMutexW(0x00100000, False, entry.APP_MUTEX)  # SYNCHRONIZE
        if found:
            kernel32.CloseHandle(found)
        return bool(found)

    handle = entry._hold_app_mutex()
    try:
        assert handle and setup_sees_it()
    finally:
        entry._release_app_mutex(handle)
    assert not setup_sees_it()


def test_bench_parser_and_confirmation(monkeypatch, capsys):
    args = bench.parser().parse_args(["install", "--family", "zkt", "--zone-id", "z",
                                      "--bundle-id", "zkt-2.9.1-e89380a1"])
    assert args.device_id == "1" and args.family == "zkt"
    monkeypatch.setattr("builtins.input", lambda prompt="": "ac:27:6e:a4:3d:31")
    with pytest.raises(SystemExit) as stop:
        bench._confirm("Type the board ID.", support.MAC)
    assert stop.value.code == 1
    assert "Nothing was written" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt="": support.MAC)
    bench._confirm("Type the board ID.", support.MAC)


def test_bench_says_to_put_a_board_running_other_software_into_setup_mode(capsys):
    from types import SimpleNamespace

    from sl_installer.core.diagnosis import Diagnoser
    from sl_installer.core.usb import UsbWatcher

    port = SimpleNamespace(device="/dev/cu.usbmodem1234561", vid=0x303A, pid=0x4001,
                           serial_number="123456", description="Espressif Device")
    inspected = []

    class Fake:
        usb = UsbWatcher(lambda: [port], support.FakeClock(), tree_macs=dict)
        diagnosis = Diagnoser()

        class boards:
            inspect = staticmethod(lambda candidate, cancel: inspected.append(candidate))

    with pytest.raises(SystemExit) as stop:
        bench.cmd_inspect(Fake, bench.parser().parse_args(["inspect"]))
    assert stop.value.code == 2 and inspected == []  # nothing was read from that port
    out = capsys.readouterr().out
    assert "Put the board into setup mode  (SETUP_MODE_NEEDED)" in out
    assert "Hold down the BOOT button on the board." in out
    assert ("/dev/cu.usbmodem1234561 (Espressif Device): Espressif USB 303A:4001, not the "
            "board's own USB-Serial/JTAG 303A:1001.") in out
    assert out.rstrip().endswith("Put the board into setup mode, then run this command again.")


def test_another_python_can_run_the_tools_from_a_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv(app_config.TOOL_PYTHON_ENV, raising=False)
    assert load_config(data_dir=tmp_path).tool_python is None
    monkeypatch.setenv(app_config.TOOL_PYTHON_ENV, "/opt/py312/bin/python3")
    assert load_config(data_dir=tmp_path).tool_python == "/opt/py312/bin/python3"
    assert load_config(data_dir=tmp_path, tool_python="/usr/bin/python3.11").tool_python == \
        "/usr/bin/python3.11"
    # The frozen app is its own esptool: nothing may point it at another program.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert load_config(data_dir=tmp_path, tool_python="/tmp/x").tool_python is None


def test_factory_runs_the_tools_with_the_configured_python(monkeypatch, tmp_path):
    import logging

    from sl_installer.core.esptool_runner import ToolCall

    monkeypatch.setenv(app_config.TOOL_PYTHON_ENV, "/opt/py312/bin/python3")
    try:
        services = make_services(load_config(data_dir=tmp_path))
        runner = services.boards._runner  # type: ignore[attr-defined]  # every board call
        assert runner.command(ToolCall("esptool", ("version",), "x")) == [
            "/opt/py312/bin/python3", "-m", "esptool", "version"]
    finally:
        logging.getLogger("sl_installer").propagate = True


@pytest.mark.parametrize("command", [["inspect"], ["install", "--family", "zkt", "--zone-id",
                                                   "LF-ZONE-X-01"]])
def test_bench_refuses_tools_that_are_not_5_1_0(monkeypatch, tmp_path, capsys, command):
    built = []
    monkeypatch.setattr(bench, "make_services", lambda cfg: built.append(cfg))
    monkeypatch.setattr(bench, "tool_check",
                        lambda cfg: ["esptool is not version 5.1.0 (it says 'esptool.py v4.7.0')"])
    assert bench.main(["--data-dir", str(tmp_path)] + command) == 5
    out = capsys.readouterr().out
    assert "Nothing was sent to the board" in out and "--tool-python" in out
    assert built == []  # never got as far as the board


def test_bench_tool_check_runs_the_chosen_python(tmp_path):
    args = bench.parser().parse_args(["--tool-python", sys.executable, "inspect"])
    cfg = load_config(data_dir=tmp_path, tool_python=args.tool_python)
    assert cfg.tool_python == sys.executable
    assert bench.tool_check(cfg) == []  # this test Python has esptool 5.1.0


def test_bench_watch_needs_no_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, "tool_check", lambda cfg: 1 / 0)
    monkeypatch.setattr(bench, "make_services", lambda cfg: "services")
    monkeypatch.setattr(bench, "cmd_watch", lambda services, args: 0)
    assert bench.main(["--data-dir", str(tmp_path), "watch"]) == 0


def test_bench_install_stops_without_the_typed_board_id(monkeypatch, tmp_path):
    class Board:
        port, mac, chip, flash_size_mb, psram_mb = support.PORT, support.MAC, "ESP32-S3", 16, 8
        hardware, security, secure_download_mode = (HardwareVerdict.OK, SecurityState.BLANK,
                                                    False)
        flash_id, chip_revision, hardware_detail = "c8 40 18", 2, ""

    class Fake:
        defaults = FieldDefaults(wifi_password="test-wifi-pass", zkt_comm_key=1979)
        ran = []

        class server:
            @staticmethod
            def login(user, password):
                return user

            @staticmethod
            def bundles(family):
                from sl_installer.core.api import BundleInfo, Family
                return [BundleInfo("zkt-2.9.1-e89380a1", Family.ZKT, "ZKT 2.9.1", "2.9.1",
                                   True, "AVAILABLE")]

            @staticmethod
            def device(mac):
                from sl_installer.core.api import DeviceRecord
                return DeviceRecord(known=False, mac=mac)

        class usb:
            @staticmethod
            def snapshot():
                return PresenceReport(PresenceState.ONE_BOARD, board=support.FakeUsb().candidate())

        class boards:
            @staticmethod
            def inspect(candidate, cancel):
                return Board()

        class installer:
            @staticmethod
            def run(*args):
                Fake.ran.append(args)

    monkeypatch.setattr("getpass.getpass", lambda prompt="": "test-admin-pass")
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    args = bench.parser().parse_args(["install", "--username", "u", "--family", "zkt",
                                      "--zone-id", "lf-zone-x-01", "--bundle-id",
                                      "zkt-2.9.1-e89380a1"])
    with pytest.raises(SystemExit):
        bench.cmd_install(Fake, args)
    assert Fake.ran == []


@pytest.mark.parametrize("allowed", [True, False])
def test_bench_settings_change_needs_no_bundle(monkeypatch, capsys, allowed):
    from sl_installer.core.api import DeviceRecord

    class Board:
        port, mac, chip, flash_size_mb, psram_mb = support.PORT, support.MAC, "ESP32-S3", None, None
        hardware, security, secure_download_mode = (HardwareVerdict.NOT_CHECKED,
                                                    SecurityState.SECURED_STATE_LIFE, True)
        flash_id, chip_revision, hardware_detail = None, None, ""

    asked = []

    class Fake:
        defaults = FieldDefaults(wifi_password="test-wifi-pass", zkt_comm_key=1979)

        class server:
            login = staticmethod(lambda user, password: user)
            bundles = staticmethod(lambda family: asked.append(family) or [])
            device = staticmethod(lambda mac: DeviceRecord(
                known=True, mac=mac, settings_change_allowed=allowed,
                settings_change_reason=None if allowed else "Set up before this app."))

        class usb:
            snapshot = staticmethod(lambda: PresenceReport(
                PresenceState.ONE_BOARD, board=support.FakeUsb().candidate()))

        class boards:
            inspect = staticmethod(lambda candidate, cancel: Board())

        class installer:
            run = staticmethod(lambda *args: asked.append("ran"))

    monkeypatch.setattr("getpass.getpass", lambda prompt="": "test-admin-pass")
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    args = bench.parser().parse_args(["install", "--username", "u", "--family", "zkt",
                                      "--zone-id", "lf-zone-x-01"])
    assert args.bundle_id is None
    if allowed:
        with pytest.raises(SystemExit):  # stops at the typed ZONE ID, after the summary
            bench.cmd_install(Fake, args)
        assert "stays the same" in capsys.readouterr().out
    else:
        assert bench.cmd_install(Fake, args) == 2
        out = capsys.readouterr().out
        assert "This app cannot change this board's settings" in out and "Set up before" in out
    assert asked == []  # no board software asked for, nothing run


def bench_services(first, restart):
    """Services for the bench's install path: a new board, then the two watched starts."""
    from sl_installer.core.api import (BundleInfo, DeviceRecord, Family, InstallSummary,
                                       ReplugPhase)
    bundle = BundleInfo("zkt-2.9.1-e89380a1", Family.ZKT, "ZKT 2.9.1", "2.9.1", True,
                        "AVAILABLE")
    seen = SimpleNamespace(starts=[], events=[], saved=[], firsts=[])

    class Board:
        port, mac, chip, flash_size_mb, psram_mb = support.PORT, support.MAC, "ESP32-S3", 16, 8
        hardware, security, secure_download_mode = (HardwareVerdict.OK, SecurityState.BLANK,
                                                    False)
        flash_id, chip_revision, hardware_detail = "c8 40 18", 2, ""

    def watched(name, report):
        def wait(mac, expect, on_phase, on_event, cancel, timeout_s=120.0, transcript=None,
                 **restart):
            seen.starts.append((name, mac, transcript))
            if "first" in restart:
                seen.firsts.append(restart.pop("first"))
            assert not restart, restart
            for phase in ReplugPhase:
                on_phase(phase)
            return report
        return staticmethod(wait)

    class Fake:
        defaults = FieldDefaults(wifi_password="test-wifi-pass", zkt_comm_key=1979)

        class server:
            login = staticmethod(lambda user, password: user)
            bundles = staticmethod(lambda family: [bundle])
            device = staticmethod(lambda mac: DeviceRecord(known=False, mac=mac))
            report = staticmethod(lambda request_id, mac, kind, detail: seen.events.append(
                (request_id, kind, detail)))

        class usb:
            snapshot = staticmethod(lambda: PresenceReport(
                PresenceState.ONE_BOARD, board=support.FakeUsb().candidate()))

        class boards:
            inspect = staticmethod(lambda candidate, cancel: Board())

        class installer:
            run = staticmethod(lambda mode, board, config, on_step, cancel: InstallSummary(
                "sli-bench-0001", mode, board.mac, config, bundle, "2026-11-02T09:00:00Z",
                "2026-11-02T09:03:00Z"))

        class boot:
            wait_first_boot = watched("first start", first)
            wait_restart = watched("restart check", restart)

        class records:
            save_install_record = staticmethod(
                lambda summary, boot, restart=None: seen.saved.append((boot, restart))
                or "setup-record.txt")

    return Fake, seen


def bench_install(monkeypatch, services) -> int:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "test-admin-pass")
    monkeypatch.setattr("builtins.input", lambda prompt="": support.MAC)  # the typed board ID
    args = bench.parser().parse_args(["install", "--username", "u", "--family", "zkt",
                                      "--zone-id", "lf-zone-x-01", "--bundle-id",
                                      "zkt-2.9.1-e89380a1"])
    return bench.cmd_install(services, args)


FIRST_OK = BootReport(BootOutcome.OK, "LF-ZONE-X-01", "1", "2.9.1",
                      advice=texts.boot_ok("LF-ZONE-X-01", "1", "2.9.1"))


@pytest.mark.parametrize("restart, code, kind", [
    (BootReport(BootOutcome.OK, "LF-ZONE-X-01", "1", "2.9.1",
                advice=texts.settings_kept("LF-ZONE-X-01", "1", "2.9.1")), 0, "RESTART_OK"),
    (BootReport(BootOutcome.SETTINGS_LOST, advice=texts.SETTINGS_LOST_ON_RESTART), 4,
     "RESTART_FAILED"),
], ids=["kept", "lost"])
def test_bench_install_checks_that_the_board_keeps_its_settings(monkeypatch, capsys, restart,
                                                                code, kind):
    services, seen = bench_services(FIRST_OK, restart)
    assert bench_install(monkeypatch, services) == code
    out = capsys.readouterr().out
    # Both starts are watched, the second after one more replug, with the install's log.
    assert seen.starts == [("first start", support.MAC, "sli-bench-0001"),
                           ("restart check", support.MAC, "sli-bench-0001")]
    assert seen.firsts == [FIRST_OK]  # the restart check compares with the first start
    assert f"One more check. {texts.RESTART_REPLUG}" in out
    # A good first start gets no words of success before the restart check.
    before = out.split("One more check")[0]
    assert "started correctly" not in before and "BOOT_OK" not in before
    assert "First start: ok zone=LF-ZONE-X-01 device=1 version=2.9.1" in before
    assert ">> Unplug the board again now." in out and ">> Watching the second start..." in out
    assert [(request, sent) for request, sent, _detail in seen.events] == [
        ("sli-bench-0001", "BOOT_OK"), ("sli-bench-0001", kind)]
    assert seen.events[1][2]["settings_kept"] is (code == 0)
    assert seen.saved == [(FIRST_OK, restart)]
    # The bench says the board is ready only when it kept its settings.
    assert (texts.SETTINGS_KEPT in out) is (code == 0)
    if code:
        assert texts.SETTINGS_LOST_ON_RESTART.title in out


def test_bench_install_watches_no_restart_after_a_failed_first_start(monkeypatch, capsys):
    first = BootReport(BootOutcome.NO_OUTPUT, advice=texts.NO_OUTPUT)
    services, seen = bench_services(first, None)
    assert bench_install(monkeypatch, services) == 4
    out = capsys.readouterr().out
    assert [name for name, _mac, _transcript in seen.starts] == ["first start"]
    assert [sent for _request, sent, _detail in seen.events] == ["BOOT_FAILED"]
    assert seen.saved == [(first, None)]
    assert "One more check" not in out and texts.SETTINGS_KEPT not in out


def test_factory_keeps_console_logs_next_to_the_app_log(tmp_path):
    import logging
    try:
        services = make_services(load_config(data_dir=tmp_path))
    finally:
        logging.getLogger("sl_installer").propagate = True
    assert services.boot._console_logs == tmp_path / "logs"  # type: ignore[attr-defined]
