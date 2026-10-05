"""Re-publication keeps the original signed bytes and completed provenance."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[2] / "scripts/check_hil_signed_reuse.py"
spec = importlib.util.spec_from_file_location("hil_signed_reuse", path)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def evidence():
    return ["12345", "2.6.17", "example/attendance",
        {"id": 12345, "name": "Zone Lite quarantined HIL candidate", "event": "workflow_dispatch",
         "path": ".github/workflows/firmware-hil-candidate.yml", "status": "completed", "conclusion": "failure",
         "head_sha": "a" * 40, "head_repository": {"full_name": "example/attendance"}},
        {"total_count": 3, "jobs": [{"name": name, "conclusion": "success", "status": "completed", "run_id": 12345}
                                   for name in ("provenance", "build", "sign")]},
        {"total_count": 1, "artifacts": [{"name": "zone-lite-2.6.17-hil-only", "expired": False, "size_in_bytes": 1380352}]}]


def test_publication_failure_can_reuse_completed_signature(evidence):
    assert checker.validate(*evidence) == "reuse_run=12345"
    evidence[3]["conclusion"] = "success"
    assert checker.validate(*evidence) == "reuse_run=12345"


@pytest.mark.parametrize("field,value", [("id", 9), ("name", "unrelated"), ("event", "pull_request"),
    ("path", ".github/workflows/other.yml"), ("status", "in_progress"), ("conclusion", "cancelled"),
    ("head_sha", "not-a-commit"), ("head_repository", {"full_name": "another/repository"})])
def test_untrusted_or_unfinished_run_rejected(evidence, field, value):
    evidence[3][field] = value
    with pytest.raises(ValueError):
        checker.validate(*evidence)


@pytest.mark.parametrize("stage", [0, 1, 2])
@pytest.mark.parametrize("fault", ["missing", "duplicate", "failed", "running", "wrong-run"])
def test_every_original_build_stage_required(evidence, stage, fault):
    job = evidence[4]["jobs"][stage]
    if fault == "missing":
        evidence[4]["jobs"].remove(job)
    elif fault == "duplicate":
        evidence[4]["jobs"].append(deepcopy(job))
    elif fault == "failed":
        job["conclusion"] = "failure"
    elif fault == "running":
        job["status"] = "in_progress"
    else:
        job["run_id"] = 9
    evidence[4]["total_count"] = len(evidence[4]["jobs"])
    with pytest.raises(ValueError):
        checker.validate(*evidence)


@pytest.mark.parametrize("fault", ["expired", "wrong-version", "missing", "duplicate", "oversize", "empty", "partial-jobs", "partial-artifacts", "invalid-run"])
def test_exact_available_artifact_and_complete_metadata_required(evidence, fault):
    artifact = evidence[5]["artifacts"][0]
    if fault == "expired":
        artifact["expired"] = True
    elif fault == "wrong-version":
        artifact["name"] = "zone-lite-2.6.16-hil-only"
    elif fault == "missing":
        evidence[5]["artifacts"] = []
    elif fault == "duplicate":
        evidence[5]["artifacts"].append(deepcopy(artifact))
        evidence[5]["total_count"] = 2
    elif fault == "oversize":
        artifact["size_in_bytes"] = 16 * 1024 * 1024 + 1
    elif fault == "empty":
        artifact["size_in_bytes"] = 0
    elif fault == "partial-jobs":
        evidence[4]["total_count"] = 101
    elif fault == "partial-artifacts":
        evidence[5]["total_count"] = 101
    else:
        evidence[0] = "12345\nreuse_run=other"
    with pytest.raises(ValueError):
        checker.validate(*evidence)
