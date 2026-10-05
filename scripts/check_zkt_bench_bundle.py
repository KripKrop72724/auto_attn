"""Compare protected, independently supplied facts with the real C/ADD decoders.

This command has no database/network access, signing or activation capability.
Its report is comparison evidence, never model or release authorization.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Literal

from pydantic import VERSION as PYDANTIC_VERSION
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/add_backend"))
from zk_add.zkt_decode import (  # noqa: E402
    DECODER_VERSION, MODEL_PROFILES, DecodeError, decode_live_packet, decode_source,
)

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"
SOURCE_FILES = (
    "firmware/zone_lite/main/zkt_record.c", "firmware/zone_lite/main/zkt_record.h",
    "firmware/zone_lite/main/zkt_clock.c", "firmware/zone_lite/main/zkt_clock.h",
    "firmware/zone_lite/main/reliability.c", "firmware/zone_lite/main/reliability.h",
    "apps/add_backend/zk_add/zkt_decode.py", "apps/add_backend/zk_add/zkt_packet.py",
    "scripts/zkt_bench_decoder_probe.c", "scripts/check_zkt_bench_bundle.py",
)


class BundleError(ValueError):
    """Only fixed, safe categories may be surfaced by the CLI."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FileRef(Strict):
    path: str = Field(min_length=1, max_length=240)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def relative_path(self):
        parts = PurePosixPath(self.path).parts
        if (not parts or self.path.startswith("/") or "\\" in self.path
                or any(part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]+", part)
                       for part in self.path.split("/"))):
            raise ValueError("BUNDLE_RELATIVE_PATH_REQUIRED")
        return self


class Case(Strict):
    case_id: str = Field(pattern=SAFE_ID)
    kind: Literal["SOURCE_RECORD", "LIVE_PACKET"]
    record_size: int
    raw: FileRef
    source_epoch: str | None = Field(default=None, pattern=r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
    ordinal: int | None = Field(default=None, ge=0, le=4294967295)
    expected_session: int | None = Field(default=None, ge=1, le=65535)

    @model_validator(mode="after")
    def coordinates(self):
        if self.kind == "SOURCE_RECORD":
            if (self.record_size not in {8, 16, 40} or self.ordinal is None
                    or self.source_epoch is None or self.expected_session is not None):
                raise ValueError("SOURCE_COORDINATES_REQUIRED")
        elif (self.record_size not in {12, 32, 36, 52} or self.expected_session is None
              or self.ordinal is not None or self.source_epoch is not None):
            raise ValueError("LIVE_SESSION_REQUIRED")
        return self


class Bundle(Strict):
    schema_version: int = Field(ge=1, le=1)
    bundle_id: str = Field(pattern=SAFE_ID)
    evidence_kind: Literal["BENCH_CAPTURE", "SYNTHETIC"]
    model: str
    terminal_serial: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,120}$")
    terminal_firmware: str = Field(min_length=1, max_length=120, pattern=r"^[ -~]+$")
    captured_at: AwareDatetime
    collector_id: str = Field(pattern=SAFE_ID)
    reviewer_id: str = Field(pattern=SAFE_ID)
    # Original model/firmware/session evidence and independent expected facts.
    # Declared identities are provenance, not verified signatures or authority.
    terminal_evidence: FileRef
    expected: FileRef
    cases: list[Case] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def bounded_scope(self):
        if self.model not in MODEL_PROFILES:
            raise ValueError("UNKNOWN_MODEL")
        if self.collector_id == self.reviewer_id:
            raise ValueError("DISTINCT_REVIEWER_REQUIRED")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("DUPLICATE_CASE")
        return self


class Fact(Strict):
    user_id: str | None = Field(min_length=1, max_length=24, pattern=r"^[ -~]+$")
    attendance_uid: int | None = Field(ge=0, le=65535)
    encoded_time: int = Field(ge=0, le=4294967295)
    local_time: AwareDatetime
    utc_time: AwareDatetime
    status: int = Field(ge=0, le=255)
    punch: int = Field(ge=0, le=255)
    offset: int = Field(ge=0, le=65535)
    length: int = Field(ge=1, le=52)

    @model_validator(mode="after")
    def explicit_clock(self):
        if (self.local_time.utcoffset() != timedelta(hours=5)
                or self.utc_time.utcoffset() != timedelta(0)
                or self.local_time != self.utc_time or self.local_time.microsecond):
            raise ValueError("INDEPENDENT_PAKISTAN_AND_UTC_TIMES_REQUIRED")
        return self


class ExpectedCase(Strict):
    case_id: str = Field(pattern=SAFE_ID)
    outcome: Literal["ACCEPT", "REJECT"]
    facts: list[Fact] = Field(default_factory=list, max_length=5460)

    @model_validator(mode="after")
    def outcome_facts(self):
        if (self.outcome == "ACCEPT") != bool(self.facts):
            raise ValueError("EXPECTED_OUTCOME_FACTS_REQUIRED")
        return self


class Expected(Strict):
    schema_version: int = Field(ge=1, le=1)
    method: Literal["INDEPENDENT_OBSERVATION", "SYNTHETIC_SPECIFICATION"]
    recorded_by: str = Field(pattern=SAFE_ID)
    recorded_at: AwareDatetime
    reference: FileRef
    cases: list[ExpectedCase] = Field(min_length=1, max_length=128)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_descriptor(descriptor: int, maximum: int) -> bytes:
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise BundleError("BUNDLE_FILE_BOUNDS")
        value = stream.read(maximum + 1)
        if not 0 < len(value) <= maximum:
            raise BundleError("BUNDLE_FILE_BOUNDS")
        return value


def read_regular(path: Path, maximum: int) -> bytes:
    """Refuse symlinks/special files and cap reads before allocating contents."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        return read_descriptor(descriptor, maximum)
    except OSError as exc:
        raise BundleError("BUNDLE_FILE_UNAVAILABLE") from exc


def referenced(base: Path, reference: FileRef, maximum: int = MAX_FILE_BYTES) -> bytes:
    # Open each path component relative to the retained directory descriptor.
    # A concurrent directory/symlink swap cannot escape the input directory.
    directory = None
    try:
        directory = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parts = PurePosixPath(reference.path).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        value = read_descriptor(descriptor, maximum)
    except OSError as exc:
        raise BundleError("BUNDLE_FILE_UNAVAILABLE") from exc
    finally:
        if directory is not None:
            os.close(directory)
    if sha256(value) != reference.sha256:
        raise BundleError("BUNDLE_DIGEST_MISMATCH")
    return value


def parse(model, data: bytes):
    # Duplicate JSON keys would make a reviewed manifest ambiguous.
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BundleError("BUNDLE_DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        json.loads(data, object_pairs_hook=unique)
        return model.model_validate_json(data)
    except (ValueError, ValidationError, RecursionError) as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError("BUNDLE_SCHEMA_INVALID") from exc


def compile_probe(directory: Path) -> tuple[Path, dict]:
    compiler = shutil.which("cc")
    if compiler is None:
        raise BundleError("C_COMPILER_UNAVAILABLE")
    binary = directory / "zkt-bench-probe"
    main = ROOT / "firmware/zone_lite/main"
    flags = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror",
             "-g", "-O1", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
             "-fno-omit-frame-pointer"]
    try:
        version = subprocess.run([compiler, "--version"], capture_output=True,
                                 timeout=10, check=True).stdout
        subprocess.run([compiler, *flags, "-I", str(main),
            str(ROOT / "scripts/zkt_bench_decoder_probe.c"),
            *(str(main / name) for name in ("zkt_record.c", "reliability.c", "zkt_clock.c")),
            "-o", str(binary)], capture_output=True, timeout=120, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleError("C_PROBE_BUILD_FAILED") from exc
    return binary, {"compiler_version": version.decode("utf-8", errors="replace").splitlines()[0][:240],
                    "compiler_version_sha256": sha256(version), "flags": flags,
                    "probe_sha256": sha256(binary.read_bytes())}


def c_facts(binary: Path, case: Case, raw: bytes) -> tuple[str, list[dict]]:
    line = f"{int(case.kind == 'LIVE_PACKET')} {case.record_size} {case.expected_session or 0} {len(raw)} {raw.hex()}\n"
    try:
        result = subprocess.run([str(binary)], input=line, capture_output=True, text=True,
                                timeout=30, check=True)
        lines = result.stdout.splitlines()
        if not lines or lines[-1] not in {"ACCEPT", "REJECT"}:
            raise ValueError("bad probe result")
        if lines[-1] == "REJECT":
            return "REJECT", []  # A late invalid row rejects the whole packet.
        records = []
        for line in lines[:-1]:
            user, uid, encoded, status, punch, offset, length, epoch = line.split()
            records.append({"user_id": None if user == "-" else bytes.fromhex(user).decode("ascii"),
                "attendance_uid": None if uid == "-1" else int(uid), "encoded_time": int(encoded),
                "status": int(status), "punch": int(punch), "offset": int(offset), "length": int(length),
                "utc_time": datetime.fromtimestamp(int(epoch), timezone.utc)})
        return "ACCEPT", records
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Captured stdout/stderr can contain protected facts; never echo them.
        raise BundleError("C_PROBE_EXECUTION_FAILED") from exc


def check_bundle(path: Path) -> dict:
    manifest = read_regular(path, MAX_FILE_BYTES)
    bundle = parse(Bundle, manifest)
    base = path.parent.resolve()
    terminal = referenced(base, bundle.terminal_evidence)
    expected_raw = referenced(base, bundle.expected)
    expected = parse(Expected, expected_raw)
    reference = referenced(base, expected.reference)
    if (expected.recorded_by != bundle.reviewer_id
            or (bundle.evidence_kind == "BENCH_CAPTURE") != (expected.method == "INDEPENDENT_OBSERVATION")
            or expected.recorded_at > datetime.now(timezone.utc)
            or bundle.captured_at > datetime.now(timezone.utc)):
        raise BundleError("BUNDLE_PROVENANCE_INVALID")
    expect = {case.case_id: case for case in expected.cases}
    if len(expect) != len(expected.cases) or set(expect) != {case.case_id for case in bundle.cases}:
        raise BundleError("BUNDLE_EXPECTATION_COVERAGE")
    raw_cases = [(case, referenced(base, case.raw, 65536)) for case in bundle.cases]
    total = len(manifest) + len(terminal) + len(expected_raw) + len(reference) + sum(len(raw) for _, raw in raw_cases)
    if total > MAX_BUNDLE_BYTES:
        raise BundleError("BUNDLE_TOTAL_BOUNDS")
    # Pin source contents on both sides of the run. A changing checkout cannot
    # claim that its observed output corresponds to one set of source hashes.
    sources = {name: sha256(read_regular(ROOT / name, MAX_FILE_BYTES)) for name in SOURCE_FILES}
    cases = []
    with tempfile.TemporaryDirectory(prefix="zkt-bench-") as directory:
        binary, build = compile_probe(Path(directory))
        for case, raw in raw_cases:
            outcome = expect[case.case_id]
            try:
                facts = ((decode_source(raw, record_size=case.record_size),) if case.kind == "SOURCE_RECORD"
                         else decode_live_packet(raw, allowed_sizes=frozenset({case.record_size}),
                                                 expected_session=case.expected_session).records)
                add_outcome = "ACCEPT"
            except DecodeError:
                facts, add_outcome = (), "REJECT"
            c_outcome, records = c_facts(binary, case, raw)
            reasons = []
            if add_outcome != outcome.outcome:
                reasons.append("ADD_OUTCOME_MISMATCH")
            if c_outcome != outcome.outcome:
                reasons.append("FIRMWARE_OUTCOME_MISMATCH")
            if outcome.outcome == "ACCEPT":
                target = [fact.model_dump() for fact in outcome.facts]
                actual = [{key: getattr(fact, key) for key in target[0]} for fact in facts]
                if actual != target:
                    reasons.append("ADD_FACTS_MISMATCH")
                c_target = [{key: value for key, value in fact.items() if key != "local_time"} for fact in target]
                if records != c_target:
                    reasons.append("FIRMWARE_FACTS_MISMATCH")
            cases.append({"case_id": case.case_id, "kind": case.kind, "record_size": case.record_size,
                          "raw_sha256": case.raw.sha256, "state": "FAILED" if reasons else "PASSED",
                          "expected_record_count": len(outcome.facts), "reasons": reasons})
    if sources != {name: sha256(read_regular(ROOT / name, MAX_FILE_BYTES)) for name in SOURCE_FILES}:
        raise BundleError("DECODER_SOURCE_CHANGED_DURING_RUN")
    hypotheses = {}
    for (case, _), result in zip(raw_cases, cases, strict=True):
        if case.kind == "LIVE_PACKET" and result["state"] == "PASSED" and expect[case.case_id].outcome == "ACCEPT":
            hypotheses.setdefault(case.raw.sha256, set()).add(case.record_size)
    return {"schema_version": 1, "scope": "EXPLICIT_RECORD_LAYOUT_AND_CLOCK_COMPARISON",
            "bundle_sha256": sha256(manifest), "expected_sha256": sha256(expected_raw),
            "terminal_evidence_sha256": sha256(terminal), "independent_reference_sha256": sha256(reference),
            "evidence_kind": bundle.evidence_kind, "model": bundle.model,
            "profile_selector": MODEL_PROFILES[bundle.model], "add_decoder_version": DECODER_VERSION,
            "completed_at": datetime.now(timezone.utc).isoformat(), "source_sha256": sources,
            "build": build, "runtime": {"python": platform.python_version(),
                "system": platform.system(), "machine": platform.machine(), "pydantic": PYDANTIC_VERSION},
            "cases": cases, "multiple_passing_live_layouts": [
                {"raw_sha256": raw_digest, "record_sizes": sorted(sizes)}
                for raw_digest, sizes in sorted(hypotheses.items()) if len(sizes) > 1],
            "comparison": "PASSED" if all(case["state"] == "PASSED" for case in cases) else "FAILED",
            "profile_qualification": "NOT_ASSERTED", "activation_authority": "NONE",
            "independence_and_capture_provenance": "REQUIRES_EXTERNAL_REVIEW",
            "transport_checksum_and_session_qualification": "NOT_PERFORMED",
            "physical_power_and_endurance": "NOT_PERFORMED"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        # Reserve a new protected file first. Never overwrite evidence or allow
        # an output symlink to change the input bundle or an unrelated file.
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        print("BUNDLE_REPORT_CREATE_FAILED", file=sys.stderr)
        return 2
    status = 2
    report = None
    try:
        report = check_bundle(args.bundle)
        status = 0 if report["comparison"] == "PASSED" else 1
    except BundleError as exc:
        report = {"schema_version": 1, "comparison": "ERROR", "reason": str(exc),
                  "profile_qualification": "NOT_ASSERTED", "activation_authority": "NONE"}
    except Exception:
        # An unexpected parser/runtime error also cannot print protected facts.
        report = {"schema_version": 1, "comparison": "ERROR", "reason": "CHECKER_FAILED",
                  "profile_qualification": "NOT_ASSERTED", "activation_authority": "NONE"}
    finally:
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(report or {"comparison": "ERROR", "reason": "CHECKER_FAILED"}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            print("BUNDLE_REPORT_WRITE_FAILED", file=sys.stderr)
            status = 2
    print(f"comparison={report['comparison']} profile_qualification=NOT_ASSERTED activation_authority=NONE")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
