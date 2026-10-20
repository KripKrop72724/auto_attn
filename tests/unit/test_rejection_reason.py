"""Every EVIDENCE_INVALID rejection carries a bounded, non-secret reason code."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from zk_add import reconciliation
from zk_add.rejection import (
    UNCLASSIFIED_REASON,
    EvidenceRejected,
    rejection_category,
    rejection_reason,
    rejection_site,
)
from zk_add.schemas import ReconciliationChunkRequest

BACKEND = Path(reconciliation.__file__).resolve().parent
CNIC = "35202-1234567-1"


def _raised(function, *args):
    try:
        function(*args)
    except Exception as exc:  # noqa: BLE001 - the test inspects what was raised
        return exc
    raise AssertionError("expected an exception")


def test_coded_rejection_keeps_its_message_and_reports_only_its_code() -> None:
    error = EvidenceRejected("CHUNK_DIGEST_MISMATCH", "Chunk digest does not match its canonical source records.")
    assert isinstance(error, ValueError)
    assert str(error) == "Chunk digest does not match its canonical source records."
    assert (rejection_category(error), rejection_reason(error)) == ("EVIDENCE_INVALID", "CHUNK_DIGEST_MISMATCH")
    assert str(EvidenceRejected("SOURCE_NOT_ANCHORED")) == "SOURCE_NOT_ANCHORED"


@pytest.mark.parametrize("code", ["lowercase_code", "NOUNDERSCORE", "_LEADING", "TRAILING_", "A_" + "B" * 80, CNIC])
def test_reason_codes_are_bounded_identifiers(code) -> None:
    with pytest.raises(TypeError):
        EvidenceRejected(code, "message")


@pytest.mark.parametrize(("error", "reason"), [
    (ValueError("RAW_SOURCE_CUSTODY_NOT_ENABLED"), "RAW_SOURCE_CUSTODY_NOT_ENABLED"),
    (ValueError("Expected source ordinal 3, got 5."), UNCLASSIFIED_REASON),
    (ValueError(f"user {CNIC} conflicts"), UNCLASSIFIED_REASON),
    (ValueError(CNIC), UNCLASSIFIED_REASON),
    (ValueError("ALI"), UNCLASSIFIED_REASON),  # an upper-case name is not a code
    (ValueError("SOURCE_" + "X" * 80), UNCLASSIFIED_REASON),
    (OperationalError("SELECT 1", {}, Exception("down")), None),
    (RuntimeError("CUSTODY_KEY_UNAVAILABLE"), None),
])
def test_plain_errors_never_leak_message_text(error, reason) -> None:
    assert rejection_reason(error) == reason


def test_schema_errors_keep_field_paths_instead_of_a_reason() -> None:
    error = _raised(ReconciliationChunkRequest.model_validate, {"job_id": CNIC})
    assert isinstance(error, ValidationError)
    assert (rejection_category(error), rejection_reason(error)) == ("SCHEMA_INVALID", None)


def test_site_is_the_innermost_add_code_location() -> None:
    error = _raised(reconciliation._require_runnable, type("Job", (), {
        "status": "RUNNING", "terminal_generation": 2})(), 1)
    assert rejection_reason(error) == "SOURCE_GENERATION_MISMATCH"
    module, function, line = rejection_site(error).split(":")
    assert (module, function) == ("zk_add.reconciliation", "_require_runnable")
    source = (BACKEND / "reconciliation.py").read_text().splitlines()[int(line) - 1]
    assert "EvidenceRejected(" in source
    assert rejection_site(_raised(int, "x")) is None  # raised outside ADD code


# Every function a reconcile_chunk passes through before it commits.
CHUNK_PATH = {
    "reconciliation.py": {
        "apply_reconciliation_chunk", "_bound_source_attendance", "_replay_conflict",
        "_device_job", "_require_runnable", "_require_source_epoch", "_require_raw_custody",
        "_release_source_synced_attendance", "_retained_elsewhere_details",
    },
    "service.py": {"ingest_attendance"},
}


@pytest.mark.parametrize("module", sorted(CHUNK_PATH))
def test_every_reconcile_chunk_rejection_has_a_reason_code(module) -> None:
    tree = ast.parse((BACKEND / module).read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert CHUNK_PATH[module] <= set(functions)
    uncoded = []
    for name in sorted(CHUNK_PATH[module]):
        for node in ast.walk(functions[name]):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            call = node.exc
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                uncoded.append(f"{name}:{node.lineno}")
                continue
            first = call.args[0] if call.args else None
            literal = first.value if isinstance(first, ast.Constant) else None
            coded = (
                call.func.id == "EvidenceRejected"
                or (call.func.id == "ValueError" and isinstance(literal, str)
                    and rejection_reason(ValueError(literal)) == literal)
            )
            if not coded:
                uncoded.append(f"{name}:{node.lineno}")
    assert uncoded == []
