"""Lay out the provisioning NVS with ESP-IDF 5.5.3's own nvs_partition_gen (2 Nov 2026).

New boards lost zone_cfg on their second boot while it sat on the ACTIVE page their
first run wrote to. The builder now packs the rows the firmware never writes onto
pages padded to exactly 126 entries, which the generator marks FULL. The Wi-Fi, COMM
key and Hikvision host rows, which only an operator's action rewrites, end one of
those pages in entry-state words of their own; the rows a board writes on its first
online run start the next, ACTIVE page (provision_zone_lite.nvs_layout;
docs/esp-installer-server.md, "NVS layout").

This builds real images (ZKT, and Hikvision with CAs up to 3967 bytes, with value
lengths swept) and reads them back with a reader of its own, using the XTS keys the
device derives from its HMAC eFuse key: the page states, the page and entry of every
key, and every value byte for byte as the earlier builder stored it. The earlier
layouts and tampered images must fail the builder's own check, and the ADD package
builder (build_provisioning_package.py) and the CLI must produce the same layout end
to end. It also pins the generator's page-fit rules the layout relies on. Test
values only; CI runs it in espressif/idf:v5.5.3, and the unit tests use its model
and checks."""
from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import hmac
import importlib
import io
import json
import os
import random
import re
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "firmware/zone_lite/tools"
ENTRIES = 126
PAGES = 6
STATE_WORD = 16  # entries whose 2-bit states share one 32-bit bitmap word
ACTIVE, FULL = 0xFFFFFFFE, 0xFFFFFFFC
PRIMITIVES = {"u8": (0x01, "<B"), "u16": (0x02, "<H"), "u32": (0x04, "<I")}

# Never real secrets: a test fleet root, a test MAC and test credentials.
TEST_FLEET_ROOT = "TEST-ONLY-fleet-root-not-a-real-secret-0123456789abcdef"
TEST_MAC = "ac:27:6e:a3:12:34"
ZKT = {
    "firmware_family": "zkt", "wifi_ssid": "SLIC-LHR-Davis-2G", "wifi_password": "TestWifiPass#2026",
    "zkt_port": 4370, "zkt_comm_key": 0, "zkt_preferred_ip": "0.0.0.0", "zkt_expected_serial": "",
    "zone_device_id": "1", "zone_id": "LF-ZONE-LHRDAVIS-01", "zone_name": "Life Zone Lahore Davis Road 01",
    "zkt_recovery_enabled": False,
    "ords_base_url": "https://ords-test.example.invalid/ords/attn/raw_attn_capture_evt",
    "ords_username": "attn_capture_test", "ords_password": "Test-Ords-Password-2026",
    "add_onboard_url": "https://autoattn.slichealth.com/device/v2/onboard",
}
HIK = dict(
    ZKT, firmware_family="hikvision", hik_host="192.168.10.64", hik_port=443, hik_transport="https",
    hik_username="admin", hik_password="Test-Hik-Password-01",
    hik_expected_serial="DS-K1T321MFWX20231234ABCD", hik_profile="DS-K1T321MFWX",
    hik_source_epoch="2026-11-02T00:00:00Z-test-epoch", hik_ca_pem="",
)
# The largest CA the builder accepts (provision_zone_lite.nvs_rows: 3967 bytes), as PEM text.
_BEGIN, _END = "-----BEGIN CERTIFICATE-----\n", "\n-----END CERTIFICATE-----\n"
MAX_CA = _BEGIN + "A" * (3967 - len(_BEGIN) - len(_END)) + _END
# A CA bundle as some tools print one: the generator drops its '#' continuation lines.
PEM_CA = _BEGIN + "\n".join(["B" * 64] * 8) + _END
BUNDLE_CA = "# Issuer: CN=Test Root\n" + PEM_CA + "# Subject: CN=Test Intermediate\n" + PEM_CA


def tool_module(name: str):
    """A firmware tool module imported as the builders import it (from the tools directory)."""
    sys.path.insert(0, str(TOOLS))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(TOOLS))


def tools():
    return tool_module("provision_zone_lite")


def config(name: str, **overrides) -> dict:
    values = dict({"zkt": ZKT, "hik": HIK}[name])
    values.update(overrides)
    return values


def rows_for(values: dict) -> list[list[object]]:
    tool = tools()
    return tool.nvs_rows(values, tool.derive_bootstrap_secret(TEST_MAC, TEST_FLEET_ROOT))


@contextlib.contextmanager
def patched(module, **values):
    """Module attributes set for the duration of a block (to build the earlier layouts)."""
    saved = {name: getattr(module, name) for name in values}
    try:
        for name, value in values.items():
            setattr(module, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(module, name, value)


def string_padding(free: int, first: int, single: bool = False) -> list[list[object]]:
    """Padding that is a string and a u8 even where nvs_layout asks for one-entry rows."""
    rows: list[list[object]] = []
    if free >= 3:
        rows.append([f"_pad{first}", "data", "string", "0" * ((free - 2) * 32 - 1)])
        free = 1
    rows.extend([f"_pad{first + len(rows) + index}", "data", "u8", 0] for index in range(free))
    return rows


def stored(row: list[object]) -> bytes:
    """A row's value bytes as csv and nvs_partition_gen write them (an independent copy of
    their rules: csv writes None as an empty field; the generator reads the file with
    universal newlines and drops every line that starts with '#')."""
    _key, kind, encoding, value = row
    if kind == "namespace":
        return b"\x01" + b"\xff" * 7
    if encoding == "string":
        text = ("" if value is None else str(value)).replace("\r\n", "\n").replace("\r", "\n")
        lines = re.findall(r"[^\n]*\n|[^\n]+", text)  # each line with its newline
        return "".join(lines[:1] + [line for line in lines[1:] if not line.startswith("#")]).encode() + b"\0"
    packed = struct.pack(PRIMITIVES[encoding][1], int(value))
    return packed + b"\xff" * (8 - len(packed))


def simulate_pages(layout: list[list[object]]) -> list[list[tuple[str, int]]]:
    """Where esp-idf-nvs-partition-gen 0.1.9 puts each CSV row, as (key, entries) per page.

    A number or the namespace entry needs one free entry; a string needs its header and
    data entries plus one more (write_varlen_data: entry_num + span >= 126 raises
    PageFullError). PageFullError starts a new page and marks the old one FULL, so every
    page but the last is FULL and the last stays ACTIVE."""
    pages: list[list[tuple[str, int]]] = [[]]
    used = 0
    for key, kind, encoding, value in layout:
        if kind == "namespace" or encoding != "string":
            span, fits = 1, used < ENTRIES
        else:
            span = 1 + -(-len(stored([key, kind, encoding, value])) // 32)
            fits = used + span < ENTRIES
        if not fits:
            pages.append([])
            used = 0
        pages[-1].append((str(key), span))
        used += span
    return pages


def sharing_tail_words(page: list[tuple[str, int, int]], tail: set[str]) -> list[str]:
    """Keys of a page, given as (key, first entry, entries), other than the tail rows and
    one-entry padding whose entry states share a bitmap word with a tail row. Erasing a
    tail row's old entry reprograms the words holding its states, so they must hold
    nothing else, and no item may reach into them from an earlier entry: on a flash that
    garbles a reprogrammed word, ESP-IDF's load would erase such an item, reprogramming
    the words before it, and so on through the page."""
    boundary = min(first for key, first, _ in page if key in tail) // STATE_WORD * STATE_WORD
    return [key for key, first, span in page
            if first + span > boundary and key not in tail and not (key.startswith("_pad") and span == 1)]


def assert_planned_layout(rows: list[list[object]], layout: list[list[object]]) -> None:
    """The planned CSV: the same rows and values as they reach the image, padded so the
    generator fills every provisioning page to 126 entries, with the Wi-Fi, COMM key and
    host rows ending one FULL page in state words of their own and the rows written on
    the first online run alone on the last, ACTIVE page."""
    tool = tools()
    padding = [row for row in layout if str(row[0]).startswith("_pad")]
    plain = [row for row in layout if row not in padding]
    assert len(plain) == len(rows), "rows added or dropped"
    assert {str(row[0]): (*row[1:3], stored(row)) for row in plain} == {
        str(row[0]): (*row[1:3], stored(row)) for row in rows}, "rows changed"
    assert layout[0] == rows[0] == ["zone_cfg", "namespace", "", ""], "namespace not first"
    assert all(row[1:3] in (["data", "string"], ["data", "u8"]) for row in padding), padding
    assert len({row[0] for row in layout}) == len(layout), "duplicate key"
    pages = simulate_pages(layout)
    *full, last = pages
    assert full and len(pages) <= PAGES - 2, f"{len(pages)} data pages"
    for number, page in enumerate(full):
        assert sum(span for _, span in page) == ENTRIES, f"page {number} not filled"
        assert not {key for key, _ in page} & tool.NVS_ACTIVE_KEYS, f"page {number} holds a first-run row"
    assert {key for key, _ in last} == {str(row[0]) for row in rows if row[0] in tool.NVS_ACTIVE_KEYS}
    tail = {str(row[0]) for row in rows if row[0] in tool.NVS_TAIL_KEYS}
    holders = [page for page in full if {key for key, _ in page} & tail]
    assert len(holders) == 1 and tail <= {key for key, _ in holders[0]}, "tail rows split"
    entries, first = [], 0
    for key, span in holders[0]:
        entries.append((key, first, span))
        first += span
    assert not sharing_tail_words(entries, tail), sharing_tail_words(entries, tail)


def device_xts_key(hmac_key: bytes) -> bytes:
    """The XTS keys nvs_sec_provider derives on the device (HMAC over the two seeds)."""
    encryption = hmac.new(hmac_key, struct.pack("<I", 0xAEBE5A5A) * 8, hashlib.sha256).digest()
    tweak = hmac.new(hmac_key, struct.pack("<I", 0xCEDEA5A5) * 8, hashlib.sha256).digest()
    return encryption + tweak


def read_image(image: bytes, xts_key: bytes) -> tuple[list[dict], dict[str, tuple[int, int, int, bytes]]]:
    """Page states and every item, decrypted entry by entry (tweak = partition offset)."""

    def decrypt(offset: int) -> bytes:
        cipher = Cipher(algorithms.AES(xts_key), modes.XTS(struct.pack("<I", offset) + bytes(12)))
        return cipher.decryptor().update(image[offset:offset + 32])

    pages, items = [], {}
    for number in range(len(image) // 4096):
        base = number * 4096
        raw = image[base:base + 4096]
        if raw == b"\xff" * 4096:
            pages.append({"state": "BLANK", "written": 0})
            continue
        state = {ACTIVE: "ACTIVE", FULL: "FULL"}.get(struct.unpack_from("<I", raw)[0], "OTHER")
        states = [(raw[32 + index // 4] >> (2 * (index % 4))) & 3 for index in range(ENTRIES)]
        index = 0
        while index < ENTRIES and states[index] == 2:
            header = decrypt(base + 64 + 32 * index)
            assert zlib.crc32(header[:4] + header[8:], 0xFFFFFFFF) == struct.unpack_from("<I", header, 4)[0]
            namespace, kind, span = header[0], header[1], header[2]
            value = header[24:]
            if kind == 0x21:
                size, crc = struct.unpack_from("<H", header, 24)[0], struct.unpack_from("<I", header, 28)[0]
                value = b"".join(decrypt(base + 64 + 32 * part) for part in range(index + 1, index + span))
                value = value[:size]
                assert zlib.crc32(value, 0xFFFFFFFF) == crc, "string CRC"
            key = header[8:24].rstrip(b"\0").decode()
            assert key not in items, f"{key} stored twice"
            items[key] = (number, index, namespace, value, span)
            index += span
        assert all(entry == 3 for entry in states[index:]), f"page {number}: entries after the items"
        pages.append({"state": state, "written": index})
    return pages, items


def assert_image_layout(image: bytes, hmac_key: bytes, rows: list[list[object]]) -> list[str]:
    """The real image, read independently: page states and every key's page, entries and value."""
    tool = tools()
    pages, items = read_image(image, device_xts_key(hmac_key))
    assert items["zone_cfg"][:3] == (0, 0, 0), "namespace entry is not page 0 entry 0"
    expected = {str(row[0]): stored(row) for row in rows}
    plain = {key: value for key, value in items.items() if not key.startswith("_pad")}
    assert set(plain) == set(expected), set(plain) ^ set(expected)
    for key, value in expected.items():
        assert plain[key][3] == value, f"{key} value changed"
    full = {items[key][0] for key in items if key not in tool.NVS_ACTIVE_KEYS}
    active = {items[key][0] for key in items if key in tool.NVS_ACTIVE_KEYS}
    for number in full:
        assert pages[number] == {"state": "FULL", "written": ENTRIES}, (number, pages[number])
    assert len(active) == 1 and min(active) > max(full), (full, active)
    assert pages[min(active)]["state"] == "ACTIVE"
    assert all(page["state"] == "BLANK" for page in pages[min(active) + 1:])
    assert sum(page["state"] == "BLANK" for page in pages) >= 2
    tail = {key for key in items if key in tool.NVS_TAIL_KEYS}
    holder = {items[key][0] for key in tail}
    assert len(holder) == 1, holder
    page = [(key, item[1], item[4]) for key, item in items.items() if item[0] in holder]
    assert not sharing_tail_words(page, tail), sharing_tail_words(page, tail)
    return [f"{page['state']}/{page['written']}" if page["state"] != "BLANK" else "BLANK"
            for page in pages]


def tail_entries(image: bytes, hmac_key: bytes) -> str:
    """Where the Wi-Fi, COMM key and host rows sit: page and first to last entry."""
    _, items = read_image(image, device_xts_key(hmac_key))
    tail = [items[key] for key in tools().NVS_TAIL_KEYS if key in items]
    return f"page {tail[0][0]} entries {min(item[1] for item in tail)}-{max(item[1] + item[4] - 1 for item in tail)}"


def generator() -> Path | None:
    idf = Path(os.environ.get("IDF_PATH", Path.home() / "esp/esp-idf-v5.5.3"))
    candidate = idf / "components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py"
    return candidate if candidate.is_file() else None


def build(rows: list[list[object]], directory: Path) -> tuple[bytes, bytes]:
    """The image the builders write (provision_zone_lite.build_nvs_partition), its HMAC key."""
    tool = tools()
    directory.mkdir(parents=True)
    hmac_key = tool.derive_nvs_hmac_key(TEST_MAC, TEST_FLEET_ROOT)
    (directory / "hmac-key.bin").write_bytes(hmac_key)
    path = tool.build_nvs_partition(rows, directory / "hmac-key.bin", directory, generator())
    return path.read_bytes(), hmac_key


def build_earlier(rows: list[list[object]], directory: Path) -> tuple[bytes, bytes]:
    """The image the builders wrote before the layout (nvs_rows order, no check), or of
    any other plan given as rows."""
    tool = tools()
    directory.mkdir(parents=True)
    hmac_key = tool.derive_nvs_hmac_key(TEST_MAC, TEST_FLEET_ROOT)
    (directory / "hmac-key.bin").write_bytes(hmac_key)
    with (directory / "provision.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["key", "type", "encoding", "value"])
        writer.writerows(rows)
    for command in (
        ["generate-key", "--key_protect_hmac", "--kp_hmac_inputkey", str(directory / "hmac-key.bin"),
         "--keyfile", str(directory / "nvs-xts-key.bin"), "--outdir", str(directory)],
        ["encrypt", str(directory / "provision.csv"), str(directory / "provision.bin"), "0x6000",
         "--inputkey", str(directory / "nvs-xts-key.bin")],
    ):
        subprocess.run([sys.executable, str(generator()), *command], check=True, capture_output=True)
    return (directory / "provision.bin").read_bytes(), hmac_key


def refused(image: bytes, hmac_key: bytes, layout: list[list[object]]) -> str:
    """The builder's own check on an image: the reason it refuses it, or '' if it passes."""
    try:
        tools().check_nvs_layout(image, device_xts_key(hmac_key), layout)
    except RuntimeError as error:
        return str(error)
    return ""


def probe_fit_rules(directory: Path) -> None:
    """The generator's rules nvs_layout relies on: a string ends by entry 124 or moves to
    a new page, a number may take entry 125, and 3967 bytes is the longest string."""
    directory.mkdir(parents=True)

    def generate(rows: list[list[object]]) -> subprocess.CompletedProcess:
        csv_path = directory / "probe.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["key", "type", "encoding", "value"])
            writer.writerows([["zone_cfg", "namespace", "", ""], *rows])
        return subprocess.run([sys.executable, str(generator()), "generate", str(csv_path),
                               str(directory / "probe.bin"), "0x6000"], capture_output=True)

    def place(rows: list[list[object]]) -> dict[str, tuple[int, int]]:
        generate(rows).check_returncode()
        image, found = (directory / "probe.bin").read_bytes(), {}
        for number in range(PAGES):
            raw = image[number * 4096:(number + 1) * 4096]
            index = 0
            while index < ENTRIES and (raw[32 + index // 4] >> (2 * (index % 4))) & 3 == 2:
                entry = raw[64 + 32 * index:96 + 32 * index]
                found[entry[8:24].rstrip(b"\0").decode()] = (number, index)
                index += entry[2] if entry[1] == 0x21 else 1
        return found

    numbers = [[f"n{index}", "data", "u8", 0] for index in range(125)]
    # After the namespace entry and 122 numbers a 2-entry string ends at entry 124;
    # after 123 numbers it would end at 125, so it starts page 1.
    assert place(numbers[:122] + [["s", "data", "string", "x"]])["s"] == (0, 123)
    assert place(numbers[:123] + [["s", "data", "string", "x"]])["s"] == (1, 0)
    # A number takes entry 125; the row after it starts page 1.
    assert place(numbers[:124] + [["u", "data", "u32", 1]])["u"] == (0, 125)
    assert place(numbers[:125] + [["u", "data", "u32", 1]])["u"] == (1, 0)
    # The longest string (3967 bytes and its NUL: 125 entries) needs a page of its own.
    assert place([["c", "data", "string", "c" * 3967]])["c"] == (1, 0)
    assert generate([["c", "data", "string", "c" * 3968]]).returncode != 0, "3968 bytes accepted"


def erase_entry(image: bytes, page: int, index: int) -> bytes:
    """The image with one entry's state set to ERASED (0b00)."""
    offset = page * 4096 + 32 + index // 4
    return image[:offset] + bytes([image[offset] & ~(3 << 2 * (index % 4)) & 0xFF]) + image[offset + 1:]


def random_config(seed: int) -> dict:
    """Value lengths anywhere inside the request limits (validate_request, nvs_rows)."""
    rng = random.Random(seed)

    def text(low: int, high: int) -> str:
        return "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(rng.randint(low, high)))

    values = config("hik" if seed % 2 else "zkt")
    values.update(
        wifi_ssid=text(1, 32), wifi_password=text(8, 63), zone_device_id=text(1, 31), zone_id=text(1, 64),
        zone_name=text(1, 120), zkt_expected_serial=text(0, 64), ords_base_url=text(20, 200),
        ords_username=text(4, 60), ords_password=text(8, 120), zkt_comm_key=rng.randint(0, 2**32 - 1),
    )
    if seed % 2:
        values.update(hik_username=text(1, 63), hik_password=text(1, 127), hik_expected_serial=text(1, 119),
                      hik_profile=text(1, 79), hik_source_epoch=text(1, 64),
                      hik_ca_pem=rng.choice(["", text(1, 3967), text(1200, 2200), text(3900, 3967)]))
    return values


def package_end_to_end(directory: Path, values: dict) -> tuple[bytes, bytes]:
    """build_provisioning_package.py as the ADD provisioner runs it, with the v2 request
    shape; returns the NVS image and HMAC key decrypted from the envelopes."""
    envelope = tool_module("provisioning_envelope")
    recipient = X25519PrivateKey.generate()
    public = recipient.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    request = {key: values[key] for key in (
        "firmware_family", "wifi_ssid", "wifi_password", "zkt_port", "zkt_comm_key", "zkt_preferred_ip",
        "zone_device_id", "zone_id", "zone_name") if key in values}
    request.update({key: value for key, value in values.items() if key.startswith("hik_")})
    request.update(request_id="nvs-layout-check-0001", target_mac=TEST_MAC, zkt_expected_serial="",
                   zkt_recovery_enabled=False, recipient_public_key_b64=base64.b64encode(public).decode())
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(json.dumps(request), encoding="utf-8")
    output = directory / "package"
    subprocess.run(
        [sys.executable, str(TOOLS / "build_provisioning_package.py"), "--request", str(directory / "request.json"),
         "--output", str(output), "--idf-path", str(generator().parents[3])],
        check=True, capture_output=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(TOOLS), "PYTHONDONTWRITEBYTECODE": "1",
             "ADD_FLEET_ROOT_SECRET": TEST_FLEET_ROOT, "ADD_ORDS_BASE_URL": values["ords_base_url"],
             "ADD_ORDS_USERNAME": values["ords_username"], "ADD_ORDS_PASSWORD": values["ords_password"]})
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    private = recipient.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                      serialization.NoEncryption())
    image = envelope.decrypt_envelope((output / "provision.bin.enc").read_bytes(), private, manifest)
    hmac_key = envelope.decrypt_envelope((output / "hmac-key.bin.enc").read_bytes(), private,
                                         manifest["hmac_key"])
    return image, hmac_key


def cli_end_to_end(directory: Path, values: dict) -> tuple[bytes, bytes]:
    """provision_zone_lite.py, the per-device CLI, with --skip-firmware-flash and stand-ins
    for the board (no serial port is opened): the image it writes to 0x11000, its HMAC key."""
    tool = tools()
    directory.mkdir(parents=True)
    (directory / "zone.json").write_text(json.dumps(values), encoding="utf-8")
    flashed: dict[str, bytes] = {}
    real_run = tool.run

    def board(command: list[str], *, cwd: Path | None = None) -> str:
        if command[0] != "test-esptool":
            return real_run(command, cwd=cwd)
        if "write_flash" in command:
            flashed["image"] = Path(command[-1]).read_bytes()
        elif "read_flash" in command:
            Path(command[-1]).write_bytes(flashed["image"])
        return ""

    saved = {name: getattr(tool, name) for name in
             ("run", "read_mac", "validate_secure_build_config", "ensure_nvs_hmac_key")}
    environment = {name: os.environ.pop(name, None)
                   for name in ("ADD_FLEET_ROOT_SECRET", "ZONE_LITE_NVS_HMAC_ROOT_SECRET")}
    argv = sys.argv
    try:
        tool.run = board
        tool.read_mac = lambda _esptool, _port: TEST_MAC
        tool.validate_secure_build_config = lambda _project: None
        tool.ensure_nvs_hmac_key = lambda **_arguments: None
        os.environ["ADD_FLEET_ROOT_SECRET"] = TEST_FLEET_ROOT
        sys.argv = ["provision_zone_lite.py", "--port", "test-port", "--config", str(directory / "zone.json"),
                    "--idf-path", str(generator().parents[3]), "--esptool", "test-esptool",
                    "--skip-build", "--skip-firmware-flash"]
        with contextlib.redirect_stdout(io.StringIO()):
            tool.main()
    finally:
        for name, value in saved.items():
            setattr(tool, name, value)
        sys.argv = argv
        for name, value in environment.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
    return flashed["image"], tool.derive_nvs_hmac_key(TEST_MAC, TEST_FLEET_ROOT)


def run_checks(directory: Path) -> list[str]:
    tool = tools()
    lines = []
    probe_fit_rules(directory / "probe")
    lines.append("generator fit rules: a string ends by entry 124, a number may take entry 125, "
                 "3967 bytes is the longest string")
    named = {
        "zkt": config("zkt"),
        "zkt long values": config("zkt", wifi_ssid="S" * 32, wifi_password="P" * 63, zone_id="Z" * 64,
                                  zone_name="N" * 120, zkt_expected_serial="T" * 64, zkt_comm_key=2**32 - 1),
        "hikvision": config("hik"),
        "hikvision 3967-byte CA": config("hik", hik_ca_pem=MAX_CA),
        "hikvision CRLF CA": config("hik", hik_ca_pem=MAX_CA[:2000].replace("\n", "\r\n")),
        "hikvision CA with '#' lines": config("hik", hik_ca_pem=BUNDLE_CA),
        "zkt JSON nulls": config("zkt", zkt_telnet_username=None, zkt_telnet_password=None),
    }
    # With these values a CA of up to 1823 bytes leaves room for the tail rows on page 0
    # and 1824 gives them a page of their own; 2271 fills page 0 exactly, and from 2272
    # rows move to page 1, which then also holds the tail rows.
    for length in (1, 1000, 1500, 1823, 1824, 2271, 2272, 2600, 3966):
        named[f"hikvision {length}-byte CA"] = config("hik", hik_ca_pem="C" * length)
    for seed in range(16):
        named[f"random {seed}"] = random_config(seed)
    shapes: dict[str, list[str]] = {}
    tails: dict[str, list[str]] = {}
    for number, (label, values) in enumerate(named.items()):
        rows = rows_for(values)
        layout = tool.nvs_layout(rows)
        assert_planned_layout(rows, layout)
        image, hmac_key = build(rows, directory / f"image-{number}")
        pages = assert_image_layout(image, hmac_key, rows)
        assert simulate_pages(layout) and len(simulate_pages(layout)) == sum(p != "BLANK" for p in pages)
        earlier, earlier_key = build_earlier(rows, directory / f"earlier-{number}")
        _, earlier_items = read_image(earlier, device_xts_key(earlier_key))
        _, items = read_image(image, device_xts_key(hmac_key))
        assert {key: item[3] for key, item in earlier_items.items()} == {
            key: item[3] for key, item in items.items() if not key.startswith("_pad")}, label
        assert refused(earlier, earlier_key, tool.nvs_layout(rows)), label
        shapes.setdefault(" ".join(pages), []).append(label)
        tails.setdefault(tail_entries(image, hmac_key), []).append(label)
    for shape, labels in shapes.items():
        lines.append(f"{shape}: {', '.join(labels)}")
    for where, labels in sorted(tails.items()):
        lines.append(f"Wi-Fi, COMM key and host rows at {where}: {len(labels)} images")
    lines.append(f"{len(named)} images: every value as the earlier builder stored it, "
                 "the earlier layout refused by the check")
    # A value whose last line starts with '#': the earlier builder's CSV lost its closing
    # quote with that line; the line is dropped before the CSV is written.
    values = config("hik", hik_ca_pem=PEM_CA + "# trailing comment")
    image, hmac_key = build(rows_for(values), directory / "trailing-comment")
    assert_image_layout(image, hmac_key, rows_for(values))
    assert read_image(image, device_xts_key(hmac_key))[1]["hik_ca"][3] == PEM_CA.encode() + b"\0"
    lines.append("a CA ending in a '#' line: stored without it, every other row intact")
    rows = rows_for(config("zkt"))
    image, hmac_key = build(rows, directory / "tamper")
    layout = tool.nvs_layout(rows)
    renamed = layout[:-1] + [[f"{layout[-1][0]}x", *layout[-1][1:]]]
    # Other plans, generated as planned: the first version of this fix (every rewritten
    # row on the ACTIVE page); the tail rows straight after the other rows (a 1870-byte
    # CA then leaves rows the firmware never writes in entries 112-113, the tail rows'
    # state word); and a padding string in that word.
    with patched(tool, NVS_TAIL_KEYS=frozenset(), NVS_ACTIVE_KEYS=tool.NVS_MUTABLE_KEYS):
        first_fix = tool.nvs_layout(rows)
    shared_rows = rows_for(config("hik", hik_ca_pem="C" * 1870))
    with patched(tool, NVS_STATE_WORD_ENTRIES=1):
        shared = tool.nvs_layout(shared_rows)
    with patched(tool, _nvs_padding=string_padding):
        spanning = tool.nvs_layout(rows)
    first_fix_image, _ = build_earlier(first_fix, directory / "tamper-first-fix")
    shared_image, _ = build_earlier(shared, directory / "tamper-shared-word")
    spanning_image, _ = build_earlier(spanning, directory / "tamper-string-padding")
    tampered = {
        "page 0 ACTIVE": (struct.pack("<I", ACTIVE) + image[4:], layout),
        "entry 125 of page 0 erased": (erase_entry(image, 0, 125), layout),
        "entry 0 of page 1 erased": (erase_entry(image, 1, 0), layout),
        "a changed byte": (image[:4096 + 96] + bytes([image[4096 + 96] ^ 1]) + image[4096 + 97:], layout),
        "a key the plan does not have": (image, renamed),
        "Wi-Fi on the ACTIVE page": (first_fix_image, first_fix),
        "a tail state word shared": (shared_image, shared),
        "a padding string in it": (spanning_image, spanning),
    }
    reasons = {}
    for label, (candidate, plan) in tampered.items():
        reasons[label] = refused(candidate, hmac_key, plan)
        assert reasons[label], f"{label} was not refused"
    assert not refused(image, hmac_key, layout)
    lines.append("refused: " + "; ".join(f"{label} ({reason.split(': ', 1)[1]})"
                                         for label, reason in reasons.items()))
    for label, values in (("zkt", config("zkt")), ("hikvision 3967-byte CA", config("hik", hik_ca_pem=MAX_CA))):
        image, hmac_key = package_end_to_end(directory / f"package-{label.split()[0]}", values)
        pages = assert_image_layout(image, hmac_key, rows_for(values))
        lines.append(f"build_provisioning_package.py {label}: {' '.join(pages)}")
    image, hmac_key = cli_end_to_end(directory / "cli", config("zkt"))
    pages = assert_image_layout(image, hmac_key, rows_for(config("zkt")))
    lines.append(f"provision_zone_lite.py zkt: {' '.join(pages)}")
    return lines


def main() -> int:
    if generator() is None:
        print("ESP-IDF 5.5.3's nvs_partition_gen is required (CI runs this in espressif/idf:v5.5.3)")
        return 1
    with tempfile.TemporaryDirectory(prefix="provisioning-nvs-") as directory:
        for line in run_checks(Path(directory)):
            print(line)
    print("provisioning NVS layout checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
