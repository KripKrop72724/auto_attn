"""Isolated reader pins; never edit the deployment matrix for tests."""
from copy import deepcopy

from test_zkt_reader_matrix import pinned, synthetic_matrix, writer_manifest  # noqa: F401
from zk_add.zkt_reader_evidence import reader_entry_for_manifest


def proof(admission, tick=100000):
    return {"schema_version": 1, "verified": True, "matrix_sha256": admission["matrix_sha256"],
            "version": admission["reader"]["version"],
            "application_sha256": admission["reader"]["application_sha256"],
            "slot_address": 0x2A0000, "slot_size": 0x280000,
            "proof_generation": "7", "sampled_uptime_ms": tick}


def admit(session, release, deployment, policy, *, version="2.6.21"):
    from zk_add.ota import FirmwareEvent
    release.manifest = {**(release.manifest or {}), **writer_manifest(policy)}
    selection = reader_entry_for_manifest(release.manifest, version)
    deployment.previous_version = version
    session.add(FirmwareEvent(deployment_id=deployment.id, state="OFFERED",
                              details={"reader_admission": deepcopy(selection)}))
    session.flush()
    return selection
