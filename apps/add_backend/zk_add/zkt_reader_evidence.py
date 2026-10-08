"""Exact selected-reader admission and device proof; no inferred qualification."""
from copy import deepcopy

from sqlalchemy import select

from zk_add.zkt_reader_matrix import canonical


def reader_entry_for_manifest(manifest, version):
    from zk_add.zkt_writer_contract import validate_writer_manifest
    contract = validate_writer_manifest(manifest)
    if contract["schema_version"] != 5:
        raise ValueError("Historical writer contracts cannot authorize a new writer.")
    row = next((row for row in contract["reader_matrix"]["readers"] if row["version"] == version), None)
    if row is None:
        raise ValueError("Reader version is outside the exact writer matrix.")
    return {"schema_version": 1, "matrix_sha256": contract["reader_matrix_sha256"], "reader": deepcopy(row)}


def current_reader_admission(release, connector):
    from zk_add.zkt_writer_contract import reader_for_writer
    reader = reader_for_writer(release, connector)
    if reader is None:
        raise ValueError("Current reader identity is not in the writer matrix.")
    return reader_entry_for_manifest(release.manifest, reader["version"])


def admitted_reader(session, deployment, release):
    from zk_add.ota import FirmwareDeployment, FirmwareEvent, FirmwareRelease, _versions_match
    deployment = session.get(FirmwareDeployment, deployment.id, populate_existing=True)
    release = session.get(FirmwareRelease, release.id, populate_existing=True)
    if deployment is None or release is None or deployment.release_id != release.id:
        raise ValueError("Writer admission deployment scope changed.")
    events = list(session.scalars(select(FirmwareEvent).where(
        FirmwareEvent.deployment_id == deployment.id, FirmwareEvent.state == "OFFERED")
        .order_by(FirmwareEvent.id).limit(2).execution_options(populate_existing=True)))
    if len(events) != 1 or not isinstance(events[0].details, dict):
        raise ValueError("Writer admission has no unique committed reader selection.")
    value = events[0].details.get("reader_admission")
    if not isinstance(value, dict) or not isinstance(value.get("reader"), dict):
        raise ValueError("Writer admission reader identity is missing.")
    expected = reader_entry_for_manifest(release.manifest, value["reader"].get("version"))
    if (canonical(value) != canonical(expected)
            or not _versions_match(deployment.previous_version, expected["reader"]["version"])):
        raise ValueError("Writer admission reader identity disagrees with the deployment.")
    return expected


PROOF_KEYS = {"schema_version", "verified", "matrix_sha256", "version", "application_sha256",
              "slot_address", "slot_size", "proof_generation", "sampled_uptime_ms"}


def qualified_reader_proof(diagnostics, admission, uptime_seconds):
    proof = diagnostics.get("qualified_reader") if isinstance(diagnostics, dict) else None
    reader = admission.get("reader") if isinstance(admission, dict) else None
    if (type(proof) is not dict or set(proof) != PROOF_KEYS or type(reader) is not dict
            or type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
            or proof.get("verified") is not True
            or proof.get("matrix_sha256") != admission.get("matrix_sha256")
            or proof.get("version") != reader.get("version")
            or proof.get("application_sha256") != reader.get("application_sha256")
            or type(proof.get("slot_address")) is not int
            or proof["slot_address"] not in (0x2A0000, 0x520000)
            or type(proof.get("slot_size")) is not int or proof["slot_size"] != 0x280000
            or type(proof.get("sampled_uptime_ms")) is not int
            or type(uptime_seconds) is not int
            or proof["sampled_uptime_ms"] < 0 or uptime_seconds < 0
            or not -1000 <= uptime_seconds * 1000 - proof["sampled_uptime_ms"] <= 45000
            or type(proof.get("proof_generation")) is not str
            or not proof["proof_generation"].isascii() or not proof["proof_generation"].isdigit()
            or not 1 <= len(proof["proof_generation"]) <= 20
            or str(int(proof["proof_generation"])) != proof["proof_generation"]
            or not 1 <= int(proof["proof_generation"]) <= 2**64 - 1):
        raise ValueError("Exact fresh retained reader proof is missing or invalid.")
    return {key: deepcopy(value) for key, value in proof.items() if key != "sampled_uptime_ms"}


def stored_reader_evidence_matches(run, release, version):
    """Use in addition to full_event_matches; this does not grant a HIL verdict."""
    try:
        expected = reader_entry_for_manifest(release.manifest, version)
        baseline = run.baseline
        sealed = run.result["evidence"]
        proof = baseline["qualified_reader"]
        if type(proof) is not dict or set(proof) != PROOF_KEYS - {"sampled_uptime_ms"}:
            return False
        qualified_reader_proof({"qualified_reader": {**proof, "sampled_uptime_ms": 1000}}, expected, 1)
        if (baseline["reader_admission"] != expected or sealed["reader_admission"] != expected
                or sealed["qualified_reader"] != proof or proof.get("verified") is not True
                or proof.get("matrix_sha256") != expected["matrix_sha256"]
                or proof.get("version") != version
                or proof.get("application_sha256") != expected["reader"]["application_sha256"]):
            return False
        samples = sealed["samples"]
        return (type(samples) is list and bool(samples)
                and all(row.get("qualified_reader") == proof or (
                    type(row) is dict and row.get("qualified_reader_pending") is True
                    and type(row.get("reboot_startup")) is dict
                    and any(test.get("command_id") == row["reboot_startup"].get("command_id")
                        and test.get("boot_after") == row.get("boot_id")
                        and row["reboot_startup"].get("recovery_telemetry_id")
                        for test in sealed.get("recovery_tests", []) if type(test) is dict)
                    and row.get("qualified_reader") is None) for row in samples)
                and sealed["current_sample"].get("qualified_reader") == proof)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
