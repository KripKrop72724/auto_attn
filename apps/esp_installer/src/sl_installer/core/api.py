"""Contracts shared by the installer engine (core) and the screens (ui).

Everything here is plain Python (no Qt) and runs on Python 3.9 or newer, so
the engine can also be exercised from a terminal on the bench. The engine
calls are blocking; the UI runs them on worker threads and must hop back to
the GUI thread itself. Callbacks passed into the engine may be called from a
worker thread.

Plain-language rule: every message a person can see is an ``Advice`` (what
happened, then what to do), written for someone who has never used a
computer for technical work. Codes are for logs and support, never the only
thing shown.
"""
from __future__ import annotations

import enum
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Protocol, Sequence, Tuple


class Family(str, enum.Enum):
    ZKT = "zkt"
    HIKVISION = "hikvision"


class Severity(str, enum.Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Advice:
    """One plain-language message: what happened, then what to do."""

    code: str
    title: str
    what_happened: str
    what_to_do: Tuple[str, ...] = ()
    severity: Severity = Severity.ERROR
    # Asset key of a picture that explains the action, e.g. "unplug",
    # "replug", "usb_socket", "boot_button", "cable", "faulty_board".
    illustration: Optional[str] = None
    can_retry: bool = True


class InstallerError(Exception):
    """Every failure the screens can show. ``advice`` is always set."""

    def __init__(self, advice: Advice, *, step: Optional["StepId"] = None,
                 irreversible_done: bool = False, cause: Optional[str] = None) -> None:
        super().__init__(advice.code)
        self.advice = advice
        self.step = step
        # True once the security key is burned: the board is now bound to
        # State Life and can only be finished (resumed), never started over.
        self.irreversible_done = irreversible_done
        self.cause = cause  # technical detail for the log only; never secrets


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; True if cancelled meanwhile."""
        return self._event.wait(seconds)


# ---------------------------------------------------------------- USB / board

class UsbKind(str, enum.Enum):
    NATIVE = "native"   # ESP32-S3 USB-Serial/JTAG, VID 0x303A PID 0x1001 (socket marked USB)
    BRIDGE = "bridge"   # CP210x / CH34x / FTDI USB-UART chip (socket marked COM or UART)
    OTHER = "other"     # VID 0x303A, another PID (e.g. 0x4001): the chip runs other software


@dataclass(frozen=True)
class UsbCandidate:
    port: str                    # "COM5" on Windows, "/dev/cu.usbmodem101" on macOS
    kind: UsbKind
    vid: Optional[int]
    pid: Optional[int]
    mac: Optional[str]           # "ac:27:6e:a4:3d:30" from the USB serial number (NATIVE only)
    description: str = ""


class PresenceState(str, enum.Enum):
    NO_BOARD = "no_board"
    ONE_BOARD = "one_board"
    WRONG_SOCKET = "wrong_socket"          # only a BRIDGE port is present
    OTHER_USB_MODE = "other_usb_mode"      # no NATIVE port but an OTHER one: not in setup mode
    SEVERAL_BOARDS = "several_boards"
    RESTART_LOOP = "restart_loop"          # the board keeps appearing and vanishing


@dataclass(frozen=True)
class PresenceReport:
    state: PresenceState
    candidates: Tuple[UsbCandidate, ...] = ()
    board: Optional[UsbCandidate] = None   # set when state is ONE_BOARD or RESTART_LOOP
    loop_period_s: Optional[float] = None  # typical ~10 s on known-faulty boards
    seconds_in_state: float = 0.0


class HardwareVerdict(str, enum.Enum):
    OK = "ok"                    # ESP32-S3, 16 MB flash, 8 MB embedded PSRAM
    WRONG_CHIP = "wrong_chip"
    WRONG_FLASH_SIZE = "wrong_flash_size"
    WRONG_PSRAM = "wrong_psram"  # no embedded PSRAM at all
    # The ESP-IDF 5.5.3 bootloader cannot start this flash chip (an XMC part off its strict
    # list, e.g. XM25QH128A 20/7018): the board loops in the bootloader for good.
    FLASH_CHIP_UNSUPPORTED = "flash_chip_unsupported"
    # A board our firmware is not built for: octal flash, PSRAM other than 8 MB AP_3v3, or a
    # chip revision outside the firmware images' range.
    BOARD_TYPE_UNSUPPORTED = "board_type_unsupported"
    NOT_CHECKED = "not_checked"  # secured boards cannot report flash/PSRAM


class SecurityState(str, enum.Enum):
    BLANK = "blank"                          # never prepared: safe to install
    KEY_BURNED_NOT_STARTED = "key_burned"    # earlier install stopped after the key burn: resume
    SECURED_STATE_LIFE = "secured_state_life"  # finished State Life board (usually Secure Download Mode)
    SECURED_OTHER = "secured_other"          # secured by someone else's keys: refuse
    UNSECURED_USED = "unsecured_used"        # no State Life key but not blank (odd key use): refuse
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BoardInfo:
    port: str
    mac: str
    chip: Optional[str]
    flash_size_mb: Optional[int]
    psram_mb: Optional[int]
    hardware: HardwareVerdict
    security: SecurityState
    secure_download_mode: bool
    evidence: Dict[str, object] = field(default_factory=dict)  # redacted raw facts for the log
    # What the hardware gate measured (board.py), for its advice, the screens and ADD's
    # BOARD_REFUSED event: the flash chip's JEDEC ID as esptool prints it ("20 70 18":
    # maker, type, size), the chip revision as ESP-IDF counts it (major * 100 + minor,
    # so v0.2 is 2) and, for a refused board, what did not fit, in esptool's words.
    flash_id: Optional[str] = None
    chip_revision: Optional[int] = None
    hardware_detail: str = ""


# ------------------------------------------------------------------- server

@dataclass(frozen=True)
class Capabilities:
    enabled: bool                 # server accepts installer requests
    provisioner_ready: bool       # server can build per-board settings right now
    min_app_version: str
    server_time: Optional[str] = None


@dataclass(frozen=True)
class BundleInfo:
    bundle_id: str
    family: Family
    label: str                    # "ZKT 2.7.8"
    version: str
    recommended: bool
    release_state: str            # "AVAILABLE", "FIELD_BASELINE", "HIL_ONLY"
    notes: str = ""
    download_bytes: int = 0


@dataclass(frozen=True)
class LocalBundle:
    """A verified bundle on disk (signature and every hash checked)."""

    info: BundleInfo
    directory: str
    manifest: Dict[str, object]


@dataclass(frozen=True)
class DeviceRecord:
    """What ADD knows about a board, looked up by MAC."""

    known: bool
    mac: str
    zone_id: Optional[str] = None
    zone_name: Optional[str] = None
    device_id: Optional[str] = None
    family: Optional[Family] = None
    firmware_version: Optional[str] = None
    online: Optional[bool] = None
    last_seen_at: Optional[str] = None
    ever_online: bool = False        # True once the board has talked to ADD from a site
    public_name: Optional[str] = None
    group: Optional[str] = None      # "Life" or "Health"
    status_text: Optional[str] = None  # plain words, e.g. "Online" or "Offline since 10:42"
    hik_source_epoch: Optional[str] = None
    # Whether ADD will build a settings-only package for this board. Boards set
    # up before this app may use an older security root, and new settings built
    # from the fleet root would leave them unable to start (review B1/F1).
    settings_change_allowed: bool = True
    settings_change_reason: Optional[str] = None  # plain words when not allowed


@dataclass(frozen=True)
class ZoneSummary:
    zone_id: str
    zone_name: str
    device_ids: Tuple[str, ...]
    families: Tuple[Family, ...] = ()


@dataclass(frozen=True)
class ConnectivityStatus:
    internet: bool
    server: bool
    detail: str = ""


# ------------------------------------------------------------- configuration

@dataclass(frozen=True)
class FieldDefaults:
    """Defaults shown to the person (owner decision, 1 Nov 2026).

    Secret-looking values (Wi-Fi password, comm key, hotspot password) are
    injected at release-build time; a developer build has them empty and
    ``is_test_build`` True.
    """

    wifi_ssid: str = "RE7"
    wifi_password: str = ""
    zkt_port: int = 4370
    zkt_comm_key: int = 0
    zkt_preferred_ip: str = "0.0.0.0"
    device_id: str = "1"
    hik_port: int = 80
    hik_transport: str = "http_digest"
    hik_username: str = "admin"
    setup_ap_password: str = ""
    is_test_build: bool = True


@dataclass(frozen=True)
class HikModel:
    key: str
    label: str
    profile: str
    tested: bool


# Profile strings go into NVS hik_profile (1-79 bytes). Only the pilot
# profile unlocks terminal user management in the firmware
# (firmware/zone_lite/main/hikvision_commands.c).
HIK_MODELS: Tuple[HikModel, ...] = (
    HikModel("ds-k1t342efwx", "DS-K1T342EFWX", "ds-k1t342efwx-v3.3.5-220310-poll5-pilot-v1", True),
    HikModel("ds-k1t341amf", "DS-K1T341AMF", "ds-k1t341amf-unverified-v1", False),
    HikModel("ds-k1t321mfwx", "DS-K1T321MFWX", "ds-k1t321mfwx-v4.61.0-20260828-unverified-v1", False),
    HikModel("other", "Another Hikvision model", "", False),
)


@dataclass(frozen=True)
class ZktSettings:
    port: int = 4370
    comm_key: int = 0
    preferred_ip: str = "0.0.0.0"


@dataclass(frozen=True)
class HikSettings:
    host: str
    password: str
    username: str = "admin"
    port: int = 80
    transport: str = "http_digest"     # "http_digest" or "https"
    expected_serial: str = ""
    model_key: str = "ds-k1t342efwx"
    profile: str = ""                  # filled from the model when empty
    source_epoch: str = ""             # filled as "<zone_id lower>-<yyyymmdd>" when empty
    ca_pem: str = ""


@dataclass(frozen=True)
class InstallConfig:
    family: Family
    zone_id: str
    zone_name: str
    device_id: str
    wifi_ssid: str
    wifi_password: str
    bundle_id: str
    zkt: Optional[ZktSettings] = None
    hik: Optional[HikSettings] = None


class InstallMode(str, enum.Enum):
    NEW_BOARD = "new_board"            # BLANK board
    RESUME = "resume"                  # KEY_BURNED_NOT_STARTED board
    CHANGE_SETTINGS = "change_settings"  # SECURED_STATE_LIFE board: NVS, storage, redo area only


class StepId(str, enum.Enum):
    PREPARE_FILES = "prepare_files"        # bundle download/verify + per-board settings from ADD
    CLEAR_START_AREA = "clear_start_area"  # erase 0x0-0xFFFF so nothing old can start
    COPY_FIRMWARE = "copy_firmware"        # every range except the bootloader
    CHECK_COPY = "check_copy"              # on-chip verify of every written range
    LOCK_KEY = "lock_key"                  # burn the per-board HMAC key (irreversible)
    COPY_STARTUP = "copy_startup"          # bootloader at 0x0, written last, then verified
    WRITE_SETTINGS = "write_settings"      # CHANGE_SETTINGS only (blind write in Secure Download Mode)


STEP_LABELS: Dict[StepId, str] = {
    StepId.PREPARE_FILES: "Getting the secure files from head office",
    StepId.CLEAR_START_AREA: "Clearing the board",
    StepId.COPY_FIRMWARE: "Copying the firmware",
    StepId.CHECK_COPY: "Checking the copy",
    StepId.LOCK_KEY: "Locking the board's security key",
    StepId.COPY_STARTUP: "Copying the start-up program",
    StepId.WRITE_SETTINGS: "Writing the new settings",
}


def steps_for(mode: InstallMode) -> Tuple[StepId, ...]:
    if mode is InstallMode.CHANGE_SETTINGS:
        return (StepId.PREPARE_FILES, StepId.WRITE_SETTINGS)
    if mode is InstallMode.RESUME:
        return (StepId.PREPARE_FILES, StepId.CLEAR_START_AREA, StepId.COPY_FIRMWARE,
                StepId.CHECK_COPY, StepId.COPY_STARTUP)
    return (StepId.PREPARE_FILES, StepId.CLEAR_START_AREA, StepId.COPY_FIRMWARE,
            StepId.CHECK_COPY, StepId.LOCK_KEY, StepId.COPY_STARTUP)


@dataclass(frozen=True)
class StepProgress:
    step: StepId
    fraction: Optional[float]   # 0.0-1.0 within the step, None if unknown
    detail: str = ""            # short plain words, e.g. "2.1 of 3.4 MB"


@dataclass(frozen=True)
class InstallSummary:
    request_id: str
    mode: InstallMode
    mac: str
    config: InstallConfig       # passwords are kept only in memory, never saved
    bundle: BundleInfo
    started_at: str
    finished_at: str


# ------------------------------------------------------------------ console

class ConsoleKind(str, enum.Enum):
    BOOT_BANNER = "boot_banner"              # ROM/bootloader/app start line seen
    PSRAM_OK = "psram_ok"
    PSRAM_FAILED = "psram_failed"
    SECURE_BOOT_FIRST_BOOT = "secure_boot_first_boot"
    APP_STARTED = "app_started"              # data: zone_id, device_id
    PROVISIONING_LOADED = "provisioning_loaded"  # data: zone_id
    APP_VERSION = "app_version"              # data: version, project
    STORAGE_READY = "storage_ready"
    STORAGE_FAILED = "storage_failed"
    NVS_KEY_DANGER = "nvs_key_danger"        # "Generating NVS encr-keys": key was missing (fatal)
    NVS_FAILED = "nvs_failed"                # encrypted settings unreadable
    SETTINGS_MISSING = "settings_missing"
    FAMILY_MISMATCH = "family_mismatch"
    WIFI_CONNECTING = "wifi_connecting"      # data: ssid
    WIFI_CONNECTED = "wifi_connected"        # data: ip
    WIFI_RETRY = "wifi_retry"
    SETUP_AP_OPEN = "setup_ap_open"          # data: ssid
    ADD_CONNECTED = "add_connected"
    ADD_FAILED = "add_failed"
    TERMINAL_FOUND = "terminal_found"        # data: ip
    TERMINAL_AUTH_FAILED = "terminal_auth_failed"
    TERMINAL_SECURE_PULL = "terminal_secure_pull"  # CONNECT answered 6001 (SenseFace etc.)
    TERMINAL_UNREACHABLE = "terminal_unreachable"
    # The second-stage bootloader refused to start the program (an E line of its own,
    # e.g. "XMC flash startup fail"). data: reason (xmc_flash, partition_table, no_app,
    # image, chip_revision, secure_boot, other) and fatal ("1" when it always restarts).
    BOOTLOADER_FAILED = "bootloader_failed"
    PANIC = "panic"                          # crash / abort / watchdog
    OTHER = "other"


@dataclass(frozen=True)
class ConsoleEvent:
    kind: ConsoleKind
    raw: str                                 # the line, already free of secrets
    data: Dict[str, str] = field(default_factory=dict)
    at: float = 0.0                          # seconds since the watch started


class BootOutcome(str, enum.Enum):
    OK = "ok"
    WRONG_SETTINGS = "wrong_settings"        # started with another zone/device than expected
    NO_OUTPUT = "no_output"                  # silent, or no line of the running program
    FAULTY_RESTART_LOOP = "faulty_restart_loop"
    SECURITY_KEY_PROBLEM = "security_key_problem"
    STORAGE_PROBLEM = "storage_problem"
    FAMILY_MISMATCH = "family_mismatch"
    NOT_REPLUGGED = "not_replugged"          # timed out waiting for unplug/replug
    STARTUP_REFUSED = "startup_refused"      # the bootloader stopped before the program
    CANCELLED = "cancelled"
    # The restart check only (BootService.wait_restart):
    SETTINGS_LOST = "settings_lost"          # missing or unreadable settings after a restart
    SETTINGS_NOT_SEEN = "settings_not_seen"  # running, but no line named its settings


@dataclass(frozen=True)
class BootReport:
    outcome: BootOutcome
    zone_id_seen: Optional[str] = None
    device_id_seen: Optional[str] = None
    app_version_seen: Optional[str] = None
    events: Tuple[ConsoleEvent, ...] = ()
    advice: Optional[Advice] = None
    # The board's own line that decided a failed start (redacted), for ADD and support.
    board_said: Optional[str] = None


class ReplugPhase(str, enum.Enum):
    WAITING_UNPLUG = "waiting_unplug"
    WAITING_REPLUG = "waiting_replug"
    WATCHING = "watching"


# ----------------------------------------------------------- service protocols

ProgressFn = Callable[[StepProgress], None]
EventFn = Callable[[ConsoleEvent], None]
PresenceFn = Callable[[PresenceReport], None]
PhaseFn = Callable[[ReplugPhase], None]
BytesProgressFn = Callable[[int, int], None]


@dataclass(frozen=True)
class PackageRequest:
    """Per-board settings request; ADD builds the encrypted settings (NVS)."""

    request_id: str
    mac: str
    mode: InstallMode
    config: InstallConfig
    recipient_public_key_b64: str   # one-time X25519 key; private half stays in memory
    app_version: str


@dataclass(frozen=True)
class EncryptedPackage:
    request_id: str
    provisioning_manifest: Dict[str, object]
    provisioning_ciphertext: bytes
    hmac_ciphertext: bytes


class ServerService(Protocol):
    @property
    def signed_in_user(self) -> Optional[str]: ...
    def login(self, username: str, password: str) -> str: ...
    def logout(self) -> None: ...
    def capabilities(self) -> Capabilities: ...
    def bundles(self, family: Optional[Family] = None) -> Sequence[BundleInfo]: ...
    def fetch_bundle(self, bundle_id: str, on_bytes: Optional[BytesProgressFn],
                     cancel: CancelToken) -> LocalBundle: ...
    def device(self, mac: str) -> DeviceRecord: ...
    def zones(self) -> Sequence[ZoneSummary]: ...
    def create_package(self, request: PackageRequest) -> EncryptedPackage: ...
    def report(self, request_id: str, mac: str, kind: str, detail: Dict[str, object]) -> None: ...


class ConnectivityService(Protocol):
    def check(self) -> ConnectivityStatus: ...


class UsbService(Protocol):
    def snapshot(self) -> PresenceReport: ...
    def watch(self, on_report: PresenceFn, cancel: CancelToken, interval_s: float = 0.3) -> None: ...


class BoardServiceProto(Protocol):
    def inspect(self, candidate: UsbCandidate, cancel: CancelToken) -> BoardInfo: ...


class InstallService(Protocol):
    def run(self, mode: InstallMode, board: BoardInfo, config: InstallConfig,
            on_step: ProgressFn, cancel: CancelToken) -> InstallSummary: ...


class BootService(Protocol):
    """Replug guidance and the verdict of a watched start.

    Every install watches two starts: the first one (``wait_first_boot``) and, once
    that passed, one more after a deliberate replug (``wait_restart``), which passes
    only when the board shows its settings again: a board must keep them through a
    restart. ``first`` is the first start's report: other settings than it showed count
    as lost. ``transcript`` names the install (its request ID); the board's lines of
    each watched start then go, redacted, into that install's console log.
    """

    def wait_first_boot(self, mac: str, expect: InstallConfig, on_phase: PhaseFn,
                        on_event: EventFn, cancel: CancelToken,
                        timeout_s: float = 120.0,
                        transcript: Optional[str] = None) -> BootReport: ...
    def wait_restart(self, mac: str, expect: InstallConfig, on_phase: PhaseFn,
                     on_event: EventFn, cancel: CancelToken,
                     timeout_s: float = 120.0,
                     transcript: Optional[str] = None,
                     first: Optional[BootReport] = None) -> BootReport: ...
    def watch_console(self, candidate: UsbCandidate, on_event: EventFn,
                      cancel: CancelToken) -> None: ...


class DiagnosisServiceProto(Protocol):
    def diagnose(self, board: Optional[BoardInfo], presence: PresenceReport,
                 device: Optional[DeviceRecord],
                 events: Sequence[ConsoleEvent]) -> Sequence[Advice]: ...


class RecordsService(Protocol):
    # ``restart``: the restart check (BootService.wait_restart), None when not watched.
    def save_install_record(self, summary: InstallSummary, boot: BootReport,
                            restart: Optional[BootReport] = None) -> str: ...
    def save_help_file(self) -> str: ...   # ZIP of redacted logs for head office


@dataclass
class Services:
    server: ServerService
    connectivity: ConnectivityService
    usb: UsbService
    boards: BoardServiceProto
    installer: InstallService
    boot: BootService
    diagnosis: DiagnosisServiceProto
    records: RecordsService
    defaults: FieldDefaults
    app_version: str = "0.0.0"
