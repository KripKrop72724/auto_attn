"""Synthetic-only factory audit regression; never reads the real Windows bundle."""

from contextlib import redirect_stdout, redirect_stderr
import base64
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils

from scripts import audit_factory_bundle as audit
from scripts import invoke_factory_audit as launch
import sys

SECRET = b"SYNTHETIC_ONLY_WIFI_SECRET_123_AND_CNIC_99999_1111111_9"


def sign_image(image, private):
    prefix = image.ljust((len(image) + 4095) // 4096 * 4096, b"\xff")
    digest = hashlib.sha256(prefix).digest()
    public = private.public_key().public_numbers()
    signature = private.sign(
        digest,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        utils.Prehashed(hashes.SHA256()),
    )
    block = struct.pack(
        "<BBxx32s384sI384sI384s",
        0xE7,
        2,
        digest,
        public.n.to_bytes(384, "little"),
        public.e,
        pow(2, 6144, public.n).to_bytes(384, "little"),
        (-pow(public.n, -1, 1 << 32)) & 0xFFFFFFFF,
        signature[::-1],
    )
    block += struct.pack("<I", zlib.crc32(block) & 0xFFFFFFFF) + b"\0" * 16
    assert len(block) == 1216
    return prefix + block.ljust(4096, b"\xff")


def application():
    head = bytearray(24)
    head[0], head[1], head[23] = 0xE9, 1, 1
    struct.pack_into("<H", head, 12, 9)
    payload = bytearray(384)
    struct.pack_into("<I", payload, 0, 0xABCD5432)
    payload[16:21] = b"2.5.2"
    payload[48:57] = b"zone_lite"
    payload[144:176] = b"\xab" * 32
    payload[256 : 256 + len(SECRET)] = SECRET
    raw = bytes(head) + struct.pack("<II", 0x3C000020, len(payload)) + bytes(payload)
    checksum = 0xEF
    for byte in payload:
        checksum ^= byte
    end = (len(raw) + 16) // 16 * 16
    raw += b"\0" * (end - len(raw) - 1) + bytes([checksum])
    return raw + hashlib.sha256(raw).digest()


def partitions():
    raw = b"".join(
        struct.pack(
            "<HBBII16sI",
            0x50AA,
            row[0],
            row[1],
            row[2],
            row[3],
            row[4].encode().ljust(16, b"\0"),
            row[5],
        )
        for row in audit.PARTITIONS
    )
    raw += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(raw, usedforsecurity=False).digest()
    return raw.ljust(0xC00, b"\xff")


class FactoryAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cls.pem = cls.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        der = cls.private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        cls.anchor = audit.TrustAnchor(audit.sha(cls.pem), audit.sha(der))
        cls.signed_app = sign_image(application(), cls.private)
        cls.signed_boot = sign_image(b"SYNTHETIC_BOOTLOADER" + SECRET, cls.private)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {
            "schema_version": 1,
            "bundle_id": audit.BUNDLE_ID,
            "version": "2.5.2",
            "git_sha": audit.SOURCE_SHA,
            "hardware_profile": "esp32s3-16mb-zone-lite-v1",
            "partition_layout": "zone-lite-factory-v1",
            "setup_password_supplied": True,
            "signing_key_ids": [self.anchor.pem_sha256, "2" * 64, "3" * 64],
            "published_at": "2026-08-27T12:00:00Z",
            "images": [],
        }
        files = {
            "bootloader-signed.bin": self.signed_boot,
            "zone-lite-signed.bin": self.signed_app,
            "partition-table.bin": partitions(),
            "ota_data_initial.bin": b"\xff" * 0x2000,
        }
        (self.root / "key-1-public.pem").write_bytes(self.pem)
        for name, raw in files.items():
            (self.root / name).write_bytes(raw)
            self.manifest["images"].append(
                {
                    "name": name,
                    "offset": audit.FILES[name][0],
                    "size": len(raw),
                    "sha256": audit.sha(raw),
                }
            )
        self.resign_manifest()

    def resign_manifest(self):
        encoded = audit.canonical(self.manifest)
        signature = self.private.sign(
            encoded, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256()
        )
        (self.root / "manifest.json").write_bytes(encoded)
        (self.root / "manifest.sig").write_bytes(base64.b64encode(signature))

    def replace_image(self, name, raw):
        (self.root / name).write_bytes(raw)
        entry = next(i for i in self.manifest["images"] if i["name"] == name)
        entry["size"], entry["sha256"] = len(raw), audit.sha(raw)
        self.resign_manifest()

    def run_audit(self):
        return audit.audit_bundle(self.root, self.anchor)

    def test_complete_legacy_bundle_metadata_only_no_write(self):
        before = {
            p.name: (p.stat().st_mtime_ns, audit.sha(p.read_bytes())) for p in self.root.iterdir()
        }
        result = self.run_audit()
        after = {
            p.name: (p.stat().st_mtime_ns, audit.sha(p.read_bytes())) for p in self.root.iterdir()
        }
        self.assertEqual(before, after)
        self.assertEqual(result["status"], "ARCHIVE_VERIFIED")
        self.assertEqual(len(result["device_hash_comparisons"]), 3)
        self.assertEqual(
            {x["connector_id"] for x in result["device_hash_comparisons"]}, set(audit.TARGETS)
        )
        self.assertFalse(
            any(x["application_digest_matches"] for x in result["device_hash_comparisons"])
        )
        self.assertNotIn(SECRET.decode(), json.dumps(result))
        self.assertNotIn("BEGIN PUBLIC KEY", json.dumps(result))
        self.assertFalse(result["installed_security_or_rollback_verified"])
        self.assertEqual(result["hil_qualification"], "NOT_ASSERTED")

    def test_pinned_idf_espsecure_accepts_same_signatures(self):
        try:
            import espsecure
        except ImportError:
            self.skipTest("Cross-check runs in pinned IDF container")
        for raw in (self.signed_app, self.signed_boot):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                if "args" in inspect.signature(espsecure.verify_signature_v2).parameters:
                    espsecure.verify_signature_v2(
                        SimpleNamespace(
                            keyfile=io.BytesIO(self.pem), datafile=io.BytesIO(raw), hsm=False
                        )
                    )
                else:
                    espsecure.verify_signature_v2(
                        hsm=False,
                        hsm_config=None,
                        keyfile=io.BytesIO(self.pem),
                        datafile=io.BytesIO(raw),
                    )

    def test_manifest_and_key_identity_refusals(self):
        for field, value in [
            ("git_sha", "f" * 40),
            ("version", "2.5.3"),
            ("bundle_id", "wrong"),
            ("firmware_family", "hikvision"),
            ("project_name", "zone_lite_hikvision"),
            ("schema_version", True),
            ("setup_password_supplied", 1),
        ]:
            with self.subTest(field=field):
                old = self.manifest.copy()
                self.manifest[field] = value
                self.resign_manifest()
                with self.assertRaises(audit.Refused):
                    self.run_audit()
                self.manifest = old
        self.resign_manifest()
        with self.assertRaises(audit.Refused):
            audit.audit_bundle(self.root)
        with self.assertRaises(audit.Refused):
            audit.audit_bundle(self.root, audit.TrustAnchor(self.anchor.pem_sha256, "0" * 64))

    def test_manifest_signature_and_duplicate_keys_rejected(self):
        (self.root / "manifest.sig").write_bytes(base64.b64encode(b"\0" * 384))
        with self.assertRaises(Exception):
            self.run_audit()
        self.resign_manifest()
        raw = (self.root / "manifest.json").read_bytes()
        (self.root / "manifest.json").write_bytes(b'{"version":"2.5.2",' + raw[1:])
        with self.assertRaises(audit.Refused):
            self.run_audit()

    def test_file_inventory_bounds_and_traversal(self):
        for field, value in [
            ("name", "../private-key.pem"),
            ("offset", 1),
            ("size", 0x400000),
            ("offset", False),
            ("sha256", "0" * 64),
        ]:
            with self.subTest(field=field):
                old = self.manifest["images"][0].copy()
                self.manifest["images"][0][field] = value
                self.resign_manifest()
                with self.assertRaises(audit.Refused):
                    self.run_audit()
                self.manifest["images"][0] = old
        self.manifest["images"][1] = self.manifest["images"][0].copy()
        self.resign_manifest()
        with self.assertRaises(audit.Refused):
            self.run_audit()

    def test_symlink_fifo_and_file_size_refused_without_opening_secret(self):
        target = self.root / "private.pem"
        target.write_bytes(SECRET)
        filename = self.root / "key-1-public.pem"
        filename.unlink()
        filename.symlink_to(target)
        with self.assertRaises(OSError):
            self.run_audit()
        filename.unlink()
        os.mkfifo(filename)
        with self.assertRaises(audit.Refused):
            self.run_audit()
        filename.unlink()
        filename.write_bytes(b"x" * 4097)
        with self.assertRaises(audit.Refused):
            self.run_audit()
        with self.assertRaises(audit.Refused):
            audit.read_regular(self.root, "private.pem", 100)

    def test_invalid_signature_even_when_manifest_rehashed(self):
        for offset in (100, len(self.signed_app) - 4096 + 812, len(self.signed_app) - 4096 + 1196):
            with self.subTest(offset=offset):
                raw = bytearray(self.signed_app)
                raw[offset] ^= 1
                self.replace_image("zone-lite-signed.bin", bytes(raw))
                with self.assertRaises(audit.Refused):
                    self.run_audit()

    def test_signature_covers_padding_after_application_validation_digest(self):
        raw = bytearray(self.signed_app)
        raw[2048] ^= 1
        # The ESP application-validation hash excludes signed-sector padding.
        # Rehashing only the manifest must not turn that tamper into a pass.
        self.assertEqual(
            audit.application_identity(bytes(raw)), audit.application_identity(self.signed_app)
        )
        self.replace_image("zone-lite-signed.bin", bytes(raw))
        with self.assertRaises(audit.Refused):
            self.run_audit()

    def test_embedded_public_key_and_rsa_primitives_bound(self):
        for offset in (36, 420, 424, 808):
            raw = bytearray(self.signed_app)
            start = len(raw) - 4096
            raw[start + offset] ^= 1
            struct.pack_into(
                "<I", raw, start + 1196, zlib.crc32(raw[start : start + 1196]) & 0xFFFFFFFF
            )
            self.replace_image("zone-lite-signed.bin", bytes(raw))
            with self.subTest(offset=offset), self.assertRaises(audit.Refused):
                self.run_audit()

    def test_altered_partition_layout_or_initial_ota_refused(self):
        raw = bytearray(partitions())
        struct.pack_into("<I", raw, 3 * 32 + 4, 0x30000)
        end = len(audit.PARTITIONS) * 32
        raw[end + 16 : end + 32] = hashlib.md5(raw[:end], usedforsecurity=False).digest()
        self.replace_image("partition-table.bin", bytes(raw))
        with self.assertRaises(audit.Refused):
            self.run_audit()
        self.replace_image("partition-table.bin", partitions())
        self.replace_image("ota_data_initial.bin", b"\0" * 0x2000)
        with self.assertRaises(audit.Refused):
            self.run_audit()

    def test_freshly_signed_wrong_application_descriptor_or_corrupt_checksum(self):
        for offset in (12, 48, 80, 423):
            raw = bytearray(application())
            raw[offset] ^= 1
            self.replace_image("zone-lite-signed.bin", sign_image(bytes(raw), self.private))
            with self.subTest(offset=offset), self.assertRaises(audit.Refused):
                self.run_audit()

    def test_cli_never_emits_exception_or_input_data(self):
        for exception in [OSError(SECRET.decode()), ValueError(SECRET.decode())]:
            output = io.StringIO()
            with (
                patch.object(audit, "audit_bundle", side_effect=exception),
                patch.object(audit.sys, "argv", ["audit"]),
                redirect_stdout(output),
            ):
                self.assertEqual(audit.main(), 1)
            self.assertNotIn(SECRET.decode(), output.getvalue())
            self.assertEqual(
                json.loads(output.getvalue())["error_code"], "INVALID_OR_UNAVAILABLE_INPUT"
            )
        with (
            patch.object(audit.sys, "argv", ["audit", "--bundle", SECRET.decode()]),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(audit.main(), 1)
        self.assertNotIn(SECRET.decode(), output.getvalue())


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FactoryAuditTests.setUpClass()

    def setUp(self):
        self.fixture = FactoryAuditTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bundle = self.fixture.root.resolve()
        self.script = Path(audit.__file__).absolute()
        self.report = self.fixture.run_audit()
        self.calls = []

    def fake(self, args, timeout, **kwargs):
        self.calls.append((args, timeout))
        if args[1:3] == ["image", "inspect"]:
            return 0, b"sha256:test", b""
        if args[1] == "run":
            return 0, json.dumps(self.report).encode(), b"PRIVATE_STDERR_NOT_EXPORTED"
        if args[1:3] == ["container", "inspect"]:
            return 0, b"container-id", b""
        if args[1:3] == ["rm", "-f"]:
            return 0, b"container-id", b""
        raise AssertionError("unexpected native call")

    def test_fixed_process_arguments_and_cleanup_only_own_container(self):
        result = launch.perform_audit(self.bundle, self.script, native_call=self.fake)
        self.assertEqual(result, self.report)
        command = self.calls[1][0]
        self.assertIn(launch.RUNTIME, command)
        self.assertEqual(command[-3:], ["-I", "-B", "/audit.py"])
        for key, value in [
            ("--pull", "never"),
            ("--network", "none"),
            ("--user", "65534:65534"),
            ("--memory", "128m"),
            ("--cpus", "1"),
            ("--pids-limit", "64"),
        ]:
            self.assertEqual(command[command.index(key) + 1], value)
        self.assertIn("--read-only", command)
        mounts = [command[i + 1] for i, v in enumerate(command) if v == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all(x.endswith(",readonly") for x in mounts))
        self.assertFalse(any(v in command for v in ("--env", "--privileged", "--rm")))
        own = command[command.index("--name") + 1]
        self.assertTrue(own.startswith("factory-readonly-audit-"))
        self.assertEqual(self.calls[-1][0], ["docker", "rm", "-f", own])
        self.assertEqual(self.calls[1][1], 80)
        self.assertNotIn("PRIVATE_STDERR", json.dumps(result))

    def test_uncached_runtime_no_run(self):
        def missing(args, timeout):
            self.calls.append(args)
            return 1, b"", b"PRIVATE_NATIVE_ERROR"

        with self.assertRaises(launch.Refused) as refused:
            launch.perform_audit(self.bundle, self.script, native_call=missing)
        self.assertEqual(refused.exception.code, "PINNED_RUNTIME_NOT_CACHED")
        self.assertEqual(refused.exception.stage, "RUNTIME_INSPECT")
        self.assertEqual(len(self.calls), 1)

    def test_missing_fixed_file_is_named_by_safe_stage_without_path_or_native_call(self):
        (self.bundle / "manifest.sig").unlink()
        with self.assertRaises(launch.Refused) as refused:
            launch.perform_audit(self.bundle, self.script, native_call=self.fake)
        result = launch.refusal_report(refused.exception, "UNCLASSIFIED")
        self.assertEqual(result["error_code"], "INPUT_MISSING")
        self.assertEqual(result["failure_stage"], "BUNDLE_MANIFEST_SIG")
        self.assertNotIn(str(self.bundle), json.dumps(result))
        self.assertEqual(self.calls, [])

    def test_run_failure_cleanup_absent_does_not_mask_primary_error(self):
        def fail(args, timeout):
            if args[1:3] == ["image", "inspect"]:
                return 0, b"", b""
            if args[1] == "run":
                raise launch.Refused("NATIVE_TIME_LIMIT")
            return 1, b"", b"container absent"

        with self.assertRaisesRegex(launch.Refused, "NATIVE_TIME_LIMIT"):
            launch.perform_audit(self.bundle, self.script, native_call=fail)

    def test_windows_reparse_leaf_and_ancestor_and_nonregular(self):
        for mode in (stat.S_IFDIR | 0o755, stat.S_IFREG | 0o644):
            info = SimpleNamespace(st_mode=mode, st_size=1, st_file_attributes=0x400)
            with self.assertRaises(launch.Refused):
                launch.validate_node(info, regular=stat.S_ISREG(mode), limit=4)
        for mode in (stat.S_IFIFO, stat.S_IFSOCK):
            with self.assertRaises(launch.Refused):
                launch.validate_node(
                    SimpleNamespace(st_mode=mode, st_size=1), regular=True, limit=4
                )
        name = self.bundle / "manifest.sig"
        name.unlink()
        name.symlink_to(self.bundle / "manifest.json")
        with self.assertRaises(launch.Refused):
            launch.perform_audit(self.bundle, self.script, native_call=self.fake)
        self.assertEqual(self.calls, [])

    def test_parent_reparse_is_rejected_before_any_native_call(self):
        original = Path.lstat

        def info(path):
            if path == self.bundle.parent:
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400, st_size=0
                )
            return original(path)

        with patch.object(Path, "lstat", info), self.assertRaises(launch.Refused):
            launch.perform_audit(self.bundle, self.script, native_call=self.fake)
        self.assertEqual(self.calls, [])

    def test_script_pin_and_oversize_refuse_before_native(self):
        script = self.bundle / "auditor.py"
        script.write_text("print('unreviewed')")
        with self.assertRaises(launch.Refused):
            launch.perform_audit(self.bundle, script, native_call=self.fake)
        (self.bundle / "manifest.sig").write_bytes(b"x" * 8193)
        with self.assertRaises(launch.Refused):
            launch.perform_audit(self.bundle, self.script, native_call=self.fake)
        self.assertEqual(self.calls, [])

    def test_device_matches_are_recomputed_and_exact_scope_required(self):
        result = launch.validate_report(0, json.dumps(self.report).encode())
        self.assertFalse(
            any(r["application_digest_matches"] for r in result["device_hash_comparisons"])
        )
        for change in ("match", "extra", "wrong", "hash"):
            bad = json.loads(json.dumps(self.report))
            rows = bad["device_hash_comparisons"]
            if change == "match":
                rows[0]["application_digest_matches"] = True
            if change == "extra":
                rows.append(rows[0])
            if change == "wrong":
                rows[0]["connector_id"] = "unbound-extra-device"
            if change == "hash":
                rows[0]["expected_application_sha256"] = "0" * 64
            with self.subTest(change=change), self.assertRaises(launch.Refused):
                launch.validate_report(0, json.dumps(bad).encode())

    def test_refusal_replaces_error_text_and_extra_output_rejected(self):
        bad = {
            "schema_version": 1,
            "status": "REFUSED",
            "error_code": "SECRET_DATA",
            "raw_firmware_exported": False,
            "hil_qualification": "NOT_ASSERTED",
        }
        result = launch.validate_report(1, json.dumps(bad).encode())
        self.assertNotIn("SECRET_DATA", json.dumps(result))
        self.assertEqual(result["error_code"], "ARCHIVE_VERIFIER_REFUSED")
        self.assertEqual(result["failure_stage"], "ARCHIVE_VERIFICATION")
        bad["error_code"] = "IMAGE_FILE_HASH"
        self.assertEqual(launch.validate_report(1, json.dumps(bad).encode())["error_code"], "IMAGE_FILE_HASH")
        self.report["private_bytes"] = "SECRET_DATA"
        with self.assertRaises(launch.Refused):
            launch.validate_report(0, json.dumps(self.report).encode())

    def test_exception_and_stage_enums_redact_arbitrary_text(self):
        private = SECRET.decode() + r" C:\private\firmware.bin https://private.invalid/token"
        cases = [
            (launch.Refused(private, private), "HOST_FAILURE_UNCLASSIFIED"),
            (RuntimeError(private), "HOST_FAILURE_UNCLASSIFIED"),
            (FileNotFoundError(private), "INPUT_MISSING"),
            (PermissionError(private), "INPUT_ACCESS_DENIED"),
            (OSError(private), "INPUT_IO_FAILURE"),
            (json.JSONDecodeError(private, private, 0), "AUDIT_OUTPUT_INVALID"),
        ]
        for error, code in cases:
            with self.subTest(code=code):
                result = launch.refusal_report(error, "OUTPUT_VALIDATION")
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["failure_stage"], "OUTPUT_VALIDATION")
                self.assertFalse(result["raw_firmware_exported"])
                self.assertEqual(result["hil_qualification"], "NOT_ASSERTED")
                self.assertNotIn(private, json.dumps(result))
        self.assertEqual(launch.refusal_report(RuntimeError(private), private)["failure_stage"], "UNCLASSIFIED")
        self.assertEqual(launch.refusal_report(FileNotFoundError(private), "RUNTIME_INSPECT")["error_code"], "NATIVE_EXECUTABLE_MISSING")

    def test_malformed_verifier_stdout_is_redacted_and_cleanup_still_runs(self):
        def malformed(args, timeout):
            if args[1] == "run":
                self.calls.append((args, timeout))
                return 1, SECRET, SECRET
            return self.fake(args, timeout)

        with self.assertRaises(launch.Refused) as refused:
            launch.perform_audit(self.bundle, self.script, native_call=malformed)
        self.assertEqual(refused.exception.code, "AUDIT_OUTPUT_INVALID")
        self.assertEqual(refused.exception.stage, "OUTPUT_VALIDATION")
        self.assertNotIn(SECRET.decode(), str(refused.exception))
        self.assertEqual(self.calls[-1][0][1:3], ["rm", "-f"])

    def test_valid_github_identity_persists_metadata_only_failure(self):
        identity = {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "1"}
        output = io.StringIO()
        failure = launch.Refused("PINNED_RUNTIME_NOT_CACHED", "RUNTIME_INSPECT")
        with (
            patch.dict(os.environ, identity, clear=True),
            patch.object(launch, "__file__", str(self.bundle / "scripts/invoke_factory_audit.py")),
            patch.object(launch, "validate_host"),
            patch.object(launch, "perform_audit", side_effect=failure),
            redirect_stdout(output),
        ):
            self.assertEqual(launch.main(), 1)
        saved = self.bundle / "factory-provenance-report-12345-1.json"
        result = json.loads(saved.read_text())
        self.assertEqual(result["github_source_sha"], identity["GITHUB_SHA"])
        self.assertEqual(result["github_run_id"], "12345")
        self.assertEqual(result["qualification"], "NOT_ASSERTED")
        self.assertEqual(result["archive_report"]["error_code"], "PINNED_RUNTIME_NOT_CACHED")
        self.assertEqual(result["archive_report"]["failure_stage"], "RUNTIME_INSPECT")
        self.assertEqual(json.loads(output.getvalue())["failure_stage"], "RUNTIME_INSPECT")
        self.assertNotIn(str(self.bundle), saved.read_text())
        self.assertNotIn(SECRET.decode(), saved.read_text())
        self.assertEqual(result["auditor_sha256"], launch.AUDITOR_SHA256)
        self.assertEqual(result["runtime"], launch.RUNTIME)

    def test_unknown_host_failure_persists_redacted_envelope_and_never_overwrites(self):
        identity = {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "12346", "GITHUB_RUN_ATTEMPT": "1"}
        with (
            patch.dict(os.environ, identity, clear=True),
            patch.object(launch, "__file__", str(self.bundle / "scripts/invoke_factory_audit.py")),
            patch.object(launch, "validate_host", side_effect=RuntimeError(SECRET.decode())),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(launch.main(), 1)
            saved = self.bundle / "factory-provenance-report-12346-1.json"
            before = saved.read_bytes()
            self.assertNotIn(SECRET, before)
            self.assertEqual(json.loads(before)["archive_report"]["error_code"], "HOST_FAILURE_UNCLASSIFIED")
            self.assertEqual(launch.main(), 1)
            self.assertEqual(saved.read_bytes(), before)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["failure_stage"], "REPORT_PERSIST")
        self.assertNotIn(SECRET.decode(), output.getvalue())

    def test_invalid_identity_cannot_choose_artifact_path_or_start_audit(self):
        identity = {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "../" + SECRET.decode(), "GITHUB_RUN_ATTEMPT": "1"}
        with (
            patch.dict(os.environ, identity, clear=True),
            patch.object(launch, "__file__", str(self.bundle / "scripts/invoke_factory_audit.py")),
            patch.object(launch, "perform_audit") as perform,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(launch.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "GITHUB_IDENTITY_REQUIRED")
        self.assertEqual(list(self.bundle.glob("factory-provenance-report-*.json")), [])
        self.assertNotIn(SECRET.decode(), output.getvalue())
        perform.assert_not_called()

    def test_native_stderr_never_emitted_and_exit_code_preserved(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code, out, err = launch.native(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('ok'); sys.stderr.write('SYNTHETIC_SECRET'); sys.exit(7)",
                ]
            )
        self.assertEqual(code, 7)
        self.assertEqual(out.strip(), b"ok")
        self.assertIn(b"SYNTHETIC_SECRET", err)
        self.assertEqual(stdout.getvalue(), "")

    def test_native_stdout_stderr_bounds_and_deadline(self):
        for channel in ("stdout", "stderr"):
            with (
                self.subTest(channel=channel),
                self.assertRaisesRegex(launch.Refused, "NATIVE_OUTPUT_BOUND"),
            ):
                launch.native(
                    [sys.executable, "-c", f"import sys; sys.{channel}.write('x'*65536)"],
                    stdout_limit=32,
                    stderr_limit=32,
                )
        with self.assertRaisesRegex(launch.Refused, "NATIVE_TIME_LIMIT"):
            launch.native([sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.05)

    def test_reviewed_verifier_hash_and_explicit_workflow_scope(self):
        self.assertEqual(
            hashlib.sha256(self.script.read_bytes()).hexdigest(), launch.AUDITOR_SHA256
        )
        workflow = (
            self.script.parent.parent / ".github/workflows/factory-provenance-audit.yml"
        ).read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("push:", workflow)
        self.assertIn("if: github.ref == 'refs/heads/main' && inputs.confirmation ==", workflow)
        self.assertIn("environment: firmware-production", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("timeout-minutes: 3", workflow)
        self.assertIn("python scripts/invoke_factory_audit.py", workflow)
        self.assertIn("if ($LASTEXITCODE -ne 0)", workflow)
        self.assertIn(
            "factory-provenance-report-${{ github.run_id }}-${{ github.run_attempt }}.json",
            workflow,
        )
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("deploy.ps1", workflow)
        self.assertNotIn("publish-factory-firmware", workflow)
        self.assertIn("if-no-files-found: error", workflow)
