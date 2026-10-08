"""Synthetic reader identities never modify the checked-in release matrix."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from types import SimpleNamespace
import zlib

import pytest

from scripts.build_zkt_reader_matrix import render_header
from zk_add import zkt_reader_matrix as matrix
from zk_add.zkt_reader_evidence import qualified_reader_proof, reader_entry_for_manifest
from zk_add.zkt_writer_contract import validate_writer_image, validate_writer_manifest, writer_predecessor_hold
from zk_add.zkt_bridge_contract import signed_hil_targets

ROOT = Path(__file__).resolve().parents[2]


def blocked_matrix():
    return {"schema_version": 1, "matrix_id": matrix.MATRIX_ID, "state": "BLOCKED", "readers": []}


def synthetic_matrix():
    return {"schema_version": 1, "matrix_id": matrix.MATRIX_ID, "state": "PINNED", "readers": [
        {"version": version, "release_id": "zone-lite-" + version,
         "application_sha256": bytes([11 + index] + [0] * 31).hex(),
         "artifact_sha256": hashlib.sha256((version + "artifact").encode()).hexdigest(),
         "source_sha": hashlib.sha1((version + "source").encode()).hexdigest(),
         "signing_key_id": "isolated-test-key"} for index, version in enumerate(matrix.VERSIONS)]}


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    path = tmp_path / "matrix.json"
    value = synthetic_matrix()
    path.write_text(json.dumps(value))
    monkeypatch.setattr(matrix, "MATRIX_PATH", path)
    return value


def writer_manifest(value):
    return {"version": "2.7.0", "release_id": "zone-lite-2.7.0", "firmware_family": "zkt",
            "project_name": "zone_lite", "release_channel": "EXPERIMENTAL_HIL_ONLY",
            "minimum_bootstrap_version": matrix.minimum_reader_version(value), "runtime_profile": "ZKT_JOURNAL_V1",
            "hil_targets": signed_hil_targets(), "queue_storage": matrix.writer_matrix_contract(value)}


def assert_cli_contract(root, policy):
    result = subprocess.run([sys.executable, str(root / "scripts/build_zkt_reader_matrix.py"),
                             "--signing-contract"], capture_output=True, text=True, timeout=30)
    assert f"#define ZJ_READER_MATRIX_COUNT {len(policy['readers'])}U" in render_header(policy)
    if policy["state"] == "BLOCKED":
        with pytest.raises(ValueError):
            matrix.writer_matrix_contract(policy)
        assert result.returncode == 1 and not result.stdout
    else:
        expected = matrix.writer_matrix_contract(policy)
        assert result.returncode == 0 and not result.stderr
        assert result.stdout == matrix.canonical(expected).decode() + "\n"
        assert expected["reader_matrix_sha256"] == matrix.matrix_hash(policy)
        assert matrix.matrix_marker(policy).endswith(expected["reader_matrix_sha256"])


def test_checked_in_matrix_matches_its_signing_contract():
    assert_cli_contract(ROOT, matrix.load_matrix())


@pytest.mark.parametrize("populated", [False, True])
def test_cli_blocked_and_pinned_states_are_isolated(tmp_path, populated, monkeypatch):
    # Exercise the real CLI's fixed policy path without adding a runtime override
    # or depending on the current release policy's activation state.
    for relative in ("scripts/build_zkt_reader_matrix.py", "apps/add_backend/zk_add/zkt_reader_matrix.py",
                     "apps/add_backend/zk_add/__init__.py"):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    path = tmp_path / "apps/add_backend/zk_add/zkt_reader_matrix.json"
    policy = synthetic_matrix() if populated else blocked_matrix()
    path.write_text(json.dumps(policy))
    monkeypatch.setattr(matrix, "MATRIX_PATH", path)
    assert matrix.load_matrix() == policy
    if not populated:
        with pytest.raises(ValueError):
            matrix.load_matrix(require_pinned=True)
        with pytest.raises(ValueError):
            matrix.writer_matrix_contract()
    assert_cli_contract(tmp_path, policy)


@pytest.mark.parametrize("change", ["schema_bool", "extra", "missing", "blocked_rows", "empty_pinned",
    "order", "duplicate_version", "unknown_version", "duplicate_app", "duplicate_artifact",
    "wrong_release", "zero_digest", "wrong_type", "entry_extra", "entry_missing", "key_control"])
def test_matrix_refuses_ambiguous_incomplete_or_unbound_entries(change):
    value = synthetic_matrix()
    if change == "schema_bool":
        value["schema_version"] = True
    elif change == "extra":
        value["authority"] = "any"
    elif change == "missing":
        del value["state"]
    elif change == "blocked_rows":
        value["state"] = "BLOCKED"
    elif change == "empty_pinned":
        value["readers"] = []
    elif change == "order":
        value["readers"].reverse()
    elif change == "duplicate_version":
        value["readers"][1] = deepcopy(value["readers"][0])
    elif change == "unknown_version":
        value["readers"][0]["version"] = "2.6.17"
    elif change in {"duplicate_app", "duplicate_artifact"}:
        key = "application_sha256" if change == "duplicate_app" else "artifact_sha256"
        value["readers"][1][key] = value["readers"][0][key]
    elif change == "wrong_release":
        value["readers"][0]["release_id"] = "zone-lite-2.6.22"
    elif change == "zero_digest":
        value["readers"][0]["source_sha"] = "0" * 40
    elif change == "wrong_type":
        value["readers"][0]["application_sha256"] = 1
    elif change == "entry_extra":
        value["readers"][0]["ready"] = True
    elif change == "entry_missing":
        del value["readers"][0]["signing_key_id"]
    else:
        value["readers"][0]["signing_key_id"] = "key\nwrong"
    with pytest.raises(ValueError):
        matrix.validate_matrix(value)


def test_duplicate_json_keys_and_input_bounds_fail(tmp_path, monkeypatch):
    path = tmp_path / "matrix.json"
    monkeypatch.setattr(matrix, "MATRIX_PATH", path)
    path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="duplicate"):
        matrix.load_matrix()
    path.write_text(" " * 4097)
    with pytest.raises(ValueError, match="bound"):
        matrix.load_matrix()


def test_marker_manifest_and_every_reader_identity_share_one_hash(pinned):
    value = writer_manifest(pinned)
    assert validate_writer_manifest(value) == matrix.writer_matrix_contract(pinned)
    image = bytearray(112)
    image[0] = 0xE9
    struct.pack_into("<I", image, 32, 0xABCD5432)
    image[48:53] = b"2.7.0"
    image[80:89] = b"zone_lite"
    binary = image + matrix.matrix_marker(pinned).encode() + b"\0"
    validate_writer_image(binary, value)
    for field in matrix.ENTRY_KEYS - {"version", "release_id"}:
        altered = deepcopy(value)
        altered["queue_storage"]["reader_matrix"]["readers"][0][field] = "wrong"
        with pytest.raises(ValueError):
            validate_writer_manifest(altered)
    with pytest.raises(ValueError):
        validate_writer_image(binary + b"ZONE_STORAGE_CONTRACT_V4:WRITER\0", value)
    for row in pinned["readers"]:
        selection = reader_entry_for_manifest(value, row["version"])
        assert selection["reader"] == row
        assert matrix.selected_reader(value["queue_storage"], row["version"], row["application_sha256"]) == row
        assert matrix.selected_reader(value["queue_storage"], row["version"], "e" * 64) is None


def test_historical_writer_is_audit_only():
    from zk_add.zkt_writer_contract import writer_contract
    value = writer_manifest(synthetic_matrix())
    value["queue_storage"] = writer_contract()
    value["minimum_bootstrap_version"] = "2.6.17"
    assert validate_writer_manifest(value) == writer_contract()
    assert writer_predecessor_hold(None, None, SimpleNamespace(state="HIL_ONLY", manifest=value)) == "JOURNAL_HISTORICAL_WRITER_AUDIT_ONLY"


def test_semantic_minimum_is_not_the_preferred_reader_order(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, "VERSIONS", ("2.6.23", "2.6.22"))
    value = synthetic_matrix()
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(matrix, "MATRIX_PATH", path)
    manifest = writer_manifest(value)
    assert manifest["minimum_bootstrap_version"] == "2.6.22"
    contract = validate_writer_manifest(manifest)
    assert contract["allowed_bootstrap_versions"] == ["2.6.23", "2.6.22"]
    for entry in value["readers"]:
        assert matrix.selected_reader(contract, entry["version"], entry["application_sha256"]) == entry
    manifest["minimum_bootstrap_version"] = "2.6.23"
    with pytest.raises(ValueError):
        validate_writer_manifest(manifest)


def test_typed_reader_proof_binds_image_slot_generation_and_freshness(pinned):
    admission = reader_entry_for_manifest(writer_manifest(pinned), "2.6.23")
    proof = {"schema_version": 1, "verified": True, "matrix_sha256": admission["matrix_sha256"],
             "version": "2.6.23", "application_sha256": pinned["readers"][0]["application_sha256"],
             "slot_address": 0x520000, "slot_size": 0x280000, "proof_generation": "1",
             "sampled_uptime_ms": 100000}
    assert qualified_reader_proof({"qualified_reader": proof}, admission, 100) == {
        k: v for k, v in proof.items() if k != "sampled_uptime_ms"}
    near_boot = {**proof, "sampled_uptime_ms": 0}
    assert qualified_reader_proof({"qualified_reader": near_boot}, admission, 0)
    for sample, uptime in ((-1, 0), (0, -1), (-1000, -1)):
        with pytest.raises(ValueError):
            qualified_reader_proof({"qualified_reader": {**near_boot, "sampled_uptime_ms": sample}}, admission, uptime)
    for field, wrong in (("verified", False), ("matrix_sha256", "a" * 64), ("version", "2.6.22"),
            ("application_sha256", pinned["readers"][1]["application_sha256"]), ("slot_address", True),
            ("slot_size", 1), ("proof_generation", "01"), ("proof_generation", str(2**64)),
            ("sampled_uptime_ms", 0), ("schema_version", True)):
        with pytest.raises(ValueError):
            qualified_reader_proof({"qualified_reader": {**proof, field: wrong}}, admission, 100)


@pytest.mark.parametrize("populated", [False, True])
def test_reader_target_is_bounded_portable_c11(tmp_path, populated):
    policy = synthetic_matrix() if populated else blocked_matrix()
    (tmp_path / "zkt_qualified_reader_matrix.h").write_text(render_header(policy))
    program = r'''
#include "zkt_reader_policy.h"
#include <assert.h>
#include <stdlib.h>
int main(void) {
    const char *versions[] = {"2.6.23", "2.6.22"};
    const char *digests[] = {
        "0b00000000000000000000000000000000000000000000000000000000000000",
        "0c00000000000000000000000000000000000000000000000000000000000000"};
    assert(!zj_reader_policy_target(NULL, digests[0]));
    assert(!zj_reader_policy_target(versions[0], NULL));
    for (unsigned i = 0; i < 2; ++i) {
        assert(zj_reader_policy_target(versions[i], digests[i]) == EXPECTED);
        assert(!zj_reader_policy_target(versions[1 - i], digests[i]));
        for (unsigned length = 0; length < 64; ++length) {
            char *short_value = malloc(length + 1); assert(short_value);
            memcpy(short_value, digests[i], length); short_value[length] = '\0';
            assert(!zj_reader_policy_target(versions[i], short_value));
            free(short_value);
        }
        char long_value[66]; memcpy(long_value, digests[i], 64);
        long_value[64] = '0'; long_value[65] = '\0';
        assert(!zj_reader_policy_target(versions[i], long_value));
        for (unsigned offset = 0; offset < 64; ++offset) {
            char invalid[65]; memcpy(invalid, digests[i], 65); invalid[offset] = 'A';
            assert(!zj_reader_policy_target(versions[i], invalid));
        }
    }
}
'''
    unit = tmp_path / "reader-target.c"
    unit.write_text(program)
    executable = tmp_path / "reader-target"
    subprocess.run([shutil.which("cc"), "-std=c11", "-pedantic-errors", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-DZONE_LITE_QUALIFIED_READER_MATRIX=1", f"-DEXPECTED={int(populated)}",
        "-I", str(tmp_path), "-I", str(ROOT / "firmware/zone_lite/main"),
        str(unit), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True, timeout=30)


@pytest.mark.parametrize("populated", [False, True])
def test_actual_c_guard_accepts_only_pinned_exact_proof_and_empty_matrix_blocks(tmp_path, populated):
    policy = synthetic_matrix() if populated else blocked_matrix()
    (tmp_path / "zkt_qualified_reader_matrix.h").write_text(render_header(policy))
    for index, version in enumerate(matrix.VERSIONS):
        proof = bytearray(192)
        proof[:8] = b"ZJREAD01"
        struct.pack_into("<10I", proof, 8, 1, 192, 1, 63, 15, 1, 1, 0x520000, 0x280000, 0)
        proof[48], proof[80], proof[112], proof[128] = 11 + index, 22, 33, 44
        struct.pack_into("<Q", proof, 160, 1)
        proof[168:174] = version.encode()
        struct.pack_into("<I", proof, 188, zlib.crc32(proof[:188]))
        (tmp_path / f"proof-{index}").write_bytes(proof)
    program = r'''
#include "zkt_journal_compat.h"
#include <assert.h>
#include <stdio.h>
static uint8_t bytes[192];
static int read_proof(void *p,uint8_t out[192]){(void)p;memcpy(out,bytes,192);return 1;}
int main(void){
    zj_reader_environment_t writer={.application="zone_lite",.version="2.7.0",.secure_boot=true,
        .encrypted_nvs=true,.ota_slot=true,.reader_ready=true,.delivery_ready=true,.persistence_verified=true};
    zj_reader_environment_t reader=writer;reader.image_validated=true;
    zj_reader_proof_port_t port={.read=read_proof};
    for(unsigned i=0;i<2;++i){char path[32];snprintf(path,sizeof(path),"proof-%u",i);
        FILE *f=fopen(path,"rb");assert(f);assert(fread(bytes,1,192,f)==192);fclose(f);
        zj_reader_identity_t prior;uint64_t generation;
        assert(zj_reader_proof_decode(bytes,&prior,&generation)&&generation==1);
        zj_reader_identity_t current=prior;current.slot_address=0x2a0000;current.image_digest[0]=99;
        reader.version=i?"2.6.22":"2.6.23";
        assert((zj_reader_check_writer(port,&writer,&current,&reader,&prior)==ZJ_COMPAT_OK)==EXPECTED);
        prior.image_digest[0]^=1;
        assert(zj_reader_check_writer(port,&writer,&current,&reader,&prior)!=ZJ_COMPAT_OK);
        prior.image_digest[0]^=1;reader.version=i?"2.6.23":"2.6.22";
        assert(zj_reader_check_writer(port,&writer,&current,&reader,&prior)!=ZJ_COMPAT_OK);
        reader.version=i?"2.6.22":"2.6.23";current.capture_epoch[0]^=1;
        assert(zj_reader_check_writer(port,&writer,&current,&reader,&prior)!=ZJ_COMPAT_OK);
        current.capture_epoch[0]^=1;bytes[188]^=1;
        assert(zj_reader_check_writer(port,&writer,&current,&reader,&prior)!=ZJ_COMPAT_OK);
    }
}
'''
    unit = tmp_path / "matrix.c"
    unit.write_text(program)
    main = ROOT / "firmware/zone_lite/main"
    executable = tmp_path / "matrix-test"
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-DZONE_LITE_QUALIFIED_READER_MATRIX=1", f"-DEXPECTED={int(populated)}",
        "-I", str(tmp_path), "-I", str(main), str(unit), str(main / "zkt_journal_compat.c"),
        str(main / "durable_queue.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], cwd=tmp_path, check=True, timeout=30)
