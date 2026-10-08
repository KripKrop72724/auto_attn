"""Actual admission and rollback rows bind one selected matrix entry."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from reader_matrix_fixtures import admit, pinned, proof  # noqa: F401
from test_zkt_failed_boot_receipt import attempt, recover  # noqa: F401
from zk_add.ota import FirmwareEvent
from zk_add.zkt_reader_evidence import admitted_reader, stored_reader_evidence_matches


@pytest.fixture(params=["2.6.21", "2.6.22"])
def selected_attempt(attempt, pinned, request):  # noqa: F811
    session, connector, writer, bridge, event, releases, _ = attempt
    entry = next(row for row in pinned["readers"] if row["version"] == request.param)
    reader = releases[0]
    reader.release_id, reader.version = entry["release_id"], entry["version"]
    reader.git_sha, reader.signing_key_id = entry["source_sha"], entry["signing_key_id"]
    reader.image_sha256 = entry["artifact_sha256"]
    reader.manifest = {**reader.manifest, "application_sha256": entry["application_sha256"]}
    connector.firmware_version = bridge.target_version = entry["version"]
    event.details = {**event.details, "running_version": entry["version"],
                     "image_sha256": entry["application_sha256"]}
    selection = admit(session, releases[1], writer, pinned, version=entry["version"])
    session.commit()
    return attempt, selection


def test_return_uses_the_committed_exact_reader_not_a_reported_allowed_version(selected_attempt):
    rows, selection = selected_attempt
    receipt = recover(rows, running_version=selection["reader"]["version"],
                      image_sha256=selection["reader"]["application_sha256"])
    assert receipt["state"] == "ROLLED_BACK"
    assert receipt["reader_admission"] == selection
    assert receipt["rollback_application_sha256"] == selection["reader"]["application_sha256"]
    assert recover(rows) == receipt  # A replay returns the committed result, not newly supplied facts.


@pytest.mark.parametrize("fault", ["missing", "duplicate", "selected_other", "matrix", "previous_version",
    "artifact", "source", "key", "release_id", "revoked", "bridge_version"])
def test_a_selected_reader_cannot_be_substituted(selected_attempt, pinned, fault):  # noqa: F811
    from fastapi import HTTPException
    rows, selection = selected_attempt
    session, _, writer, bridge, _, releases, _ = rows
    event = session.scalar(select(FirmwareEvent).where(FirmwareEvent.deployment_id == writer.id,
                                                       FirmwareEvent.state == "OFFERED"))
    if fault == "missing":
        session.delete(event)
    elif fault == "duplicate":
        session.add(FirmwareEvent(deployment_id=writer.id, state="OFFERED", details=deepcopy(event.details)))
    elif fault in {"selected_other", "matrix"}:
        changed = deepcopy(event.details)
        if fault == "matrix":
            changed["reader_admission"]["matrix_sha256"] = "e" * 64
        else:
            changed["reader_admission"]["reader"] = next(row for row in pinned["readers"]
                if row["version"] != selection["reader"]["version"])
        event.details = changed
    elif fault == "previous_version":
        writer.previous_version = "2.6.17"
    elif fault == "bridge_version":
        bridge.target_version = "2.6.17"
    elif fault == "revoked":
        releases[0].state = "REVOKED"
    else:
        setattr(releases[0], {"artifact": "image_sha256", "source": "git_sha",
            "key": "signing_key_id", "release_id": "release_id"}[fault], "wrong")
    session.commit()
    with pytest.raises(HTTPException) as error:
        recover(rows, running_version=selection["reader"]["version"],
                image_sha256=selection["reader"]["application_sha256"])
    assert error.value.status_code == 409
    assert writer.status == "READY_TO_BOOT"
    assert not session.scalar(select(FirmwareEvent).where(FirmwareEvent.deployment_id == writer.id,
                                                         FirmwareEvent.state == "ROLLED_BACK"))


def test_sealed_reader_evidence_does_not_treat_membership_or_a_label_as_proof(selected_attempt):
    from types import SimpleNamespace
    rows, selection = selected_attempt
    session, _, deployment, _, _, releases, _ = rows
    assert admitted_reader(session, deployment, releases[1]) == selection
    normalized = proof(selection)
    normalized.pop("sampled_uptime_ms")
    sealed = {"reader_admission": selection, "qualified_reader": normalized,
              "samples": [{"qualified_reader": normalized}], "current_sample": {"qualified_reader": normalized}}
    run = SimpleNamespace(baseline={"reader_admission": selection, "qualified_reader": normalized},
                          result={"evidence": sealed})
    version = selection["reader"]["version"]
    assert stored_reader_evidence_matches(run, releases[1], version)
    for field in ("version", "application_sha256", "slot_address", "proof_generation", "matrix_sha256"):
        altered = deepcopy(run)
        altered.result["evidence"]["samples"][0]["qualified_reader"] = {**normalized, field: "wrong"}
        assert not stored_reader_evidence_matches(altered, releases[1], version)
    forged = deepcopy(run)
    forged.baseline["qualified_reader"]["slot_size"] = 0
    # deepcopy preserves aliases: even consistently altered baseline/seal cannot be a valid proof.
    assert not stored_reader_evidence_matches(forged, releases[1], version)
    pending = deepcopy(run)
    pending.result["evidence"]["samples"] = [{"qualified_reader": None,
        "qualified_reader_pending": True, "reboot_startup": {"phase": "RECOVERING"}}]
    assert not stored_reader_evidence_matches(pending, releases[1], version)
