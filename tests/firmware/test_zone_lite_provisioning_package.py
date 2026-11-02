from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import zlib

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "firmware" / "zone_lite" / "tools"
FIRMWARE = ROOT / "firmware" / "zone_lite"


def load_envelope():
    path = TOOLS / "provisioning_envelope.py"
    spec = importlib.util.spec_from_file_location("provisioning_envelope", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_nvs_check():
    """tests/firmware/tools/check_provisioning_nvs.py: test values, a model of the
    generator and an independent image reader (CI also runs it in espressif/idf)."""
    path = ROOT / "tests" / "firmware" / "tools" / "check_provisioning_nvs.py"
    spec = importlib.util.spec_from_file_location("check_provisioning_nvs", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# name: (family, overrides, FULL pages, page holding the Wi-Fi, COMM key and host rows)
NVS_CONFIGS = {
    "zkt": ("zkt", {}, 1, 0),
    "zkt long values": ("zkt", {"wifi_ssid": "S" * 32, "wifi_password": "P" * 63, "zone_id": "Z" * 64,
                                "zone_name": "N" * 120, "zkt_expected_serial": "T" * 64}, 1, 0),
    "hikvision without CA": ("hik", {}, 1, 0),
    # The CA takes page 1 alone, so the tail rows end page 0.
    "hikvision 3967-byte CA": ("hik", {"hik_ca_pem": "max"}, 2, 0),
    # With these values a CA of up to 1823 bytes leaves room for the tail rows on page 0;
    # from 1824 bytes they end page 1, alone or after the rows that move there from 2272.
    **{f"hikvision {size}-byte CA": ("hik", {"hik_ca_pem": "C" * size}, pages, tail)
       for size, pages, tail in ((1, 1, 0), (1500, 1, 0), (1823, 1, 0), (1824, 2, 1), (2271, 2, 1),
                                 (2272, 2, 1), (2600, 2, 1), (3966, 2, 0))},
}


@pytest.mark.parametrize("name", NVS_CONFIGS)
def test_nvs_layout_fills_the_provisioning_pages_and_keeps_every_value(name):
    check = load_nvs_check()
    tool = check.tools()
    family, overrides, full_pages, tail_page = NVS_CONFIGS[name]
    if overrides.get("hik_ca_pem") == "max":
        overrides = {"hik_ca_pem": check.MAX_CA}
        assert len(check.MAX_CA.encode()) == 3967
    rows = check.rows_for(check.config(family, **overrides))
    layout = tool.nvs_layout(rows)
    # Same rows and values; every page with a row the firmware never writes, or with
    # the Wi-Fi, COMM key and host rows, holds exactly 126 entries (the generator marks
    # it FULL); those rows end a page in state words of their own; the rows written on
    # the first online run are the last, ACTIVE page; at least two pages stay erased.
    check.assert_planned_layout(rows, layout)
    pages = check.simulate_pages(layout)
    assert pages[0][0] == ("zone_cfg", 1)
    assert len(pages) - 1 == full_pages
    assert {page for page, entries in enumerate(pages) for key, _ in entries if key in tool.NVS_TAIL_KEYS} == {
        tail_page}
    # No more FULL pages than the entries need: the rows the firmware never writes, and
    # the tail rows from the start of their first state word.
    fixed = sum(tool.nvs_row_entries(row) for row in rows if row[0] not in tool.NVS_MUTABLE_KEYS)
    tail = sum(tool.nvs_row_entries(row) for row in rows if row[0] in tool.NVS_TAIL_KEYS)
    assert full_pages == -(-(fixed + 126 - (126 - tail) // 16 * 16) // 126)


def test_nvs_layout_knows_every_row_the_firmware_writes():
    """Every provisioned zone_cfg key a running board writes (zone_config.c, the USB
    Wi-Fi rescue) is a rewritten row. The ones a new board writes on its first online
    run (onboarding, the pinned ZKT serial) start the ACTIVE page; the others only an
    operator's action writes, so they stay on a FULL page. A firmware change writing
    another provisioned key, or a tail key from another function, must update the layout."""
    check = load_nvs_check()
    tool = check.tools()
    pattern = r'nvs_(?:set_\w+|erase_key)\(\s*handle,\s*"(\w+)"'
    config_source = (FIRMWARE / "main" / "zone_config.c").read_text(encoding="utf-8")
    rescue = (FIRMWARE / "usb_wifi_rescue" / "main" / "wifi_keys.c").read_text(encoding="utf-8")
    written = set(re.findall(pattern, config_source)) | set(re.findall(pattern, rescue))
    provisioned = {str(row[0]) for family in ("zkt", "hik")
                   for row in check.rows_for(check.config(family))}
    assert written & provisioned == tool.NVS_MUTABLE_KEYS
    assert "zkt_key_rev" in written and "zkt_op_applied" in written  # runtime-only keys
    functions = re.findall(r"^(?:static )?esp_err_t (\w+)\([^)]*\)\n\{\n(.*?)^\}", config_source, re.M | re.S)
    writes = {name: set(re.findall(pattern, body)) & provisioned for name, body in functions}
    assert writes["zone_config_save_connector"] | writes["zone_config_save_zkt_serial"] == tool.NVS_ACTIVE_KEYS
    # Wi-Fi from the setup portal, a COMM key from an authenticated ADD command (or its
    # completion after a reset) and a re-discovered Hikvision host.
    assert {name for name, keys in writes.items() if keys & tool.NVS_TAIL_KEYS} == {
        "recover_pending_comm_key", "zone_config_save_hik_host", "zone_config_save_wifi",
        "zone_config_save_zkt_comm_key"}
    assert set(re.findall(pattern, rescue)) == {"wifi_ssid", "wifi_pass"}
    # zone_config.c is the firmware's only writer of zone_cfg; the padding is never read.
    writers = {path.name for path in (FIRMWARE / "main").glob("*.c")
               if 'nvs_open("zone_cfg", NVS_READWRITE' in path.read_text(encoding="utf-8")}
    assert writers == {"zone_config.c"}
    for path in (FIRMWARE / "main").glob("*.[ch]"):
        assert not re.search(r'"' + tool.NVS_PADDING_PREFIX, path.read_text(encoding="utf-8")), path.name


def test_nvs_layout_writes_values_as_the_generator_stores_them():
    # csv writes None as an empty field; the generator reads universal newlines and
    # drops every line that starts with '#' before it parses the CSV. The plan holds
    # the values that way, so a CLI JSON null or a CA bundle's '# Subject:' lines no
    # longer fail the build's check, and a last line starting with '#' can no longer
    # take the CSV's closing quote (and the rows after it) with it.
    check = load_nvs_check()
    tool = check.tools()
    rows = check.rows_for(check.config("hik", hik_ca_pem=check.BUNDLE_CA + "# trailing comment",
                                       zkt_telnet_password=None))
    layout = tool.nvs_layout(rows)
    check.assert_planned_layout(rows, layout)
    values = {row[0]: row[3] for row in layout}
    assert values["hik_ca"] == check.BUNDLE_CA.replace("# Subject: CN=Test Intermediate\n", "")
    assert values["hik_ca"].startswith("# Issuer: CN=Test Root\n")  # a first line is never dropped
    assert values["tel_pass"] == "" and tool.nvs_stored_string(None) == b"\0"
    assert tool.nvs_string_value("a\r\nb\r#c\rd\n#e") == "a\nb\nd\n"
    assert all(isinstance(value, int) or ("\r" not in value and "\n#" not in value) for value in values.values())


def test_build_refuses_a_plan_that_drops_or_changes_a_row(monkeypatch, tmp_path: Path):
    check = load_nvs_check()
    tool = check.tools()
    rows = check.rows_for(check.config("zkt"))
    layout = tool.nvs_layout

    def generator(_command, **_arguments):
        raise AssertionError("the generator ran on a wrong plan")

    monkeypatch.setattr(tool, "run", generator)
    for broken, named in (
        (lambda given: [row for row in layout(given) if row[0] != "boot_secret"], "boot_secret"),
        (lambda given: [[*row[:3], "x"] if row[0] == "zone_id" else row for row in layout(given)], "zone_id"),
        (lambda given: layout(given)[1:], "zone_cfg"),
    ):
        monkeypatch.setattr(tool, "nvs_layout", broken)
        with pytest.raises(RuntimeError, match=f"the plan does not hold the given rows: {named}$"):
            tool.build_nvs_partition(rows, tmp_path / "hmac-key.bin", tmp_path, tmp_path / "nvs_partition_gen.py")


def test_rewritten_rows_have_distinct_item_hashes_on_their_active_page():
    # ESP-IDF 5.5.3 loads an ACTIVE page by erasing any earlier entry whose 24-bit
    # item hash (namespace, key, chunk) matches a later one, without comparing keys
    # (Page::mLoadEntryTable). FULL pages are never scanned. The first-run rows start
    # the ACTIVE page and every rewrite lands on an ACTIVE page, so no two may collide.
    tool = load_nvs_check().tools()
    hashes = {zlib.crc32(bytes([1]) + key.encode().ljust(16, b"\0") + b"\xff", 0xFFFFFFFF) & 0xFFFFFF
              for key in tool.NVS_MUTABLE_KEYS}
    assert len(hashes) == len(tool.NVS_MUTABLE_KEYS)


def test_nvs_layout_refuses_rows_it_cannot_lay_out():
    check = load_nvs_check()
    tool = check.tools()
    rows = check.rows_for(check.config("zkt"))
    with pytest.raises(ValueError, match="padding prefix"):
        tool.nvs_layout(rows + [["_pad0", "data", "u8", 0]])
    with pytest.raises(ValueError, match="one namespace"):
        tool.nvs_layout(rows[1:])
    # Four page-sized values leave fewer than two erased pages.
    huge = [[f"big{index}", "data", "string", "x" * 3000] for index in range(4)]
    with pytest.raises(ValueError, match="do not fit the NVS partition"):
        tool.nvs_layout(rows + huge)
    with pytest.raises(ValueError, match="does not fit one NVS page"):
        tool.nvs_layout(rows + [["big", "data", "string", "x" * 3968]])


def test_both_builders_write_the_checked_layout():
    provisioner = (TOOLS / "provision_zone_lite.py").read_text(encoding="utf-8")
    package = (TOOLS / "build_provisioning_package.py").read_text(encoding="utf-8")
    for source in (provisioner[provisioner.index("def main()"):], package):
        assert "build_nvs_partition(" in source
        assert '"encrypt"' not in source and "writerows(" not in source
    builder = provisioner[provisioner.index("def build_nvs_partition("):provisioner.index("def main()")]
    assert "layout = nvs_layout(rows)" in builder and "writer.writerows(layout)" in builder
    assert builder.index("the plan does not hold the given rows") < builder.index('"generate-key"')
    assert builder.index('"encrypt"') < builder.index("check_nvs_layout(") < builder.index("return binary_path")


def test_provisioning_nvs_with_esp_idf_generator(tmp_path: Path):
    check = load_nvs_check()
    if check.generator() is None:
        pytest.skip("ESP-IDF 5.5.3's nvs_partition_gen is required (CI runs this in espressif/idf:v5.5.3)")
    output = "\n".join(check.run_checks(tmp_path))
    assert "FULL/126 ACTIVE/8 BLANK BLANK BLANK BLANK: zkt," in output
    assert "FULL/126 FULL/126 ACTIVE/8 BLANK BLANK BLANK: hikvision 3967-byte CA" in output
    assert "32 images: every value as the earlier builder stored it, the earlier layout refused" in output
    assert "Wi-Fi, COMM key and host rows at page 0 entries 119-125" in output
    assert "a CA ending in a '#' line: stored without it" in output
    assert "refused: page 0 ACTIVE (page 0 holds provisioning rows but is not FULL" in output
    assert "Wi-Fi on the ACTIVE page (page 1 holds provisioning rows but is not FULL" in output
    assert "a tail state word shared (the Wi-Fi, COMM key and host rows do not have their state words to themselves (also there: " in output
    assert "a padding string in it (the Wi-Fi, COMM key and host rows do not have their state words to themselves" in output
    assert "build_provisioning_package.py hikvision 3967-byte CA: FULL/126 FULL/126 ACTIVE/8" in output
    assert "provision_zone_lite.py zkt: FULL/126 ACTIVE/8" in output


def test_ci_runs_the_provisioning_nvs_check_in_the_esp_idf_image():
    workflow = (ROOT / ".github" / "workflows" / "add-ci.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("  firmware-production:"):workflow.index("  firmware-journal:")]
    step = job[job.index("Lay out provisioning NVS with ESP-IDF's own generator"):]
    step = step[:step.index("      - name:", 10)]
    assert "espressif/idf:v5.5.3" in step
    assert "python tests/firmware/tools/check_provisioning_nvs.py" in step


def test_provisioning_envelope_round_trip_and_aad_binding():
    envelope = load_envelope()
    recipient = X25519PrivateKey.generate()
    public = recipient.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    import base64

    aad = {
        "schema_version": 1,
        "request_id": "request-12345678",
        "target_mac": "e0:72:a1:d7:05:c4",
        "nvs_size": 24576,
    }
    plaintext = b"encrypted-nvs-test-vector"
    ciphertext, metadata = envelope.encrypt_for_recipient(
        plaintext, base64.b64encode(public).decode("ascii"), aad
    )
    private = recipient.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    assert envelope.decrypt_envelope(
        ciphertext, private, {"aad": aad, "envelope": metadata}
    ) == plaintext


def test_workflow_never_uploads_plaintext_nvs():
    workflow = (ROOT / ".github" / "workflows" / "zone-lite-device-provisioning.yml").read_text(
        encoding="utf-8"
    )
    assert "provision.bin.enc" in workflow
    assert "hmac-key.bin.enc" in workflow
    assert "manifest.json" in workflow
    assert "retention-days: 1" in workflow
    assert "path: provisioning-output/" in workflow
    assert "ADD_FLEET_ROOT_SECRET:" not in workflow
    assert "ADD_PII_LOOKUP_KEY:" not in workflow


def test_decrypt_package_recovers_device_bound_hmac_key(tmp_path: Path):
    envelope = load_envelope()
    recipient = X25519PrivateKey.generate()
    public = recipient.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    private = recipient.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    import base64
    import hashlib
    import json

    request_id = "karachi-device-12345678"
    target_mac = "a4:cb:8f:d4:67:70"
    nvs = b"N" * 24576
    hmac_key = b"H" * 32
    nvs_aad = {
        "schema_version": 1,
        "request_id": request_id,
        "target_mac": target_mac,
        "nvs_size": len(nvs),
        "nvs_sha256": hashlib.sha256(nvs).hexdigest(),
    }
    hmac_aad = {
        "schema_version": 1,
        "request_id": request_id,
        "target_mac": target_mac,
        "purpose": "zone-lite-nvs-hmac-efuse-key",
        "key_size": 32,
    }
    public_b64 = base64.b64encode(public).decode("ascii")
    nvs_ciphertext, nvs_envelope = envelope.encrypt_for_recipient(
        nvs, public_b64, nvs_aad
    )
    hmac_ciphertext, hmac_envelope = envelope.encrypt_for_recipient(
        hmac_key, public_b64, hmac_aad
    )
    package = tmp_path / "package"
    package.mkdir()
    (package / "provision.bin.enc").write_bytes(nvs_ciphertext)
    (package / "hmac-key.bin.enc").write_bytes(hmac_ciphertext)
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "aad": nvs_aad,
                "envelope": nvs_envelope,
                "ciphertext_sha256": hashlib.sha256(nvs_ciphertext).hexdigest(),
                "hmac_key": {
                    "aad": hmac_aad,
                    "envelope": hmac_envelope,
                    "ciphertext_sha256": hashlib.sha256(
                        hmac_ciphertext
                    ).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    private_path = tmp_path / "request.key"
    private_path.write_bytes(private)
    nvs_output = tmp_path / "provision.bin"
    hmac_output = tmp_path / "hmac-key.bin"

    envelope.decrypt_package(
        package,
        private_path,
        nvs_output,
        hmac_output,
        request_id,
        target_mac,
    )

    assert nvs_output.read_bytes() == nvs
    assert hmac_output.read_bytes() == hmac_key


def test_first_use_flasher_is_fail_closed_and_verifies_all_ranges():
    source = (TOOLS / "flash_prepared_zone_lite.py").read_text(encoding="utf-8")
    assert '("bootloader-signed.bin", 0x0)' in source
    assert '("partition-table.bin", 0x10000)' in source
    assert '("provision.bin", 0x11000)' in source
    assert '("ota_data_initial.bin", 0x17000)' in source
    assert '("zone-lite-signed.bin", 0x20000)' in source
    assert "--confirm-efuse-burn-for" in source
    assert "--confirm-secure-boot-for" in source
    assert "ensure_nvs_hmac_key(" in source
    assert "Flash readback verification failed" in source
    # A production (first-use 2.7.x) package also writes the ota_1 rollback
    # predecessor and the empty storage image, verified on chip by the stub.
    assert "(ROLLBACK_PREDECESSOR_IMAGE, OTA_1_OFFSET)" in source
    assert "(STORAGE_IMAGE, STORAGE_OFFSET)" in source
    assert '"verify_flash"' in source
    assert "Flash verification failed for" in source
    assert '"first boot is intentionally pending"' in source
