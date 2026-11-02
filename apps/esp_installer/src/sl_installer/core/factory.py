"""Builds the real Services the screens use (fakes.py builds scripted ones)."""
from __future__ import annotations

from ..config import AppConfig, load_field_defaults
from .add_client import AddClient, LoginThrottle
from .api import FieldDefaults, Services
from .board import BoardInspector
from .bundles import BundleStore, Verifier, load_public_key
from .clock import Clock
from .connectivity import Connectivity
from .console import BootWatcher
from .diagnosis import Diagnoser
from .esptool_runner import SubprocessRunner
from .installer import Installer, cleanup_stale_work
from .logging_setup import get_logger, register_secret, setup_logging
from .records import Records
from .usb import UsbWatcher

log = get_logger("factory")


def field_defaults(cfg: AppConfig) -> FieldDefaults:
    try:
        return load_field_defaults(cfg.field_defaults_path)
    except ValueError as error:
        log.error("ignoring field defaults: %s", error)
        return FieldDefaults()


def make_services(cfg: AppConfig) -> Services:
    cfg.ensure_dirs()
    setup_logging(cfg.logs_dir)
    # Only work folders older than any install: the self-test (and the bench's watch and
    # diagnose) build these services without the instance lock. The window, which holds
    # it, wipes the rest (__main__._run_gui).
    cleanup_stale_work(cfg.work_dir)
    log.info("State Life Device Installer %s, server %s", cfg.app_version, cfg.server_url)
    defaults = field_defaults(cfg)
    for secret in (defaults.wifi_password, defaults.setup_ap_password):
        register_secret(secret)
    placeholders = (defaults.wifi_ssid,) if defaults.wifi_ssid else ()

    clock = Clock()
    runner = SubprocessRunner(cfg.work_dir / "tools", frozen=cfg.frozen,
                              executable=cfg.tool_python)
    if cfg.tool_python:
        log.info("tools run with %s", cfg.tool_python)
    key = load_public_key(cfg.signing_key_path)
    store = BundleStore(cfg.bundles_dir, Verifier(manifest_key=key),
                        storage_gz=cfg.storage_image_path)
    server = AddClient(cfg.server_url, app_version=cfg.app_version, store=store, clock=clock,
                       throttle=LoginThrottle(clock, cfg.state_dir / "sign-in-pause.json"))
    usb = UsbWatcher(clock=clock)
    return Services(
        server=server,
        connectivity=Connectivity(cfg.server_url),
        usb=usb,
        boards=BoardInspector(runner, temp_root=cfg.work_dir),
        installer=Installer(runner=runner, server=server, usb=usb, store=store,
                            app_version=cfg.app_version, clock=clock, temp_root=cfg.work_dir),
        # Each watched start's lines go, redacted, to a per-install file next to installer.log.
        boot=BootWatcher(usb, clock, placeholder_ssids=placeholders, console_logs=cfg.logs_dir),
        diagnosis=Diagnoser(placeholders),
        records=Records(cfg.records_dir, cfg.logs_dir, cfg.app_version),
        defaults=defaults,
        app_version=cfg.app_version,
    )
