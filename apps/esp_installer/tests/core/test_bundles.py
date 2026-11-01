"""The bundle gate (design sections 4.6 and 6) and the bundle cache."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sl_installer.core import bundles, texts
from sl_installer.core.api import CancelToken, Family, InstallerError
from sl_installer.core.bundles import (BundleStore, check_manifest, manifest_sha256, parse_image,
                                       signature_blocks)

from . import support


def entry(manifest, role):
    return next(item for item in manifest["images"] if item["role"] == role)


def fetch(store, bundle, progress=None):
    source = support.FakeSource(bundle)
    return store.fetch(source, bundle.bundle_id, progress, CancelToken()), source


def refused(store, bundle, code="BUNDLE_NOT_TRUSTED"):
    with pytest.raises(InstallerError) as caught:
        fetch(store, bundle)
    assert caught.value.advice.code == code, caught.value.cause
    return caught.value


# ----------------------------------------------------------- image parsing

@pytest.mark.parametrize("name", sorted(support.DIGESTS))
def test_image_digest_matches_esptool(name):
    facts = parse_image(support.image(name))
    assert facts.digest == support.DIGESTS[name]
    assert facts.digest_valid and facts.chip_id == 9


def test_app_descriptor_and_signatures():
    facts = parse_image(support.image("app-zkt-2.9.1-signed.bin"))
    assert (facts.project, facts.version) == ("zone_lite", "2.9.1")
    assert len(facts.signatures) == 1 and facts.signatures[0].valid
    assert facts.signatures[0].modulus == support.sb_key(1).public_numbers().n
    hik = parse_image(support.image("app-hik-3.1.1-signed.bin"))
    assert (hik.project, hik.version) == ("zone_lite_hikvision", "3.1.1")
    boot = signature_blocks(support.image("bootloader-signed.bin"))
    assert [b.modulus for b in boot] == [support.sb_key(i).public_numbers().n for i in (1, 2, 3)]
    assert all(b.valid for b in boot)


def test_tampered_image_signature_is_invalid():
    data = bytearray(support.image("app-zkt-2.9.1-signed.bin"))
    data[200] ^= 0xFF
    facts = parse_image(bytes(data))
    assert not facts.digest_valid
    assert not facts.signatures[0].valid


# --------------------------------------------------------- whole bundles

def test_valid_zkt_bundle_downloads_and_verifies(store, zkt_bundle):
    seen = []
    local, source = fetch(store, zkt_bundle, lambda done, total: seen.append((done, total)))
    assert local.info.bundle_id == "zkt-2.9.1-e89380a1"
    assert local.info.family is Family.ZKT and local.info.recommended
    assert local.info.release_state == "AVAILABLE"
    # The 8 MiB storage image comes from the copy inside the app, not the network.
    assert "storage-empty.bin" not in source.downloads
    assert sorted(source.downloads) == ["bootloader-signed.bin", "ota-data.bin",
                                        "partition-table.bin", "rollback-predecessor.bin",
                                        "zone-lite-signed.bin"]
    assert seen[0][0] == 0 and seen[-1][0] == seen[-1][1]
    ranges = {r.role: r for r in bundles.flash_ranges(local)}
    assert ranges["ota_0"].path == ranges["factory"].path
    assert ranges["storage"].size == 0x800000


def test_valid_hikvision_bundle(store, hik_bundle):
    local, _source = fetch(store, hik_bundle)
    assert local.info.family is Family.HIKVISION
    assert {r.role for r in bundles.flash_ranges(local)} == {
        "bootloader", "partition-table", "otadata", "factory", "storage"}


def test_cached_bundle_is_not_downloaded_twice(store, zkt_bundle):
    fetch(store, zkt_bundle)
    _local, source = fetch(store, zkt_bundle)
    assert source.downloads == []


def test_verify_local_rechecks_files_on_every_use(store, zkt_bundle):
    local, _ = fetch(store, zkt_bundle)
    target = Path(local.directory) / "zone-lite-signed.bin"
    data = bytearray(target.read_bytes())
    data[100] ^= 1
    target.write_bytes(bytes(data))
    with pytest.raises(InstallerError) as caught:
        store.verify_local(zkt_bundle.bundle_id)
    assert caught.value.advice is texts.BUNDLE_FILE_DAMAGED


def test_damaged_download_is_removed(store, zkt_bundle):
    source = support.FakeSource(zkt_bundle)
    source.corrupt["zone-lite-signed.bin"] = b"\x00" * len(zkt_bundle.files["zone-lite-signed.bin"])
    with pytest.raises(InstallerError) as caught:
        store.fetch(source, zkt_bundle.bundle_id, None, CancelToken())
    assert caught.value.advice is texts.BUNDLE_FILE_DAMAGED
    directory = store.directory(zkt_bundle.bundle_id)
    assert not (directory / "zone-lite-signed.bin").exists()
    assert not (directory / "zone-lite-signed.bin.part").exists()


def test_catalogue_hash_must_match(store, zkt_bundle):
    source = support.FakeSource(zkt_bundle)
    with pytest.raises(InstallerError):
        store.fetch(source, zkt_bundle.bundle_id, None, CancelToken(),
                    expected_manifest_sha256="0" * 64)
    good = manifest_sha256(zkt_bundle.manifest)
    store.fetch(source, zkt_bundle.bundle_id, None, CancelToken(), expected_manifest_sha256=good)


def test_bad_signature_is_refused(tmp_path, store, manifest_key):
    bundle = support.build_bundle(tmp_path, Family.ZKT, manifest_key)
    bundle.manifest["notes"] = "changed after signing"
    refused(store, bundle)
    other = support.signing_key()
    resigned = support.build_bundle(tmp_path / "b", Family.ZKT, other)
    refused(store, resigned)


def test_cancel_stops_the_download(store, zkt_bundle):
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(InstallerError) as caught:
        store.fetch(support.FakeSource(zkt_bundle), zkt_bundle.bundle_id, None, cancel)
    assert caught.value.advice is texts.CANCELLED


def _bundle(tmp_path, key, change, family=Family.ZKT):
    return support.build_bundle(tmp_path, family, key, change=change)


def _set_file(name, data):
    def change(manifest, files):
        files[name] = data
        for item in manifest["images"]:
            if item["name"] == name:
                item["size"] = len(data)
                item["sha256"] = hashlib.sha256(data).hexdigest()
    return change


@pytest.mark.parametrize("label, change", [
    ("partition table at 0x8000 as in the design example",
     lambda m, f: entry(m, "partition-table").update(offset=0x8000)),
    ("unknown schema", lambda m, f: m.update(schema="zone-lite-installer-bundle-v2")),
    ("other layout", lambda m, f: m.update(partition_layout="zone-lite-factory-v1")),
    ("hil only release", lambda m, f: m.update(release_state="HIL_ONLY")),
    ("missing ota_1", lambda m, f: m.update(images=[i for i in m["images"]
                                                    if i["role"] != "ota_1"])),
    ("storage not the empty image", lambda m, f: entry(m, "storage").update(sha256="1" * 64)),
    ("ota_0 differs from factory", lambda m, f: entry(m, "ota_0").update(
        name="rollback-predecessor.bin", sha256=entry(m, "ota_1")["sha256"],
        size=entry(m, "ota_1")["size"])),
    ("predecessor newer than release", lambda m, f: entry(m, "ota_1").update(app_version="3.0.0")),
    ("bundle id does not match the digest", lambda m, f: m.update(bundle_id="zkt-2.9.1-00000000")),
    ("path in a file name", lambda m, f: entry(m, "otadata").update(name="../ota-data.bin")),
    ("factory project of the other family",
     lambda m, f: entry(m, "factory").update(app_project="zone_lite_hikvision")),
])
def test_manifest_checks(tmp_path, store, manifest_key, label, change):
    refused(store, _bundle(tmp_path, manifest_key, change))


def test_partition_table_must_be_production(tmp_path, store, manifest_key):
    table = support.partition_table(storage=(1, 0x82, 0x7A0000, 0x400000))
    refused(store, _bundle(tmp_path, manifest_key, _set_file("partition-table.bin", table)))


def test_zkt_otadata_must_select_ota_0(tmp_path, store, manifest_key):
    refused(store, _bundle(tmp_path, manifest_key,
                           _set_file("ota-data.bin", bundles.blank_otadata())))


def test_hikvision_otadata_must_be_blank(tmp_path, store, manifest_key):
    refused(store, _bundle(tmp_path, manifest_key, _set_file(
        "ota-data.bin", bundles.otadata_selecting_ota_0()), family=Family.HIKVISION))


def test_predecessor_must_be_qualified(tmp_path, manifest_key):
    strict = BundleStore(tmp_path / "strict", bundles.Verifier(
        manifest_key=manifest_key.public_key(), image_key=support.sb_key(1)),
        storage_gz=support.STORAGE_GZ)
    # The real table pins 2.5.2 to 4b4aa069...; the test image has another digest.
    refused(strict, support.build_bundle(tmp_path, Family.ZKT, manifest_key))


def test_bootloader_must_trust_the_pinned_key(tmp_path, store, manifest_key):
    untrusting = BundleStore(tmp_path / "other", bundles.Verifier(
        manifest_key=manifest_key.public_key(), image_key=manifest_key.public_key(),
        qualified=support.TEST_QUALIFIED), storage_gz=support.STORAGE_GZ)
    refused(untrusting, support.build_bundle(tmp_path, Family.ZKT, manifest_key))


def test_app_signed_by_an_untrusted_key_is_refused(tmp_path, store, manifest_key):
    # A bootloader carrying only key 1 three times would not trust anything else; here
    # the app is unsigned (its signature sector removed) instead.
    app = support.image("app-zkt-2.9.1-signed.bin")[:-4096] + b"\xff" * 4096
    refused(store, _bundle(tmp_path, manifest_key, _set_file("zone-lite-signed.bin", app)))


def test_unsigned_bootloader_is_refused(tmp_path, store, manifest_key):
    boot = support.image("bootloader-signed.bin")[:-4096] + b"\xff" * 4096
    refused(store, _bundle(tmp_path, manifest_key, _set_file("bootloader-signed.bin", boot)))


def test_wrong_project_for_family_is_refused(tmp_path, store, manifest_key):
    hik_app = support.image("app-hik-3.1.1-signed.bin")

    def change(manifest, files):
        _set_file("zone-lite-signed.bin", hik_app)(manifest, files)
        entry(manifest, "factory").update(app_digest=support.DIGESTS["app-hik-3.1.1-signed.bin"])
        manifest["bundle_id"] = "zkt-2.9.1-77620195"
    refused(store, _bundle(tmp_path, manifest_key, change))


def test_check_manifest_lists_each_file_once(zkt_bundle):
    checked = check_manifest(zkt_bundle.manifest, zkt_bundle.bundle_id)
    assert len(checked.images) == 7 and len(checked.files()) == 6


def test_embedded_storage_image_is_verified(tmp_path, manifest_key):
    store = BundleStore(tmp_path / "c", support.verifier(manifest_key),
                        storage_gz=support.STORAGE_GZ)
    path = store.storage_image()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == bundles.STORAGE_EMPTY_SHA256
    broken = tmp_path / "broken.gz"
    import gzip
    broken.write_bytes(gzip.compress(b"\x00" * 16))
    bad = BundleStore(tmp_path / "d", support.verifier(manifest_key), storage_gz=broken)
    with pytest.raises(InstallerError):
        bad.storage_image()


def test_shipped_storage_image_is_small_and_exact():
    data = support.STORAGE_GZ.read_bytes()
    assert len(data) < 64 * 1024
    assert hashlib.sha256(support.storage_image()).hexdigest() == bundles.STORAGE_EMPTY_SHA256


def test_pinned_production_key_is_rsa_3072_vault_key_1():
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    path = support.SRC / "sl_installer" / "resources" / "firmware-signing-key-1.pem"
    key = bundles.load_public_key(path)
    assert key.key_size == 3072
    # The key itself (its DER SubjectPublicKeyInfo, KEY_SPKI_SHA256 in
    # scripts/audit_factory_bundle.py), whatever line endings the checkout gave the file:
    # Git on Windows runners turns the PEM's LF into CRLF. This is the pin that
    # docs/esp-installer-bundles.md publishes and the bundle publisher checks.
    der = key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    assert hashlib.sha256(der).hexdigest() == \
        "0523b6a90880e60c1526533128931c770024659d89f798a756c92fa7edae7bf0"
    # And the vault's key ID (KEY_PEM_SHA256), the hash of the PEM file as committed,
    # with LF line endings.
    committed = path.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(committed).hexdigest() == \
        "9fc614e085ba24f260ad92be0812103beef0b43c74881d72e800caf68c602a55"


def test_key_fingerprint_does_not_depend_on_line_endings(tmp_path):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    source = support.SRC / "sl_installer" / "resources" / "firmware-signing-key-1.pem"
    crlf = tmp_path / "key-crlf.pem"
    crlf.write_bytes(source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    spki = [bundles.load_public_key(path).public_bytes(Encoding.DER,
                                                        PublicFormat.SubjectPublicKeyInfo)
            for path in (source, crlf)]
    assert spki[0] == spki[1]


def test_manifest_written_to_cache_round_trips(store, zkt_bundle):
    local, _ = fetch(store, zkt_bundle)
    saved = json.loads((Path(local.directory) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_sha256(saved) == manifest_sha256(zkt_bundle.manifest)
