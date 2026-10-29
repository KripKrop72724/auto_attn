from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "firmware" / "zone_lite" / "tools"))

import build_empty_storage_image as storage_tool  # noqa: E402
import flash_prepared_zone_lite as flasher  # noqa: E402

MAC = "14:c1:9f:46:ae:1c"
PREDECESSOR = b"exact published 2.5.2 bytes"
STORAGE = b"\x19" * 4096 + b"\xff" * (0x800000 - 4096)


def partition_table(rows) -> bytes:
    table = b""
    for label, kind, subtype, offset, size in rows:
        table += b"\xaa\x50" + struct.pack("<BBII", kind, subtype, offset, size)
        table += label.encode().ljust(16, b"\0") + b"\0" * 4
    return table + b"\xff" * (0xC00 - len(table))


ZONE_LITE_TABLE = [
    ("nvs", 1, 0x02, 0x11000, 0x6000),
    ("otadata", 1, 0x00, 0x17000, 0x2000),
    ("phy_init", 1, 0x01, 0x19000, 0x1000),
    ("factory", 0, 0x00, 0x20000, 0x280000),
    ("ota_0", 0, 0x10, 0x2A0000, 0x280000),
    ("ota_1", 0, 0x11, 0x520000, 0x280000),
    ("storage", 1, 0x82, 0x7A0000, 0x800000),
]


def package(tmp_path: Path, *, production=True, table=ZONE_LITE_TABLE) -> Path:
    bundle = tmp_path / "signed"
    bundle.mkdir()
    application = b"exact published release bytes"
    (bundle / "bootloader-signed.bin").write_bytes(b"bootloader")
    (bundle / "partition-table.bin").write_bytes(partition_table(table))
    (bundle / "ota_data_initial.bin").write_bytes(b"\xff" * 0x2000)
    (bundle / "zone-lite-signed.bin").write_bytes(application)
    manifest = {
        "schema_version": 1, "firmware_family": "zkt", "target_mac": MAC, "git_sha": "a" * 40,
        "bootloader_sha256": hashlib.sha256(b"bootloader").hexdigest(),
        "application_sha256": hashlib.sha256(application).hexdigest(),
    }
    if production:
        (bundle / "rollback-predecessor.bin").write_bytes(PREDECESSOR)
        (bundle / "storage-empty.bin").write_bytes(STORAGE)
        manifest["production_release"] = {
            "release_id": "zone-lite-2.7.8", "version": "2.7.8", "git_sha": "b" * 40,
            "image_sha256": manifest["application_sha256"], "application_sha256": "c" * 64,
            "boot_slot": "ota_0",
        }
        manifest["rollback_predecessor"] = {
            "release_id": "zone-lite-2.5.2", "version": "2.5.2", "git_sha": "d" * 40,
            "image_sha256": hashlib.sha256(PREDECESSOR).hexdigest(),
            "application_sha256": "4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b",
            "slot": "ota_1",
        }
        manifest["storage_image_sha256"] = hashlib.sha256(STORAGE).hexdigest()
        manifest["storage_image_size"] = len(STORAGE)
    (bundle / "bootstrap-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def provisioning_inputs(tmp_path: Path, bundle: Path) -> dict:
    nvs = tmp_path / "provision.bin"
    nvs.write_bytes(b"n" * 16)
    hmac = tmp_path / "hmac.bin"
    hmac.write_bytes(b"k" * 32)
    provisioning = tmp_path / "provisioning.json"
    provisioning.write_text(json.dumps({
        "aad": {"firmware_family": "zkt", "target_mac": MAC, "nvs_sha256": hashlib.sha256(b"n" * 16).hexdigest(),
                "nvs_size": 16},
        "hmac_key": {"aad": {"target_mac": MAC, "purpose": "zone-lite-nvs-hmac-efuse-key", "key_size": 32}},
    }))
    return dict(signed_package=bundle, provisioning_manifest=provisioning, provision_nvs=nvs,
                hmac_key=hmac, mac=MAC, expected_git_sha="a" * 40)


def rewrite_manifest(bundle: Path, change) -> None:
    manifest = json.loads((bundle / "bootstrap-manifest.json").read_text())
    change(manifest)
    (bundle / "bootstrap-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_otadata_selects_ota_0_with_esp_idf_crc():
    data = flasher.otadata_selecting_ota_0()
    assert len(data) == 0x2000
    seq, state, crc = struct.unpack_from("<I20xII", data, 0)
    assert (seq, state) == (1, 2) and (seq - 1) % 2 == 0
    assert crc == zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF)
    assert data[32:] == b"\xff" * (0x2000 - 32)


def test_production_package_boots_the_exact_release_from_ota_0(tmp_path, monkeypatch):
    bundle = package(tmp_path)
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        assert kwargs["sources"]["otadata-ota0.bin"].read_bytes() == flasher.otadata_selecting_ota_0()

    monkeypatch.setattr(flasher, "flash_sources_and_verify", capture)
    flasher.flash_and_verify(esptool="esptool", port="PORT", signed_package=bundle,
                             provision_nvs=tmp_path / "provision.bin")
    (call,) = calls
    assert call["layout"] == flasher.PRODUCTION_LAYOUT
    offsets = [offset for name, offset in call["layout"] if name == "zone-lite-signed.bin"]
    assert offsets == [0x20000, 0x2A0000]
    assert ("otadata-ota0.bin", 0x17000) in call["layout"]
    assert "ota_data_initial.bin" not in dict(call["layout"])
    # 2.7.x needs a qualified predecessor in the other OTA slot and a
    # mountable SPIFFS; neither exists on a never-used board.
    assert ("rollback-predecessor.bin", 0x520000) in call["layout"]
    assert ("storage-empty.bin", 0x7A0000) in call["layout"]
    assert call["sources"]["rollback-predecessor.bin"].read_bytes() == PREDECESSOR
    assert call["sources"]["storage-empty.bin"].read_bytes() == STORAGE
    assert call["verify_with_stub"] is True
    starts = [offset for _name, offset in call["layout"]]
    assert starts == sorted(starts)


def test_production_partitions_match_the_firmware_partition_table():
    kinds = {"app": 0, "data": 1}
    subtypes = {"factory": 0x00, "ota_0": 0x10, "ota_1": 0x11, "ota": 0x00, "nvs": 0x02,
                "phy": 0x01, "spiffs": 0x82}
    rows = {}
    for line in (ROOT / "firmware/zone_lite/partitions.csv").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, kind, subtype, offset, size = (field.strip() for field in line.split(",")[:5])
            rows[name] = (kinds[kind], subtypes[subtype], int(offset, 16), int(size, 16))
    assert flasher.PRODUCTION_PARTITIONS == {name: rows[name] for name in flasher.PRODUCTION_PARTITIONS}
    assert flasher.PRODUCTION_PARTITIONS["ota_1"] == (0, 0x11, 0x520000, 0x280000)
    assert flasher.PRODUCTION_PARTITIONS["storage"] == (1, 0x82, 0x7A0000, 0x800000)
    slots = {offset: name for name, (_kind, _subtype, offset, _size) in rows.items()}
    assert [slots.get(offset) for image, offset in flasher.PRODUCTION_LAYOUT
            if image in {"zone-lite-signed.bin", "rollback-predecessor.bin", "storage-empty.bin"}] == [
        "factory", "ota_0", "ota_1", "storage",
    ]


def test_built_package_keeps_the_original_factory_layout(tmp_path, monkeypatch):
    bundle = package(tmp_path, production=False)
    calls = []
    monkeypatch.setattr(flasher, "flash_sources_and_verify", lambda **kwargs: calls.append(kwargs))
    flasher.flash_and_verify(esptool="esptool", port="PORT", signed_package=bundle,
                             provision_nvs=tmp_path / "provision.bin")
    assert calls[0]["layout"] == flasher.FLASH_LAYOUT
    assert calls[0].get("verify_with_stub", False) is False


@pytest.mark.parametrize("fault", ["slot", "hash", "version", "shape"])
def test_production_release_claim_must_be_exact(tmp_path, fault):
    bundle = package(tmp_path)
    manifest = json.loads((bundle / "bootstrap-manifest.json").read_text())
    release = manifest["production_release"]
    if fault == "slot":
        release["boot_slot"] = "factory"
    elif fault == "hash":
        release["image_sha256"] = "0" * 64
    elif fault == "version":
        release["version"] = "latest"
    else:
        manifest["production_release"] = "2.7.8"
    with pytest.raises(RuntimeError, match="production release is invalid"):
        flasher.production_release(manifest)


@pytest.mark.parametrize("row", [
    ("ota_0", 0, 0x10, 0x2B0000, 0x270000),
    ("ota_1", 0, 0x11, 0x530000, 0x270000),
    ("storage", 1, 0x81, 0x7A0000, 0x800000),
    ("storage", 1, 0x82, 0x7A0000, 0x400000),
    None,
])
def test_production_package_refuses_a_different_partition_layout(tmp_path, row):
    if row is None:
        table = [entry for entry in ZONE_LITE_TABLE if entry[0] != "storage"]
    else:
        table = [entry if entry[0] != row[0] else row for entry in ZONE_LITE_TABLE]
    bundle = package(tmp_path, table=table)
    arguments = provisioning_inputs(tmp_path, bundle)
    with pytest.raises(RuntimeError, match="production OTA layout"):
        flasher.validate_inputs(**arguments)
    (bundle / "partition-table.bin").write_bytes(partition_table(ZONE_LITE_TABLE))
    flasher.validate_inputs(**arguments)


@pytest.mark.parametrize("fault, message", [
    ("missing", "no rollback predecessor for ota_1"),
    ("slot", "rollback predecessor is invalid"),
    ("version", "rollback predecessor is invalid"),
    ("hash", "rollback predecessor is invalid"),
    ("same-image", "rollback predecessor is invalid"),
    ("same-version", "must be older than the production release"),
    ("newer", "must be older than the production release"),
    ("no-storage", "no empty storage image"),
    ("storage-size", "no empty storage image"),
])
def test_production_claim_requires_an_older_predecessor_and_storage(tmp_path, fault, message):
    manifest = json.loads((package(tmp_path) / "bootstrap-manifest.json").read_text())
    predecessor = manifest["rollback_predecessor"]
    if fault == "missing":
        del manifest["rollback_predecessor"]
    elif fault == "slot":
        predecessor["slot"] = "ota_0"
    elif fault == "version":
        predecessor["version"] = "latest"
    elif fault == "hash":
        predecessor["image_sha256"] = "not-a-digest"
    elif fault == "same-image":
        predecessor["image_sha256"] = manifest["production_release"]["image_sha256"]
    elif fault == "same-version":
        predecessor["version"] = "2.7.8"
    elif fault == "newer":
        predecessor["version"] = "2.10.0"
    elif fault == "no-storage":
        del manifest["storage_image_sha256"]
    else:
        manifest["storage_image_size"] = 0x400000
    with pytest.raises(RuntimeError, match=message):
        flasher.production_release(manifest)


def test_versions_compare_numerically_not_as_text():
    assert flasher.version_tuple("2.5.2") < flasher.version_tuple("2.7.8") < flasher.version_tuple("2.10.0")


@pytest.mark.parametrize("fault, message", [
    ("missing-predecessor", "missing rollback-predecessor.bin"),
    ("missing-storage", "missing storage-empty.bin"),
    ("changed-predecessor", "rollback-predecessor.bin hash does not match"),
    ("changed-storage", "storage-empty.bin hash does not match"),
    ("short-storage", "does not cover the whole storage partition"),
    ("oversized-predecessor", "does not fit its OTA slot"),
])
def test_production_package_must_carry_the_exact_predecessor_and_storage(tmp_path, fault, message):
    bundle = package(tmp_path)
    arguments = provisioning_inputs(tmp_path, bundle)
    flasher.validate_inputs(**arguments)
    if fault == "missing-predecessor":
        (bundle / "rollback-predecessor.bin").unlink()
    elif fault == "missing-storage":
        (bundle / "storage-empty.bin").unlink()
    elif fault == "changed-predecessor":
        (bundle / "rollback-predecessor.bin").write_bytes(b"exact published 2.6.15 bytes")
    elif fault == "changed-storage":
        (bundle / "storage-empty.bin").write_bytes(b"\xff" * 0x800000)
    else:
        name = "storage-empty.bin" if fault == "short-storage" else "rollback-predecessor.bin"
        image = b"\x19" * (0x400000 if fault == "short-storage" else 0x280001)
        (bundle / name).write_bytes(image)
        if fault == "short-storage":
            rewrite_manifest(bundle, lambda m: m.update(storage_image_sha256=hashlib.sha256(image).hexdigest()))
        else:
            rewrite_manifest(bundle, lambda m: m["rollback_predecessor"].update(
                image_sha256=hashlib.sha256(image).hexdigest()))
    with pytest.raises(RuntimeError, match=message):
        flasher.validate_inputs(**arguments)


def test_built_package_needs_no_predecessor_or_storage(tmp_path):
    bundle = package(tmp_path, production=False)
    flasher.validate_inputs(**provisioning_inputs(tmp_path, bundle))


def stub_verify_run(commands, *, failing_offset=None, matched="-- verify OK (digest matched)"):
    def fake_run(command, line_callback=None):
        commands.append(command)
        if "verify_flash" in command:
            offset = int(command[command.index("verify_flash") + 1], 16)
            if offset == failing_offset:
                raise subprocess.CalledProcessError(2, command, output="-- verify FAILED (digest mismatch)")
            return f"Verifying 0x{offset:x}...\n{matched}\n"
        if line_callback:
            line_callback("Writing at 0x00000000 (0%)\n")
        return ""
    return fake_run


def stub_sources(tmp_path):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"A" * 300_000)
    second.write_bytes(b"B" * 300_000)
    return {"first.bin": first, "second.bin": second}, (("first.bin", 0), ("second.bin", 0x7A0000))


def test_production_layout_is_verified_on_chip_by_the_stub(tmp_path, monkeypatch):
    sources, layout = stub_sources(tmp_path)
    commands: list[list[str]] = []
    monkeypatch.setattr(flasher, "run", stub_verify_run(commands))
    progress: list[tuple[str, int, int]] = []
    flasher.flash_sources_and_verify(
        esptool="esptool", port="PORT", sources=sources, layout=layout,
        progress=lambda state, done, total: progress.append((state, done, total)),
        verify_with_stub=True,
    )
    write, *verifies = commands
    assert "write_flash" in write
    assert not any("read_flash" in command for command in commands)
    assert [command[command.index("verify_flash") + 1:] for command in verifies] == [
        ["0x0", str(sources["first.bin"])],
        ["0x7a0000", str(sources["second.bin"])],
    ]
    # The stub hashes the range on chip; --no-stub would fall back to the
    # slow ROM path that failed its CRC on the new boards.
    assert all("--no-stub" not in command for command in verifies)
    assert progress[-3:] == [
        ("FLASHING", 600_000, 1_200_000),
        ("READBACK_VERIFYING", 900_000, 1_200_000),
        ("READBACK_VERIFYING", 1_200_000, 1_200_000),
    ]


@pytest.mark.parametrize("failure", ["exit", "no-match"])
def test_stub_verification_failure_is_refused(tmp_path, monkeypatch, failure):
    sources, layout = stub_sources(tmp_path)
    commands: list[list[str]] = []
    if failure == "exit":
        fake = stub_verify_run(commands, failing_offset=0x7A0000)
    else:
        fake = stub_verify_run(commands, matched="")
    monkeypatch.setattr(flasher, "run", fake)
    expected = "second.bin at 0x7a0000" if failure == "exit" else "first.bin at 0x0"
    with pytest.raises(RuntimeError, match=f"Flash verification failed for {expected}"):
        flasher.flash_sources_and_verify(esptool="esptool", port="PORT", sources=sources,
                                         layout=layout, verify_with_stub=True)


def test_bootstrap_carries_only_an_available_exact_store_release():
    signer = (ROOT / "deploy/add/bootstrap-firmware-signing.ps1").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/firmware-key-bootstrap.yml").read_text(encoding="utf-8")
    for required in (
        "[string]$ProductionReleaseDirectory = ''",
        "Only a factory bootstrap can carry a production release",
        "@('.hil-only.json', '.diagnostic-only.json')",
        "$Purpose image does not match its signed manifest",
        "-Purpose 'Production release'",
        "Bootstrap application is not the exact production release image",
        "$productionRelease.boot_slot = 'ota_0'",
        "$packageFields.production_release = $productionRelease",
    ):
        assert required in signer, required
    for required in (
        "default: latest",
        "PRODUCTION_RELEASE: ${{ inputs.production_release || 'latest' }}",
        "-not (Test-Path -LiteralPath (Join-Path $_.FullName '.hil-only.json'))",
        "-not (Test-Path -LiteralPath (Join-Path $_.FullName '.diagnostic-only.json'))",
        "-not $family -or $family -eq 'zkt'",
        "Sort-Object { [version]$_.Name } | Select-Object -Last 1",
        "@productionArguments",
    ):
        assert required in workflow, required


def test_bootstrap_carries_an_older_available_predecessor_and_empty_storage():
    signer = (ROOT / "deploy/add/bootstrap-firmware-signing.ps1").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/firmware-key-bootstrap.yml").read_text(encoding="utf-8")
    for required in (
        "[string]$RollbackPredecessorDirectory = ''",
        "A rollback predecessor is carried only together with a production release",
        "A production release bootstrap must carry a rollback predecessor for ota_1",
        "-Purpose 'Rollback predecessor'",
        "if ($predecessor.Version -ge $production.Version) {",
        "$rollbackPredecessor.slot = 'ota_1'",
        "Unsigned firmware package is missing storage-empty.bin",
        "$storagePartitionSize = 0x800000",
        "-Destination (Join-Path $output 'rollback-predecessor.bin')",
        "-Destination (Join-Path $output 'storage-empty.bin')",
        "Bootstrap ota_1 image is not the exact rollback predecessor image",
        "$packageFields.rollback_predecessor = $rollbackPredecessor",
        "$packageFields.storage_image_sha256 =",
        "$packageFields.storage_image_size = $storageImage.Length",
    ):
        assert required in signer, required
    # The predecessor comes from the same AVAILABLE-only ZKT list as the
    # production release and must be strictly older.
    for required in (
        "rollback_predecessor:",
        "default: '2.5.2'",
        "ROLLBACK_PREDECESSOR: ${{ inputs.rollback_predecessor || '2.5.2' }}",
        "if ($env:ROLLBACK_PREDECESSOR -notmatch '^(none|[0-9]+\\.[0-9]+\\.[0-9]+)$') {",
        "if ($env:ROLLBACK_PREDECESSOR -eq 'none' -and $env:PRODUCTION_RELEASE -ne 'none') {",
        "$predecessor = $available | Where-Object { $_.Name -eq $env:ROLLBACK_PREDECESSOR }",
        "if ([version]$predecessor.Name -ge [version]$chosen.Name) {",
        "$productionArguments.RollbackPredecessorDirectory = $predecessor.FullName",
        "python tools/build_empty_storage_image.py --build-dir build-bootstrap "
        "--output build-bootstrap/storage-empty.bin",
        "firmware/zone_lite/build-bootstrap/storage-empty.bin",
        "'flasher_args.json', 'storage-empty.bin'",
    ):
        assert required in workflow, required
    build = workflow.index("idf.py -B build-bootstrap")
    assert build < workflow.index("tools/build_empty_storage_image.py") < workflow.index("generate-and-sign:")


def test_ci_pins_the_bench_verified_empty_storage_image():
    ci = (ROOT / ".github/workflows/add-ci.yml").read_text(encoding="utf-8")
    assert ("python tools/build_empty_storage_image.py --build-dir build-ci "
            "--output build-ci/storage-empty.bin") in ci
    assert ("f39db7c1c7626ee6156dbab5da7912968040a489d1d927c1fb288d1f42c5ec70  "
            "firmware/zone_lite/build-ci/storage-empty.bin\" | sha256sum --check") in ci
    assert ci.count("./tests/firmware/test_bootstrap_production_signing.ps1") == 2


FIRMWARE_SPIFFS = {
    "SPIFFS_PAGE_SIZE": 256,
    "SPIFFS_OBJ_NAME_LEN": 32,
    "SPIFFS_META_LENGTH": 4,
    "SPIFFS_USE_MAGIC": True,
    "SPIFFS_USE_MAGIC_LENGTH": True,
    "SPIFFS_FOLLOW_SYMLINKS": False,
}


def storage_build(tmp_path: Path, *, sdkconfig=None, table=ZONE_LITE_TABLE) -> Path:
    build = tmp_path / "build-bootstrap"
    (build / "config").mkdir(parents=True)
    (build / "partition_table").mkdir()
    (build / "config" / "sdkconfig.json").write_text(json.dumps(sdkconfig or FIRMWARE_SPIFFS))
    (build / "partition_table" / "partition-table.bin").write_bytes(partition_table(table))
    return build


def fake_spiffsgen(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "spiffsgen.py"
    script.write_text(
        "import sys\n"
        "size, base, output = int(sys.argv[1], 16), sys.argv[2], sys.argv[3]\n"
        "open(sys.argv[0] + '.args', 'w').write(' '.join(sys.argv[1:]))\n" + body
    )
    return script


def test_empty_storage_image_uses_spiffsgen_with_the_firmware_spiffs_options(tmp_path):
    build = storage_build(tmp_path)
    spiffsgen = fake_spiffsgen(
        tmp_path, "open(output, 'wb').write(b'\\x19' * 256 + b'\\xff' * (size - 256))\n"
    )
    output = build / "storage-empty.bin"
    digest = storage_tool.build_empty_storage_image(build_dir=build, output=output, spiffsgen=spiffsgen)
    assert output.stat().st_size == 0x800000
    assert digest == hashlib.sha256(output.read_bytes()).hexdigest()
    arguments = (tmp_path / "spiffsgen.py.args").read_text().split()
    assert arguments[0] == "0x800000" and arguments[2] == str(output)
    assert arguments[3:] == [
        "--page-size", "256", "--block-size", "4096", "--obj-name-len", "32",
        "--meta-len", "4", "--use-magic", "--use-magic-len",
    ]
    assert storage_tool.spiffsgen_command(Path("spiffsgen.py"), 0x800000, Path("empty"), output)[1:] == [
        "spiffsgen.py", "0x800000", "empty", str(output), *arguments[3:],
    ]


@pytest.mark.parametrize("key, value", [
    ("SPIFFS_PAGE_SIZE", 512),
    ("SPIFFS_OBJ_NAME_LEN", 64),
    ("SPIFFS_META_LENGTH", 0),
    ("SPIFFS_USE_MAGIC", False),
    ("SPIFFS_USE_MAGIC_LENGTH", False),
    ("SPIFFS_USE_MAGIC_LENGTH", None),
    ("SPIFFS_META_LENGTH", True),
])
def test_empty_storage_image_refuses_a_build_with_other_spiffs_options(tmp_path, key, value):
    sdkconfig = dict(FIRMWARE_SPIFFS)
    if value is None:
        del sdkconfig[key]
    else:
        sdkconfig[key] = value
    build = storage_build(tmp_path, sdkconfig=sdkconfig)
    spiffsgen = fake_spiffsgen(tmp_path, "raise SystemExit('must not run')\n")
    with pytest.raises(RuntimeError, match=f"CONFIG_{key}="):
        storage_tool.build_empty_storage_image(build_dir=build, output=build / "storage.bin",
                                               spiffsgen=spiffsgen)
    assert not (build / "storage.bin").exists()


@pytest.mark.parametrize("table", [
    [entry for entry in ZONE_LITE_TABLE if entry[0] != "storage"],
    [entry if entry[0] != "storage" else ("storage", 1, 0x81, 0x7A0000, 0x800000) for entry in ZONE_LITE_TABLE],
    [entry if entry[0] != "storage" else ("storage", 1, 0x82, 0x7A0000, 0x800100) for entry in ZONE_LITE_TABLE],
])
def test_empty_storage_image_requires_one_spiffs_storage_partition(tmp_path, table):
    build = storage_build(tmp_path, table=table)
    with pytest.raises(RuntimeError, match="(?i)partition named storage|storage partition size"):
        storage_tool.build_empty_storage_image(build_dir=build, output=build / "storage.bin",
                                               spiffsgen=tmp_path / "spiffsgen.py")


@pytest.mark.parametrize("body, message", [
    ("open(output, 'wb').write(b'\\x19' * (size // 2))\n", "size does not match"),
    ("open(output, 'wb').write(b'\\xff' * size)\n", "erased flash"),
])
def test_empty_storage_image_output_is_checked(tmp_path, body, message):
    build = storage_build(tmp_path)
    with pytest.raises(RuntimeError, match=message):
        storage_tool.build_empty_storage_image(build_dir=build, output=build / "storage.bin",
                                               spiffsgen=fake_spiffsgen(tmp_path, body))
