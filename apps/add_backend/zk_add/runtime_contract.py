"""Versioned runtime obligations; device reports cannot waive required workers."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeContract:
    workers: frozenset[str]
    queues: frozenset[str]
    delivery_authority: str


LEGACY_QUEUES = frozenset({"live", "bulk", "segmented_live", "segmented_bulk",
                          "segmented_ords", "segmented_blocked", "segmented_receipts",
                          "segmented_evidence"})
RUNTIMES = {
    "ZKT_LEGACY": RuntimeContract(frozenset({"add_delivery", "ords_delivery"}),
                                   LEGACY_QUEUES, "LEGACY_DUAL"),
    "HIKVISION_V1": RuntimeContract(frozenset({"add_delivery", "hikvision_source"}),
                                    frozenset({"hikvision_source"}), "ADD"),
    "ZKT_JOURNAL_V1": RuntimeContract(frozenset({"capture", "storage_owner", "add_delivery"}),
                                      frozenset({"journal", "legacy_migration"}), "ADD"),
}


def runtime_contract(diagnostics: dict, family: str = "zkt") -> RuntimeContract:
    default = "HIKVISION_V1" if family == "hikvision" else "ZKT_LEGACY"
    profile = diagnostics.get("runtime_profile") or default
    if profile not in RUNTIMES or (profile == "HIKVISION_V1") != (family == "hikvision"):
        raise ValueError("RUNTIME_PROFILE_MISMATCH")
    contract = RUNTIMES[profile]
    if profile == "ZKT_JOURNAL_V1" and (
        diagnostics.get("schema_version") != 2 or diagnostics.get("journal_format") != 1
        or diagnostics.get("delivery_authority") != "ADD"
    ):
        raise ValueError("JOURNAL_CAPABILITIES_INCOMPLETE")
    return contract
