"""Fail-closed family ownership at onboarding and signed OTA boundaries."""
from typing import Any

FAMILIES = frozenset({"zkt", "hikvision"})
HIKVISION_RELEASE_GATES = frozenset({
    "live_latency", "live_during_history_and_crud", "full_retained_history",
    "profile_crud", "identity_lifecycle", "add_ords_independent_delivery",
    "power_loss_recovery", "storage_pressure", "source_epoch_reset",
    "signed_family_ota_rollback", "capacity_150000", "soak_72_hours",
})
HIKVISION_POLL_RELEASE_GATES = (HIKVISION_RELEASE_GATES - {
    "live_latency", "live_during_history_and_crud",
}) | {"poll_5s_latency", "poll_during_history_and_crud"}


def firmware_family(value: Any = None) -> str:
    # Absent fields belong only to the existing ZKT protocol.
    if value is None:
        return "zkt"
    if not isinstance(value, str) or value not in FAMILIES:
        raise ValueError("UNKNOWN_FIRMWARE_FAMILY")
    return value


def release_family(manifest: dict) -> str:
    family = firmware_family(manifest.get("firmware_family"))
    if family == "hikvision" and manifest.get("project_name") != "zone_lite_hikvision":
        raise ValueError("FIRMWARE_PROJECT_FAMILY_MISMATCH")
    return family


def require_family_match(connector_family: str | None, manifest: dict) -> None:
    if firmware_family(connector_family) != release_family(manifest):
        raise ValueError("FIRMWARE_FAMILY_MISMATCH")


def require_production_qualification(manifest: dict) -> None:
    if release_family(manifest) != "hikvision":
        return
    proof = manifest.get("hardware_qualification")
    if not isinstance(proof, dict):
        raise ValueError("HIKVISION_HARDWARE_QUALIFICATION_REQUIRED")
    digest = proof.get("evidence_sha256", "")
    mode = proof.get("capture_mode", "stream")
    gates = HIKVISION_POLL_RELEASE_GATES if mode == "poll" else HIKVISION_RELEASE_GATES
    if (proof.get("model") != "DS-K1T342EFWX"
            or mode not in {"stream", "poll"}
            or (mode == "poll" and (type(proof.get("poll_interval_seconds")) is not int
                                   or proof["poll_interval_seconds"] not in {2, 5}))
            or not proof.get("terminal_firmware") or not proof.get("profile_id")
            or type(proof.get("soak_seconds")) is not int or proof["soak_seconds"] < 259200
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or not isinstance(proof.get("gates"), dict)
            or any(proof["gates"].get(gate) != "PASS" for gate in gates)):
        raise ValueError("HIKVISION_HARDWARE_QUALIFICATION_INCOMPLETE")
