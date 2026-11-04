"""Inspect a connected board: identity, hardware and how it is locked.

``get-security-info`` runs first because it is the one command Secure Download Mode
(SDM) allows; it also prints the chip type, features and MAC banner on boards that
are not in SDM. Boards not in SDM are then asked for their flash size
(``flash-id``) and their eFuse summary (``espefuse summary --format json``). The
banner already carries the MAC, so ``chip-id`` would add nothing.

espefuse 5.1.0 JSON records hold name/value/readable/writeable/... and no
``raw_value`` (the field provision_zone_lite.py reads but never gets): a blank key
block is readable, writeable and all zeros.

Every call uses ``--after no-reset``. Inspection leaves the board in download
mode, so it only starts again after the person unplugs and replugs it.

The board ID is the banner MAC, or the USB serial number in SDM, whose banner has
none. When both are there they must agree: a USB board ID that is not the chip's
is refused here, because every install call finds the board by it.

The hardware gate (hardware_gate) refuses, before anything is written or burned,
every board our firmware cannot start. On 3 Nov 2026 two ESP32-S3 N16R8 boards with
an XMC XM25QH128A flash chip (JEDEC 20 70 18) got their HMAC key and then looped for
good in the ESP-IDF 5.5.3 bootloader ("XMC flash startup fail"). That bootloader's
bootloader_flash_xmc_startup() accepts a chip whose ID is on is_xmc_chip_strict()'s
list; otherwise, when the SFDP maker byte is XMC's (0x20), it runs the XM25QHxxC
startup flow and stops the boot unless the ID is then on the list. esptool 5.1.0 runs
the same check before flash-id (cmds.py flash_xmc_startup) and prints "XMC flash chip
boot-up failure detected" exactly when that flow runs. The gate refuses a board when:

* esptool printed "XMC flash boot-up fix failed": the flow ran and the ID is still off
  the list, which is exactly when the bootloader stops. esptool's check is the
  bootloader's, SFDP byte included, so a listed XMC chip that recovers in the flow, and
  a 0x20 chip whose SFDP byte is not XMC's (the bootloader skips the flow for it), pass;
* the flash is not 16 MB, or the flash-type eFuse says octal (8 data lines);
* the embedded PSRAM is not exactly "8MB (AP_3v3)" (none at all: WRONG_PSRAM);
* the chip revision is above v0.99, the range ESP-IDF 5.5.3 builds for the ESP32-S3
  by default (the installer checks the chosen bundle's own image headers again);
* the chip is not an ESP32-S3.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import texts
from .api import (Advice, BoardInfo, CancelToken, HardwareVerdict, InstallerError,
                  SecurityState, UsbCandidate, UsbKind)
from .esptool_runner import ESPEFUSE, ESPTOOL, ToolCall, ToolResult, ToolRunner, advice_for, \
    failure_cause
from .flashplan import espefuse_args, esptool_args
from .logging_setup import get_logger
from .naming import normalize_mac

log = get_logger("board")

REQUIRED_CHIP = "ESP32-S3"
REQUIRED_FLASH_MB = 16
REQUIRED_PSRAM_MB = 8
REQUIRED_PSRAM_VENDOR = "AP_3v3"
# ESP-IDF 5.5.3's default ESP32-S3 range (CONFIG_ESP32S3_REV_MIN_FULL / _MAX_FULL),
# major * 100 + minor: v0.0 to v0.99.
DEFAULT_CHIP_REVISIONS = (0, 99)
XMC_VENDOR_ID = 0x20

SECURE_BOOT_EN = 1 << 0
AGGRESSIVE_REVOKE = 1 << 1
SECURE_DOWNLOAD_ENABLE = 1 << 2
DIS_USB = 1 << 8
# get-security-info key purposes KEY0..KEY5 on a finished State Life board: HMAC_UP
# then the three Secure Boot digests the first boot burns. Field fingerprint
# (8, 9, 10, 11, 0, 0, 12); the seventh value is not a key purpose and is ignored.
STATE_LIFE_KEY_PURPOSES = (8, 9, 10, 11, 0, 0)
STATE_LIFE_FLAGS = 0xC5
HMAC_UP = 8
DIGESTS = ("SECURE_BOOT_DIGEST0", "SECURE_BOOT_DIGEST1", "SECURE_BOOT_DIGEST2")

_CHIP_LINE = re.compile(r"^\s*Chip (?:type:|is)\s+(.+?)\s*$", re.M)
_FEATURES = re.compile(r"^\s*Features:\s+(.+?)\s*$", re.M)
_MAC_LINE = re.compile(r"^\s*MAC:\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b", re.M)
_FLAGS = re.compile(r"^\s*Flags:\s+0x([0-9a-fA-F]+)", re.M)
_PURPOSES = re.compile(r"^\s*Key Purposes:\s*\(([^)]*)\)", re.M)
_SECURE_BOOT = re.compile(r"^\s*Secure Boot:\s+(Enabled|Disabled)", re.M)
_FLASH_ENC = re.compile(r"^\s*Flash Encryption:\s+(Enabled|Disabled)", re.M)
_FLASH_SIZE = re.compile(r"Detected flash size:\s*(\d+)\s*MB", re.I)
_PSRAM = re.compile(r"Embedded PSRAM\s+(\d+)\s*MB", re.I)
_PSRAM_PART = re.compile(r"(?:Unknown )?Embedded PSRAM[^,]*", re.I)
_PSRAM_VENDOR = re.compile(r"Embedded PSRAM\s+\d+\s*MB\s*\(([^)]*)\)", re.I)
_WRONG_CHIP = re.compile(r"This chip is ([\w-]+), not ESP32-S3")
_CHIP_REVISION = re.compile(r"\(revision v(\d+)\.(\d+)\)")
# esptool 5.1.0 print_flash_id and flash_id (cmds.py).
_FLASH_MAKER = re.compile(r"^\s*Manufacturer:\s*([0-9a-fA-F]{1,2})\s*$", re.M)
_FLASH_DEVICE = re.compile(r"^\s*Device:\s*([0-9a-fA-F]{1,4})\s*$", re.M)
_FLASH_TYPE = re.compile(r"Flash type set in eFuse:\s*(quad|octal)", re.I)
# esptool 5.1.0 flash_xmc_startup (cmds.py): the first when the bootloader would run the
# XM25QHxxC startup flow, the second when the ID is still off the list after it.
XMC_STARTUP_NEEDED = re.compile(r"XMC flash chip boot-up failure detected")
XMC_STARTUP_FAILED = re.compile(r"XMC flash boot-up fix failed")


@dataclass(frozen=True)
class Banner:
    chip_line: Optional[str]
    chip: Optional[str]
    features: Optional[str]
    mac: Optional[str]
    secure_download_mode: bool


@dataclass(frozen=True)
class SecurityInfo:
    flags: int
    key_purposes: Tuple[int, ...]
    secure_boot: bool
    flash_encryption: bool

    @property
    def secure_download_mode(self) -> bool:
        return bool(self.flags & SECURE_DOWNLOAD_ENABLE)


def parse_banner(output: str) -> Banner:
    chip_match = _CHIP_LINE.search(output)
    chip_line = chip_match.group(1) if chip_match else None
    sdm = bool(chip_line and "Secure Download Mode" in chip_line)
    chip = None
    if chip_line:
        found = re.match(r"(ESP32(?:-[A-Z0-9]+)?)", chip_line)
        chip = found.group(1) if found else chip_line.split()[0]
    features = _FEATURES.search(output)
    mac = _MAC_LINE.search(output)
    return Banner(chip_line, chip, features.group(1) if features else None,
                  normalize_mac(mac.group(1)) if mac else None, sdm)


def parse_security_info(output: str) -> Optional[SecurityInfo]:
    flags = _FLAGS.search(output)
    if not flags:
        return None
    purposes = _PURPOSES.search(output)
    values: Tuple[int, ...] = ()
    if purposes:
        values = tuple(int(part) for part in re.findall(r"\d+", purposes.group(1)))
    secure_boot = _SECURE_BOOT.search(output)
    flash_enc = _FLASH_ENC.search(output)
    flag_value = int(flags.group(1), 16)
    return SecurityInfo(
        flags=flag_value, key_purposes=values,
        secure_boot=(secure_boot.group(1) == "Enabled") if secure_boot
        else bool(flag_value & SECURE_BOOT_EN),
        flash_encryption=bool(flash_enc and flash_enc.group(1) == "Enabled"))


def parse_flash_size_mb(output: str) -> Optional[int]:
    found = _FLASH_SIZE.search(output)
    return int(found.group(1)) if found else None


def parse_psram_mb(features: Optional[str]) -> int:
    found = _PSRAM.search(features or "")
    return int(found.group(1)) if found else 0


def parse_psram(features: Optional[str]) -> Optional[str]:
    """The embedded PSRAM as esptool names it ("Embedded PSRAM 8MB (AP_3v3)"), or None."""
    found = _PSRAM_PART.search(features or "")
    return found.group(0).strip() if found else None


def parse_chip_revision(chip_line: Optional[str]) -> Optional[int]:
    """"ESP32-S3 (QFN56) (revision v0.2)" -> 2, ESP-IDF's major * 100 + minor."""
    found = _CHIP_REVISION.search(chip_line or "")
    return int(found.group(1)) * 100 + int(found.group(2)) if found else None


def revision_words(revision: Optional[int]) -> str:
    return f"v{revision // 100}.{revision % 100}" if revision is not None else "unknown"


@dataclass(frozen=True)
class FlashChip:
    """What esptool's flash-id says about the flash chip."""

    maker: Optional[int]           # JEDEC manufacturer ("Manufacturer: 20")
    device: Optional[int]          # memory type << 8 | capacity ("Device: 7018")
    size_mb: Optional[int]
    octal: Optional[bool]          # the flash-type eFuse; None when esptool did not say
    xmc_startup_needed: bool = False
    xmc_startup_failed: bool = False

    @property
    def jedec_id(self) -> Optional[str]:
        """"20 70 18": maker, memory type and capacity, the bytes ESP-IDF reads."""
        if self.maker is None or self.device is None:
            return None
        return f"{self.maker:02x} {self.device >> 8 & 0xFF:02x} {self.device & 0xFF:02x}"


def parse_flash_chip(output: str) -> FlashChip:
    maker = _FLASH_MAKER.search(output)
    device = _FLASH_DEVICE.search(output)
    kind = _FLASH_TYPE.search(output)
    return FlashChip(
        maker=int(maker.group(1), 16) if maker else None,
        device=int(device.group(1), 16) if device else None,
        size_mb=parse_flash_size_mb(output),
        octal=(kind.group(1).lower() == "octal") if kind else None,
        xmc_startup_needed=bool(XMC_STARTUP_NEEDED.search(output)),
        xmc_startup_failed=bool(XMC_STARTUP_FAILED.search(output)))


def is_xmc_chip_strict(maker: Optional[int], device: Optional[int]) -> bool:
    """ESP-IDF 5.5.3 bootloader_flash.c is_xmc_chip_strict(), on esptool's numbers.

    The bootloader's rdid is maker << 16 | memory type << 8 | capacity; esptool prints
    the maker and then memory type and capacity as "Device".
    """
    if maker != XMC_VENDOR_ID or device is None:
        return False
    mfid, cpid = (device >> 8) & 0xFF, device & 0xFF
    if mfid == 0x40:
        return 0x13 <= cpid <= 0x20
    if mfid == 0x41:
        return 0x17 <= cpid <= 0x20
    if mfid == 0x50:
        return 0x15 <= cpid <= 0x16
    return False


def flash_chip_starts(chip: FlashChip) -> bool:
    """Whether our bootloader gets past its XMC startup check with this chip (module notes)."""
    if chip.xmc_startup_failed:
        return False
    if chip.xmc_startup_needed:
        # The flow ran: the ID esptool read after it decides, as in the bootloader.
        return is_xmc_chip_strict(chip.maker, chip.device)
    # No flow: the ID is on the list, or the SFDP maker byte is not XMC's.
    return True


def efuse_flash_octal(summary: Dict[str, object]) -> Optional[bool]:
    """FLASH_TYPE from an espefuse JSON summary ("4 data lines" / "8 data lines")."""
    value = _field(summary, "FLASH_TYPE").get("value")
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return "8" in str(value)


def classify_sdm(info: SecurityInfo) -> SecurityState:
    """A board in Secure Download Mode: only get-security-info can describe it."""
    flags_ok = (info.flags & SECURE_BOOT_EN and info.flags & SECURE_DOWNLOAD_ENABLE
                and not info.flags & AGGRESSIVE_REVOKE and not info.flags & DIS_USB)
    if (flags_ok and info.secure_boot and not info.flash_encryption
            and tuple(info.key_purposes[:6]) == STATE_LIFE_KEY_PURPOSES):
        return SecurityState.SECURED_STATE_LIFE
    return SecurityState.SECURED_OTHER


def _field(summary: Dict[str, object], name: str) -> Dict[str, object]:
    value = summary.get(name)
    return value if isinstance(value, dict) else {}


def _zero_key(value: object) -> bool:
    text = str(value or "").replace(" ", "")
    return len(text) == 64 and set(text) == {"0"}


def key_block_unused(summary: Dict[str, object], index: int) -> bool:
    """espefuse's own test (base_operations._key_block_is_unused) on JSON fields."""
    block = _field(summary, f"BLOCK_KEY{index}")
    purpose = _field(summary, f"KEY_PURPOSE_{index}")
    return (block.get("readable") is True and block.get("writeable") is True
            and _zero_key(block.get("value"))
            and purpose.get("value") == "USER" and purpose.get("writeable") is True)


def key0_locked_hmac(summary: Dict[str, object]) -> bool:
    """KEY_PURPOSE_0 is HMAC_UP and BLOCK_KEY0 is read- and write-protected."""
    block = _field(summary, "BLOCK_KEY0")
    purpose = _field(summary, "KEY_PURPOSE_0")
    return (purpose.get("value") == "HMAC_UP" and purpose.get("writeable") is False
            and block.get("readable") is False and block.get("writeable") is False)


def efuse_mac(summary: Dict[str, object]) -> Optional[str]:
    value = str(_field(summary, "MAC").get("value") or "")
    return normalize_mac(value.split(" ")[0]) if value else None


def flash_encryption_enabled(summary: Dict[str, object]) -> bool:
    value = _field(summary, "SPI_BOOT_CRYPT_CNT").get("value")
    if isinstance(value, int):
        return bin(value).count("1") % 2 == 1
    return str(value).lower() == "enable"


def classify_efuses(summary: Dict[str, object]) -> Tuple[SecurityState, Optional[str]]:
    """The board's state from an espefuse JSON summary, plus an optional note code."""
    if not _field(summary, "BLOCK_KEY0") or not _field(summary, "KEY_PURPOSE_0"):
        return SecurityState.UNKNOWN, None
    purposes = [_field(summary, f"KEY_PURPOSE_{i}").get("value") for i in range(6)]
    secure_boot = _field(summary, "SECURE_BOOT_EN").get("value") is True
    sdm = _field(summary, "ENABLE_SECURITY_DOWNLOAD").get("value") is True
    if flash_encryption_enabled(summary):
        return SecurityState.SECURED_OTHER, None
    digests_in_place = tuple(purposes[1:4]) == DIGESTS
    if secure_boot:
        if key0_locked_hmac(summary) and digests_in_place:
            return SecurityState.SECURED_STATE_LIFE, None
        return SecurityState.SECURED_OTHER, None
    if not sdm and all(key_block_unused(summary, i) for i in range(6)):
        return SecurityState.BLANK, None
    if key0_locked_hmac(summary) and all(key_block_unused(summary, i) for i in range(1, 6)):
        return SecurityState.KEY_BURNED_NOT_STARTED, None
    if key0_locked_hmac(summary) and digests_in_place:
        # The first boot burned the digests but stopped before Secure Boot was on.
        return SecurityState.UNKNOWN, "FIRST_START_UNFINISHED"
    return SecurityState.UNSECURED_USED, None


def efuse_evidence(summary: Dict[str, object]) -> Dict[str, object]:
    """Lock state for the log; key block contents are never copied."""
    evidence: Dict[str, object] = {}
    for index in range(6):
        block = _field(summary, f"BLOCK_KEY{index}")
        purpose = _field(summary, f"KEY_PURPOSE_{index}")
        evidence[f"key{index}"] = {
            "purpose": purpose.get("value"), "purpose_writeable": purpose.get("writeable"),
            "readable": block.get("readable"), "writeable": block.get("writeable"),
            "empty": _zero_key(block.get("value")) if block.get("readable") else None,
        }
    for name in ("SECURE_BOOT_EN", "ENABLE_SECURITY_DOWNLOAD", "DIS_DOWNLOAD_MODE",
                 "SPI_BOOT_CRYPT_CNT", "DIS_USB_JTAG", "SOFT_DIS_JTAG", "DIS_PAD_JTAG",
                 "PSRAM_CAP", "PSRAM_VENDOR", "FLASH_CAP", "FLASH_TYPE", "FLASH_VENDOR",
                 "PKG_VERSION"):
        if name in summary:
            evidence[name.lower()] = _field(summary, name).get("value")
    return evidence


def _extra_memory_words(psram: str) -> str:
    """esptool's "Embedded PSRAM 8MB (AP_1v8)" as the screens say it: "8 MB, type AP_1v8"."""
    size = parse_psram_mb(psram)
    vendor = _PSRAM_VENDOR.search(psram)
    kind = vendor.group(1).strip() if vendor else ""
    words = f"{size} MB" if size else "of unknown size"
    return f"{words}, type {kind}" if kind else f"{words}, type not given"


def hardware_gate(chip: Optional[str], chip_line: Optional[str], features: Optional[str],
                  flash: FlashChip, summary: Optional[Dict[str, object]] = None
                  ) -> Tuple[HardwareVerdict, str]:
    """The board's hardware verdict, and for a refused board what did not fit (module notes).

    The memory chip comes first: it is the one fault that leaves a locked board looping.
    The detail is what the screens show next to the advice ("The app found: ..."); the
    log and ADD keep esptool's own words (evidence, refusal_detail).
    """
    if chip != REQUIRED_CHIP:
        return HardwareVerdict.WRONG_CHIP, f"chip {chip or 'unknown'}"
    if not flash_chip_starts(flash):
        return (HardwareVerdict.FLASH_CHIP_UNSUPPORTED,
                f"memory chip {flash.jedec_id or 'of unknown make'}")
    if flash.size_mb != REQUIRED_FLASH_MB:
        size = f"{flash.size_mb} MB" if flash.size_mb is not None else "of unknown size"
        return HardwareVerdict.WRONG_FLASH_SIZE, f"memory chip {size}"
    octal = flash.octal if flash.octal is not None else efuse_flash_octal(summary or {})
    if octal:
        return HardwareVerdict.BOARD_TYPE_UNSUPPORTED, "memory chip with 8 data lines (octal)"
    psram = parse_psram(features)
    if psram is None:
        return HardwareVerdict.WRONG_PSRAM, "no extra memory"
    vendor = _PSRAM_VENDOR.search(psram)
    if (parse_psram_mb(psram) != REQUIRED_PSRAM_MB or not vendor
            or vendor.group(1).strip() != REQUIRED_PSRAM_VENDOR):
        return HardwareVerdict.BOARD_TYPE_UNSUPPORTED, f"extra memory {_extra_memory_words(psram)}"
    revision = parse_chip_revision(chip_line)
    low, high = DEFAULT_CHIP_REVISIONS
    if revision is not None and not low <= revision <= high:
        return HardwareVerdict.BOARD_TYPE_UNSUPPORTED, f"chip revision {revision_words(revision)}"
    return HardwareVerdict.OK, ""


def hardware_advice(board: BoardInfo, locked: Optional[bool] = None) -> Optional[Advice]:
    """What to tell the person about a board the hardware gate refused, or None.

    ``locked``: an earlier setup locked the board's key (the default follows its state).
    """
    verdict = board.hardware
    if locked is None:
        locked = board.security is SecurityState.KEY_BURNED_NOT_STARTED
    if verdict is HardwareVerdict.FLASH_CHIP_UNSUPPORTED:
        return texts.flash_chip_unsupported(board.flash_id, locked=locked)
    if verdict is HardwareVerdict.BOARD_TYPE_UNSUPPORTED:
        return texts.board_type_unsupported(board.hardware_detail)
    return {HardwareVerdict.WRONG_CHIP: texts.WRONG_CHIP,
            HardwareVerdict.WRONG_FLASH_SIZE: texts.WRONG_FLASH_SIZE,
            HardwareVerdict.WRONG_PSRAM: texts.WRONG_PSRAM}.get(verdict)


def refusal_detail(board: BoardInfo, advice: Advice) -> Dict[str, object]:
    """ADD's BOARD_REFUSED event detail (no secrets: the board's own hardware facts)."""
    return {"reason": advice.code, "flash_id": board.flash_id,
            "chip": board.evidence.get("chip_line") or board.chip,
            "features": board.evidence.get("features")}


def read_summary_file(path: Path) -> Dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class BoardInspector:
    """BoardServiceProto: looks at one board without starting it."""

    def __init__(self, runner: ToolRunner, temp_root: Optional[Path] = None) -> None:
        self._runner = runner
        self._temp_root = temp_root

    def _run(self, call: ToolCall, cancel: CancelToken) -> ToolResult:
        result = self._runner.run(call, cancel=cancel)
        if cancel.cancelled:
            raise InstallerError(texts.CANCELLED, cause=f"{call.label}: cancelled")
        return result

    def inspect(self, candidate: UsbCandidate, cancel: CancelToken) -> BoardInfo:
        if candidate.kind is not UsbKind.NATIVE:
            raise InstallerError(texts.WRONG_SOCKET, cause=f"{candidate.port} is not native USB")
        port = candidate.port
        usb_mac = normalize_mac(candidate.mac)
        result = self._run(ToolCall(ESPTOOL, esptool_args(port, "get-security-info", stub=False),
                                    "check board", timeout_s=60.0, idle_timeout_s=45.0), cancel)
        evidence: Dict[str, object] = {"usb_mac": usb_mac}
        wrong = _WRONG_CHIP.search(result.output)
        if wrong:
            evidence["chip_reported"] = wrong.group(1)
            return BoardInfo(port, usb_mac or "", wrong.group(1), None, None,
                             HardwareVerdict.WRONG_CHIP, SecurityState.UNKNOWN, False, evidence)
        if not result.ok:
            raise InstallerError(advice_for(result), cause=failure_cause(result))
        banner = parse_banner(result.output)
        info = parse_security_info(result.output)
        if info is None:
            raise InstallerError(texts.SECURITY_UNKNOWN, cause="no security report")
        sdm = banner.secure_download_mode or info.secure_download_mode
        evidence.update({
            "chip_line": banner.chip_line, "features": banner.features,
            "flags": f"{info.flags:#010x}", "key_purposes": list(info.key_purposes),
            "secure_boot": info.secure_boot, "flash_encryption": info.flash_encryption,
            "secure_download_mode": sdm,
        })
        if banner.mac and usb_mac and banner.mac != usb_mac:
            # The installer finds the board by its USB board ID before every call, so a
            # wrong one would lose the board or follow another one.
            raise InstallerError(texts.BOARD_ID_UNKNOWN,
                                 cause=f"USB serial {usb_mac} differs from chip MAC {banner.mac}")
        mac = banner.mac or usb_mac
        if not mac:
            raise InstallerError(texts.BOARD_ID_UNKNOWN, cause="no MAC from chip or USB")
        if sdm:
            state = classify_sdm(info)
            log.info("board %s in Secure Download Mode: %s", mac, state.value)
            return BoardInfo(port, mac, banner.chip or REQUIRED_CHIP, None, None,
                             HardwareVerdict.NOT_CHECKED, state, True, evidence)

        flash = self._run(ToolCall(ESPTOOL, esptool_args(port, "flash-id", stub=False),
                                   "read flash size", timeout_s=60.0, idle_timeout_s=45.0),
                          cancel)
        if not flash.ok:
            raise InstallerError(advice_for(flash), cause=failure_cause(flash))
        chip = parse_flash_chip(flash.output)
        psram_mb = parse_psram_mb(banner.features)
        revision = parse_chip_revision(banner.chip_line)
        evidence.update({"flash_size_mb": chip.size_mb, "psram_mb": psram_mb,
                         "flash_id": chip.jedec_id, "flash_octal": chip.octal,
                         "xmc_startup_needed": chip.xmc_startup_needed,
                         "xmc_startup_failed": chip.xmc_startup_failed})

        state, note, summary = self._efuse_state(port, mac, cancel, evidence)
        if note:
            evidence["note"] = note
        verdict, detail = hardware_gate(banner.chip, banner.chip_line, banner.features, chip,
                                        summary)
        if detail:
            evidence["hardware_detail"] = detail
        log.info("board %s: hardware %s%s, security %s", mac, verdict.value,
                 f" ({detail})" if detail else "", state.value)
        return BoardInfo(port, mac, banner.chip, chip.size_mb, psram_mb, verdict, state, False,
                         evidence, flash_id=chip.jedec_id, chip_revision=revision,
                         hardware_detail=detail)

    def _efuse_state(self, port: str, mac: str, cancel: CancelToken,
                     evidence: Dict[str, object]
                     ) -> Tuple[SecurityState, Optional[str], Dict[str, object]]:
        if self._temp_root is not None:
            self._temp_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="sl-inspect-",
                                     dir=str(self._temp_root) if self._temp_root else None))
        try:
            path = work / "key-state.json"
            result = self._run(ToolCall(
                ESPEFUSE, espefuse_args(port, "summary", "--format", "json", "--file", str(path)),
                "read key state", timeout_s=90.0, idle_timeout_s=60.0), cancel)
            if not result.ok:
                raise InstallerError(advice_for(result), cause=failure_cause(result))
            summary = read_summary_file(path)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if not summary:
            return SecurityState.UNKNOWN, None, {}
        evidence["efuse"] = efuse_evidence(summary)
        reported = efuse_mac(summary)
        if reported and reported != mac:
            raise InstallerError(texts.BOARD_SWAPPED,
                                 cause=f"eFuse MAC {reported} differs from {mac}")
        state, note = classify_efuses(summary)
        return state, note, summary
