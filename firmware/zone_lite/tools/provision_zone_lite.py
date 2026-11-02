#!/usr/bin/env python3
"""Build, provision, flash, and verify one Zone Lite ESP32-S3.

Secrets are accepted only through environment variables or a caller-owned JSON
file. Generated CSV/NVS material lives in a TemporaryDirectory and is destroyed
after the read-back hash has been verified.
"""

from __future__ import annotations

import argparse
import hmac
import csv
import hashlib
import json
import ipaddress
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from typing import NoReturn
import zlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import base64


HKDF_SALT = b"state-life-zone-lite-onboarding-v1"
NVS_HMAC_SALT = b"state-life-zone-lite-nvs-hmac-v1"
MAC_PATTERN = re.compile(r"MAC:\s*([0-9a-f:]{17})", re.IGNORECASE)
NVS_PARTITION_SIZE = 0x6000
NVS_PARTITION_OFFSET = "0x11000"
# Pages of the generated NVS (esp-idf-nvs-partition-gen 0.1.9, as ESP-IDF 5.5.3's
# nvs_flash reads them): a 32-byte header, a 2-bit state per entry, 126 entries.
NVS_PAGE_SIZE = 0x1000
NVS_PAGE_COUNT = NVS_PARTITION_SIZE // NVS_PAGE_SIZE
NVS_PAGE_ENTRIES = 126
NVS_ENTRY_SIZE = 32
NVS_PAGE_ACTIVE = 0xFFFFFFFE
NVS_PAGE_FULL = 0xFFFFFFFC
NVS_PRIMITIVES = {
    "u8": (0x01, "<B"), "i8": (0x11, "<b"), "u16": (0x02, "<H"), "i16": (0x12, "<h"),
    "u32": (0x04, "<I"), "i32": (0x14, "<i"), "u64": (0x08, "<Q"), "i64": (0x18, "<q"),
}
NVS_STRING = 0x21
NVS_XTS_KEY_SIZE = 64
# The zone_cfg rows a running board rewrites itself (zone_config.c of ZKT 2.7.8,
# HIK 3.1.1 and main, and the USB Wi-Fi rescue). No other provisioned row is ever
# written after installation.
NVS_MUTABLE_KEYS = frozenset({
    "wifi_ssid", "wifi_pass", "zkt_key", "zkt_serial", "add_ws", "conn_id", "dev_token", "hik_host",
})
# Of those, the rows only an operator's action rewrites: Wi-Fi saved by the setup
# portal or the USB rescue, an authenticated COMM-key change and, in Hikvision
# builds from main, a re-discovered terminal host. A board needs them to come back
# online, so nvs_layout keeps them on a FULL page.
NVS_TAIL_KEYS = frozenset({"wifi_ssid", "wifi_pass", "zkt_key", "hik_host"})
# The rows a new board writes on its first online run: the ADD connector identity
# and the pinned ZKT serial. Every image holds them empty and ADD issues the
# identity again when a board onboards, so they start the ACTIVE page.
NVS_ACTIVE_KEYS = NVS_MUTABLE_KEYS - NVS_TAIL_KEYS
# Entries per 32-bit word of a page's entry-state bitmap (2 bits each). Erasing an
# entry programs the whole word that holds its state.
NVS_STATE_WORD_ENTRIES = 16
NVS_PADDING_PREFIX = "_pad"
# The page ESP-IDF keeps erased for its garbage collection, and one more.
NVS_MIN_BLANK_PAGES = 2
SECURE_SDKCONFIG_VALUES = (
    "CONFIG_NVS_ENCRYPTION=y",
    "CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC=y",
    "CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID=0",
    "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y",
    "CONFIG_SECURE_BOOT=y",
    "CONFIG_SECURE_BOOT_V2_ENABLED=y",
)
SECURE_BUILD_DEFINES = (
    "#define CONFIG_NVS_ENCRYPTION 1",
    "#define CONFIG_NVS_SEC_KEY_PROTECT_USING_HMAC 1",
    "#define CONFIG_NVS_SEC_HMAC_EFUSE_KEY_ID 0",
    "#define CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE 1",
    "#define CONFIG_SECURE_BOOT 1",
    "#define CONFIG_SECURE_BOOT_V2_ENABLED 1",
)


def run(command: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.stdout


def normalize_mac(value: str) -> str:
    compact = "".join(character for character in value.lower() if character in "0123456789abcdef")
    if len(compact) != 12:
        raise ValueError("Could not normalize the ESP Wi-Fi MAC address")
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def derive_bootstrap_secret(mac: str, fleet_root: str) -> str:
    normalized = normalize_mac(mac)
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=HKDF_SALT,
        info=f"zone-lite:{normalized}".encode("ascii"),
    ).derive(fleet_root.encode("utf-8"))
    return base64.urlsafe_b64encode(derived).decode("ascii")


def derive_nvs_hmac_key(mac: str, fleet_root: str) -> bytes:
    normalized = normalize_mac(mac)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=NVS_HMAC_SALT,
        info=f"zone-lite-nvs:{normalized}".encode("ascii"),
    ).derive(fleet_root.encode("utf-8"))


def load_config(path: Path) -> dict:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError("Provisioning JSON must contain an object")
    fleet_root = os.environ.get("ADD_FLEET_ROOT_SECRET")
    if not fleet_root:
        raise ValueError(
            "Set ADD_FLEET_ROOT_SECRET explicitly before provisioning; "
            "ADD_PII_LOOKUP_KEY is never a safe provisioning fallback"
        )
    values["fleet_root"] = fleet_root
    values["nvs_hmac_root"] = (
        os.environ.get("ZONE_LITE_NVS_HMAC_ROOT_SECRET") or fleet_root
    )
    return values


def validate_root_selection(
    *,
    mac: str,
    fleet_root: str,
    nvs_hmac_root: str,
    recovery_confirmation: str | None,
    trust_existing: bool,
) -> bool:
    """Require an exact-MAC ceremony before using separate onboarding/NVS roots.

    Normal provisioning derives both domains from ADD_FLEET_ROOT_SECRET. A split
    is supported only to recover a device whose unreadable HMAC eFuse was
    previously derived from the wrong-but-known root. It must never become the
    accidental default for a new eFuse burn.
    """
    roots_match = hmac.compare_digest(
        hashlib.sha256(fleet_root.encode("utf-8")).digest(),
        hashlib.sha256(nvs_hmac_root.encode("utf-8")).digest(),
    )
    if roots_match:
        if recovery_confirmation:
            raise RuntimeError(
                "--confirm-split-root-recovery-for was supplied but the roots match"
            )
        return False
    if not trust_existing:
        raise RuntimeError(
            "Split-root recovery is allowed only with --trust-existing-derived-hmac"
        )
    try:
        confirmed_mac = normalize_mac(recovery_confirmation or "")
    except ValueError:
        confirmed_mac = ""
    if confirmed_mac != mac:
        raise RuntimeError(
            "Split-root recovery requires explicit approval for this exact ESP. "
            f"Re-run with --confirm-split-root-recovery-for {mac}."
        )
    return True


def read_mac(esptool: str, port: str) -> str:
    output = run([esptool, "--port", port, "chip_id"])
    match = MAC_PATTERN.search(output)
    if not match:
        raise RuntimeError("esptool did not report a Wi-Fi MAC address")
    return normalize_mac(match.group(1))


def nvs_rows(config: dict, bootstrap_secret: str) -> list[list[object]]:
    family = config.get("firmware_family", "zkt")
    if family not in {"zkt", "hikvision"}:
        raise ValueError("Unknown firmware family")
    required = (
        "wifi_ssid",
        "wifi_password",
        "zone_device_id",
        "zone_id",
        "zone_name",
        "ords_base_url",
        "ords_username",
        "ords_password",
    )
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing provisioning values: {', '.join(missing)}")
    values: list[tuple[str, str, str, object]] = [
        ("zone_cfg", "namespace", "", ""),
        ("fw_family", "data", "string", family),
        ("provisioned", "data", "u8", 1),
        ("wifi_ssid", "data", "string", config["wifi_ssid"]),
        ("wifi_pass", "data", "string", config["wifi_password"]),
        ("zkt_port", "data", "u16", int(config.get("zkt_port", 4370))),
        ("zkt_key", "data", "u32", int(config.get("zkt_comm_key", 0))),
        ("zkt_ip", "data", "string", config.get("zkt_preferred_ip", "0.0.0.0")),
        ("zkt_serial", "data", "string", config.get("zkt_expected_serial", "")),
        ("zone_dev", "data", "string", config["zone_device_id"]),
        ("zone_id", "data", "string", config["zone_id"]),
        ("zone_name", "data", "string", config["zone_name"]),
        ("ords_url", "data", "string", config["ords_base_url"]),
        ("ords_user", "data", "string", config["ords_username"]),
        ("ords_pass", "data", "string", config["ords_password"]),
        ("rec_enable", "data", "u8", int(bool(config.get("zkt_recovery_enabled", False)))),
        ("rec_fails", "data", "u32", int(config.get("zkt_recovery_failures", 2))),
        ("rec_cool", "data", "u32", int(config.get("zkt_recovery_cooldown_ms", 1_800_000))),
        ("reboot_wait", "data", "u32", int(config.get("zkt_reboot_wait_ms", 90_000))),
        ("tel_port", "data", "u16", int(config.get("zkt_telnet_port", 23))),
        ("tel_user", "data", "string", config.get("zkt_telnet_username", "")),
        ("tel_pass", "data", "string", config.get("zkt_telnet_password", "")),
        ("tel_banner", "data", "string", config.get("zkt_telnet_banner", "Linux")),
        ("tel_cmd", "data", "string", config.get("zkt_telnet_command", "reboot")),
        ("add_enabled", "data", "u8", 1),
        (
            "add_onboard",
            "data",
            "string",
            config.get("add_onboard_url", "https://autoattn.slichealth.com/device/v2/onboard"),
        ),
        ("add_ws", "data", "string", ""),
        ("boot_secret", "data", "string", bootstrap_secret),
        ("conn_id", "data", "string", ""),
        ("dev_token", "data", "string", ""),
    ]
    if family == "hikvision":
        def text(key: str, maximum: int, optional: bool = False) -> str:
            value = config.get(key, "")
            if (not isinstance(value, str) or (not value and not optional)
                    or len(value.encode()) > maximum or "\x00" in value):
                raise ValueError(f"Invalid Hikvision provisioning field: {key}")
            return value

        host = text("hik_host", 63)
        address = ipaddress.ip_address(host)
        if not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError("Hikvision host must be a LAN address")
        if address.version != 4 or address.is_link_local:
            raise ValueError("Hikvision installation currently requires an IPv4 LAN address")
        mode = config.get("hik_transport", "https")
        if mode not in {"https", "http_digest"}:
            raise ValueError("Explicit Hikvision transport is required")
        port = config.get("hik_port", 443 if mode == "https" else 80)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid Hikvision port")
        values.extend([
            ("hik_host", "data", "string", host),
            ("hik_port", "data", "u16", port),
            ("hik_https", "data", "u8", int(mode == "https")),
            ("hik_http", "data", "u8", int(mode == "http_digest")),
            ("hik_user", "data", "string", text("hik_username", 63)),
            ("hik_pass", "data", "string", text("hik_password", 127)),
            ("hik_serial", "data", "string", text("hik_expected_serial", 119)),
            ("hik_profile", "data", "string", text("hik_profile", 79)),
            ("hik_epoch", "data", "string", text("hik_source_epoch", 64)),
            # nvs_partition_gen (ESP-IDF 5.5.3) fits one string and its NUL on
            # one page: 3967 bytes at most, though the firmware buffer is 4096.
            ("hik_ca", "data", "string", text("hik_ca_pem", 3967, optional=True)),
        ])
    return [[key, kind, encoding, value] for key, kind, encoding, value in values]


def find_nvs_generator(idf_path: Path) -> Path:
    candidate = idf_path / "components" / "nvs_flash" / "nvs_partition_generator" / "nvs_partition_gen.py"
    if not candidate.is_file():
        raise FileNotFoundError(f"NVS partition generator not found under {idf_path}")
    return candidate


def nvs_string_value(value: object) -> str:
    """A string row's value as it reaches the image, which nvs_layout writes to the CSV.

    csv writes None as an empty field. nvs_partition_gen reads the CSV with universal
    newlines (CR and CRLF become LF) and drops every line that starts with '#' before
    parsing it, so a multi-line value (a CA bundle with '# Subject:' lines) loses
    those continuation lines, each with its newline; the earlier builder stored the
    values that way too. Written to the CSV this way, a value's last line can no
    longer take the closing quote with it.
    """
    lines = ("" if value is None else str(value)).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "".join(line + "\n" * (index < len(lines) - 1) for index, line in enumerate(lines)
                   if not (index and line.startswith("#")))


def nvs_stored_string(value: object) -> bytes:
    """A string row's bytes in the image: nvs_string_value and the NUL."""
    return nvs_string_value(value).encode("utf-8") + b"\0"


def _nvs_normalized(row: list[object]) -> list[object]:
    """A row with its string value as it reaches the image."""
    key, kind, encoding, value = row
    return [key, kind, encoding, nvs_string_value(value) if encoding == "string" else value]


def nvs_row_entries(row: list[object]) -> int:
    """The 32-byte entries a row takes: one for a namespace or a number, else a header
    plus the string's data entries."""
    _key, kind, encoding, _value = row
    if kind == "namespace" or encoding in NVS_PRIMITIVES:
        return 1
    if encoding == "string":
        return 1 + -(-len(nvs_stored_string(row[3])) // NVS_ENTRY_SIZE)
    raise ValueError("Unsupported provisioning NVS encoding")


def nvs_row_fits(used: int, row: list[object]) -> bool:
    """nvs_partition_gen 0.1.9's rule: a number may take the last entry (125); a string
    and its NUL must end by entry 124, or the generator starts a new page for it."""
    if row[1] == "namespace" or row[2] in NVS_PRIMITIVES:
        return used < NVS_PAGE_ENTRIES
    return used + nvs_row_entries(row) < NVS_PAGE_ENTRIES


def _nvs_padding(free: int, first: int, single: bool = False) -> list[list[object]]:
    """Rows the firmware never reads that take exactly `free` entries: a string, then
    one u8 (so the string never takes entry 125), or a u8 per entry when free < 3 or
    `single` asks for one-entry rows only."""
    rows: list[list[object]] = []
    if free >= 3 and not single:
        filler = "0" * ((free - 2) * NVS_ENTRY_SIZE - 1)
        rows.append([f"{NVS_PADDING_PREFIX}{first}", "data", "string", filler])
        free = 1
    start = first + len(rows)
    rows.extend([f"{NVS_PADDING_PREFIX}{start + index}", "data", "u8", 0] for index in range(free))
    return rows


def nvs_layout(rows: list[list[object]]) -> list[list[object]]:
    """Order nvs_rows() for the image so that normal operation never writes a page
    holding provisioning data.

    1. The namespace entry and every row the firmware never writes, largest first,
       on pages padded with _padN keys to exactly 126 entries. The generator marks
       those pages FULL. ESP-IDF 5.5.3 programs a FULL page only to erase an entry on
       it, and reclaims only pages that have erased entries.
    2. NVS_TAIL_KEYS (Wi-Fi, COMM key, Hikvision host) end the last such page that
       has room, from a state-word boundary: the bitmap words holding their states
       hold nothing but them and one-entry padding. An operator's rewrite of one
       erases its entry there, which programs only those words: never the namespace
       entry's, and no other row's. No entry there belongs to an item that starts
       before them, so a word that a faulty flash garbles when it is reprogrammed
       cannot make ESP-IDF's load-time repairs erase their way into the rest of the
       page.
    3. NVS_ACTIVE_KEYS, which a new board writes on its first online run, start the
       next page. It stays ACTIVE and takes every runtime write.

    String values are given as they reach the image (nvs_string_value).
    """
    if not rows or rows[0][1] != "namespace" or any(row[1] == "namespace" for row in rows[1:]):
        raise ValueError("Provisioning NVS rows must hold exactly one namespace")
    if any(str(row[0]).startswith(NVS_PADDING_PREFIX) for row in rows):
        raise ValueError("Provisioning NVS keys may not use the padding prefix")
    rows = [_nvs_normalized(row) for row in rows]
    pages: list[list[list[object]]] = [[rows[0]]]
    used = [nvs_row_entries(rows[0])]
    fixed = [row for row in rows[1:] if row[0] not in NVS_MUTABLE_KEYS]
    # Largest first (stable): a long Hikvision CA may need a page of its own.
    for row in sorted(fixed, key=nvs_row_entries, reverse=True):
        page = next((index for index, count in enumerate(used) if nvs_row_fits(count, row)), None)
        if page is None:
            if not nvs_row_fits(0, row):
                raise ValueError("A provisioning value does not fit one NVS page")
            pages.append([])
            used.append(0)
            page = len(pages) - 1
        pages[page].append(row)
        used[page] += nvs_row_entries(row)
    # Strings first: a number may take entry 125, a string may not, so a u8 closes
    # a tail that ends with a string.
    tail = sorted((row for row in rows[1:] if row[0] in NVS_TAIL_KEYS), key=lambda row: row[2] in NVS_PRIMITIVES)
    closing = bool(tail) and tail[-1][2] not in NVS_PRIMITIVES
    start = NVS_PAGE_ENTRIES - sum(nvs_row_entries(row) for row in tail) - int(closing)
    boundary = start // NVS_STATE_WORD_ENTRIES * NVS_STATE_WORD_ENTRIES
    target = None
    if tail:
        if start < 0:
            raise ValueError("Provisioning values do not fit one NVS page")
        target = next((index for index in reversed(range(len(pages))) if used[index] <= boundary), None)
        if target is None:
            pages.append([])
            used.append(0)
            target = len(pages) - 1
    active = [row for row in rows[1:] if row[0] in NVS_ACTIVE_KEYS]
    active_used = 0
    for row in active:
        if not nvs_row_fits(active_used, row):
            raise ValueError("Provisioning values do not fit the NVS partition")
        active_used += nvs_row_entries(row)
    # Only a row after it makes the generator mark the last padded page FULL.
    if not active or len(pages) + 1 + NVS_MIN_BLANK_PAGES > NVS_PAGE_COUNT:
        raise ValueError("Provisioning values do not fit the NVS partition")
    layout: list[list[object]] = []
    padding = 0
    for index, (page, count) in enumerate(zip(pages, used)):
        layout.extend(page)
        if index == target:
            filler = _nvs_padding(boundary - count, padding)
            filler += _nvs_padding(start - boundary, padding + len(filler), single=True)
        else:
            filler = _nvs_padding(NVS_PAGE_ENTRIES - count, padding)
        padding += len(filler)
        layout.extend(filler)
        if index == target:
            layout.extend(tail)
            if closing:
                layout.append([f"{NVS_PADDING_PREFIX}{padding}", "data", "u8", 0])
                padding += 1
    layout.extend(active)
    return layout


def _nvs_decrypt(xts_key: bytes, offset: int, entry: bytes) -> bytes:
    """One entry, decrypted as ESP-IDF does: AES-XTS, tweak = its partition offset."""
    decryptor = Cipher(algorithms.AES(xts_key), modes.XTS(offset.to_bytes(16, "little"))).decryptor()
    return decryptor.update(entry) + decryptor.finalize()


def nvs_image_pages(image: bytes, xts_key: bytes) -> list[dict]:
    """Each page's plaintext header and entry states, and its decrypted items.

    An item is (entry index, entries, namespace index, type, key, value bytes); a
    string's value is its stored bytes with the NUL. Reading stops at the first entry that is
    not written or whose header or data CRC does not verify (`intact` is False).
    """
    pages = []
    for number in range(len(image) // NVS_PAGE_SIZE):
        base = number * NVS_PAGE_SIZE
        raw = image[base:base + NVS_PAGE_SIZE]
        if raw == b"\xff" * NVS_PAGE_SIZE:
            pages.append({"blank": True})
            continue
        state, sequence = struct.unpack_from("<II", raw)
        states = [(raw[32 + index // 4] >> (index % 4 * 2)) & 3 for index in range(NVS_PAGE_ENTRIES)]

        def entry(index: int) -> bytes:
            offset = NVS_ENTRY_SIZE * (2 + index)
            return _nvs_decrypt(xts_key, base + offset, raw[offset:offset + NVS_ENTRY_SIZE])

        items, index, intact = [], 0, True
        while index < NVS_PAGE_ENTRIES and states[index] == 2:
            header = entry(index)
            span = header[2]
            if (zlib.crc32(header[:4] + header[8:], 0xFFFFFFFF) != struct.unpack_from("<I", header, 4)[0]
                    or not span or index + span > NVS_PAGE_ENTRIES
                    or any(entry_state != 2 for entry_state in states[index:index + span])):
                intact = False
                break
            value = header[24:]
            if header[1] == NVS_STRING:
                size = struct.unpack_from("<H", header, 24)[0]
                value = b"".join(entry(part) for part in range(index + 1, index + span))[:size]
                if (len(value) != size
                        or zlib.crc32(value, 0xFFFFFFFF) != struct.unpack_from("<I", header, 28)[0]):
                    intact = False
                    break
            key = header[8:24].split(b"\0", 1)[0].decode("ascii", "replace")
            items.append((index, span, header[0], header[1], key, value))
            index += span
        pages.append({
            "blank": False, "state": state, "sequence": sequence, "version": raw[8],
            "header_ok": zlib.crc32(raw[4:28], 0xFFFFFFFF) == struct.unpack_from("<I", raw, 28)[0],
            "written": states.count(2), "states": states, "items": items, "intact": intact,
        })
    return pages


def _nvs_expected(row: list[object]) -> tuple[int, int, bytes]:
    """(namespace index, type, value bytes) the generator writes for a row."""
    _key, kind, encoding, value = row
    if kind == "namespace":
        return 0, NVS_PRIMITIVES["u8"][0], b"\x01" + b"\xff" * 7
    if encoding == "string":
        return 1, NVS_STRING, nvs_stored_string(value)
    code, layout = NVS_PRIMITIVES[encoding]
    packed = struct.pack(layout, int(value))
    return 1, code, packed + b"\xff" * (8 - len(packed))


def check_nvs_layout(image: bytes, xts_key: bytes, layout: list[list[object]]) -> None:
    """Refuse a generated image unless it holds nvs_layout()'s rows as planned.

    Headers and entry states are plaintext; the entries are decrypted with the
    image's own XTS key, in memory only. Every page holding the namespace entry, a
    row the firmware never writes, an NVS_TAIL_KEYS row or padding must be FULL with
    all 126 entries written; the state words of the tail rows must hold nothing but
    them and one-entry padding; the NVS_ACTIVE_KEYS rows must be on one later page
    that is ACTIVE; at least NVS_MIN_BLANK_PAGES pages must be blank; and every row
    must be stored once with its value. Errors name keys and pages, never values.
    """

    def fail(reason: str) -> NoReturn:
        raise RuntimeError(f"Provisioning NVS layout check failed: {reason}")

    if len(image) != NVS_PARTITION_SIZE or len(xts_key) != NVS_XTS_KEY_SIZE:
        fail("wrong image or key size")
    pages = nvs_image_pages(image, xts_key)
    data_pages = [number for number, page in enumerate(pages) if not page["blank"]]
    if data_pages != list(range(len(data_pages))):
        fail("data pages are not the first pages")
    # (namespace index, key) -> (page, entry index, entries, type, value bytes)
    found: dict[tuple[int, str], tuple[int, int, int, int, bytes]] = {}
    for number in data_pages:
        page = pages[number]
        written = page["written"]
        if (not page["header_ok"] or page["sequence"] != number or page["version"] != 0xFE
                or page["state"] not in (NVS_PAGE_ACTIVE, NVS_PAGE_FULL) or not page["intact"]
                or page["states"] != [2] * written + [3] * (NVS_PAGE_ENTRIES - written)):
            fail(f"page {number} is not a clean generated page")
        for index, span, namespace, kind, key, value in page["items"]:
            if (namespace, key) in found:
                fail(f"{key} is stored twice")
            found[(namespace, key)] = (number, index, span, kind, value)
    expected = {}
    for row in layout:
        namespace, kind, value = _nvs_expected(row)
        expected[(namespace, str(row[0]))] = (kind, value)
    if set(found) != set(expected):
        fail("the stored keys differ from the planned rows: "
             + ", ".join(sorted(key for _, key in set(found) ^ set(expected))))
    for item, (kind, value) in expected.items():
        if found[item][3:] != (kind, value):
            fail(f"{item[1]} is not stored as planned")
    if found[(0, str(layout[0][0]))][:2] != (0, 0):
        fail("the namespace entry is not the first entry")
    full_pages = {0} | {found[(1, str(row[0]))][0] for row in layout[1:] if row[0] not in NVS_ACTIVE_KEYS}
    active_pages = {found[(1, str(row[0]))][0] for row in layout[1:] if row[0] in NVS_ACTIVE_KEYS}
    for number in sorted(full_pages):
        if pages[number]["state"] != NVS_PAGE_FULL or pages[number]["written"] != NVS_PAGE_ENTRIES:
            fail(f"page {number} holds provisioning rows but is not FULL with 126 entries")
    if (len(active_pages) != 1 or min(active_pages) <= max(full_pages)
            or pages[min(active_pages)]["state"] != NVS_PAGE_ACTIVE):
        fail("the rows written on the first online run are not on one later ACTIVE page")
    tail = [found[(1, str(row[0]))] for row in layout[1:] if row[0] in NVS_TAIL_KEYS]
    if tail:
        number = tail[0][0]
        boundary = min(item[1] for item in tail) // NVS_STATE_WORD_ENTRIES * NVS_STATE_WORD_ENTRIES
        sharing = sorted(
            key for index, span, _namespace, _kind, key, _value in pages[number]["items"]
            if index + span > boundary and key not in NVS_TAIL_KEYS
            and not (key.startswith(NVS_PADDING_PREFIX) and span == 1)
        )
        if any(item[0] != number for item in tail) or sharing:
            fail("the Wi-Fi, COMM key and host rows do not have their state words to themselves"
                 + (f" (also there: {', '.join(sharing)})" if sharing else ""))
    if sum(page["blank"] for page in pages) < NVS_MIN_BLANK_PAGES:
        fail(f"fewer than {NVS_MIN_BLANK_PAGES} pages are left erased")


def build_nvs_partition(
    rows: list[list[object]], hmac_key_path: Path, directory: Path, generator: Path
) -> Path:
    """Generate the encrypted provisioning NVS for nvs_rows() in `directory`.

    The rows are laid out by nvs_layout, which must hold exactly the given rows
    besides padding; ESP-IDF's generator derives the XTS keys from the HMAC eFuse key
    and encrypts the image, and check_nvs_layout must pass before the image
    (provision.bin) is used.
    """
    layout = nvs_layout(rows)
    planned = [row for row in layout if not str(row[0]).startswith(NVS_PADDING_PREFIX)]
    by_key = {str(row[0]): row[1:] for row in planned}
    given = {str(row[0]): _nvs_normalized(row)[1:] for row in rows}
    if layout[:1] != [_nvs_normalized(rows[0])] or len(planned) != len(rows) or by_key != given:
        changed = (set(by_key) ^ set(given)) | {key for key in by_key.keys() & given.keys()
                                                 if by_key[key] != given[key]}
        raise RuntimeError("Provisioning NVS layout check failed: the plan does not hold the given rows"
                           + (": " + ", ".join(sorted(changed)) if changed else ""))
    csv_path = directory / "provision.csv"
    binary_path = directory / "provision.bin"
    nvs_key_path = directory / "nvs-xts-key.bin"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["key", "type", "encoding", "value"])
        writer.writerows(layout)
    run(
        [
            sys.executable,
            str(generator),
            "generate-key",
            "--key_protect_hmac",
            "--kp_hmac_inputkey",
            str(hmac_key_path),
            "--keyfile",
            str(nvs_key_path),
            "--outdir",
            str(directory),
        ]
    )
    run(
        [
            sys.executable,
            str(generator),
            "encrypt",
            str(csv_path),
            str(binary_path),
            hex(NVS_PARTITION_SIZE),
            "--inputkey",
            str(nvs_key_path),
        ]
    )
    image = binary_path.read_bytes()
    if len(image) != NVS_PARTITION_SIZE:
        raise RuntimeError("Generated NVS partition has the wrong size")
    check_nvs_layout(image, nvs_key_path.read_bytes()[:NVS_XTS_KEY_SIZE], layout)
    return binary_path


def validate_secure_build_config(project: Path) -> None:
    """Refuse provisioning unless the effective image uses HMAC-backed NVS.

    sdkconfig.defaults is intentionally not sufficient evidence: an ignored,
    previously generated sdkconfig can override those defaults. Checking both
    the resolved sdkconfig and the generated build header ties this gate to the
    image that is about to be flashed.
    """
    sdkconfig_path = project / "sdkconfig"
    build_header_path = project / "build" / "config" / "sdkconfig.h"
    if not sdkconfig_path.is_file() or not build_header_path.is_file():
        raise RuntimeError(
            "Secure build configuration is missing; run a clean idf.py build before provisioning"
        )
    sdkconfig = sdkconfig_path.read_text(encoding="utf-8")
    build_header = build_header_path.read_text(encoding="utf-8")
    missing_values = [value for value in SECURE_SDKCONFIG_VALUES if value not in sdkconfig]
    missing_defines = [value for value in SECURE_BUILD_DEFINES if value not in build_header]
    if missing_values or missing_defines:
        raise RuntimeError(
            "Refusing to provision an image without HMAC-backed NVS encryption; "
            "remove the stale generated sdkconfig, rebuild from sdkconfig.defaults, and retry"
        )


def efuse_summary(espefuse: str, port: str, destination: Path) -> dict:
    run(
        [
            espefuse,
            "--chip",
            "esp32s3",
            "--port",
            port,
            "summary",
            "--format",
            "json",
            "--file",
            str(destination),
        ]
    )
    return json.loads(destination.read_text(encoding="utf-8"))


def ensure_nvs_hmac_key(
    *,
    espefuse: str,
    port: str,
    mac: str,
    key_path: Path,
    summary_path: Path,
    confirmation: str | None,
    trust_existing: bool,
    split_root_recovery: bool,
) -> None:
    summary = efuse_summary(espefuse, port, summary_path)
    block = summary.get("BLOCK_KEY0", {})
    purpose = summary.get("KEY_PURPOSE_0", {}).get("value")
    raw = str(block.get("raw_value", ""))
    is_empty = raw in {"", "0x" + ("0" * 64)} and bool(block.get("writeable", False))
    if is_empty and purpose == "USER":
        if split_root_recovery:
            raise RuntimeError(
                "Split-root recovery is forbidden for an empty eFuse; provision new devices "
                "with one explicit ADD_FLEET_ROOT_SECRET"
            )
        try:
            confirmed_mac = normalize_mac(confirmation or "")
        except ValueError:
            confirmed_mac = ""
        if confirmed_mac != mac:
            raise RuntimeError(
                "Secure NVS requires one irreversible HMAC eFuse burn. "
                f"Re-run with --confirm-efuse-burn-for {mac} after explicit approval."
            )
        run(
            [
                espefuse,
                "--chip",
                "esp32s3",
                "--port",
                port,
                "--do-not-confirm",
                "burn_key",
                "BLOCK_KEY0",
                str(key_path),
                "HMAC_UP",
            ]
        )
        verified = efuse_summary(espefuse, port, summary_path)
        if verified.get("KEY_PURPOSE_0", {}).get("value") != "HMAC_UP":
            raise RuntimeError("HMAC eFuse purpose verification failed after burn")
        return
    if purpose != "HMAC_UP":
        raise RuntimeError("eFuse BLOCK_KEY0 is not available for Zone Lite HMAC NVS encryption")
    if not trust_existing:
        raise RuntimeError(
            "BLOCK_KEY0 already contains an unreadable HMAC key. Refusing to assume it was "
            "derived by this fleet; pass --trust-existing-derived-hmac only for a previously "
            "provisioned Zone Lite device."
        )


def firmware_flash_arguments(
    project: Path,
    esptool: str,
    port: str,
    nvs_binary: Path,
    signed_bootloader: Path,
    signed_app: Path,
) -> list[str]:
    build = project / "build"
    values = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    extra = values["extra_esptool_args"]
    command = [
        esptool,
        "--chip",
        str(extra.get("chip", "esp32s3")),
        "--port",
        port,
        "--before",
        str(extra.get("before", "default_reset")),
        "--after",
        str(extra.get("after", "hard_reset")),
        "write_flash",
        *[str(item) for item in values.get("write_flash_args", [])],
    ]
    for offset, filename in values["flash_files"].items():
        source = build / filename
        if str(filename).endswith("bootloader.bin"):
            source = signed_bootloader
        elif str(filename).endswith("zone_lite.bin"):
            source = signed_app
        command.extend([offset, str(source)])
    command.extend([NVS_PARTITION_OFFSET, str(nvs_binary)])
    return command


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--idf-path", type=Path, default=os.environ.get("IDF_PATH"))
    parser.add_argument("--esptool", default=shutil.which("esptool.py") or "esptool.py")
    parser.add_argument("--espefuse", default=shutil.which("espefuse.py") or "espefuse.py")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-firmware-flash", action="store_true")
    parser.add_argument("--confirm-efuse-burn-for")
    parser.add_argument("--confirm-secure-boot-for")
    parser.add_argument("--signed-bootloader", type=Path)
    parser.add_argument("--signed-app", type=Path)
    parser.add_argument("--trust-existing-derived-hmac", action="store_true")
    parser.add_argument("--confirm-split-root-recovery-for")
    args = parser.parse_args()
    if not args.idf_path:
        raise SystemExit("Pass --idf-path or export IDF_PATH")
    if not args.skip_firmware_flash:
        if normalize_mac(args.confirm_secure_boot_for or "") != read_mac(args.esptool, args.port):
            raise SystemExit(
                "Secure Boot is irreversible. Re-run with --confirm-secure-boot-for set to the exact detected MAC."
            )
        if not args.signed_bootloader or not args.signed_bootloader.is_file():
            raise SystemExit("Pass the independently verified three-key signed bootloader with --signed-bootloader.")
        if not args.signed_app or not args.signed_app.is_file():
            raise SystemExit("Pass the independently verified signed Zone Lite image with --signed-app.")

    project = Path(__file__).resolve().parents[1]
    config = load_config(args.config)
    mac = read_mac(args.esptool, args.port)
    fleet_root = config.pop("fleet_root")
    nvs_hmac_root = config.pop("nvs_hmac_root")
    split_root_recovery = validate_root_selection(
        mac=mac,
        fleet_root=fleet_root,
        nvs_hmac_root=nvs_hmac_root,
        recovery_confirmation=args.confirm_split_root_recovery_for,
        trust_existing=args.trust_existing_derived_hmac,
    )
    bootstrap_secret = derive_bootstrap_secret(mac, fleet_root)
    nvs_hmac_key = derive_nvs_hmac_key(mac, nvs_hmac_root)
    generator = find_nvs_generator(args.idf_path)

    if not args.skip_build:
        run(["idf.py", "build"], cwd=project)
    validate_secure_build_config(project)
    with tempfile.TemporaryDirectory(prefix="zone-lite-provision-") as directory:
        temporary = Path(directory)
        readback_path = temporary / "readback.bin"
        hmac_key_path = temporary / "hmac-key.bin"
        summary_path = temporary / "efuse-summary.json"
        hmac_key_path.write_bytes(nvs_hmac_key)
        binary_path = build_nvs_partition(
            nvs_rows(config, bootstrap_secret), hmac_key_path, temporary, generator
        )
        ensure_nvs_hmac_key(
            espefuse=args.espefuse,
            port=args.port,
            mac=mac,
            key_path=hmac_key_path,
            summary_path=summary_path,
            confirmation=args.confirm_efuse_burn_for,
            trust_existing=args.trust_existing_derived_hmac,
            split_root_recovery=split_root_recovery,
        )
        if args.skip_firmware_flash:
            run(
                [
                    args.esptool,
                    "--chip",
                    "esp32s3",
                    "--port",
                    args.port,
                    "write_flash",
                    NVS_PARTITION_OFFSET,
                    str(binary_path),
                ]
            )
        else:
            run(
                firmware_flash_arguments(
                    project,
                    args.esptool,
                    args.port,
                    binary_path,
                    args.signed_bootloader,
                    args.signed_app,
                )
            )
        run(
            [
                args.esptool,
                "--chip",
                "esp32s3",
                "--port",
                args.port,
                "read_flash",
                NVS_PARTITION_OFFSET,
                hex(NVS_PARTITION_SIZE),
                str(readback_path),
            ]
        )
        expected = hashlib.sha256(binary_path.read_bytes()).digest()
        actual = hashlib.sha256(readback_path.read_bytes()).digest()
        if expected != actual:
            raise RuntimeError("Provisioning partition read-back hash did not match")
    print(f"Provisioned and verified Zone Lite device {mac}")


if __name__ == "__main__":
    main()
