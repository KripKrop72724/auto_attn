"""Runs one install, step by step, with the board-safety rules of design section 4.

PREPARE_FILES checks head office, refuses a family switch ADD would reject,
downloads and verifies the bundle, asks ADD for this board's encrypted settings
with a one-time key and opens them. The flash plan (flashplan.py) then runs one
call at a time. Before each call the board is found again by its board ID, so a
re-enumerated port is followed and a swapped board is caught; after each call its
output is checked (board ID, lock state, write and verify counts).

The security key file exists only for the burn call, in a private temporary
directory, and is overwritten and deleted straight after it. Cancel works until
the key step starts. After the burn every failure says the board must be resumed.
Windows is kept awake for the whole run (keepawake.py).
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Tuple

from . import texts, validation
from .api import (Advice, BoardInfo, BootReport, BundleInfo, CancelToken, DeviceRecord,
                  EncryptedPackage, Family, HardwareVerdict, InstallConfig, InstallerError,
                  InstallMode, InstallSummary, LocalBundle, PackageRequest, ProgressFn,
                  SecurityState, ServerService, StepId, StepProgress, UsbCandidate)
from .board import (classify_efuses, classify_sdm, efuse_mac, key0_locked_hmac,
                    parse_banner, parse_security_info, read_summary_file)
from .bundles import BundleStore, flash_ranges
from .clock import Clock, iso_utc
from .envelope import EnvelopeError, OneTimeKey, OpenedPackage, open_package
from .esptool_runner import (ESPEFUSE, PROGRESS_LINE, ToolCall, ToolResult, ToolRunner,
                             advice_for, failure_cause)
from .flashplan import (NVS_OFFSET, NVS_SIZE, STORAGE_OFFSET, STORAGE_SIZE, FlashPlan,
                        FlashRange, PlanError, PlannedCall, Purpose, build_plan, espefuse_args,
                        with_port)
from .keepawake import keep_awake
from .logging_setup import get_logger, register_secret

log = get_logger("installer")

PORT_WAIT_S = 15.0
_WROTE = re.compile(r"^Wrote \d+ bytes", re.M)
_HASH_OK = re.compile(r"^Hash of data verified\.", re.M)
_VERIFY_OK = re.compile(r"Verification successful \(digest matched\)\.")
_VERIFY_BAD = re.compile(r"Verification failed")

_ALLOWED = {
    InstallMode.NEW_BOARD: (SecurityState.BLANK,),
    InstallMode.RESUME: (SecurityState.KEY_BURNED_NOT_STARTED,),
    InstallMode.CHANGE_SETTINGS: (SecurityState.SECURED_STATE_LIFE,),
}


class PortFinder(Protocol):
    def find(self, mac: str) -> Tuple[Optional[UsbCandidate], int]: ...


def new_request_id() -> str:
    return f"sli-{uuid.uuid4().hex}"


def write_private(path: Path, data: bytes) -> None:
    """Create ``path`` readable only by this user, write and flush it to disk."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def wipe(path: Path) -> None:
    """Overwrite a secret file with random bytes, then delete it."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    try:
        with path.open("r+b") as handle:
            handle.write(os.urandom(max(size, 1)))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        log.warning("could not overwrite %s: %s", path.name, error)
    try:
        path.unlink()
    except OSError as error:
        log.error("could not delete %s: %s", path.name, error)


STALE_WORK_AGE_S = 6 * 3600


def cleanup_stale_work(root: Path, now: Optional[float] = None) -> None:
    """Wipe settings and key files a crashed run left behind.

    Only folders older than any install could take are touched, so a second window
    of the app can never remove a running install's files.
    """
    if not root.is_dir():
        return
    moment = time.time() if now is None else now
    for child in root.glob("sl-install-*"):
        try:
            if not child.is_dir() or moment - child.stat().st_mtime < STALE_WORK_AGE_S:
                continue
        except OSError:
            continue
        for path in child.rglob("*"):
            if path.is_file():
                wipe(path)
        shutil.rmtree(child, ignore_errors=True)


@dataclass
class _Run:
    mode: InstallMode
    board: BoardInfo
    config: InstallConfig
    request_id: str
    work: Path
    on_step: ProgressFn
    cancel: CancelToken
    key_burned: bool = False
    burn_started: bool = False
    hmac_key: Optional[bytes] = None
    secret_files: List[Path] = field(default_factory=list)
    step: StepId = StepId.PREPARE_FILES


class _Progress:
    """Turns esptool lines into StepProgress for one step, without flooding the screen."""

    def __init__(self, run: _Run, step: StepId, start: float, end: float,
                 ranges: Tuple[FlashRange, ...], clock: Clock, verify: bool = False) -> None:
        self._run, self._step, self._start, self._end = run, step, start, end
        self._ranges = tuple(sorted(ranges, key=lambda item: item.offset))
        self._total = sum(item.size for item in ranges) or 1
        self._verify = verify
        self._verified = 0
        self._clock = clock
        self._last = -1.0
        self._last_at = 0.0

    def line(self, text: str) -> None:
        done: Optional[int] = None
        if self._verify:
            if _VERIFY_OK.search(text) and self._verified < len(self._ranges):
                self._verified += 1
                done = sum(item.size for item in self._ranges[:self._verified])
        else:
            found = PROGRESS_LINE.search(text)
            if found:
                done = self._written(int(found.group(1), 16))
        if done is not None:
            self.emit(done)

    def _written(self, address: int) -> int:
        completed = 0
        for item in self._ranges:
            if address >= item.end:
                completed += item.size
            elif address >= item.offset:
                completed += address - item.offset
                break
            else:
                break
        return max(0, min(self._total, completed))

    def emit(self, done: int, force: bool = False) -> None:
        fraction = self._start + (self._end - self._start) * done / self._total
        now = self._clock.monotonic()
        if not force and fraction - self._last < 0.005 and now - self._last_at < 0.5:
            return
        self._last, self._last_at = fraction, now
        detail = f"{done / 1e6:.1f} of {self._total / 1e6:.1f} MB" if self._total > 1 else ""
        try:
            self._run.on_step(StepProgress(self._step, min(1.0, fraction), detail))
        except Exception as error:  # a screen problem must not break a write
            log.warning("progress listener failed: %s", error)


class Installer:
    """InstallService."""

    def __init__(self, *, runner: ToolRunner, server: ServerService, usb: PortFinder,
                 store: BundleStore, app_version: str, clock: Optional[Clock] = None,
                 temp_root: Optional[Path] = None,
                 today: Callable[[], date] = date.today) -> None:
        self._runner = runner
        self._server = server
        self._usb = usb
        self._store = store
        self._app_version = app_version
        self._clock = clock or Clock()
        self._temp_root = temp_root
        self._today = today

    # ------------------------------------------------------------- run

    def run(self, mode: InstallMode, board: BoardInfo, config: InstallConfig,
            on_step: ProgressFn, cancel: CancelToken) -> InstallSummary:
        # A PC that sleeps halfway suspends USB under esptool.
        with keep_awake():
            return self._run(mode, board, config, on_step, cancel)

    def _run(self, mode: InstallMode, board: BoardInfo, config: InstallConfig,
             on_step: ProgressFn, cancel: CancelToken) -> InstallSummary:
        self._check_mode(mode, board)
        problems = validation.problems(config, mode)
        if problems:
            # Part of preparing the files, so the screen offers to change the details.
            raise InstallerError(problems[0], step=StepId.PREPARE_FILES, cause="settings check")
        register_secret(config.wifi_password)
        if config.hik is not None:
            register_secret(config.hik.password)
        if self._temp_root is not None:
            self._temp_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="sl-install-",
                                     dir=str(self._temp_root) if self._temp_root else None))
        run = _Run(mode, board, config, new_request_id(), work, on_step, cancel,
                   key_burned=mode is InstallMode.RESUME)
        started = iso_utc(self._clock.now_utc())
        log.info("install %s for %s (%s) request %s", mode.value, board.mac, config.family.value,
                 run.request_id)
        try:
            try:
                bundle_info, plan = self._prepare(run)
                self._report(run, "STARTED", {"bundle_id": bundle_info.bundle_id or None})
                for step in plan.steps():
                    run.step = step
                    self._emit(run, step, 0.0)
                    calls = plan.for_step(step)
                    for index, planned in enumerate(calls):
                        if index and not planned.ranges:
                            self._emit(run, step, index / len(calls))
                        self._execute(run, planned)
                    self._emit(run, step, 1.0)
                    self._report(run, _MILESTONES.get(step, step.value.upper()), {})
            except InstallerError:
                raise
            except PlanError as error:
                raise InstallerError(texts.BUNDLE_NOT_TRUSTED, cause=f"plan: {error}") from error
            except Exception as error:  # every failure must reach the screen as advice
                log.exception("unexpected install failure")
                raise InstallerError(texts.UNEXPECTED, cause=repr(error)[:300]) from error
            self._report(run, "COMPLETED", {"bundle_id": bundle_info.bundle_id or None})
            log.info("install %s finished for %s", run.request_id, board.mac)
            return InstallSummary(run.request_id, mode, board.mac, run.config, bundle_info,
                                  started, iso_utc(self._clock.now_utc()))
        except InstallerError as error:
            final = self._final_error(run, error)
            # Named "lock", not "key": ADD stores any detail name containing "key" as
            # [REDACTED] (installer_api EVENT_SECRET_PARTS), and head office needs the flag.
            self._report(run, "CANCELLED" if final.advice.code == texts.CANCELLED.code
                         else "FAILED", {"step": run.step.value, "code": final.advice.code,
                                         "lock_burned": run.key_burned})
            log.error("install %s failed at %s: %s (%s)", run.request_id, run.step.value,
                      final.advice.code, final.cause)
            raise final from None
        finally:
            run.hmac_key = None
            for path in run.secret_files:
                wipe(path)
            shutil.rmtree(work, ignore_errors=True)

    def _check_mode(self, mode: InstallMode, board: BoardInfo) -> None:
        if board.security not in _ALLOWED[mode]:
            advice = texts.ALREADY_SET_UP if board.security is SecurityState.SECURED_STATE_LIFE \
                else texts.MODE_NOT_ALLOWED
            raise InstallerError(advice, cause=f"{mode.value} on a {board.security.value} board")
        if mode is not InstallMode.CHANGE_SETTINGS and (
                board.hardware is not HardwareVerdict.OK or board.secure_download_mode):
            raise InstallerError(texts.MODE_NOT_ALLOWED,
                                 cause=f"{mode.value} needs checked hardware outside SDM")

    def _final_error(self, run: _Run, error: InstallerError) -> InstallerError:
        if run.key_burned and not error.irreversible_done:
            advice = error.advice
            if advice.can_retry or advice.code == texts.CANCELLED.code:
                advice = texts.resume_needed(error.advice)
            return InstallerError(advice, step=run.step, irreversible_done=True,
                                  cause=error.cause)
        if error.step is None:
            return InstallerError(error.advice, step=run.step,
                                  irreversible_done=error.irreversible_done, cause=error.cause)
        return error

    # --------------------------------------------------------- prepare

    def _prepare(self, run: _Run) -> Tuple[BundleInfo, FlashPlan]:
        step = StepId.PREPARE_FILES
        self._emit(run, step, 0.0, "Checking head office")
        caps = self._server.capabilities()
        if not caps.enabled:
            raise InstallerError(texts.SERVER_DISABLED, cause="installer disabled")
        if not caps.provisioner_ready:
            raise InstallerError(texts.PROVISIONER_UNAVAILABLE, cause="provisioner not ready")
        if _older(self._app_version, caps.min_app_version):
            raise InstallerError(texts.APP_TOO_OLD,
                                 cause=f"app {self._app_version} < {caps.min_app_version}")
        record = self._server.device(run.board.mac)
        if record.known and record.family is not None and record.family is not run.config.family:
            raise InstallerError(texts.family_mismatch(record.family),
                                 cause=f"ADD knows {record.family.value}")
        if run.mode is InstallMode.CHANGE_SETTINGS and not record.settings_change_allowed:
            # Head office cannot build settings this board could read (it may be locked
            # with an older security root): written, they would stop it from starting.
            raise InstallerError(texts.SETTINGS_CHANGE_NOT_SUPPORTED,
                                 cause="ADD does not allow a settings change for this board")
        run.config = self._complete(run.config, record)
        self._emit(run, step, 0.05)

        bundle: Optional[LocalBundle] = None
        if run.mode is not InstallMode.CHANGE_SETTINGS:
            def on_bytes(done: int, total: int) -> None:
                fraction = 0.05 + 0.7 * (done / total if total else 1.0)
                self._emit(run, step, fraction,
                           f"Downloading the board software: {done / 1e6:.1f} of "
                           f"{total / 1e6:.1f} MB")
            bundle = self._server.fetch_bundle(run.config.bundle_id, on_bytes, run.cancel)
            if bundle.info.family is not run.config.family:
                raise InstallerError(texts.BUNDLE_WRONG_FAMILY, cause="bundle family")
        self._check_cancel(run)

        self._emit(run, step, 0.8, "Getting this board's settings from head office")
        opened = self._package(run)
        nvs_path = run.work / "settings.bin"
        write_private(nvs_path, opened.nvs)
        run.secret_files.append(nvs_path)
        if opened.hmac_key is not None:
            run.hmac_key = opened.hmac_key
        nvs = FlashRange("nvs", NVS_OFFSET, str(nvs_path), NVS_SIZE)
        if bundle is None:
            storage = self._store.storage_image()
            ranges = [nvs, FlashRange("storage", STORAGE_OFFSET, str(storage), STORAGE_SIZE)]
            info = BundleInfo(run.config.bundle_id, run.config.family, "Settings only", "",
                              False, "", "Settings and empty storage only.")
        else:
            # Checked again right before writing (design: verification on every use).
            fresh = self._store.verify_local(bundle.info.bundle_id)
            ranges = flash_ranges(fresh) + [nvs]
            info = fresh.info
        plan = build_plan(run.mode, run.board.port, run.config.family, ranges, run.work)
        self._emit(run, step, 1.0, "Ready")
        return info, plan

    def _complete(self, config: InstallConfig, record: DeviceRecord) -> InstallConfig:
        if config.family is not Family.HIKVISION or config.hik is None:
            return config
        hik = config.hik
        hik = replace(hik, profile=validation.hik_profile_for(hik),
                      source_epoch=hik.source_epoch or validation.default_source_epoch(
                          config.zone_id, self._today(), record.hik_source_epoch))
        return replace(config, hik=hik)

    def _package(self, run: _Run) -> OpenedPackage:
        key = OneTimeKey()
        try:
            package: EncryptedPackage = self._server.create_package(PackageRequest(
                run.request_id, run.board.mac, run.mode, run.config, key.public_key_b64,
                self._app_version))
            opened = open_package(package, key, request_id=run.request_id, mac=run.board.mac,
                                  want_hmac_key=run.mode is InstallMode.NEW_BOARD)
        except EnvelopeError as error:
            raise InstallerError(texts.PACKAGE_BAD, cause=str(error)) from error
        finally:
            key.discard()
        aad = opened.aad
        if (aad.get("firmware_family") != run.config.family.value
                or aad.get("zone_id") != run.config.zone_id
                or aad.get("zone_device_id") != run.config.device_id):
            raise InstallerError(texts.PACKAGE_BAD, cause="package is for other settings")
        return opened

    # --------------------------------------------------------- execute

    def _execute(self, run: _Run, planned: PlannedCall) -> None:
        if not run.burn_started:
            self._check_cancel(run)
        port = self._resolve_port(run)
        if port != planned.call.args[planned.call.args.index("--port") + 1]:
            log.info("board moved to %s", port)
            planned = with_port(planned, port)
        if planned.purpose is Purpose.BURN_KEY:
            self._burn(run, planned)
            return
        progress = self._progress_for(run, planned)
        # Once the key step starts nothing is interrupted: a half-written board is worse
        # than a finished one.
        result = self._runner.run(planned.call, cancel=None if run.burn_started else run.cancel,
                                  on_line=progress.line if progress else None)
        if not result.ok:
            raise InstallerError(advice_for(result), step=run.step,
                                 cause=failure_cause(result))
        self._check(run, planned, result)
        if progress:
            progress.emit(planned.total_bytes or 1, force=True)

    def _progress_for(self, run: _Run, planned: PlannedCall) -> Optional[_Progress]:
        if not planned.ranges:
            return None
        verify = planned.purpose in (Purpose.VERIFY_FIRMWARE, Purpose.VERIFY_STARTUP)
        start, end = (0.0, 0.6) if planned.purpose is Purpose.WRITE_STARTUP else \
            (0.6, 1.0) if planned.purpose is Purpose.VERIFY_STARTUP else \
            (0.05, 1.0) if planned.purpose is Purpose.WRITE_SETTINGS else (0.0, 1.0)
        return _Progress(run, planned.step, start, end, planned.ranges, self._clock,
                         verify=verify)

    def _resolve_port(self, run: _Run) -> str:
        deadline = self._clock.monotonic() + PORT_WAIT_S
        while True:
            candidate, natives = self._usb.find(run.board.mac)
            if candidate is not None:
                return candidate.port
            if natives > 1:
                raise InstallerError(texts.SEVERAL_BOARDS, step=run.step,
                                     cause="board not found among several")
            if self._clock.monotonic() >= deadline:
                raise InstallerError(texts.BOARD_DISCONNECTED, step=run.step,
                                     cause="board not on USB")
            cancel = None if run.burn_started else run.cancel
            if self._clock.sleep(0.25, cancel):
                self._check_cancel(run)

    def _check_cancel(self, run: _Run) -> None:
        if run.cancel.cancelled and not run.burn_started:
            raise InstallerError(texts.CANCELLED, step=run.step, cause="cancelled")

    def _burn(self, run: _Run, planned: PlannedCall) -> None:
        if run.mode is not InstallMode.NEW_BOARD or run.hmac_key is None or not planned.key_path:
            raise InstallerError(texts.MODE_NOT_ALLOWED, step=run.step, cause="no key to burn")
        key_path = Path(planned.key_path)
        write_private(key_path, run.hmac_key)
        run.secret_files.append(key_path)
        run.burn_started = True
        self._emit(run, run.step, 0.4)
        try:
            result = self._runner.run(planned.call, cancel=None)
        finally:
            wipe(key_path)
            run.hmac_key = None
        if result.ok:
            run.key_burned = True
            return
        # The burn may or may not have landed: look before deciding what the board is.
        state = self._key_state_after_failure(run, planned)
        if state is SecurityState.KEY_BURNED_NOT_STARTED:
            run.key_burned = True
            raise InstallerError(texts.resume_needed(advice_for(result)), step=run.step,
                                 irreversible_done=True, cause=failure_cause(result))
        if state is SecurityState.BLANK:
            raise InstallerError(texts.KEY_NOT_LOCKED, step=run.step, cause=failure_cause(result))
        raise InstallerError(texts.KEY_STATE_UNCLEAR, step=run.step, irreversible_done=True,
                             cause=failure_cause(result))

    def _key_state_after_failure(self, run: _Run, burn: PlannedCall) -> Optional[SecurityState]:
        try:
            port = self._resolve_port(run)
        except InstallerError:
            return None
        path = run.work / "key-state-check.json"
        result = self._runner.run(ToolCall(
            ESPEFUSE, espefuse_args(port, "summary", "--format", "json", "--file", str(path)),
            "read key state", timeout_s=90.0, idle_timeout_s=60.0), cancel=None)
        if not result.ok:
            return None
        summary = read_summary_file(path)
        if not summary or efuse_mac(summary) not in (None, run.board.mac):
            return None
        state, _note = classify_efuses(summary)
        return state

    # ----------------------------------------------------------- checks

    def _check(self, run: _Run, planned: PlannedCall, result: ToolResult) -> None:
        purpose = planned.purpose
        if purpose is Purpose.IDENTITY:
            self._check_identity(run, result)
        elif purpose in (Purpose.KEY_STATE_BEFORE_BURN, Purpose.KEY_STATE_FOR_RESUME,
                         Purpose.KEY_STATE_AFTER_BURN):
            self._check_key_state(run, planned)
        else:
            self._check_mac_in_banner(run, result)
            count = len(planned.ranges)
            if purpose in (Purpose.WRITE_FIRMWARE, Purpose.WRITE_STARTUP):
                if (len(_WROTE.findall(result.output)) != count
                        or len(_HASH_OK.findall(result.output)) != count):
                    raise InstallerError(texts.COPY_CHECK_FAILED, step=run.step,
                                         cause=f"{planned.call.label}: write not confirmed")
            elif purpose in (Purpose.VERIFY_FIRMWARE, Purpose.VERIFY_STARTUP):
                if (len(_VERIFY_OK.findall(result.output)) != count
                        or _VERIFY_BAD.search(result.output)):
                    raise InstallerError(texts.COPY_CHECK_FAILED, step=run.step,
                                         cause=f"{planned.call.label}: digest mismatch")
            elif purpose is Purpose.WRITE_SETTINGS:
                if len(_WROTE.findall(result.output)) != count:
                    raise InstallerError(texts.SETTINGS_WRITE_FAILED, step=run.step,
                                         cause="settings write not confirmed")

    def _check_mac_in_banner(self, run: _Run, result: ToolResult) -> None:
        banner = parse_banner(result.output)
        if banner.mac and banner.mac != run.board.mac:
            raise InstallerError(texts.BOARD_SWAPPED, step=run.step,
                                 irreversible_done=run.key_burned,
                                 cause=f"board {banner.mac} instead of {run.board.mac}")

    def _check_identity(self, run: _Run, result: ToolResult) -> None:
        banner = parse_banner(result.output)
        info = parse_security_info(result.output)
        if info is None:
            raise InstallerError(texts.SECURITY_UNKNOWN, step=run.step, cause="no security report")
        sdm = banner.secure_download_mode or info.secure_download_mode
        self._check_mac_in_banner(run, result)
        if run.mode is InstallMode.CHANGE_SETTINGS:
            if sdm:
                ok = classify_sdm(info) is SecurityState.SECURED_STATE_LIFE
            else:
                ok = (banner.mac == run.board.mac and info.secure_boot
                      and tuple(info.key_purposes[:4]) == (8, 9, 10, 11))
            if not ok:
                raise InstallerError(texts.MODE_NOT_ALLOWED, step=run.step,
                                     cause="board is no longer a finished State Life board")
            return
        if sdm or info.secure_boot or info.flash_encryption:
            raise InstallerError(texts.ALREADY_SET_UP, step=run.step,
                                 irreversible_done=run.key_burned, cause="board is secured")
        if banner.mac != run.board.mac:
            raise InstallerError(texts.BOARD_SWAPPED, step=run.step,
                                 irreversible_done=run.key_burned,
                                 cause="board ID missing from the identity check")

    def _check_key_state(self, run: _Run, planned: PlannedCall) -> None:
        summary = read_summary_file(Path(planned.summary_path or ""))
        if not summary:
            raise InstallerError(texts.SECURITY_UNKNOWN, step=run.step, cause="no key summary")
        reported = efuse_mac(summary)
        if reported != run.board.mac:
            raise InstallerError(texts.BOARD_SWAPPED, step=run.step,
                                 irreversible_done=run.key_burned,
                                 cause=f"key summary is for {reported}")
        state, _note = classify_efuses(summary)
        if planned.purpose is Purpose.KEY_STATE_BEFORE_BURN and state is not SecurityState.BLANK:
            raise InstallerError(texts.MODE_NOT_ALLOWED, step=run.step,
                                 cause=f"key block not blank before burn ({state.value})")
        if (planned.purpose is Purpose.KEY_STATE_FOR_RESUME
                and state is not SecurityState.KEY_BURNED_NOT_STARTED):
            raise InstallerError(texts.MODE_NOT_ALLOWED, step=run.step, irreversible_done=True,
                                 cause=f"resume needs a locked key ({state.value})")
        if planned.purpose is Purpose.KEY_STATE_AFTER_BURN and not key0_locked_hmac(summary):
            raise InstallerError(texts.KEY_CHECK_FAILED, step=run.step, irreversible_done=True,
                                 cause="key purpose or protection wrong after burn")

    # ---------------------------------------------------------- helpers

    def _emit(self, run: _Run, step: StepId, fraction: Optional[float], detail: str = "") -> None:
        try:
            run.on_step(StepProgress(step, fraction, detail))
        except Exception as error:  # a screen problem must not break a write
            log.warning("progress listener failed: %s", error)

    def _report(self, run: _Run, kind: str, detail: dict) -> None:
        payload = {"mode": run.mode.value, "app_version": self._app_version, **detail}
        try:
            self._server.report(run.request_id, run.board.mac, kind, payload)
        except Exception as error:
            log.warning("could not report %s: %s", kind, error)


_MILESTONES = {
    StepId.CLEAR_START_AREA: "START_AREA_CLEARED",
    StepId.COPY_FIRMWARE: "FIRMWARE_COPIED",
    StepId.CHECK_COPY: "COPY_CHECKED",
    StepId.LOCK_KEY: "KEY_LOCKED",
    StepId.COPY_STARTUP: "STARTUP_COPIED",
    StepId.WRITE_SETTINGS: "SETTINGS_WRITTEN",
}


def _older(version: str, minimum: str) -> bool:
    try:
        return validation.version_tuple(version) < validation.version_tuple(minimum)
    except ValueError:
        return False


def report_boot(server: ServerService, summary: InstallSummary, boot: BootReport) -> None:
    """Tell ADD how the first start went (call after wait_first_boot)."""
    detail = {"outcome": boot.outcome.value, "zone_id_seen": boot.zone_id_seen,
              "device_id_seen": boot.device_id_seen, "app_version_seen": boot.app_version_seen,
              "advice": boot.advice.code if boot.advice else None}
    try:
        server.report(summary.request_id, summary.mac,
                      "BOOT_OK" if boot.outcome.value == "ok" else "BOOT_FAILED", detail)
    except Exception as error:
        log.warning("could not report first start: %s", error)


def report_faulty_board(server: ServerService, mac: str, period_s: Optional[float],
                        advice: Optional[Advice] = None) -> None:
    """ADD's BOARD_FAULTY event for a board put aside (design section 7)."""
    try:
        server.report(new_request_id(), mac, "BOARD_FAULTY",
                      {"loop_period_s": period_s, "advice": advice.code if advice else None})
    except Exception as error:
        log.warning("could not report a faulty board: %s", error)
