"""Test doubles for the engine: a simulated board behind esptool/espefuse, fakes for
the clock, USB, serial and ADD, and signed test bundles.

Tool output follows esptool/espefuse 5.1.0's own print statements
(esptool/__init__.py prepare_esp_object, cmds.py write_flash, verify_flash,
get_security_info, print_flash_id; espefuse base_fields.burn_all) so the parsers
are tested against what the real tools print.
"""
from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import importlib.util
import json
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from sl_installer.core import flashplan
from sl_installer.core.api import (BundleInfo, Capabilities, CancelToken, DeviceRecord,
                                   EncryptedPackage, Family, InstallConfig, LocalBundle,
                                   PackageRequest, UsbCandidate, UsbKind, ZoneSummary,
                                   ZktSettings)
from sl_installer.core.bundles import STORAGE_EMPTY_SHA256, BundleStore, Verifier
from sl_installer.core.esptool_runner import ToolCall, ToolResult, ToolRunner, PROGRESS_LINE

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
REPO = HERE.parents[3]
SRC = HERE.parents[1] / "src"
STORAGE_GZ = SRC / "sl_installer" / "resources" / "storage-empty.bin.gz"
MAC = "ac:27:6e:a4:3d:30"
OTHER_MAC = "ac:27:6e:a4:ce:a8"
PORT = "/dev/cu.usbmodem101"
GIT_SHA = "dc7d9c3" + "0" * 33
# esptool 5.1.0 "Validation hash" of each signed fixture image (esptool image-info).
DIGESTS = {
    "app-zkt-2.9.1-signed.bin": "e89380a125f60aaf32bd12af68344310c3aa5b03fcc66fb40914839e9707fa6e",
    "app-zkt-2.5.2-signed.bin": "623f3ac98ea0b765ffdf77c2d6218502c265121111098f4eb7a596a278ba9d1a",
    "app-hik-3.1.1-signed.bin": "77620195c18a7e865f31fe16714b798b833ab5bb1a195aebbead9c8f01b61baf",
    "bootloader-signed.bin": "b9b6038fbdce7cab8f7a8cb7843662a9e9838719abf8ab1c1fad8a76ff2c1e99",
}
TEST_QUALIFIED = {"2.5.2": DIGESTS["app-zkt-2.5.2-signed.bin"]}


# --------------------------------------------------------------- fixtures

def efuse(name: str, mac: str = MAC) -> Dict[str, dict]:
    summary = json.loads((FIXTURES / "efuse" / f"{name}.json").read_text(encoding="utf-8"))
    summary["MAC"]["value"] = f"{mac} (OK)"
    summary["PSRAM_CAP"]["value"] = "8M"
    return summary


def image(name: str) -> bytes:
    return (FIXTURES / "images" / name).read_bytes()


def sb_key(index: int) -> rsa.RSAPublicKey:
    return serialization.load_pem_public_key(
        (FIXTURES / "images" / f"test-sb-key-{index}-public.pem").read_bytes())


def storage_image() -> bytes:
    data = gzip.decompress(STORAGE_GZ.read_bytes())
    assert hashlib.sha256(data).hexdigest() == STORAGE_EMPTY_SHA256
    return data


def partition_table(**overrides: Tuple[int, int, int, int]) -> bytes:
    rows = {
        "nvs": (1, 0x02, 0x11000, 0x6000),
        "otadata": (1, 0x00, 0x17000, 0x2000),
        "phy_init": (1, 0x01, 0x19000, 0x1000),
        "factory": (0, 0x00, 0x20000, 0x280000),
        "ota_0": (0, 0x10, 0x2A0000, 0x280000),
        "ota_1": (0, 0x11, 0x520000, 0x280000),
        "storage": (1, 0x82, 0x7A0000, 0x800000),
    }
    rows.update(overrides)
    table = b""
    for label, (kind, subtype, offset, size) in rows.items():
        table += struct.pack("<2sBBII16sI", b"\xaa\x50", kind, subtype, offset, size,
                             label.encode(), 0)
    return table + b"\xff" * (3072 - len(table))


# ------------------------------------------------------- esptool output

def connect_banner(mac: Optional[str], *, sdm: bool = False, port: str = PORT,
                   chip: str = "ESP32-S3 (QFN56) (revision v0.2)",
                   features: str = "Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, "
                                   "Embedded PSRAM 8MB (AP_3v3)") -> str:
    lines = ["esptool v5.1.0", f"Serial port {port}:", "Connecting....",
             f"Connected to ESP32-S3 on {port}:"]
    if sdm:
        lines.append(f"{'Chip type:':<20}ESP32-S3 in Secure Download Mode")
    else:
        lines += [f"{'Chip type:':<20}{chip}", f"{'Features:':<20}{features}",
                  f"{'Crystal frequency:':<20}40MHz", f"{'USB mode:':<20}USB-Serial/JTAG"]
        if mac:
            lines.append(f"{'MAC:':<20}{mac}")
    return "\n".join(lines + [""])


_PURPOSE_NAMES = {0: "USER/EMPTY", 8: "HMAC_UP", 9: "SECURE_BOOT_DIGEST0",
                  10: "SECURE_BOOT_DIGEST1", 11: "SECURE_BOOT_DIGEST2", 4: "XTS_AES_128_KEY"}


def security_report(flags: int, purposes: Sequence[int], *, secure_boot: bool,
                    flash_encryption: bool = False) -> str:
    lines = ["Security Information:", "=====================",
             "Flags: {:#010x} ({})".format(flags, bin(flags)),
             f"Key Purposes: {tuple(purposes)}"]
    lines.append("  " + "\n  ".join(f"BLOCK_KEY{i} - {_PURPOSE_NAMES.get(p, 'UNKNOWN')}"
                                    for i, p in enumerate(purposes) if i <= 5))
    lines += ["Chip ID: 9", "API Version: 0",
              "Secure Boot: Enabled" if secure_boot else "Secure Boot: Disabled",
              "Flash Encryption: Enabled" if flash_encryption else "Flash Encryption: Disabled",
              "SPI Boot Crypt Count (SPI_BOOT_CRYPT_CNT): 0x0"]
    if flags & 0x80:
        lines.append("JTAG: Permanently Disabled")
    return "\n".join(lines + ["", "Staying in bootloader."])


FLASH_ID_16MB = "\n".join([
    "Flash Memory Information:", "=========================", "Manufacturer: c8",
    "Device: 4018", "Detected flash size: 16MB", "Flash type set in eFuse: quad (4 data lines)",
    "Flash voltage set by eFuse: 3.3V", "", "Staying in bootloader."])

FAILED_TO_CONNECT = ("A fatal error occurred: Failed to connect to ESP32-S3: No serial data "
                     "received.\nFor troubleshooting steps visit: https://docs.espressif.com/"
                     "projects/esptool/en/latest/troubleshooting.html")
PORT_BUSY = ("A fatal error occurred: Could not open COM5, the port is busy or doesn't exist.\n"
             "(could not open port 'COM5': PermissionError(13, 'Access is denied.', None, 5))\n"
             "\nHint: Check if the port is not used by another task")
PORT_GONE = ("A fatal error occurred: Could not open COM5, the port is busy or doesn't exist.\n"
             "(could not open port 'COM5': FileNotFoundError(2, 'The system cannot find the "
             "file specified.', None, 2))")
WRONG_CHIP = "A fatal error occurred: This chip is ESP32-C3, not ESP32-S3. Wrong chip argument?"
SERIAL_NOISE = ("A fatal error occurred: Serial data stream stopped: Possible serial noise or "
                "corruption.")
SDM_REFUSAL = ("A fatal error occurred: The 'flash-id' command is not available in Secure "
               "Download Mode.")


def write_lines(offset: int, size: int, stub: bool) -> List[str]:
    lines = [f"Flash will be erased from {offset:#010x} to {offset + size - 1:#010x}..."]
    if not stub:
        lines.append("Erasing flash...")
    step = 0x4000 if stub else 0x400
    for done in list(range(0, size, max(step, size // 8))) + [size]:
        percent = 100.0 * done / size
        lines.append(f"\rWriting at {offset + done:#010x} [{'=' * int(30 * done // size):30}] "
                     f"{percent:5.1f}% {done}/{size} bytes... ")
    if stub:
        lines += [f"Wrote {size} bytes ({size // 3} compressed) at {offset:#010x} in 0.4 seconds "
                  "(2048.0 kbit/s).", "Hash of data verified."]
    else:
        lines += [f"Wrote {size} bytes at {offset:#010x} in 2.0 seconds (330.0 kbit/s).",
                  "Cannot verify written data if encrypted or in secure download mode."]
    return lines


# ------------------------------------------------------------ fake board

@dataclass
class Failure:
    returncode: int
    output: str
    times: int = 1                 # how many calls fail before the fake recovers
    then: Optional[Callable[["FakeBoard"], None]] = None


class FakeBoard:
    """Answers esptool/espefuse command lines the way a real ESP32-S3 would."""

    def __init__(self, mac: str = MAC, *, efuse_state: str = "blank", sdm: bool = False,
                 chip: str = "ESP32-S3 (QFN56) (revision v0.2)", flash_mb: int = 16,
                 psram: str = "Embedded PSRAM 8MB (AP_3v3)") -> None:
        self.mac = mac
        self.summary = efuse(efuse_state, mac) if efuse_state != "sdm" else None
        self.sdm = sdm
        self.chip = chip
        self.flash_mb = flash_mb
        self.features = f"Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, {psram}".rstrip(", ")
        self.flash: Dict[int, bytes] = {}
        self.erased: List[Tuple[int, int]] = []
        self.burned_key: Optional[bytes] = None
        self.failures: Dict[str, Failure] = {}
        self.reported_mac: Optional[str] = None   # simulate a swapped board in banners
        self.sdm_purposes = (8, 9, 10, 11, 0, 0, 12)
        self.sdm_flags = 0xC5

    # -- helpers
    def _banner(self) -> str:
        return connect_banner(self.reported_mac or self.mac, sdm=self.sdm, chip=self.chip,
                              features=self.features)

    def _purposes(self) -> Tuple[int, ...]:
        if self.sdm:
            return self.sdm_purposes
        names = {"USER": 0, "HMAC_UP": 8, "SECURE_BOOT_DIGEST0": 9, "SECURE_BOOT_DIGEST1": 10,
                 "SECURE_BOOT_DIGEST2": 11, "XTS_AES_128_KEY": 4}
        assert self.summary is not None
        return tuple(names[self.summary[f"KEY_PURPOSE_{i}"]["value"]] for i in range(6)) + (12,)

    def _secure_boot(self) -> bool:
        return self.sdm or bool(self.summary and self.summary["SECURE_BOOT_EN"]["value"])

    def burn_hmac(self, key: bytes) -> None:
        assert self.summary is not None
        self.burned_key = key
        self.summary["KEY_PURPOSE_0"].update(value="HMAC_UP", writeable=False)
        self.summary["BLOCK_KEY0"].update(value=" ".join(["??"] * 32), readable=False,
                                          writeable=False)

    # -- dispatch
    def handle(self, call: ToolCall) -> Tuple[int, str]:
        args = list(call.args)
        command = next((a for a in args if a in (
            "get-security-info", "flash-id", "erase-region", "write-flash", "verify-flash",
            "summary", "burn-key", "version", "--help", "chip-id")), None)
        failure = self.failures.get(command or "") or self.failures.get(call.label)
        if failure is not None:
            failure.times -= 1
            if failure.times <= 0:
                self.failures = {k: v for k, v in self.failures.items() if v is not failure}
            if failure.then:
                failure.then(self)
            return failure.returncode, failure.output
        assert "--after" not in args or args[args.index("--after") + 1] == "no-reset"
        if command == "get-security-info":
            flags = self.sdm_flags if self.sdm else (1 if self._secure_boot() else 0)
            return 0, self._banner() + "\n" + security_report(
                flags, self._purposes(), secure_boot=self._secure_boot())
        if self.sdm and call.tool == "esptool" and command not in ("write-flash", "erase-region"):
            return 2, self._banner() + "\n" + SDM_REFUSAL
        if self.sdm and call.tool == "espefuse":
            return 2, ("A fatal error occurred: Secure Download Mode is enabled. The tool can "
                       "not read eFuses.")
        if command == "flash-id":
            return 0, self._banner() + "\n" + FLASH_ID_16MB.replace("16MB", f"{self.flash_mb}MB")
        if command == "erase-region":
            start, size = int(args[-2], 16), int(args[-1], 16)
            self.erased.append((start, size))
            for offset in [o for o in self.flash if start <= o < start + size]:
                del self.flash[offset]
            return 0, self._banner() + "\nErasing flash memory region (this may take a while " \
                "depending on size)...\nFlash memory region erased successfully in 0.3 seconds."
        if command in ("write-flash", "verify-flash"):
            pairs = _pairs(args[args.index(command) + 1:])
            stub = "--no-stub" not in args
            lines = [self._banner()]
            ok = True
            for offset, path in pairs:
                data = Path(path).read_bytes()
                if command == "write-flash":
                    self.flash[offset] = data
                    lines += write_lines(offset, len(data), stub)
                else:
                    lines.append(f"Verifying {len(data):#x} ({len(data)}) bytes at {offset:#010x} "
                                 f"in flash against '{path}'...")
                    if self.flash.get(offset) == data:
                        lines.append("Verification successful (digest matched).")
                    else:
                        ok = False
                        lines.append("Verification failed (digest mismatch).")
            if not ok:
                lines.append("A fatal error occurred: Verification failed.")
                return 2, "\n".join(lines)
            return 0, "\n".join(lines + ["", "Staying in bootloader."])
        if command == "summary":
            path = Path(args[args.index("--file") + 1])
            path.write_text(json.dumps(self.summary, sort_keys=True, indent=4), encoding="utf-8")
            return 0, "espefuse v5.1.0\nConnecting....\nSaving eFuse values to " + str(path)
        if command == "burn-key":
            assert "--do-not-confirm" in args
            key_file = Path(args[args.index("BLOCK_KEY0") + 1])
            key = key_file.read_bytes()
            assert len(key) == 32
            self.burn_hmac(key)
            return 0, "\n".join([
                "espefuse v5.1.0", "Connecting....", "Burn keys to blocks:",
                " - BLOCK_KEY0 -> [" + " ".join(["??"] * 32) + "]",
                "\t'KEY_PURPOSE_0': 'USER' -> 'HMAC_UP'.",
                "\tDisabling write to 'KEY_PURPOSE_0'...", "\tDisabling read to key block...",
                "\tDisabling write to key block...", "", "Check all blocks for burn...",
                "This is an irreversible operation!",
                "BURN BLOCK4  - OK (write block == read block)",
                "BURN BLOCK0  - OK (write block == read block)", "Reading updated eFuses...",
                "Successful."])
        if command == "version":
            return 0, "esptool v5.1.0\n5.1.0"
        if command == "--help":
            return 0, "Usage: espefuse [OPTIONS] COMMAND1 [ARGS]...\n espefuse v5.1.0"
        return 2, f"A fatal error occurred: unsupported fake command {args}"


def _pairs(rest: Sequence[str]) -> List[Tuple[int, str]]:
    values = [item for item in rest if not item.startswith("--")
              and item not in ("keep", "16MB")]
    return [(int(values[i], 16), values[i + 1]) for i in range(0, len(values), 2)]


@dataclass
class Seen:
    call: ToolCall
    cancellable: bool
    key_file_bytes: Optional[bytes] = None
    files_present: Dict[str, bool] = field(default_factory=dict)


class FakeRunner(ToolRunner):
    def __init__(self, board: FakeBoard, before: Optional[Callable[[ToolCall], None]] = None
                 ) -> None:
        self.board = board
        self.seen: List[Seen] = []
        self.before = before
        self.lock = threading.Lock()

    @property
    def calls(self) -> List[ToolCall]:
        return [item.call for item in self.seen]

    def run(self, call: ToolCall, *, cancel: Optional[CancelToken] = None,
            on_line: Optional[Callable[[str], None]] = None) -> ToolResult:
        if self.before:
            self.before(call)
        seen = Seen(call, cancel is not None)
        if "burn-key" in call.args:
            path = Path(call.args[call.args.index("BLOCK_KEY0") + 1])
            seen.key_file_bytes = path.read_bytes() if path.exists() else None
        for arg in call.args:
            if arg.endswith(".bin") and Path(arg).is_absolute():  # C:\... on Windows
                seen.files_present[arg] = Path(arg).exists()
        self.seen.append(seen)
        returncode, output = self.board.handle(call)
        kept = []
        for line in output.replace("\r", "\n").splitlines():
            if line.strip() and on_line:
                on_line(line.strip())
            if line.strip() and not PROGRESS_LINE.search(line):
                kept.append(line.strip())
        return ToolResult(call, returncode, "\n".join(kept), 0.1)


# --------------------------------------------------------------- bundles

def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


def sign_manifest(manifest: dict, key: rsa.RSAPrivateKey) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    signature = key.sign(canonical, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                salt_length=32), hashes.SHA256())
    return base64.b64encode(signature).decode()


@dataclass
class TestBundle:
    bundle_id: str
    family: Family
    directory: Path            # what the "server" holds
    manifest: dict
    signature: str
    files: Dict[str, bytes]


def build_bundle(root: Path, family: Family, key: rsa.RSAPrivateKey, *,
                 change: Optional[Callable[[dict, Dict[str, bytes]], None]] = None,
                 resign: bool = True) -> TestBundle:
    """A bundle of the design's format, built from the espsecure-signed fixtures."""
    storage = storage_image()
    if family is Family.ZKT:
        factory_name, version = "app-zkt-2.9.1-signed.bin", "2.9.1"
        files = {
            "bootloader-signed.bin": image("bootloader-signed.bin"),
            "partition-table.bin": partition_table(),
            "ota-data.bin": flashplan.otadata_selecting_ota_0(),
            "zone-lite-signed.bin": image(factory_name),
            "rollback-predecessor.bin": image("app-zkt-2.5.2-signed.bin"),
            "storage-empty.bin": storage,
        }
        roles = [("bootloader", "bootloader-signed.bin"), ("partition-table", "partition-table.bin"),
                 ("otadata", "ota-data.bin"), ("factory", "zone-lite-signed.bin"),
                 ("ota_0", "zone-lite-signed.bin"), ("ota_1", "rollback-predecessor.bin"),
                 ("storage", "storage-empty.bin")]
        bundle_id = f"zkt-{version}-{DIGESTS[factory_name][:8]}"
        state, label = "AVAILABLE", "ZKT 2.9.1"
    else:
        factory_name, version = "app-hik-3.1.1-signed.bin", "3.1.1"
        files = {
            "bootloader-signed.bin": image("bootloader-signed.bin"),
            "partition-table.bin": partition_table(),
            "ota-data.bin": flashplan.blank_otadata(),
            "zone-lite-hikvision-signed.bin": image(factory_name),
            "storage-empty.bin": storage,
        }
        roles = [("bootloader", "bootloader-signed.bin"), ("partition-table", "partition-table.bin"),
                 ("otadata", "ota-data.bin"), ("factory", "zone-lite-hikvision-signed.bin"),
                 ("storage", "storage-empty.bin")]
        bundle_id = f"hikvision-{version}-{GIT_SHA[:7]}-{DIGESTS[factory_name][:8]}"
        state, label = "FIELD_BASELINE", "HIK 3.1.1"
    images = []
    for role, name in roles:
        entry = {"role": role, "name": name, "offset": flashplan.ROLE_OFFSETS[role],
                 "size": len(files[name]), "sha256": hashlib.sha256(files[name]).hexdigest()}
        if role == "factory":
            entry.update(app_project="zone_lite" if family is Family.ZKT else "zone_lite_hikvision",
                         app_version=version, app_digest=DIGESTS[factory_name])
        if role == "ota_1":
            entry.update(app_version="2.5.2", app_digest=DIGESTS["app-zkt-2.5.2-signed.bin"])
        images.append(entry)
    manifest = {
        "schema": "zone-lite-installer-bundle-v1", "bundle_id": bundle_id,
        "firmware_family": family.value, "label": label, "version": version,
        "release_state": state, "recommended": True, "git_sha": GIT_SHA,
        "created_at": "2026-11-01T00:00:00Z", "partition_layout": "zone-lite-ota-v1",
        "notes": "Test bundle.", "images": images,
    }
    if change:
        change(manifest, files)
    signature = sign_manifest(manifest, key) if resign else ""
    directory = root / "server" / manifest["bundle_id"]
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)
    return TestBundle(manifest["bundle_id"], family, directory, manifest, signature, files)


def verifier(key: rsa.RSAPrivateKey) -> Verifier:
    return Verifier(manifest_key=key.public_key(), image_key=sb_key(1),
                    qualified=dict(TEST_QUALIFIED))


class FakeSource:
    """BundleSource over a TestBundle directory, counting downloads."""

    def __init__(self, bundle: TestBundle) -> None:
        self.bundle = bundle
        self.downloads: List[str] = []
        self.corrupt: Dict[str, bytes] = {}

    def bundle_manifest(self, bundle_id: str):
        assert bundle_id == self.bundle.bundle_id
        return copy.deepcopy(self.bundle.manifest), self.bundle.signature

    def download_bundle_file(self, bundle_id: str, name: str, destination: Path,
                             on_chunk: Callable[[int], None], cancel: CancelToken) -> None:
        self.downloads.append(name)
        data = self.corrupt.get(name, (self.bundle.directory / name).read_bytes())
        for start in range(0, len(data), 65536):
            chunk = data[start:start + 65536]
            with destination.open("ab") as out:
                out.write(chunk)
            on_chunk(len(chunk))


# -------------------------------------------------------------- envelope

def _envelope_tool():
    path = REPO / "firmware" / "zone_lite" / "tools" / "provisioning_envelope.py"
    spec = importlib.util.spec_from_file_location("provisioning_envelope_for_tests", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ENVELOPE_TOOL = _envelope_tool()


def make_package(request_id: str, mac: str, recipient_b64: str, *, family: str, zone_id: str,
                 device_id: str, nvs: bytes, hmac_key: Optional[bytes],
                 tamper: Optional[Callable[[dict], None]] = None) -> EncryptedPackage:
    """A package exactly as build_provisioning_package.py encrypts one."""
    aad = {"schema_version": 1, "firmware_family": family, "request_id": request_id,
           "target_mac": mac, "zone_id": zone_id, "zone_device_id": device_id,
           "nvs_offset": "0x11000", "nvs_size": len(nvs),
           "nvs_sha256": hashlib.sha256(nvs).hexdigest()}
    ciphertext, envelope = ENVELOPE_TOOL.encrypt_for_recipient(nvs, recipient_b64, aad)
    manifest = {"schema_version": 1, "created_at": "2026-11-01T00:00:00Z", "aad": aad,
                "envelope": envelope, "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest()}
    key_ciphertext = b""
    if hmac_key is not None:
        key_aad = {"schema_version": 1, "request_id": request_id, "target_mac": mac,
                   "purpose": "zone-lite-nvs-hmac-efuse-key", "key_size": 32}
        key_ciphertext, key_envelope = ENVELOPE_TOOL.encrypt_for_recipient(
            hmac_key, recipient_b64, key_aad)
        manifest["hmac_key"] = {"aad": key_aad, "envelope": key_envelope,
                                "ciphertext_sha256": hashlib.sha256(key_ciphertext).hexdigest()}
    if tamper:
        tamper(manifest)
    return EncryptedPackage(request_id, manifest, ciphertext, key_ciphertext)


# ------------------------------------------------------------ fake ADD

class FakeServer:
    """ServerService for installer tests, backed by a real BundleStore."""

    def __init__(self, store: BundleStore, bundle: Optional[TestBundle] = None, *,
                 record: Optional[DeviceRecord] = None,
                 capabilities: Optional[Capabilities] = None) -> None:
        self.store = store
        self.source = FakeSource(bundle) if bundle else None
        self.record = record or DeviceRecord(known=False, mac=MAC)
        self.caps = capabilities or Capabilities(True, True, "1.0.0")
        self.requests: List[PackageRequest] = []
        self.events: List[Tuple[str, str, str, dict]] = []
        self.nvs = bytes(range(256)) * (0x6000 // 256)
        self.hmac_key = bytes(range(100, 132))
        self.tamper: Optional[Callable[[dict], None]] = None
        self.package_override: Optional[Callable[[PackageRequest], EncryptedPackage]] = None
        self.fetches = 0

    signed_in_user = "StateHealthAdmin"

    def login(self, username: str, password: str) -> str:
        return username

    def logout(self) -> None:
        pass

    def capabilities(self) -> Capabilities:
        return self.caps

    def bundles(self, family: Optional[Family] = None) -> List[BundleInfo]:
        return []

    def fetch_bundle(self, bundle_id, on_bytes, cancel) -> LocalBundle:
        self.fetches += 1
        assert self.source is not None
        return self.store.fetch(self.source, bundle_id, on_bytes, cancel)

    def device(self, mac: str) -> DeviceRecord:
        return self.record

    def zones(self) -> List[ZoneSummary]:
        return []

    def create_package(self, request: PackageRequest) -> EncryptedPackage:
        self.requests.append(request)
        if self.package_override:
            return self.package_override(request)
        return make_package(request.request_id, request.mac, request.recipient_public_key_b64,
                            family=request.config.family.value, zone_id=request.config.zone_id,
                            device_id=request.config.device_id, nvs=self.nvs,
                            hmac_key=self.hmac_key, tamper=self.tamper)

    def report(self, request_id: str, mac: str, kind: str, detail: dict) -> None:
        self.events.append((request_id, mac, kind, dict(detail)))


# ------------------------------------------------------- clock, usb, serial

class FakeClock:
    """Time moves only when the code under test sleeps (or a test advances it)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.hooks: List[Callable[[float], None]] = []

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        return 1_780_000_000.0 + self.now

    def now_utc(self):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self.wall(), tz=timezone.utc)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for hook in list(self.hooks):
            hook(self.now)

    def sleep(self, seconds: float, cancel: Optional[CancelToken] = None) -> bool:
        if cancel is not None and cancel.cancelled:
            return True
        self.advance(max(seconds, 0.001))
        return bool(cancel and cancel.cancelled)


class FakeUsb:
    """PortFinder whose single board can be unplugged, replugged or renamed."""

    def __init__(self, mac: Optional[str] = MAC, port: str = PORT) -> None:
        self.mac = mac
        self.port = port
        self.present = True
        self.extra_boards = 0

    def candidate(self) -> UsbCandidate:
        return UsbCandidate(self.port, UsbKind.NATIVE, 0x303A, 0x1001, self.mac,
                            "USB JTAG/serial debug unit")

    def find(self, mac: str):
        natives = (1 if self.present else 0) + self.extra_boards
        if self.present and (self.mac is None or self.mac == mac):
            return self.candidate(), natives
        return None, natives


@dataclass
class SerialScript:
    """A board's console over time: when it is plugged in and what it prints."""

    clock: FakeClock
    usb: FakeUsb
    timeline: List[Tuple[float, str, object]] = field(default_factory=list)
    generation: int = 0
    pending: bytes = b""
    opened: List[str] = field(default_factory=list)
    busy_until: float = -1.0

    def at(self, moment: float, action: str, value: object = None) -> "SerialScript":
        self.timeline.append((moment, action, value))
        self.timeline.sort(key=lambda item: item[0])
        return self

    def tick(self, now: float) -> None:
        while self.timeline and self.timeline[0][0] <= now:
            _moment, action, value = self.timeline.pop(0)
            if action == "unplug":
                self.usb.present = False
                self.generation += 1
                self.pending = b""
            elif action == "plug":
                self.usb.present = True
            elif action == "print":
                text = value if isinstance(value, str) else "\n".join(value)  # type: ignore
                self.pending += (text + "\n").encode()

    def opener(self, port: str):
        if self.clock.now < self.busy_until:
            raise PermissionError(13, "Access is denied.")
        if not self.usb.present:
            raise FileNotFoundError(2, "No such file or directory")
        self.opened.append(port)
        return FakeSerial(self, self.generation)


class FakeSerial:
    def __init__(self, script: SerialScript, generation: int) -> None:
        self.script = script
        self.generation = generation
        self.closed = False

    def read(self, size: int) -> bytes:
        if self.script.generation != self.generation or not self.script.usb.present:
            raise OSError("device disconnected")
        data, self.script.pending = self.script.pending[:size], self.script.pending[size:]
        if not data:
            self.script.clock.advance(0.05)
        return data

    def close(self) -> None:
        self.closed = True


def zkt_config(**changes) -> InstallConfig:
    values = dict(family=Family.ZKT, zone_id="LF-ZONE-GUJRAT-01", zone_name="LF-ZONE-GUJRAT-01",
                  device_id="1", wifi_ssid="RE7", wifi_password="test-wifi-pass",
                  bundle_id="zkt-2.9.1-e89380a1",
                  zkt=ZktSettings(port=4370, comm_key=1979, preferred_ip="0.0.0.0"))
    values.update(changes)
    return InstallConfig(**values)
