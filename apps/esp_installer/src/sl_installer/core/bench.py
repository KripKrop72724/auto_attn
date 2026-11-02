"""Bench tool: drive the engine from a terminal with a real board.

    python -m sl_installer.core.bench inspect
    python -m sl_installer.core.bench watch [--console] [--seconds N]
    python -m sl_installer.core.bench diagnose [--seconds N] [--username U]
    python -m sl_installer.core.bench install --family zkt --zone-id Z --device-id 1 ...

``install`` signs in to ADD, inspects the board and shows exactly what it will do.
It goes on only after you type the board ID (new board or resume) or the ZONE ID
(changing settings): the key burn is permanent and a settings change loses the
board's unsent records. Passwords are asked for, never taken from the command line.

Every command line the engine builds is esptool 5.1.0's, so ``inspect`` and
``install`` first check that the tools say 5.1.0 and stop before touching the board
when they do not. esptool 5.1.0 needs Python 3.10+: on a Python 3.9 bench (the Mac's
own python3 has esptool 4.7), add ``--tool-python /path/to/python3.12`` (or set
SL_INSTALLER_TOOL_PYTHON) to run the tools with a Python that has esptool 5.1.0.

``inspect`` and ``install`` hold the app's instance lock (single_instance.py) for
the same data folder: they stop while the app's window is open there, and the
window does not open while they run.
"""
from __future__ import annotations

import argparse
import getpass
import sys
import threading
from pathlib import Path
from typing import List, Optional, Sequence

from ..config import TOOL_PYTHON_ENV, AppConfig, load_config
from . import texts
from .api import (STEP_LABELS, Advice, BoardInfo, CancelToken, ConsoleEvent, Family,
                  HikSettings, InstallConfig, InstallerError, InstallMode, PresenceReport,
                  PresenceState, ReplugPhase, SecurityState, Services, StepProgress, UsbKind,
                  ZktSettings, steps_for)
from .esptool_runner import TOOL_VERSION, SubprocessRunner, tool_version_problems
from .factory import make_services
from .naming import hotspot_name, public_name_preview
from .single_instance import LOCK_FILE_NAME, InstanceLock
from .usb import usb_id

_MODES = {
    SecurityState.BLANK: InstallMode.NEW_BOARD,
    SecurityState.KEY_BURNED_NOT_STARTED: InstallMode.RESUME,
    SecurityState.SECURED_STATE_LIFE: InstallMode.CHANGE_SETTINGS,
}


def _print_advice(items: Sequence[Advice]) -> None:
    for advice in items:
        print(f"\n[{advice.severity.value.upper()}] {advice.title}  ({advice.code})")
        print(f"  {advice.what_happened}")
        for step in advice.what_to_do:
            print(f"   - {step}")


def _print_board(board: BoardInfo) -> None:
    print(f"Board ID:        {board.mac}")
    print(f"Port:            {board.port}")
    print(f"Chip:            {board.chip}")
    print(f"Flash / PSRAM:   {board.flash_size_mb} MB / {board.psram_mb} MB")
    print(f"Hardware:        {board.hardware.value}")
    print(f"Security:        {board.security.value}")
    print(f"Download mode:   {'secure' if board.secure_download_mode else 'normal'}")
    print(f"Hotspot:         {hotspot_name(board.mac)}")


def _one_board(services: Services) -> PresenceReport:
    report = services.usb.snapshot()
    if report.state is not PresenceState.ONE_BOARD or report.board is None:
        _print_advice(services.diagnosis.diagnose(None, report, None, ()))
        if report.state is PresenceState.OTHER_USB_MODE:
            # The advice names the window's Next and Help; here the next run reads the board.
            for candidate in report.candidates:
                if candidate.kind is UsbKind.OTHER:
                    what = f" ({candidate.description})" if candidate.description else ""
                    print(f"\n{candidate.port}{what}: Espressif USB {usb_id(candidate)}, not "
                          "the board's own USB-Serial/JTAG 303A:1001.")
            print("Put the board into setup mode, then run this command again.")
        raise SystemExit(2)
    return report


def _confirm(prompt: str, expected: str) -> None:
    print()
    typed = input(f"{prompt}\nType it exactly to continue, or press Enter to stop: ").strip()
    if typed != expected:
        print("Stopped. Nothing was written to the board.")
        raise SystemExit(1)


def _sign_in(services: Services, username: Optional[str]) -> None:
    user = username or input("ADD username: ").strip()
    services.server.login(user, getpass.getpass("ADD password: "))


def cmd_inspect(services: Services, args: argparse.Namespace) -> int:
    report = _one_board(services)
    board = services.boards.inspect(report.board, CancelToken())
    _print_board(board)
    _print_advice(services.diagnosis.diagnose(board, report, None, ()))
    print("\nThe board is now in download mode. Unplug and replug it to start it.")
    return 0


def cmd_watch(services: Services, args: argparse.Namespace) -> int:
    cancel = CancelToken()
    timer = threading.Timer(args.seconds, cancel.cancel)
    timer.start()
    try:
        if args.console:
            report = _one_board(services)
            print(f"Watching the console of {report.board.port} for {args.seconds:.0f} s")
            services.boot.watch_console(report.board, _print_event, cancel)
        else:
            services.usb.watch(lambda r: print(
                f"{r.state.value:15} {r.seconds_in_state:6.1f}s "
                f"period={r.loop_period_s} {[c.port + ':' + c.kind.value for c in r.candidates]}"),
                cancel)
    except KeyboardInterrupt:
        cancel.cancel()
    finally:
        timer.cancel()
    return 0


def _print_event(event: ConsoleEvent) -> None:
    if event.kind.value == "other" and not event.data:
        print(f"{event.at:7.1f}  .. {event.raw}")
    else:
        print(f"{event.at:7.1f}  {event.kind.value:22} {event.data}  | {event.raw}")


def cmd_diagnose(services: Services, args: argparse.Namespace) -> int:
    report = _one_board(services)
    events: List[ConsoleEvent] = []
    cancel = CancelToken()
    timer = threading.Timer(args.seconds, cancel.cancel)
    timer.start()
    print(f"Reading the console for {args.seconds:.0f} s (no reset is sent)...")

    def keep(event: ConsoleEvent) -> None:
        events.append(event)
        _print_event(event)

    try:
        services.boot.watch_console(report.board, keep, cancel)
    except KeyboardInterrupt:
        cancel.cancel()
    finally:
        timer.cancel()
    device = None
    if args.username and report.board.mac:
        _sign_in(services, args.username)
        device = services.server.device(report.board.mac)
        print(f"\nADD: known={device.known} zone={device.zone_id} online={device.online} "
              f"status={device.status_text}")
    _print_advice(services.diagnosis.diagnose(None, services.usb.snapshot(), device, events))
    return 0


def _install_config(services: Services, args: argparse.Namespace) -> InstallConfig:
    defaults = services.defaults
    family = Family(args.family)
    wifi_ssid = args.wifi_ssid or defaults.wifi_ssid
    wifi_password = defaults.wifi_password or getpass.getpass(f"Wi-Fi password for {wifi_ssid}: ")
    zkt = hik = None
    if family is Family.ZKT:
        zkt = ZktSettings(port=args.zkt_port or defaults.zkt_port,
                          comm_key=args.comm_key if args.comm_key is not None
                          else defaults.zkt_comm_key,
                          preferred_ip=args.preferred_ip or defaults.zkt_preferred_ip)
    else:
        if not (args.hik_host and args.hik_serial):
            raise SystemExit("Hikvision needs --hik-host and --hik-serial")
        hik = HikSettings(host=args.hik_host,
                          password=getpass.getpass("Hikvision machine password: "),
                          username=args.hik_username or defaults.hik_username,
                          port=args.hik_port or defaults.hik_port,
                          transport=args.hik_transport or defaults.hik_transport,
                          expected_serial=args.hik_serial, model_key=args.hik_model)
    zone_id = args.zone_id.strip().upper()
    return InstallConfig(family=family, zone_id=zone_id, zone_name=args.zone_name or zone_id,
                         device_id=args.device_id, wifi_ssid=wifi_ssid,
                         wifi_password=wifi_password,
                         bundle_id=args.bundle_id or "", zkt=zkt, hik=hik)


def cmd_install(services: Services, args: argparse.Namespace) -> int:
    _sign_in(services, args.username)
    report = _one_board(services)
    board = services.boards.inspect(report.board, CancelToken())
    _print_board(board)
    mode = _MODES.get(board.security)
    if mode is None:
        _print_advice(services.diagnosis.diagnose(board, report, None, ()))
        return 2
    config = _install_config(services, args)
    device = services.server.device(board.mac)
    if mode is InstallMode.CHANGE_SETTINGS:
        # A settings change writes no board software, so it needs no --bundle-id.
        if not device.settings_change_allowed:
            _print_advice([texts.SETTINGS_CHANGE_NOT_SUPPORTED])
            if device.settings_change_reason:
                print(f"  (head office: {device.settings_change_reason})")
            return 2
        firmware = "stays the same (settings, empty storage and a blank redo area only)"
    else:
        bundles = {b.bundle_id: b for b in services.server.bundles(config.family)}
        if config.bundle_id not in bundles:
            print("Firmware on offer:", ", ".join(bundles) or "none")
            return 2
        firmware = f"{bundles[config.bundle_id].label} ({config.bundle_id})"
    preview = public_name_preview(config.zone_name)
    print(f"\nWhat will happen: {mode.value}")
    print(f"  Firmware:  {firmware}")
    print(f"  Place:     {config.zone_id} / {config.zone_name} -> {preview.name} ({preview.group})")
    print(f"  Device ID: {config.device_id}   Wi-Fi: {config.wifi_ssid}")
    print(f"  ADD:       known={device.known} family={device.family} "
          f"ever_online={device.ever_online}")
    print("  Steps:     " + " > ".join(STEP_LABELS[s] for s in steps_for(mode)))
    if mode is InstallMode.CHANGE_SETTINGS:
        if device.ever_online:
            print("\nWARNING: this board has been online at a site. Unsent punches on it "
                  "will be lost.")
        _confirm(f"This rewrites the settings of the board for zone {config.zone_id}.",
                 config.zone_id)
    else:
        _confirm(f"This prepares board {board.mac} for State Life for good "
                 "(the security key burn cannot be undone). Type the board ID.", board.mac)

    def on_step(progress: StepProgress) -> None:
        fraction = "" if progress.fraction is None else f"{progress.fraction * 100:5.1f}%"
        print(f"  {STEP_LABELS[progress.step]:40} {fraction} {progress.detail}", flush=True)

    try:
        summary = services.installer.run(mode, board, config, on_step, CancelToken())
    except InstallerError as error:
        print(f"\nInstall stopped (key burned: {error.irreversible_done}).")
        _print_advice([error.advice])
        return 3
    print("\nWritten. Now unplug the board, wait 3 seconds and plug it back in.")

    def on_phase(phase: ReplugPhase) -> None:
        print({ReplugPhase.WAITING_UNPLUG: ">> Unplug the board now.",
               ReplugPhase.WAITING_REPLUG: ">> Wait 3 seconds, then plug it back in.",
               ReplugPhase.WATCHING: ">> Watching the first start..."}[phase], flush=True)

    boot = services.boot.wait_first_boot(board.mac, summary.config, on_phase, _print_event,
                                         CancelToken(), timeout_s=args.boot_seconds)
    print(f"\nFirst start: {boot.outcome.value} zone={boot.zone_id_seen} "
          f"device={boot.device_id_seen} version={boot.app_version_seen}")
    if boot.advice:
        _print_advice([boot.advice])
    print("Record saved:", services.records.save_install_record(summary, boot))
    return 0 if boot.outcome.value == "ok" else 4


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="python -m sl_installer.core.bench")
    main.add_argument("--server", help="ADD server (tests only)")
    main.add_argument("--data-dir", type=Path)
    main.add_argument("--tool-python", metavar="PYTHON",
                      help=f"a Python that has esptool {TOOL_VERSION}, to run the tools when "
                           f"this one has another version (or set {TOOL_PYTHON_ENV})")
    commands = main.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect")
    watch = commands.add_parser("watch")
    watch.add_argument("--console", action="store_true")
    watch.add_argument("--seconds", type=float, default=60.0)
    diagnose = commands.add_parser("diagnose")
    diagnose.add_argument("--seconds", type=float, default=150.0)
    diagnose.add_argument("--username")
    install = commands.add_parser("install")
    install.add_argument("--username")
    install.add_argument("--family", choices=("zkt", "hikvision"), required=True)
    install.add_argument("--zone-id", required=True)
    install.add_argument("--zone-name")
    install.add_argument("--device-id", default="1")
    install.add_argument("--bundle-id", help="the firmware to write (not needed to change the "
                                             "settings of a finished board)")
    install.add_argument("--wifi-ssid")
    install.add_argument("--zkt-port", type=int)
    install.add_argument("--comm-key", type=int)
    install.add_argument("--preferred-ip")
    install.add_argument("--hik-host")
    install.add_argument("--hik-serial")
    install.add_argument("--hik-username")
    install.add_argument("--hik-port", type=int)
    install.add_argument("--hik-transport", choices=("http_digest",))  # HTTPS needs a CA
    install.add_argument("--hik-model", default="ds-k1t342efwx")
    install.add_argument("--boot-seconds", type=float, default=150.0)
    return main


_TOOL_COMMANDS = ("inspect", "install")


def tool_check(cfg: AppConfig) -> List[str]:
    """Problems with the tools ``cfg`` runs; both must say TOOL_VERSION (no port is opened)."""
    runner = SubprocessRunner(cfg.work_dir / "tool-check", frozen=cfg.frozen,
                              executable=cfg.tool_python)
    return tool_version_problems(runner)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    cfg = load_config(args.server, args.data_dir, tool_python=args.tool_python)
    if args.command in _TOOL_COMMANDS:
        problems = tool_check(cfg)
        if problems:
            for problem in problems:
                print(f"Stopped: {problem}")
            print(f"Nothing was sent to the board. Run the tools with a Python that has esptool "
                  f"{TOOL_VERSION}: add --tool-python PYTHON or set {TOOL_PYTHON_ENV}.")
            return 5
    # The app's own lock: its window may be using the board, and when it opens it wipes
    # every work folder, a running install's security key file too.
    lock = InstanceLock(cfg.state_dir / LOCK_FILE_NAME)
    if args.command in _TOOL_COMMANDS and not lock.acquire():
        print("Stopped: the app is open on this computer. Close its window and run this "
              "again. Nothing was sent to the board.")
        return 6
    try:
        services = make_services(cfg)
        handlers = {"inspect": cmd_inspect, "watch": cmd_watch, "diagnose": cmd_diagnose,
                    "install": cmd_install}
        try:
            return handlers[args.command](services, args)
        except InstallerError as error:
            _print_advice([error.advice])
            if error.cause:
                print(f"  (detail: {error.cause})")
            return 3
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
