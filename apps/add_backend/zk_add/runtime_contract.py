"""Versioned runtime obligations; device reports cannot waive required workers."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeContract:
    workers: frozenset[str]
    queues: frozenset[str]
    delivery_authority: str
    auxiliary_workers: frozenset[str] = frozenset()


LEGACY_QUEUES = frozenset({"live", "bulk", "segmented_live", "segmented_bulk",
                          "segmented_ords", "segmented_blocked", "segmented_receipts",
                          "segmented_evidence"})
RUNTIMES = {
    "ZKT_LEGACY": RuntimeContract(frozenset({"add_delivery", "ords_delivery"}),
                                   LEGACY_QUEUES, "LEGACY_DUAL", frozenset({"storage_owner", "journal_add_delivery"})),
    "HIKVISION_V1": RuntimeContract(frozenset({"add_delivery", "hikvision_source"}),
                                    frozenset({"hikvision_source"}), "ADD"),
    "ZKT_JOURNAL_V1": RuntimeContract(frozenset({"capture", "storage_owner", "add_delivery"}),
                                      frozenset({"journal", "legacy_migration"}), "ADD",
                                      frozenset({"legacy_add_delivery", "legacy_ords_delivery"})),
}


def runtime_contract(diagnostics: dict, family: str = "zkt", *, allow_unknown_authority: bool = False) -> RuntimeContract:
    default = "HIKVISION_V1" if family == "hikvision" else "ZKT_LEGACY"
    profile = diagnostics.get("runtime_profile") or default
    if profile not in RUNTIMES or (profile == "HIKVISION_V1") != (family == "hikvision"):
        raise ValueError("RUNTIME_PROFILE_MISMATCH")
    contract = RUNTIMES[profile]
    if profile == "ZKT_JOURNAL_V1" and (
        diagnostics.get("schema_version") != 2 or diagnostics.get("journal_format") != 1
        or diagnostics.get("delivery_authority") not in ({"ADD", "UNKNOWN"} if allow_unknown_authority else {"ADD"})
    ):
        raise ValueError("JOURNAL_CAPABILITIES_INCOMPLETE")
    return contract


def worker_snapshot_fresh(worker: dict, uptime_seconds: int | None, runtime: RuntimeContract) -> bool:
    """An idle synchronous capture adapter has no task heartbeat to refresh.

    Its bounded snapshot read proves availability; the separate operation
    deadline prevents a stuck call from becoming healthy through that read.
    All task workers still require actual execution ticks.
    """
    if uptime_seconds is None:
        return False
    now = uptime_seconds * 1000
    tick = worker.get("last_activity_uptime_ms")
    model = worker.get("execution_model", "TASK")
    if model == "ON_DEMAND":
        if runtime != RUNTIMES["ZKT_JOURNAL_V1"] or worker.get("name") != "capture":
            return False
        tick = worker.get("sampled_uptime_ms")
        started = worker.get("operation_started_uptime_ms")
        if started is not None:
            if not isinstance(started, int) or isinstance(started, bool) or not -5000 <= now - started < 15000:
                return False
        elif worker.get("pending_requests") != 0:
            return False
    elif model != "TASK":
        return False
    return isinstance(tick, int) and not isinstance(tick, bool) and -5000 <= now - tick <= 90000


def journal_storage_status(diagnostics: dict, uptime_seconds: int | None) -> str | None:
    if diagnostics.get("runtime_profile") != "ZKT_JOURNAL_V1":
        return None
    storage = diagnostics.get("journal_storage") or {}
    sampled = storage.get("sampled_uptime_ms")
    if (uptime_seconds is None or not isinstance(sampled, int) or isinstance(sampled, bool)
            or not -5000 <= uptime_seconds * 1000 - sampled < 45000
            or storage.get("observed") is not True or storage.get("fresh") is not True):
        return "UNKNOWN"
    if storage.get("durability") in {"DEGRADED", "FULL"}:
        return storage["durability"]
    if storage.get("ready") is not True or storage.get("checkpoint_recovery_pending") is not False:
        return "DEGRADED"
    if storage.get("last_append_result") not in {None, "OK"}:
        return "FULL" if storage["last_append_result"] == "FULL" else "DEGRADED"
    return "HEALTHY" if storage.get("durability") == "HEALTHY" else "UNKNOWN"
