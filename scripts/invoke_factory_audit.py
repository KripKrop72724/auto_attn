"""Bounded Windows launcher for one metadata-only retained factory audit."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import uuid

BUNDLE = r"C:\Users\Public\Documents\StateLife\AttendanceDeviceDashboard\factory-firmware\zone-lite-2.5.2-27c3bb80eb20"
BUNDLE_ID = "zone-lite-2.5.2-27c3bb80eb20"
BUNDLE_SOURCE = "27c3bb80eb203ea7ee19a42cdc3f871664a97d1b"
AUDITOR_SHA256 = "59dbbbf0c4624926b87c30b1729b2af5a177a2c5bfb701602b21ae67c4a98208"
RUNTIME = "espressif/idf@sha256:8ccd4d2ce413889c6c2bba57e986c670302094efb91c913c6091152e317a7805"
LIMITS = {"manifest.json": 65536, "manifest.sig": 8192, "key-1-public.pem": 4096,
          "bootloader-signed.bin": 0x10000, "partition-table.bin": 0x1000,
          "ota_data_initial.bin": 0x2000, "zone-lite-signed.bin": 0x280000}
TARGETS = {
    "a1ff7b24-4dcb-4dde-ad41-1a8401c7b006": "e068ee75073e3198f7894f04a249169eef96feb996d9b7db7e5a8c7a04829e91",
    "2f5cedd8-e314-47b0-8074-bc4bb8a603cc": "191b63c5f18485a9aa7f705f77679f932e79f326e1bf87e3c4bb237d249fb786",
    "a886e2d9-204d-425c-bc8f-ded85fc89874": "00dcc3514b997570fcdf7495f7b8a85302bcff6c2670120d13245f93c0424e8b",
}
SUCCESS_KEYS = set("schema_version status bundle_id source_sha manifest_sha256 public_key_pem_sha256 public_key_spki_sha256 manifest_signature application_signature_block bootloader_signature_block application images partitions initial_otadata_empty device_hash_comparisons installed_security_or_rollback_verified hil_qualification raw_firmware_exported".split())
HOST_CODES = frozenset("""REPARSE_POINT_NOT_ALLOWED FILE_SIZE_OR_TYPE NATIVE_TIME_LIMIT
NATIVE_OUTPUT_BOUND AUDIT_OUTPUT_BOUND AUDIT_OUTPUT_CONTRACT AUDIT_APPLICATION_DIGEST
AUDIT_TARGET_SET AUDIT_TARGET_COMPARISON AUDITOR_SCRIPT_CHANGED PINNED_RUNTIME_NOT_CACHED
WINDOWS_FIXED_CLI_REQUIRED REVIEWED_MAIN_REQUIRED GITHUB_IDENTITY_REQUIRED INPUT_MISSING
INPUT_ACCESS_DENIED INPUT_IO_FAILURE NATIVE_EXECUTABLE_MISSING NATIVE_EXECUTION_FAILED
AUDIT_OUTPUT_INVALID HOST_FAILURE_UNCLASSIFIED""".split())
ARCHIVE_CODES = frozenset("""APPLICATION_CHECKSUM APPLICATION_CHIP APPLICATION_DESCRIPTOR
APPLICATION_HEADER APPLICATION_IDENTITY APPLICATION_SEGMENT_BOUND APPLICATION_VALIDATION_DIGEST
AUDIT_TIME_LIMIT BUNDLE_DIRECTORY_INVALID CLI_ARGUMENTS_NOT_ALLOWED DUPLICATE_JSON_KEY
ESP_SIGNATURE_SIZE ESP_TRUST_ANCHOR_SIGNATURE_INVALID FILE_CHANGED_DURING_READ FILE_NOT_ALLOWED
FILE_SIZE_OR_TYPE IMAGE_FILE_HASH INITIAL_OTADATA_NOT_EMPTY INVALID_OR_UNAVAILABLE_INPUT
MANIFEST_CHANGED MANIFEST_IDENTITY MANIFEST_IMAGE_BOUNDS MANIFEST_IMAGE_INVENTORY MANIFEST_KEY_IDS
PARTITION_LAYOUT_MISMATCH PARTITION_TABLE_MD5_OR_TAIL PARTITION_TABLE_SIZE PUBLIC_KEY_PEM_PIN
PUBLIC_KEY_SPKI_PIN PUBLIC_KEY_TYPE""".split())
STAGES = frozenset({"GITHUB_IDENTITY", "HOST_PRECONDITIONS", "BUNDLE_DIRECTORY",
                    "AUDITOR_SCRIPT", "RUNTIME_INSPECT", "ARCHIVE_EXECUTION",
                    "ARCHIVE_VERIFICATION", "OUTPUT_VALIDATION", "REPORT_PERSIST", "UNCLASSIFIED",
                    *("BUNDLE_" + name.upper().replace("-", "_").replace(".", "_") for name in LIMITS)})


class Refused(Exception):
    def __init__(self, code, stage="UNCLASSIFIED"):
        self.code = code if type(code) is str and code in HOST_CODES else "HOST_FAILURE_UNCLASSIFIED"
        self.stage = stage if type(stage) is str and stage in STAGES else "UNCLASSIFIED"
        super().__init__(self.code)


def require(value, code):
    if not value:
        raise Refused(code)


def safe_refusal(error, stage):
    """Export fixed classifications only, never exception text, paths or stderr."""
    stage = stage if stage in STAGES else "UNCLASSIFIED"
    if isinstance(error, Refused):
        return Refused(error.code, error.stage if error.stage != "UNCLASSIFIED" else stage)
    if isinstance(error, FileNotFoundError):
        code = "NATIVE_EXECUTABLE_MISSING" if stage in {"RUNTIME_INSPECT", "ARCHIVE_EXECUTION"} else "INPUT_MISSING"
    elif isinstance(error, PermissionError):
        code = "INPUT_ACCESS_DENIED"
    elif isinstance(error, OSError):
        code = "INPUT_IO_FAILURE"
    elif isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)) and stage == "OUTPUT_VALIDATION":
        code = "AUDIT_OUTPUT_INVALID"
    else:
        code = "HOST_FAILURE_UNCLASSIFIED"
    return Refused(code, stage)


def at_stage(stage, action):
    try:
        return action()
    except Exception as error:
        raise safe_refusal(error, stage) from None


def refusal_report(error, stage):
    refusal = safe_refusal(error, stage)
    return {"schema_version": 1, "status": "HOST_AUDIT_REFUSED",
            "error_code": refusal.code, "failure_stage": refusal.stage,
            "raw_firmware_exported": False, "hil_qualification": "NOT_ASSERTED"}


def validate_node(info, *, regular=False, limit=None):
    require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "REPARSE_POINT_NOT_ALLOWED")
    require((stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit) if regular
            else stat.S_ISDIR(info.st_mode), "FILE_SIZE_OR_TYPE")


def preflight_path(path, *, regular=False, limit=None):
    validate_node(path.lstat(), regular=regular, limit=limit)
    for ancestor in path.parents:
        validate_node(ancestor.lstat())


def native(arguments, timeout=15, *, stdout_limit=16384, stderr_limit=4096):
    """Cap both pipes. Never use a shell or native PowerShell redirection."""
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output, error, overflow = bytearray(), bytearray(), threading.Event()

    def drain(stream, target, limit):
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                remaining = max(0, limit - len(target))
                target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    process.kill()
        except (OSError, ValueError):
            overflow.set()

    readers = [threading.Thread(target=drain, args=(process.stdout, output, stdout_limit), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, error, stderr_limit), daemon=True)]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait(timeout=5)
    for reader in readers:
        reader.join(timeout=2)
    for reader, stream in zip(readers, (process.stdout, process.stderr)):
        if not reader.is_alive():
            stream.close()
    require(not timed_out, "NATIVE_TIME_LIMIT")
    require(not overflow.is_set() and not any(t.is_alive() for t in readers), "NATIVE_OUTPUT_BOUND")
    return process.returncode, bytes(output), bytes(error)


def validate_report(code, raw):
    require(len(raw) <= 16384, "AUDIT_OUTPUT_BOUND")
    report = json.loads(raw)
    require(type(report) is dict and type(report.get("schema_version")) is int
            and report["schema_version"] == 1 and report.get("raw_firmware_exported") is False
            and report.get("hil_qualification") == "NOT_ASSERTED", "AUDIT_OUTPUT_CONTRACT")
    if report.get("status") == "REFUSED":
        require(code != 0 and set(report) == {"schema_version", "status", "error_code", "raw_firmware_exported", "hil_qualification"}, "AUDIT_OUTPUT_CONTRACT")
        # Only these fixed codes from the hash-pinned verifier may cross the
        # host boundary. Unknown error text is never retained or printed.
        code = report.get("error_code")
        report["error_code"] = code if type(code) is str and code in ARCHIVE_CODES else "ARCHIVE_VERIFIER_REFUSED"
        report["failure_stage"] = "ARCHIVE_VERIFICATION"
        return report
    require(code == 0 and set(report) == SUCCESS_KEYS and report.get("status") == "ARCHIVE_VERIFIED"
            and report.get("bundle_id") == BUNDLE_ID and report.get("source_sha") == BUNDLE_SOURCE
            and report.get("installed_security_or_rollback_verified") is False, "AUDIT_OUTPUT_CONTRACT")
    digest = (report.get("application") or {}).get("application_sha256")
    require(type(digest) is str and re.fullmatch("[0-9a-f]{64}", digest), "AUDIT_APPLICATION_DIGEST")
    rows = report.get("device_hash_comparisons")
    require(type(rows) is list and len(rows) == 3 and all(type(row) is dict for row in rows)
            and {row.get("connector_id") for row in rows} == set(TARGETS), "AUDIT_TARGET_SET")
    for row in rows:
        require(set(row) == {"connector_id", "expected_application_sha256", "application_digest_matches"}
                and row["expected_application_sha256"] == TARGETS[row["connector_id"]]
                and row["application_digest_matches"] is (digest == row["expected_application_sha256"]), "AUDIT_TARGET_COMPARISON")
    return report


def perform_audit(bundle, script, *, native_call=native):
    at_stage("BUNDLE_DIRECTORY", lambda: preflight_path(bundle))
    at_stage("AUDITOR_SCRIPT", lambda: preflight_path(script, regular=True, limit=65536))
    at_stage("AUDITOR_SCRIPT", lambda: require(hashlib.sha256(script.read_bytes()).hexdigest() == AUDITOR_SHA256, "AUDITOR_SCRIPT_CHANGED"))
    for name, limit in LIMITS.items():
        at_stage("BUNDLE_" + name.upper().replace("-", "_").replace(".", "_"),
                 lambda: preflight_path(bundle / name, regular=True, limit=limit))
    code, _, _ = at_stage("RUNTIME_INSPECT", lambda: native_call(["docker", "image", "inspect", RUNTIME, "--format", "{{.Id}}"], timeout=10))
    at_stage("RUNTIME_INSPECT", lambda: require(code == 0, "PINNED_RUNTIME_NOT_CACHED"))
    name = "factory-readonly-audit-" + uuid.uuid4().hex
    arguments = ["docker", "run", "--name", name, "--pull", "never", "--read-only", "--network", "none",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "65534:65534",
                 "--memory", "128m", "--cpus", "1", "--pids-limit", "64",
                 "--mount", f"type=bind,source={bundle},target=/factory-bundle,readonly",
                 "--mount", f"type=bind,source={script},target=/audit.py,readonly",
                 "--entrypoint", "/opt/esp/python_env/idf5.5_py3.12_env/bin/python", RUNTIME, "-I", "-B", "/audit.py"]
    try:
        code, output, _ = at_stage("ARCHIVE_EXECUTION", lambda: native_call(arguments, timeout=80))
        at_stage("ARCHIVE_EXECUTION", lambda: require(code not in {125, 126, 127}, "NATIVE_EXECUTION_FAILED"))
        return at_stage("OUTPUT_VALIDATION", lambda: validate_report(code, output))
    finally:
        # Only our unique audit container is touched. Missing cleanup must not
        # mask a valid result or the primary refusal after a failed create.
        try:
            code, _, _ = native_call(["docker", "container", "inspect", name, "--format", "{{.Id}}"], timeout=5)
            if code == 0:
                native_call(["docker", "rm", "-f", name], timeout=10)
        except Exception:
            pass


def validate_host():
    require(os.name == "nt" and len(sys.argv) == 1, "WINDOWS_FIXED_CLI_REQUIRED")
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "REVIEWED_MAIN_REQUIRED")


def main():
    identity = None
    repo = None
    stage = "GITHUB_IDENTITY"
    try:
        source, run, attempt = (os.environ.get(k, "") for k in ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))
        require(re.fullmatch("[0-9a-f]{40}", source) and re.fullmatch("[0-9]{1,20}", run)
                and re.fullmatch("[0-9]{1,6}", attempt), "GITHUB_IDENTITY_REQUIRED")
        identity = (source, run, attempt)
        repo = Path(__file__).absolute().parent.parent
        stage = "HOST_PRECONDITIONS"
        validate_host()
        report = perform_audit(Path(BUNDLE), repo / "scripts" / "audit_factory_bundle.py")
    except Exception as error:
        report = refusal_report(error, stage)
    if identity is not None and repo is not None:
        source, run, attempt = identity
        envelope = {"schema_version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
                    "github_source_sha": source, "github_run_id": run, "github_run_attempt": attempt,
                    "auditor_sha256": AUDITOR_SHA256, "runtime": RUNTIME, "archive_report": report,
                    "qualification": "NOT_ASSERTED"}
        output = repo / f"factory-provenance-report-{run}-{attempt}.json"
        try:
            fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(envelope, stream, sort_keys=True, separators=(",", ":"))
        except Exception as error:
            report = refusal_report(error, "REPORT_PERSIST")
    summary = {"status": report["status"], "qualification": "NOT_ASSERTED"}
    for key in ("error_code", "failure_stage"):
        if key in report:
            summary[key] = report[key]
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "ARCHIVE_VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
