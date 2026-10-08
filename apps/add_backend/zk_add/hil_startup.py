"""Recognize a controlled writer's unfinished boot checks without making them healthy.

This helper consumes authenticated telemetry and an already validated reboot
control. It never establishes recovery, supplies missing counters, or handles
arbitrary resets. Rejected shapes remain ordinary unhealthy/unknown samples.
"""
from __future__ import annotations

from zk_add.hil_validation import HEALTH_FIELDS, RebootStartupTransition
from zk_add.runtime_contract import RUNTIMES, worker_snapshot_fresh

RUNTIME = RUNTIMES["ZKT_JOURNAL_V1"]
PHASES = {"NOT_STARTED", "STORAGE_WAIT", "OWNER_START", "RECOVERING", "TRANSPORT_START",
          "CHECKING_READER", "CAPTURE_START", "READY"}
STORAGE_COUNTERS = ("write_failures", "read_failures", "persistence_probe_failures",
    "persistence_probe_total_failures", "legacy_read_faults", "legacy_append_faults", "legacy_retire_faults")
STORAGE_ERRORS = ("error_code", "upgrade_error", "persistence_probe_error", "legacy_error_code",
    "local_failure_source", "error_operation", "persistence_probe_operation", "legacy_error_operation")
WORKER_COUNTERS = {"storage_owner": ("failures", "refusals"), "capture": ("failures", "timeouts"),
                  "add_delivery": ("failures", "consecutive_failures", "timeouts", "refusals")}


def integer(value):
    return type(value) is int and value >= 0


def zero(value):
    return type(value) is int and value == 0


def fresh(value, tick):
    return integer(value) and -5000 <= tick - value < 45000


def classify_startup(row, sample, details, control, test, recovered):
    try:
        return _classify_startup(row, sample, details, control, test, recovered)
    except (AttributeError, KeyError, TypeError, ValueError):
        # Malformed retained diagnostics never waive the ordinary evaluator.
        return None


def _classify_startup(row, sample, details, control, test, recovered):
    """Return an explicit transition plus its raw diagnostic basis, or None.

    Missing pre-probe counters remain unobserved. Any reported fault, retry,
    sticky incident or unavailable mandatory worker counter prevents this
    classification, including failures that later appear to recover.
    """
    if (test is None or recovered is None or not isinstance(control, dict)
            or control.get("recovery_telemetry_id") != recovered.telemetry_id
            or sample.boot_id != test.boot_after or recovered.boot_id != test.boot_after
            or not test.started_at <= sample.diagnostics_at <= sample.recorded_at < recovered.recorded_at <= test.recovered_at
            or recovered.reboot_startup is not None
            or any(getattr(recovered, name) is not True for name in HEALTH_FIELDS)
            or not integer(row.uptime_seconds)
            or row.uptime_seconds > (sample.diagnostics_at - test.started_at).total_seconds() + 5
            or any(error not in {"DELIVERY_AUTHORITY_UNVERIFIED", "QUALIFIED_READER_PROOF_PENDING"}
                   for error in details["errors"])):
        return None
    payload = row.payload
    diag = payload.get("diagnostics")
    if not isinstance(diag, dict):
        return None
    storage, journal, runtime, terminal = (diag.get("storage"), diag.get("journal_storage"),
                                         diag.get("journal_runtime"), payload.get("zkt"))
    if not all(isinstance(value, dict) for value in (storage, journal, runtime, terminal)):
        return None
    tick, phase = diag.get("sampled_uptime_ms"), runtime.get("phase")
    if "QUALIFIED_READER_PROOF_PENDING" in details["errors"] and (
            details.get("qualified_reader_pending") is not True
            or runtime.get("writer_ready") is not False or phase == "READY"):
        return None
    if (not integer(tick) or phase not in PHASES
            or diag.get("delivery_authority") not in {"ADD", "UNKNOWN"}
            or runtime.get("delivery_authority") != diag.get("delivery_authority")
            or any(type(runtime.get(key)) is not bool for key in ("observed", "reader_ready", "writer_ready"))
            or not zero(runtime.get("failures"))
            or any(not integer(runtime.get(key)) or runtime[key] > 1
                for key in ("storage_starts", "delivery_starts", "capture_starts", "proof_attempts"))
            or not integer(runtime.get("start_attempts")) or runtime["start_attempts"] > 3
            or runtime.get("compatibility") not in (None, "")
            or (phase == "NOT_STARTED") != (runtime["observed"] is False)
            or (phase == "NOT_STARTED" and (runtime["reader_ready"] or runtime["writer_ready"]))
            or (runtime["writer_ready"] and phase != "READY")
            or (runtime["observed"] and not fresh(runtime.get("sampled_uptime_ms"), tick))
            or any(storage.get(key) not in (None, "", 0) for key in STORAGE_ERRORS)
            or any(storage.get(key) is not None and not zero(storage[key]) for key in STORAGE_COUNTERS)
            or storage.get("fault_class") not in (None, "NONE")
            or journal.get("hil_reboot_persistence_incident") is not False
            or journal.get("last_append_result") not in (None, "OK")
            or journal.get("last_failure_operation") not in (None, "")
            or any(journal.get(key) not in (None, 0) for key in ("last_filesystem_error", "last_nvs_error"))
            or any(type(journal.get(key)) is not bool for key in
                   ("observed", "fresh", "ready", "checkpoint_recovery_pending"))
            or not zero(journal.get("pending_appends"))
            or journal.get("checkpoint_recovery_pending") is not False):
        # A damaged retirement checkpoint is a recovery incident, not ordinary
        # boot initialization, even if automatic recovery later succeeds.
        return None
    if journal["fresh"]:
        if (journal["observed"] is not True or not fresh(journal.get("sampled_uptime_ms"), tick)
                or journal.get("durability") != ("HEALTHY" if journal["ready"] else "DEGRADED")):
            return None
    elif (journal["observed"] is not False or journal["ready"] is not False
            or journal.get("durability") != "UNKNOWN"):
        return None
    if (storage.get("durability") != journal["durability"] and not
            (journal["durability"] == "HEALTHY" and storage.get("durability") == "UNKNOWN"
             and (storage.get("persistence_verified") is False or storage.get("recovery_complete") is False))):
        return None
    if any(type(storage.get(key)) is not bool for key in ("persistence_verified", "recovery_complete")):
        return None
    if not journal["ready"] and (storage["persistence_verified"] or storage["recovery_complete"]):
        return None
    if phase == "READY" and (runtime["writer_ready"] is not True or runtime["reader_ready"] is not True
            or not journal["ready"] or diag["delivery_authority"] != "ADD"):
        return None
    workers = diag.get("workers")
    if not isinstance(workers, list) or not all(isinstance(item, dict) for item in workers):
        return None
    by_name = {item.get("name"): item for item in workers if isinstance(item.get("name"), str)}
    if (len(by_name) != len(workers)
            or not RUNTIME.workers <= by_name.keys() <= RUNTIME.workers | RUNTIME.auxiliary_workers):
        return None
    for name, worker in by_name.items():
        if (not zero(worker.get("restart_count"))
                or any(worker.get(key) is not None and not zero(worker[key])
                       for key in ("restart_attempts", "failures", "timeouts", "refusals", "consecutive_failures"))
                or any(not zero(worker.get(key)) for key in WORKER_COUNTERS.get(name, ()))
                or worker.get("state") not in {"UNKNOWN", "STOPPED", "WAITING_RESOURCE", "RUNNING", "WAITING_NETWORK"}):
            return None
        if worker["state"] in {"RUNNING", "WAITING_NETWORK"}:
            if not worker_snapshot_fresh(worker, row.uptime_seconds, RUNTIME):
                return None
        elif name == "storage_owner":
            if journal["ready"] or (worker["state"] == "UNKNOWN") != (journal["observed"] is False):
                return None
        elif name == "capture":
            if runtime["writer_ready"]:
                return None
        elif name == "add_delivery":
            if runtime["delivery_starts"] > 0 and worker["state"] != "UNKNOWN":
                return None
        # An executing operation is never excused from its normal deadline.
        started = worker.get("operation_started_uptime_ms")
        if started is not None and (not integer(started) or not -5000 <= tick - started < 15000):
            return None
    queues = diag.get("queues")
    if not isinstance(queues, list) or not all(isinstance(item, dict) for item in queues):
        return None
    queue_names = {item.get("name"): item for item in queues if isinstance(item.get("name"), str)}
    if len(queue_names) != len(queues) or not RUNTIME.queues <= queue_names.keys():
        return None
    for name in RUNTIME.queues:
        item = queue_names[name]
        if item.get("count_known") is True:
            if not integer(item.get("records")):
                return None
        elif (item.get("count_known") is not False or item.get("records") is not None
                or item.get("count_reason") not in {"STALE_OWNER", "NONEMPTY_OR_UNVERIFIED", "UNVERIFIED_MIGRATION"}):
            return None
    if (terminal.get("serial") not in ("", sample.target.terminal_serial)
            or type(terminal.get("online")) is not bool
            or terminal.get("connection_state") not in {"BOOTING", "UNKNOWN", "DISCOVERING", "CONNECTING", "RECOVERING", "ONLINE"}
            or not zero(terminal.get("consecutive_failures"))
            or not zero(terminal.get("flap_count_15m"))):
        return None
    unobserved = []
    for name in ("write_failures", "read_failures", "source_generation", "committed_cursor"):
        if getattr(sample, name) is None:
            unobserved.append(name)
    if ((sample.source_generation is None) != (sample.committed_cursor is None)
            or (sample.write_failures is None) != (sample.read_failures is None)):
        return None
    if sample.write_failures is None and (storage["persistence_verified"] or storage["recovery_complete"]
            or any(storage.get(key) is not None for key in STORAGE_COUNTERS)):
        return None
    if (sample.source_generation is None and terminal["online"] is False
            and terminal["connection_state"] in {"BOOTING", "UNKNOWN", "DISCOVERING", "CONNECTING"}
            and sample.source_count == 0):
        unobserved.append("source_count")
    elif sample.source_count is None:
        return None
    pending = tuple(name for name in HEALTH_FIELDS if getattr(sample, name) is not True)
    if not pending:
        return None
    transition = RebootStartupTransition(command_id=test.command_id,
        recovery_telemetry_id=recovered.telemetry_id, phase=phase,
        pending_checks=pending, unobserved_fields=tuple(unobserved))
    # Keep the actual reported values next to the interpretation; no copied
    # employee information or arbitrary log text is added to the seal.
    facts = {"storage": storage, "journal_storage": journal, "journal_runtime": runtime,
             "workers": workers, "queues": queues, "terminal": {key: terminal.get(key) for key in
                 ("serial", "online", "connection_state", "attendance_count", "consecutive_failures", "flap_count_15m")},
             "delivery_authority": diag["delivery_authority"],
             "source_generation": diag.get("source_generation"),
             "committed_source_cursor": diag.get("committed_source_cursor")}
    return transition, facts
