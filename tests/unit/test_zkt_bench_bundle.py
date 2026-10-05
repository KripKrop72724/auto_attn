"""Protected bench handoff is reproducible; synthetic comparisons grant no authority."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("zkt_bench_checker", ROOT / "scripts/check_zkt_bench_bundle.py")
bench = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bench
spec.loader.exec_module(bench)


def save(directory, name, value):
    data = json.dumps(value).encode() if isinstance(value, dict) else value
    (directory / name).write_bytes(data)
    return {"path": name, "sha256": bench.sha256(data)}


def bundle(directory, model="G3"):
    """Wire bytes and expected observations are explicit synthetic specifications.

    No expected fields come from either implementation under test.
    """
    cases, expected = [], []
    for kind, size in [("SOURCE_RECORD", 8), ("SOURCE_RECORD", 16), ("SOURCE_RECORD", 40),
                       ("LIVE_PACKET", 12), ("LIVE_PACKET", 32), ("LIVE_PACKET", 36), ("LIVE_PACKET", 52)]:
        name = f"{kind}-{size}"
        raw = bytearray(size)
        user, uid = None, None
        if kind == "SOURCE_RECORD":
            if size == 8:
                struct.pack_into("<HBI", raw, 0, 456, 7, 859972462)
                raw[7] = 2
                uid = 456
            elif size == 16:
                struct.pack_into("<IIBB", raw, 0, 123, 859972462, 7, 2)
                user = "123"
            else:
                struct.pack_into("<H", raw, 0, 456)
                raw[2:8] = b"000123"
                raw[26] = 7
                struct.pack_into("<I", raw, 27, 859972462)
                raw[31] = 2
                user, uid = "000123", 456
            offsets = [0]
        else:
            base = 4 if size == 12 else 24
            if size == 12:
                struct.pack_into("<I", raw, 0, 123)
                user = "123"
            else:
                raw[:6] = b"000123"
                user = "000123"
            raw[base:base+8] = bytes([7, 2, 26, 10, 3, 9, 14, 22])
            raw = bytearray(struct.pack("<HHHH", 500, 4321, 23, 9) + raw + raw)
            offsets = [8, 8+size]  # Equal same-second occurrences stay distinct.
        coordinates = {"source_epoch": "c5722c99-f5b0-49ea-afd1-2b814cd71a17", "ordinal": size} if kind == "SOURCE_RECORD" else {"expected_session": 23}
        cases.append({"case_id": name, "kind": kind, "record_size": size,
                      "raw": save(directory, name + ".bin", bytes(raw)), **coordinates})
        expected.append({"case_id": name, "outcome": "ACCEPT", "facts": [
            {"user_id": user, "attendance_uid": uid, "encoded_time": 859972462,
             "local_time": "2026-10-03T09:14:22+05:00", "utc_time": "2026-10-03T04:14:22+00:00",
             "status": 7, "punch": 2, "offset": offset, "length": size} for offset in offsets]})
    truth = {"schema_version": 1, "method": "SYNTHETIC_SPECIFICATION", "recorded_by": "test-reviewer",
             "recorded_at": "2026-10-03T05:00:00Z",
             "reference": save(directory, "independent.txt", b"Synthetic manually specified punch at 09:14:22 Pakistan."),
             "cases": expected}
    manifest = {"schema_version": 1, "bundle_id": "synthetic-test-1", "evidence_kind": "SYNTHETIC",
                "model": model, "terminal_serial": "TEST01", "terminal_firmware": "test-only",
                "captured_at": "2026-10-03T04:14:23Z", "collector_id": "test-collector",
                "reviewer_id": "test-reviewer", "terminal_evidence": save(directory, "terminal.txt", b"Synthetic TEST01"),
                "expected": save(directory, "expected.json", truth), "cases": cases}
    path = directory / "bundle.json"
    path.write_text(json.dumps(manifest))
    return path, manifest, truth


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    return bench.compile_probe(tmp_path_factory.mktemp("bench-probe"))


@pytest.fixture
def fast_compile(monkeypatch, compiled):
    monkeypatch.setattr(bench, "compile_probe", lambda directory: compiled)


def rewrite(path, manifest, truth=None):
    if truth is not None:
        manifest["expected"] = save(path.parent, "expected.json", truth)
    path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("model", list(bench.MODEL_PROFILES))
def test_actual_c_add_and_independent_facts_agree_without_qualification(tmp_path, fast_compile, model):
    path, _, _ = bundle(tmp_path, model)
    report = bench.check_bundle(path)
    assert report["comparison"] == "PASSED" and len(report["cases"]) == 7
    assert report["profile_qualification"] == "NOT_ASSERTED" and report["activation_authority"] == "NONE"
    assert report["independence_and_capture_provenance"] == "REQUIRES_EXTERNAL_REVIEW"
    assert report["transport_checksum_and_session_qualification"] == "NOT_PERFORMED"
    assert all(len(digest) == 64 for digest in report["source_sha256"].values())
    assert {case["expected_record_count"] for case in report["cases"]} == {1, 2}
    text = json.dumps(report)
    assert "TEST01" not in text and "000123" not in text and "test-reviewer" not in text


@pytest.mark.parametrize("field,value", [("user_id", "123"), ("attendance_uid", 457),
    ("encoded_time", 1), ("status", 8), ("punch", 3), ("offset", 1), ("length", 16)])
def test_both_decoders_must_match_independent_expected_fields(tmp_path, fast_compile, field, value):
    path, manifest, truth = bundle(tmp_path)
    truth["cases"][2]["facts"][0][field] = value
    rewrite(path, manifest, truth)
    report = bench.check_bundle(path)
    failed = [case for case in report["cases"] if case["state"] == "FAILED"]
    assert len(failed) == 1
    assert set(failed[0]["reasons"]) == {"ADD_FACTS_MISMATCH", "FIRMWARE_FACTS_MISMATCH"}
    assert report["comparison"] == "FAILED"


@pytest.mark.parametrize("mutation", ["collapsed_occurrence", "clock", "record_order"])
def test_complete_count_time_and_byte_order_are_independently_required(tmp_path, fast_compile, mutation):
    path, manifest, truth = bundle(tmp_path)
    rows = truth["cases"][4]["facts"]
    if mutation == "collapsed_occurrence":
        rows.pop()
    elif mutation == "clock":
        for row in rows:
            row["local_time"] = "2026-10-03T09:14:23+05:00"
            row["utc_time"] = "2026-10-03T04:14:23Z"
    else:
        rows.reverse()
    rewrite(path, manifest, truth)
    result = bench.check_bundle(path)["cases"][4]
    assert set(result["reasons"]) == {"ADD_FACTS_MISMATCH", "FIRMWARE_FACTS_MISMATCH"}


def test_nested_regular_references_and_declared_bench_provenance_remain_unqualified(tmp_path, fast_compile):
    path, manifest, truth = bundle(tmp_path)
    (tmp_path / "raw").mkdir()
    for case in manifest["cases"]:
        name = case["raw"]["path"]
        (tmp_path / name).rename(tmp_path / "raw" / name)
        case["raw"]["path"] = "raw/" + name
    manifest["evidence_kind"] = "BENCH_CAPTURE"
    truth["method"] = "INDEPENDENT_OBSERVATION"
    rewrite(path, manifest, truth)
    report = bench.check_bundle(path)
    assert report["comparison"] == "PASSED"
    assert report["profile_qualification"] == "NOT_ASSERTED"
    assert report["independence_and_capture_provenance"] == "REQUIRES_EXTERNAL_REVIEW"


def test_two_passing_layout_hypotheses_are_reported_without_selecting_one(tmp_path, fast_compile):
    path, manifest, truth = bundle(tmp_path)
    raw = bytearray(36)
    for offset in (0, 12, 24):
        raw[offset] = 7
        raw[offset+6:offset+12] = bytes([26, 9, 28, 14, 55, 17])
    raw[0] = ord("7")
    raw[27:30] = bytes([9, 1, 1])
    reference = save(tmp_path, "ambiguous.bin", struct.pack("<HHHH", 500, 4321, 23, 9) + raw)
    manifest["cases"] = []
    truth["cases"] = []
    for size in (12, 36):
        name = f"layout-{size}"
        manifest["cases"].append({"case_id": name, "kind": "LIVE_PACKET", "record_size": size,
                                  "expected_session": 23, "raw": reference})
        rows = []
        for index in range(3 if size == 12 else 1):
            local = datetime(2026, 9, 28, 14, 55, 17, tzinfo=timezone(timedelta(hours=5))) if size == 12 else datetime(2000, 9, 1, 1, 26, 9, tzinfo=timezone(timedelta(hours=5)))
            encoded = (((((local.year - 2000) * 12 + local.month - 1) * 31 + local.day - 1) * 24 + local.hour) * 60 + local.minute) * 60 + local.second
            rows.append({"user_id": ["55", "7", "150994951"][index] if size == 12 else "7",
                "attendance_uid": None, "encoded_time": encoded, "local_time": local.isoformat(),
                "utc_time": local.astimezone(timezone.utc).isoformat(),
                "status": 1 if size == 12 and index == 2 else 7 if size == 36 else 0,
                "punch": 1 if size == 12 and index == 2 else 0,
                "offset": 8+index*size, "length": size})
        truth["cases"].append({"case_id": name, "outcome": "ACCEPT", "facts": rows})
    rewrite(path, manifest, truth)
    report = bench.check_bundle(path)
    assert report["comparison"] == "PASSED"
    assert report["multiple_passing_live_layouts"] == [{"raw_sha256": reference["sha256"], "record_sizes": [12, 36]}]
    assert report["profile_qualification"] == "NOT_ASSERTED"


@pytest.mark.parametrize("mutation", ["invalid_date", "control_character", "partial_tail", "wrong_session", "invalid_source_clock"])
def test_negative_wire_cases_require_both_decoders_to_reject_entire_input(tmp_path, fast_compile, mutation):
    path, manifest, truth = bundle(tmp_path)
    index = 2 if mutation == "invalid_source_clock" else 4
    case = manifest["cases"][index]
    raw = bytearray((tmp_path / case["raw"]["path"]).read_bytes())
    if mutation == "invalid_date":
        raw[8+32+24+3:8+32+24+5] = bytes([2, 30])  # Bad second row after a good first row.
    elif mutation == "control_character":
        raw[8+1] = 1
    elif mutation == "partial_tail":
        raw.pop()
    elif mutation == "wrong_session":
        raw[4] = 22
    else:
        struct.pack_into("<I", raw, 27, 0xFFFFFFFF)
    case["raw"] = save(tmp_path, case["raw"]["path"], bytes(raw))
    truth["cases"][index].update(outcome="REJECT", facts=[])
    rewrite(path, manifest, truth)
    assert bench.check_bundle(path)["comparison"] == "PASSED"
    truth["cases"][index].update(outcome="ACCEPT", facts=deepcopy(truth["cases"][0]["facts"]))
    rewrite(path, manifest, truth)
    report = bench.check_bundle(path)
    assert "ADD_OUTCOME_MISMATCH" in report["cases"][index]["reasons"]
    assert "FIRMWARE_OUTCOME_MISMATCH" in report["cases"][index]["reasons"]


@pytest.mark.parametrize("mutation,reason", [
    ("raw_changed", "BUNDLE_DIGEST_MISMATCH"), ("truth_changed", "BUNDLE_DIGEST_MISMATCH"),
    ("reference_changed", "BUNDLE_DIGEST_MISMATCH"), ("terminal_changed", "BUNDLE_DIGEST_MISMATCH"),
    ("missing_case", "BUNDLE_EXPECTATION_COVERAGE"), ("extra_case", "BUNDLE_EXPECTATION_COVERAGE"),
    ("duplicate_expected", "BUNDLE_EXPECTATION_COVERAGE"), ("duplicate_case", "BUNDLE_SCHEMA_INVALID"),
    ("same_reviewer", "BUNDLE_SCHEMA_INVALID"), ("unknown_model", "BUNDLE_SCHEMA_INVALID"),
    ("different_reviewer", "BUNDLE_PROVENANCE_INVALID"), ("synthetic_as_bench", "BUNDLE_PROVENANCE_INVALID"),
    ("future_capture", "BUNDLE_PROVENANCE_INVALID"), ("future_expectation", "BUNDLE_PROVENANCE_INVALID"),
    ("local_timezone", "BUNDLE_SCHEMA_INVALID"), ("utc_timezone", "BUNDLE_SCHEMA_INVALID"),
    ("clock_disagreement", "BUNDLE_SCHEMA_INVALID"), ("missing_source_epoch", "BUNDLE_SCHEMA_INVALID"),
    ("missing_session", "BUNDLE_SCHEMA_INVALID"), ("boolean_integer", "BUNDLE_SCHEMA_INVALID"),
    ("unknown_field", "BUNDLE_SCHEMA_INVALID"), ("traversal", "BUNDLE_SCHEMA_INVALID"),
    ("absolute_path", "BUNDLE_SCHEMA_INVALID"), ("duplicate_json_key", "BUNDLE_DUPLICATE_JSON_KEY"),
])
def test_incomplete_or_changed_evidence_never_runs_the_probe(tmp_path, monkeypatch, mutation, reason):
    path, manifest, truth = bundle(tmp_path)
    def forbidden(*args):
        pytest.fail("Must reject evidence before compiling")
    monkeypatch.setattr(bench, "compile_probe", forbidden)
    if mutation.endswith("_changed"):
        target = {"raw_changed": manifest["cases"][0]["raw"]["path"], "truth_changed": "expected.json",
                  "reference_changed": "independent.txt", "terminal_changed": "terminal.txt"}[mutation]
        (tmp_path / target).write_bytes(b"changed")
    elif mutation == "missing_case":
        truth["cases"].pop()
    elif mutation == "extra_case":
        truth["cases"].append({**deepcopy(truth["cases"][0]), "case_id": "unexpected"})
    elif mutation == "duplicate_expected":
        truth["cases"].append(truth["cases"][0])
    elif mutation == "duplicate_case":
        manifest["cases"].append(manifest["cases"][0])
    elif mutation == "same_reviewer":
        manifest["reviewer_id"] = manifest["collector_id"]
    elif mutation == "different_reviewer":
        truth["recorded_by"] = "someone-else"
    elif mutation == "unknown_model":
        manifest["model"] = "G3-unverified"
    elif mutation == "synthetic_as_bench":
        manifest["evidence_kind"] = "BENCH_CAPTURE"
    elif mutation == "future_capture":
        manifest["captured_at"] = "2099-01-01T00:00:00Z"
    elif mutation == "future_expectation":
        truth["recorded_at"] = "2099-01-01T00:00:00Z"
    elif mutation in {"local_timezone", "utc_timezone", "clock_disagreement"}:
        key, value = {"local_timezone": ("local_time", "2026-10-03T09:14:22Z"),
                      "utc_timezone": ("utc_time", "2026-10-03T09:14:22+05:00"),
                      "clock_disagreement": ("utc_time", "2026-10-03T04:14:23Z")}[mutation]
        truth["cases"][0]["facts"][0][key] = value
    elif mutation == "missing_source_epoch":
        del manifest["cases"][0]["source_epoch"]
    elif mutation == "missing_session":
        del manifest["cases"][3]["expected_session"]
    elif mutation == "boolean_integer":
        truth["cases"][0]["facts"][0]["status"] = True
    elif mutation == "unknown_field":
        manifest["qualified"] = True
    elif mutation == "traversal":
        manifest["cases"][0]["raw"]["path"] = "../outside"
    elif mutation == "absolute_path":
        manifest["cases"][0]["raw"]["path"] = str(tmp_path / "terminal.txt")
    if not mutation.endswith("_changed"):
        rewrite(path, manifest, truth)
    if mutation == "duplicate_json_key":
        path.write_text(path.read_text().replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1))
    with pytest.raises(bench.BundleError, match=reason):
        bench.check_bundle(path)


@pytest.mark.parametrize("kind", ["file", "directory", "fifo", "oversized"])
def test_external_symlink_special_and_oversized_files_are_refused(tmp_path, kind):
    path, manifest, _ = bundle(tmp_path)
    raw_path = tmp_path / manifest["cases"][0]["raw"]["path"]
    raw_path.unlink()
    if kind == "file":
        raw_path.symlink_to(tmp_path / "terminal.txt")
    elif kind == "directory":
        (tmp_path / "subdir").symlink_to(tmp_path, target_is_directory=True)
        manifest["cases"][0]["raw"]["path"] = "subdir/terminal.txt"
        rewrite(path, manifest)
    elif kind == "fifo":
        os.mkfifo(raw_path)
    else:
        raw_path.write_bytes(b"x" * 65537)
    with pytest.raises(bench.BundleError, match="BUNDLE_(FILE_|SYMLINK)"):
        bench.check_bundle(path)


def test_report_has_protected_permissions_and_never_overwrites_evidence(tmp_path, fast_compile, capsys):
    path, _, _ = bundle(tmp_path)
    report = tmp_path / "report.json"
    assert bench.main([str(path), "--output", str(report)]) == 0
    original = report.read_bytes()
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert bench.main([str(path), "--output", str(report)]) == 2
    assert report.read_bytes() == original
    original = path.read_bytes()
    assert bench.main([str(path), "--output", str(path)]) == 2
    assert path.read_bytes() == original
    output = capsys.readouterr()
    assert "000123" not in output.out + output.err


def test_bad_input_reports_only_safe_category(tmp_path, capsys):
    path, _, _ = bundle(tmp_path)
    path.write_text('{"protected":"sensitive-test-marker","bad":true}')
    report = tmp_path / "report.json"
    assert bench.main([str(path), "--output", str(report)]) == 2
    output = capsys.readouterr()
    assert "sensitive-test-marker" not in report.read_text() + output.out + output.err
    assert json.loads(report.read_text())["reason"] == "BUNDLE_SCHEMA_INVALID"


def test_probe_crash_is_not_a_decoder_rejection(tmp_path, monkeypatch, fast_compile, capsys):
    path, _, _ = bundle(tmp_path)
    def crash(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["probe"], output="sensitive-test-marker")
    monkeypatch.setattr(bench.subprocess, "run", crash)
    report = tmp_path / "report.json"
    assert bench.main([str(path), "--output", str(report)]) == 2
    assert json.loads(report.read_text())["reason"] == "C_PROBE_EXECUTION_FAILED"
    captured = capsys.readouterr()
    assert "sensitive-test-marker" not in report.read_text() + captured.out + captured.err


def test_source_change_during_comparison_cannot_get_a_result(tmp_path, fast_compile, monkeypatch):
    path, _, _ = bundle(tmp_path)
    original = bench.read_regular
    reads = 0
    def changed(file, maximum):
        nonlocal reads
        value = original(file, maximum)
        if file == ROOT / bench.SOURCE_FILES[0]:
            reads += 1
            if reads > 1:
                return value + b"changed-source"
        return value
    monkeypatch.setattr(bench, "read_regular", changed)
    with pytest.raises(bench.BundleError, match="DECODER_SOURCE_CHANGED_DURING_RUN"):
        bench.check_bundle(path)


def test_report_sync_failure_is_reported_as_an_error(tmp_path, fast_compile, monkeypatch, capsys):
    path, _, _ = bundle(tmp_path)
    def fail(*args):
        raise OSError("synthetic-fsync-failure")
    monkeypatch.setattr(bench.os, "fsync", fail)
    assert bench.main([str(path), "--output", str(tmp_path / "result.json")]) == 2
    assert "BUNDLE_REPORT_WRITE_FAILED" in capsys.readouterr().err
