from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Annotated, Any, Literal

from zk_add.hikvision_clock import HikvisionClockSample
from zk_add.capture_latency import CaptureLatencyHistogram

from pydantic import (
    BaseModel, ConfigDict, Field, SecretStr, StrictInt, ValidationError, ValidationInfo, field_validator,
    model_validator,
)


class LoginRequest(BaseModel):
    username: str
    password: str


class StepUpRequest(BaseModel):
    password: str


class OnboardRequest(BaseModel):
    firmware_family: Literal["zkt", "hikvision"] = "zkt"
    hardware_id: str = Field(min_length=17, max_length=17)
    zone_id: str = Field(min_length=1, max_length=100)
    zone_name: str = Field(min_length=1, max_length=255)
    device_id: str = Field(min_length=1, max_length=120)
    firmware_version: str = Field(min_length=1, max_length=80)
    expected_serial: str | None = Field(default=None, max_length=120)

    @field_validator("hardware_id")
    @classmethod
    def validate_mac(cls, value: str) -> str:
        compact = "".join(character for character in value.lower() if character in "0123456789abcdef")
        if len(compact) != 12:
            raise ValueError("hardware_id must be a 6-byte Wi-Fi MAC address")
        return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


class Envelope(BaseModel):
    schema_version: str = "1"
    message_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    connector_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    boot_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    seq: int = Field(ge=0, le=2**63 - 1)
    sent_at: datetime
    type: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    payload: dict[str, Any] = Field(default_factory=dict)


UPPER_CODE = r"^[A-Z_]{1,40}$"


class OtaStorageException(BaseModel):
    # ZKT 2.7.7 and later: the confirm-first storage exception (owner decision
    # D3 (a), 21 October 2026). On an unconfirmed boot: the gate's state
    # (EVALUATING, REFUSED, ELIGIBLE, CONFIRMING), its reason and for how long;
    # once confirmed that way: the record's state (CONFIRMED, ROUND_DONE,
    # ROUND_SHORT with the round's outcome, UNREPAIRED, UNHEALTHY, ABANDONED)
    # and its bounded restarts. Every field is optional and codes are text, so
    # a later firmware never fails validation.
    state: str | None = Field(default=None, pattern=UPPER_CODE)
    reason: str | None = Field(default=None, pattern=UPPER_CODE)
    since_seconds: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    restarts: int | None = Field(default=None, ge=0, le=255)
    round_boots: int | None = Field(default=None, ge=0, le=255)
    crashes: int | None = Field(default=None, ge=0, le=255)


class OtaHeartbeatPayload(BaseModel):
    """Bounded OTA runtime evidence emitted by Zone Lite heartbeats."""

    running_version: str | None = Field(default=None, min_length=1, max_length=80)
    running_partition: Literal["factory", "ota_0", "ota_1"] | None = None
    image_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    capable: bool = False
    secure_boot: bool = False
    rollback_enabled: bool = False
    partition_layout: str = Field(default="", max_length=80)
    state: str = Field(default="", max_length=40)
    target_version: str = Field(default="", max_length=80)
    bytes_written: int = Field(default=0, ge=0, le=2**63 - 1)
    image_size: int = Field(default=0, ge=0, le=2**63 - 1)
    last_error: str = Field(default="", max_length=64)
    boot_health_checks: int = Field(default=0, ge=0, le=2**32 - 1)
    boot_health_last_ready: bool = False
    progress_attempts: int = Field(default=0, ge=0, le=2**32 - 1)
    progress_successes: int = Field(default=0, ge=0, le=2**32 - 1)
    progress_last_http_status: int = Field(default=0, ge=-1, le=599)
    # ZKT 2.7.1 and later: new-image confirmation progress. Accountable time
    # counts toward the 900 s budget; paused time is a healthy-looking image
    # waiting on the site; elapsed is bounded by a hard cap.
    boot_confirm_active_seconds: int | None = Field(default=None, ge=0, le=2**32 - 1)
    boot_confirm_paused_seconds: int | None = Field(default=None, ge=0, le=2**32 - 1)
    boot_confirm_elapsed_seconds: int | None = Field(default=None, ge=0, le=2**32 - 1)
    # ZKT 2.7.7 and later: the confirm-first storage exception, how the
    # running image was confirmed, and the boot-health terms that fail while
    # it is unconfirmed. Unreadable values are dropped, never the heartbeat.
    storage_exception: OtaStorageException | None = None
    confirm_reason: str | None = Field(default=None, pattern=UPPER_CODE)
    boot_health_blockers: list[Annotated[str, Field(pattern=UPPER_CODE)]] = Field(default_factory=list, max_length=32)

    @field_validator("storage_exception", mode="before")
    @classmethod
    def readable_storage_exception(cls, value: Any) -> OtaStorageException | None:
        try:
            return OtaStorageException.model_validate(value) if value is not None else None
        except ValidationError:
            return None

    @field_validator("confirm_reason", mode="before")
    @classmethod
    def readable_confirm_reason(cls, value: Any) -> str | None:
        return value if isinstance(value, str) and re.fullmatch(UPPER_CODE, value) else None

    @field_validator("boot_health_blockers", mode="before")
    @classmethod
    def readable_blockers(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str) and re.fullmatch(UPPER_CODE, item)][:32]


class QueueDiagnostics(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    bytes: int | None = Field(default=None, ge=0)
    records: int | None = Field(default=None, ge=0)
    count_known: bool = False
    count_reason: Literal["VERIFIED_EMPTY", "NONEMPTY_OR_UNVERIFIED", "PENDING_APPEND",
                          "STALE_OWNER", "UNVERIFIED_MIGRATION"] | None = None
    oldest_pending_age_seconds: int | None = Field(default=None, ge=0)
    last_progress_uptime_ms: int | None = Field(default=None, ge=0)


class WorkerDiagnostics(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    state: Literal["RUNNING", "WAITING_NETWORK", "WAITING_RESOURCE", "STOPPED", "FAULT", "UNKNOWN"]
    last_activity_uptime_ms: int | None = Field(default=None, ge=0)
    operation: str | None = Field(default=None, max_length=80)
    restart_count: int | None = Field(default=None, ge=0)
    restart_attempts: int | None = Field(default=None, ge=0)
    last_progress_uptime_ms: int | None = Field(default=None, ge=0)
    operation_started_uptime_ms: int | None = Field(default=None, ge=0)
    pending_requests: int | None = Field(default=None, ge=0)
    execution_model: Literal["TASK", "ON_DEMAND"] = "TASK"
    sampled_uptime_ms: int | None = Field(default=None, ge=0)
    completed_operations: int | None = Field(default=None, ge=0)
    failures: int | None = Field(default=None, ge=0)
    consecutive_failures: int | None = Field(default=None, ge=0)
    timeouts: int | None = Field(default=None, ge=0)
    refusals: int | None = Field(default=None, ge=0)
    max_operation_ms: int | None = Field(default=None, ge=0)
    packet_commit_latency_ms: CaptureLatencyHistogram | None = None
    fragment_commit_latency_ms: CaptureLatencyHistogram | None = None


class JournalStorageDiagnostics(BaseModel):
    observed: bool
    fresh: bool
    ready: bool
    durability: Literal["HEALTHY", "DEGRADED", "FULL", "UNKNOWN"]
    checkpoint_recovery_pending: bool
    hil_reboot_persistence_incident: bool | None = Field(default=None, strict=True)
    mailbox_capacity: int = Field(ge=1, le=32)
    mailbox_high_watermark: int = Field(ge=0, le=32)
    pending_appends: int = Field(ge=0, le=32)
    sampled_uptime_ms: int | None = Field(default=None, ge=0)
    segments: int | None = Field(default=None, ge=0, le=256)
    last_append_result: Literal["OK", "EMPTY", "FULL", "IO", "CORRUPT", "STALE", "INVALID", "UNCERTAIN", "UNKNOWN"] | None = None
    last_append_uptime_ms: int | None = Field(default=None, ge=0)
    last_failure_operation: str | None = Field(default=None, max_length=80)
    last_filesystem_error: int | None = None
    last_nvs_error: int | None = None


PRINTABLE_TEXT = r"^[\x20-\x7e]+$"


class StorageInventoryFile(BaseModel):
    # One file of the store and the bytes the filesystem could not read back,
    # with the first failing offset and errno; the read-error fields are only
    # present for a file with unreadable bytes.
    name: str = Field(max_length=32, pattern=PRINTABLE_TEXT)
    bytes: int = Field(ge=0, le=0xFFFFFFFF)
    unreadable_bytes: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    first_error: int | None = None
    first_error_offset: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    # ZKT 2.7.7 and later: the spans that would not read back one at a time,
    # the last one's offset, an order-free digest of their indices (compared
    # with the spans the boot repair holds), and bytes lost any other way.
    unreadable_spans: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    last_error_offset: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    span_digest: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    other_unreadable: bool | None = None


class StorageInventory(BaseModel):
    # ZKT 2.7.4 and later: a read-only scan of the store, once per boot
    # (13FL could not confirm 2.7.3 and nothing said which file was damaged).
    # Unreadable files are listed first, then the largest, at most 16.
    complete: bool
    files: int = Field(ge=0, le=0xFFFFFFFF)
    bytes: int = Field(ge=0)
    unreadable_files: int = Field(ge=0, le=0xFFFFFFFF)
    unreadable_bytes: int = Field(ge=0)
    error: int | None = None
    entries: list[StorageInventoryFile] = Field(default_factory=list, max_length=16)


class StorageRepairCheck(BaseModel):
    # What SPIFFS_check reported over every pass of the boot repair.
    passes: int | None = Field(default=None, ge=0, le=64)
    fix_index: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    fix_lookup: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    delete_orphaned_index: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    delete_page: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    delete_bad_file: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    deleted_live_files: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    errors: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    last_error: int | None = Field(default=None, ge=-(2**31), le=2**31 - 1)


class StorageRepairFile(BaseModel):
    name: str = Field(max_length=32, pattern=PRINTABLE_TEXT)
    class_: str | None = Field(default=None, alias="class", max_length=16, pattern=r"^[a-z_]{1,16}$")
    size: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    gaps: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    unreadable_bytes: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    spans_rewritten: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    # After a run deferred for room: spans SPIFFS still cannot read, and the first.
    blocked_spans: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    blocked_offset: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    # ZKT 2.7.7 and later: the last blocked span and an order-free digest of them.
    blocked_last: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    blocked_digest: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    actions: list[Annotated[str, Field(pattern=UPPER_CODE)]] = Field(default_factory=list, max_length=8)

    model_config = ConfigDict(populate_by_name=True)


class StorageRepairReclaim(BaseModel):
    # ZKT 2.7.6 and later: the repair's last resort for a store with no room,
    # blocks reformatted in place with their kept pages in a redo area past
    # the last partition, sent every boot. `area` is that area's state;
    # `trigger` and the block counts describe a run that ended without room
    # (the exposure histogram: blocks a round may take now, on an unconfirmed
    # image, only on a confirmed one, and the ones kept out). Every field is
    # optional and codes are text, so a later firmware never fails validation.
    area: str | None = Field(default=None, pattern=UPPER_CODE)
    trigger: str | None = Field(default=None, max_length=24, pattern=r"^[a-z_]{1,24}$")
    short: str | None = Field(default=None, max_length=24, pattern=r"^[a-z_]{1,24}$")
    rounds: int | None = Field(default=None, ge=0, le=64)
    blocks: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    pages_freed: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    pages_kept: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    erased_after: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    redone: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    sources: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    settled: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    reported: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    records_open: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    failed: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    held_mismatch: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    ms: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    candidates: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    unconfirmed_blocks: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    custody_blocks: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    index_blocks: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    excluded_blocks: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    low_magic_blocks: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    # A foreign area (written by something else; never claimed): how many of
    # its 96 sectors, the first one, and the CRC-32 of that sector.
    area_foreign_sectors: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    area_first_foreign: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    area_foreign_crc: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    # ZKT 2.7.7 and later (diagnostics): the pages the round considered short
    # and its target; the pages the confirmed rules would drop from the
    # custody blocks and from the blocks any image may take; custody blocks
    # left out for an unheld custody file; the excluded blocks by the one
    # reason their excluding pages share (or mixed) and the object excluding
    # the most; the store as the first survey found it; queue spans waiting
    # for byte sources; the redo area's program/erase test; the OTA floor.
    want: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    target: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    custody_gain: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    unconfirmed_gain: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    held_out: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    excluded_hidden: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    excluded_unwindowed: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    excluded_last_page: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    excluded_index_flag: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    excluded_busy: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    excluded_mixed: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    excluded_file: str | None = Field(default=None, max_length=32, pattern=PRINTABLE_TEXT)
    excluded_file_blocks: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    erased_at_mount: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    free_blocks_at_mount: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    waiting: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    area_test: str | None = Field(default=None, pattern=UPPER_CODE)
    ota_floor: bool | None = None

    @field_validator("excluded_file", mode="before")
    @classmethod
    def readable_excluded_file(cls, value: Any) -> str | None:
        return value if isinstance(value, str) and len(value) <= 32 and re.fullmatch(PRINTABLE_TEXT, value) else None


class StorageRepairDiagnostics(BaseModel):
    # ZKT 2.7.5 and later: the custody-first SPIFFS repair that runs at every
    # boot before the store is mounted. Codes are text, not literals, so a
    # later firmware state never fails validation; an unreadable object is
    # dropped rather than rejecting the heartbeat that carries it.
    result: str | None = Field(default=None, pattern=UPPER_CODE)
    mode: str | None = Field(default=None, pattern=UPPER_CODE)
    phase: str | None = Field(default=None, pattern=UPPER_CODE)
    previous_phase: str | None = Field(default=None, pattern=UPPER_CODE)
    previous_boot_phase: str | None = Field(default=None, pattern=UPPER_CODE)
    previous_boot_deadline: bool | None = None
    previous_boot_returned: bool | None = None
    generation: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    incomplete: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    failures: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    level: int | None = Field(default=None, ge=0, le=8)
    check_skipped: bool | None = None
    mirror: bool | None = None
    commands_damaged: bool | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    check_ms: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    budget_bytes: int | None = Field(default=None, ge=0, le=2**40)
    files: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    files_held: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    bytes_held: int | None = Field(default=None, ge=0, le=2**40)
    files_unheld_custody: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    gaps: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    unreadable_bytes: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    records_reported: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    records_pending: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    records_dropped: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    spans_rewritten: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    index_rewritten: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    duplicates_removed: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    orphan_index_removed: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    blocks_compacted: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    reserve_pages: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    # A run deferred for room: pages its work needs, and the erased pages there were.
    room_needed_pages: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    room_erased_pages: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    sizes_recovered: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    strays_removed: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    files_superseded: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    lookup_fixed: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    tails_terminated: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    files_restored: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    files_rebuilt: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    cursors_reset: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    expendables_removed: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    settled_removed: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    interrupted_losses: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    used_before: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    used_after: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    total_bytes: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    nvs_free_entries_before: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    nvs_free_entries_after: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    # Free entries less the page NVS keeps for its own GC (what writes can use).
    nvs_available_entries_before: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    nvs_available_entries_after: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    internal_largest_before: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    internal_largest_after: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    stack_free_bytes: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    log_lines_dropped: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    error_stage: str | None = Field(default=None, max_length=24, pattern=r"^[a-z_]{1,24}$")
    error_code: int | None = Field(default=None, ge=-(2**31), le=2**31 - 1)
    check: StorageRepairCheck | None = None
    entries: list[StorageRepairFile] = Field(default_factory=list, max_length=8)
    # ZKT 2.7.6 and later: runs of the repair this boot (a round of the last
    # resort is followed by another), and the last resort itself; an
    # unreadable reclaim object is dropped rather than the whole report.
    runs: int | None = Field(default=None, ge=0, le=64)
    reclaim: StorageRepairReclaim | None = None
    # ZKT 2.7.7 and later: a manifest still active when the boot's last run
    # ended; whether the survey saw a journal file's header.
    manifest_active: bool | None = None
    journal_names: bool | None = None

    @field_validator("reclaim", mode="before")
    @classmethod
    def readable_reclaim(cls, value: Any) -> StorageRepairReclaim | None:
        try:
            return StorageRepairReclaim.model_validate(value) if value is not None else None
        except ValidationError:
            return None


class StorageDiagnostics(BaseModel):
    local_failure_source: str | None = Field(default=None, max_length=80)
    # ZKT 2.7.1 and later: the latest incident of a latched LOCAL_FAILURE, and
    # incidents and verified recoveries counted since boot.
    local_failure_last_source: str | None = Field(default=None, max_length=80)
    local_failures: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    local_failure_recoveries: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    upgrade_contract: str | None = Field(default=None, max_length=160)
    upgrade_error: str | None = Field(default=None, max_length=80)
    upgrade_ready: bool | None = None
    total_bytes: int | None = Field(default=None, ge=0)
    used_bytes: int | None = Field(default=None, ge=0)
    admission_reserve_bytes: int | None = Field(default=None, ge=0)
    write_failures: int | None = Field(default=None, ge=0)
    read_failures: int | None = Field(default=None, ge=0)
    legacy_read_faults: int | None = Field(default=None, ge=0, le=7)
    legacy_append_faults: int | None = Field(default=None, ge=0, le=7)
    legacy_retire_faults: int | None = Field(default=None, ge=0, le=7)
    legacy_read_recoveries: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    legacy_write_recoveries: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    legacy_error_queue: Literal["add_live", "add_bulk", "ords_pending", "identity_blocked",
                                "ords_quarantine", "add_quarantine", "add_quarantine_backup"] | None = None
    legacy_error_operation: Literal["legacy_read", "legacy_append", "legacy_retire"] | None = None
    legacy_error_code: int | None = None
    persistence_probe_failures: int | None = Field(default=None, ge=0)
    persistence_probe_total_failures: int | None = Field(default=None, ge=0)
    persistence_probe_error: int | None = None
    persistence_probe_operation: str | None = Field(default=None, max_length=80)
    # ZKT 2.7.4 and later: probe files replaced because they could not be
    # reopened for writing, and the store inventory. An unreadable inventory
    # is dropped rather than rejecting the heartbeat that carries it.
    persistence_probe_repairs: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    inventory: StorageInventory | None = None
    # ZKT 2.7.5 and later: the boot storage repair; dropped when unreadable.
    repair: StorageRepairDiagnostics | None = None
    fault_class: Literal["NONE", "LOCK_CONTENTION", "CAPACITY", "MISSING_FILE", "CORRUPTION",
                         "ALLOCATION", "PERSISTENCE", "RECOVERY"] | None = None
    rejected_optional_writes: int | None = Field(default=None, ge=0)
    live_commit_p99_ms: int | None = Field(default=None, ge=0)
    recovery_cursor: int | None = Field(default=None, ge=0)
    durability: Literal["HEALTHY", "DEGRADED", "FULL", "UNKNOWN"] = "UNKNOWN"
    persistence_verified: bool = False
    recovery_complete: bool = False
    error_operation: str | None = Field(default=None, max_length=80)
    error_code: int | None = None
    # ZKT 2.7.1 and later: the file a failed local write was for.
    error_file: str | None = Field(default=None, max_length=32, pattern=PRINTABLE_TEXT)
    # ZKT 2.7.1 and later: faults recovered this boot after a fresh audit of
    # every queue, room for live attendance and a passing persistence probe.
    recoveries: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    recovered_error_operation: str | None = Field(default=None, max_length=80)
    recovered_error_code: int | None = None
    recovered_uptime_ms: int | None = Field(default=None, ge=0)
    recovered_error_file: str | None = Field(default=None, max_length=32, pattern=PRINTABLE_TEXT)

    @field_validator("inventory", mode="before")
    @classmethod
    def readable_inventory(cls, value: Any) -> StorageInventory | None:
        try:
            return StorageInventory.model_validate(value) if value is not None else None
        except ValidationError:
            return None

    @field_validator("repair", mode="before")
    @classmethod
    def readable_repair(cls, value: Any) -> StorageRepairDiagnostics | None:
        try:
            return StorageRepairDiagnostics.model_validate(value) if value is not None else None
        except ValidationError:
            return None

    @model_validator(mode="after")
    def retain_legacy_recovery_hold(self) -> "StorageDiagnostics":
        if (self.legacy_read_faults or self.legacy_append_faults or self.legacy_retire_faults
                or self.legacy_error_code):
            if self.durability != "FULL":
                self.durability = "DEGRADED"
            self.recovery_complete = False
            self.persistence_verified = False
        return self


class AllocationFailure(BaseModel):
    bytes: int = Field(ge=0, le=0xFFFFFFFF)
    caps: int = Field(ge=0, le=0xFFFFFFFF)


class StackHeadroom(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    free_bytes: int = Field(ge=0)


class MemoryDiagnostics(BaseModel):
    internal_free_bytes: int = Field(ge=0)
    internal_largest_block_bytes: int = Field(ge=0)
    # ZKT 2.7.1 and later: every capability pool, its minimum since boot, the
    # allocation failures and the tasks with the least stack headroom.
    internal_min_free_bytes: int | None = Field(default=None, ge=0)
    dma_free_bytes: int | None = Field(default=None, ge=0)
    dma_largest_block_bytes: int | None = Field(default=None, ge=0)
    dma_min_free_bytes: int | None = Field(default=None, ge=0)
    psram_free_bytes: int | None = Field(default=None, ge=0)
    psram_largest_block_bytes: int | None = Field(default=None, ge=0)
    psram_min_free_bytes: int | None = Field(default=None, ge=0)
    alloc_failures: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    alloc_failures_dropped: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    recent_alloc_failures: list[AllocationFailure] = Field(default_factory=list, max_length=4)
    stacks: list[StackHeadroom] = Field(default_factory=list, max_length=32)


HEX_WORD = r"^0x[0-9a-f]{8}$"


class BootPanicCore(BaseModel):
    # ZKT 2.7.3 and later: a core's task at the panic and, when the core
    # entered the panic handler itself, its PC and the backtrace ESP-IDF
    # printed (at most eight entries; corrupted when the walk stopped early).
    core: int = Field(ge=0, le=7)
    task: str | None = Field(default=None, max_length=32, pattern=PRINTABLE_TEXT)
    pc: str | None = Field(default=None, pattern=HEX_WORD)
    backtrace: list[str] = Field(default_factory=list, max_length=8)
    corrupted: bool = False

    @field_validator("backtrace")
    @classmethod
    def backtrace_addresses(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(HEX_WORD, entry) for entry in value):
            raise ValueError("backtrace entries are 32-bit hex addresses")
        return value


class BootPanic(BaseModel):
    # Present only when the previous boot ended in a panic or a failed
    # ESP_ERROR_CHECK whose abort never reached the panic handler.
    exception: str | None = Field(default=None, pattern=r"^[A-Z_]{1,32}$")
    address: str | None = Field(default=None, pattern=HEX_WORD)
    core: int | None = Field(default=None, ge=0, le=7)
    task: str | None = Field(default=None, max_length=32, pattern=PRINTABLE_TEXT)
    detail: str | None = Field(default=None, max_length=128, pattern=PRINTABLE_TEXT)
    # ZKT 2.7.3 and later: ESP-IDF's panic reason ("Cache error",
    # "LoadProhibited"), the cache error's first line, the address a load,
    # store, fetch or cache access failed on, and each core's trace.
    reason: str | None = Field(default=None, max_length=40)
    cache_error: str | None = Field(default=None, max_length=80)
    fault_address: str | None = None
    cores: list[BootPanicCore] | None = None

    # The 2.7.3 trace explains a crash; it must never cost the heartbeat that
    # carries it (3FL, 12 Oct: 560 heartbeats rejected over one panic field).
    # An unreadable part is dropped instead.
    @field_validator("reason", "cache_error", mode="before")
    @classmethod
    def readable_text(cls, value: Any, info: ValidationInfo) -> str | None:
        limit = 40 if info.field_name == "reason" else 80
        if isinstance(value, str) and len(value) <= limit and re.fullmatch(PRINTABLE_TEXT, value):
            return value
        return None

    @field_validator("fault_address", mode="before")
    @classmethod
    def readable_address(cls, value: Any) -> str | None:
        return value if isinstance(value, str) and re.fullmatch(HEX_WORD, value) else None

    @field_validator("cores", mode="before")
    @classmethod
    def readable_cores(cls, value: Any) -> list[BootPanicCore] | None:
        if not isinstance(value, list):
            return None
        cores = []
        for row in value[:8]:
            try:
                cores.append(BootPanicCore.model_validate(row))
            except ValidationError:
                continue
        return cores or None

    @field_validator("core", mode="before")
    @classmethod
    def unknown_core(cls, value):
        # The firmware stores the panic's core as an unsigned word. A value no
        # core can have is unknown; it must not reject the heartbeat carrying
        # the rest of the crash report (3FL, 12 Oct: 560 heartbeats rejected).
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 7:
            return value
        return None


class BootReport(BaseModel):
    # ZKT 2.7.1 and later: why the ESP last reset and how the previous boot
    # ended. Counts run from the last power-on; previous_* and panic are
    # absent when RTC memory kept nothing trustworthy from the previous boot.
    reset_reason: str = Field(pattern=r"^[A-Z_]{1,32}$")
    boot_count: int = Field(ge=0, le=0xFFFFFFFF)
    panic_count: int = Field(ge=0, le=0xFFFFFFFF)
    task_wdt_count: int = Field(ge=0, le=0xFFFFFFFF)
    int_wdt_count: int = Field(ge=0, le=0xFFFFFFFF)
    other_wdt_count: int = Field(ge=0, le=0xFFFFFFFF)
    brownout_count: int = Field(ge=0, le=0xFFFFFFFF)
    last_invalid_partition: str | None = Field(default=None, max_length=16, pattern=PRINTABLE_TEXT)
    previous_uptime_seconds: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    previous_stage: str | None = Field(default=None, pattern=r"^[A-Z_]{1,32}$")
    panic: BootPanic | None = None


class JournalRuntimeDiagnostics(BaseModel):
    observed: bool
    phase: Literal["NOT_STARTED", "DISABLED", "SECURITY_HOLD", "BINDING_HOLD", "STORAGE_WAIT",
                   "OWNER_START", "RECOVERING", "TRANSPORT_START", "CHECKING_READER", "READER_HOLD",
                   "CAPTURE_START", "WRITER_DISABLED", "READY", "STALLED", "QUIESCING",
                   "AUTHORITY_HOLD", "BRIDGE_VALIDATION", "UNKNOWN"]
    reader_ready: bool
    writer_ready: bool
    delivery_authority: Literal["UNKNOWN", "LEGACY", "ADD"] | None = None
    start_attempts: int = Field(ge=0, le=0xFFFFFFFF)
    storage_starts: int = Field(ge=0, le=0xFFFFFFFF)
    delivery_starts: int = Field(ge=0, le=0xFFFFFFFF)
    capture_starts: int = Field(ge=0, le=0xFFFFFFFF)
    proof_attempts: int = Field(ge=0, le=0xFFFFFFFF)
    failures: int = Field(ge=0, le=0xFFFFFFFF)
    sampled_uptime_ms: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    last_progress_uptime_ms: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    compatibility: str | None = Field(default=None, max_length=64)


class SourceBoundaryDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    observed: bool
    verified: bool
    migration_certified: Literal[False]
    result: Literal["OK", "EMPTY", "FULL", "IO", "CORRUPT", "STALE", "INVALID", "UNCERTAIN", "NOT_OBSERVED", "UNKNOWN"]
    next_ordinal: int | None = Field(default=None, ge=0, le=2**31 - 1)
    record_size: Literal[0, 8, 16, 40] | None = None
    capture_epoch: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    writer_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    terminal_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    anchor_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="before")
    @classmethod
    def exact_types(cls, value):
        if isinstance(value, dict) and (
            type(value.get("schema_version")) is not int or value.get("migration_certified") is not False
            or (value.get("record_size") is not None and type(value["record_size"]) is not int)
        ):
            raise ValueError("SOURCE_BOUNDARY_TYPES")
        return value

    @model_validator(mode="after")
    def complete_binding(self):
        fields = (self.next_ordinal, self.record_size, self.capture_epoch,
                  self.writer_digest, self.terminal_digest, self.anchor_digest)
        if self.verified:
            if (not self.observed or self.result != "OK" or any(value is None for value in fields)
                    or self.capture_epoch == "0" * 32 or self.writer_digest == "0" * 64
                    or self.terminal_digest == "0" * 64
                    or (self.next_ordinal == 0 and (self.record_size != 0 or self.anchor_digest != "0" * 64))
                    or (self.next_ordinal != 0 and (self.record_size == 0 or self.anchor_digest == "0" * 64))):
                raise ValueError("SOURCE_BOUNDARY_INCOMPLETE")
        elif any(value is not None for value in fields):
            raise ValueError("SOURCE_BOUNDARY_UNVERIFIED_FACTS")
        return self


class LegacyInventoryDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    fresh: bool
    verified_empty: bool
    idle: bool
    generation: str = Field(pattern=r"^(0|[1-9][0-9]{0,19})$")
    required_mask: int = Field(ge=0, le=8191)
    empty_mask: int = Field(ge=0, le=8191)

    @model_validator(mode="before")
    @classmethod
    def exact_schema_type(cls, value):
        if isinstance(value, dict) and type(value.get("schema_version")) is not int:
            raise ValueError("LEGACY_INVENTORY_TYPES")
        return value

    @model_validator(mode="after")
    def valid_inventory(self):
        if (type(self.schema_version) is not int or int(self.generation) > 2**64 - 1
                or (self.verified_empty and (not self.fresh or self.generation == "0"
                    or self.required_mask != 8191 or self.empty_mask != 8191))):
            raise ValueError("LEGACY_INVENTORY_INVALID")
        return self


class QualifiedReaderDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    verified: bool
    matrix_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: str | None = Field(default=None, max_length=19, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    application_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    slot_address: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    slot_size: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    proof_generation: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    sampled_uptime_ms: int | None = Field(default=None, ge=0)


class FirmwareDiagnostics(BaseModel):
    schema_version: Literal[1, 2] = 1
    runtime_profile: Literal["ZKT_LEGACY", "HIKVISION_V1", "ZKT_JOURNAL_V1"] | None = None
    delivery_authority: Literal["LEGACY_DUAL", "ADD", "UNKNOWN"] | None = None
    journal_format: Literal[1] | None = None
    controlled_esp_reboot_v1: bool | None = Field(default=None, strict=True)
    boot_id: str | None = Field(default=None, max_length=100)
    sample_sequence: int | None = Field(default=None, ge=0)
    sampled_at: datetime | None = None
    sampled_uptime_ms: int | None = Field(default=None, ge=0)
    storage: StorageDiagnostics | None = None
    memory: MemoryDiagnostics | None = None
    boot_report: BootReport | None = None
    journal_runtime: JournalRuntimeDiagnostics | None = None
    qualified_reader: QualifiedReaderDiagnostics | None = None
    journal_storage: JournalStorageDiagnostics | None = None
    source_boundary: SourceBoundaryDiagnostics | None = None
    legacy_inventory: LegacyInventoryDiagnostics | None = None
    queues: list[QueueDiagnostics] = Field(default_factory=list, max_length=12)
    workers: list[WorkerDiagnostics] = Field(default_factory=list, max_length=8)
    reconciliation_mode: str | None = Field(default=None, max_length=40)
    last_light_check_uptime_ms: int | None = Field(default=None, ge=0)
    last_tail_audit_uptime_ms: int | None = Field(default=None, ge=0)
    source_generation: int | None = Field(default=None, ge=0)
    committed_source_cursor: int | None = Field(default=None, ge=0)


class HikvisionLightReconcile(BaseModel):
    state: Literal["WAITING", "SCANNING", "WAITING_DELIVERY", "WAITING_TERMINAL", "RETRYING", "COMPLETE", "BLOCKED"]
    policy: Literal["retained_history_20_records_30s_repeat_6h"]
    scanned: int = Field(ge=0, le=150000)
    cursor: int = Field(ge=0, le=3_000_000_000)
    last_completed_epoch: int = Field(ge=0)
    error: int = Field(ge=0, le=8)


class HikvisionTerminalPayload(BaseModel):
    schema_version: Literal[2]
    vendor: Literal["hikvision"]
    protocol: Literal["isapi"]
    serial: str = Field(min_length=1, max_length=120)
    ip_address: str = Field(max_length=64)
    capability_profile: str = Field(max_length=80)
    qualification_state: Literal["NOT_QUALIFIED"]
    online: bool
    connection_state: Literal["ONLINE", "OFFLINE"]
    stream_open: bool
    stream_error: int = Field(ge=0)
    current_event_count: int = Field(ge=0)
    replay_event_count: int = Field(ge=0)
    last_stream_message_epoch: int = Field(ge=0)
    source_storage_failures: int = Field(ge=0)
    source_queue_depth: int | None = Field(default=None, ge=0)
    capture_mode: Literal["stream", "poll"] = "stream"
    poll_interval_seconds: Literal[2, 5] | None = None
    clock_sample: HikvisionClockSample | None = None
    poll_count: int | None = Field(default=None, ge=0)
    last_poll_interval_ms: int | None = Field(default=None, ge=0)
    history_page_count: int | None = Field(default=None, ge=0)
    last_successful_poll_epoch: int | None = Field(default=None, ge=0)
    poll_error: int | None = Field(default=None, ge=0)
    poll_stage: Literal["checkpoint", "identity", "history_bounds", "events", "checkpoint_commit"] | None = None
    poll_http_status: int | None = Field(default=None, ge=0, le=599)
    poll_duration_ms: int | None = Field(default=None, ge=0)
    consecutive_poll_failures: int | None = Field(default=None, ge=0)
    durable_poll_cursor: int | None = Field(default=None, ge=0, le=3_000_000_000)
    full_history_required: bool = True
    profile_command_version: Literal[1, 2, 3, 4] | None = None
    light_reconcile: HikvisionLightReconcile | None = None


class HeartbeatPayload(BaseModel):
    firmware_family: Literal["zkt", "hikvision"] = "zkt"
    firmware_version: str | None = None
    config_version: int = 1
    comm_key_management: bool = False
    comm_key_revision: int = Field(default=0, ge=0)
    uptime_seconds: int | None = None
    rssi: int | None = None
    free_heap: int | None = None
    outbox_depth: int = 0
    current_activity: str | None = None
    led_state: str | None = None
    ota: OtaHeartbeatPayload = Field(default_factory=OtaHeartbeatPayload)
    diagnostics: FirmwareDiagnostics | None = None
    zkt: dict[str, Any] = Field(default_factory=dict)
    terminal: HikvisionTerminalPayload | None = None

    @model_validator(mode="after")
    def family_payload(self):
        if self.firmware_family == "hikvision" and (self.terminal is None or self.zkt):
            raise ValueError("Hikvision requires a versioned terminal payload")
        if self.firmware_family == "zkt" and self.terminal is not None:
            raise ValueError("Legacy ZKT does not accept Hikvision terminal metadata")
        if self.diagnostics:
            from zk_add.runtime_contract import runtime_contract

            # Recovery telemetry is evidence, not permission to capture or
            # pass HIL. Qualification still requires known ADD authority.
            runtime_contract(self.diagnostics.model_dump(), self.firmware_family, allow_unknown_authority=True)
        return self


class UserSnapshotRow(BaseModel):
    uid: str
    user_id: str
    name: str
    privilege: int = 0
    card: int | None = None
    terminal_identity_fingerprint: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    terminal_state_fingerprint: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class UserSnapshotRequest(BaseModel):
    snapshot_id: str
    complete: bool = True
    stable: bool = True
    state_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    reason: str = Field(default="PERIODIC", min_length=1, max_length=80)
    started_at: datetime | None = None
    observed_at: datetime
    users: list[UserSnapshotRow]


class AttendanceEventIn(BaseModel):
    terminal_serial: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    event_uid: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    uid: str | None = Field(default=None, max_length=40)
    terminal_identity_fingerprint: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    user_id: str = Field(min_length=1, max_length=100)
    raw_name: str | None = Field(default=None, max_length=255)
    device_event_time: datetime
    captured_at: datetime
    source: Literal[
        "LIVE",
        "LIVE_POLL",
        "DUMP_STARTUP",
        "DUMP_RECONNECT",
        "MANUAL_REPROCESS",
        "RECONCILE_15M",
        "FULL_HISTORY",
        "CURRENT_RECONCILE",
    ]
    status: str | int | None = None
    punch: str | int | None = None
    raw_punch: bool = False
    clock_drift_seconds: float | None = None
    clock_quality: str = Field(default="UNKNOWN", max_length=40)
    boot_id: str | None = Field(default=None, max_length=100)
    sequence: int | None = Field(
        default=None,
        ge=-(2**63),
        le=2**63 - 1,
    )
    raw_event: dict[str, Any] = Field(default_factory=dict)

    @field_validator("uid", "user_id", "raw_name", "clock_quality", "boot_id")
    @classmethod
    def validate_persisted_text(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("Value contains a database-unsafe null character")
        return value

    @field_validator("status", "punch")
    @classmethod
    def validate_persisted_code(cls, value: str | int | None) -> str | int | None:
        if value is not None:
            rendered = str(value)
            if len(rendered) > 40:
                raise ValueError("Value exceeds the 40-character persistence limit")
            if "\x00" in rendered:
                raise ValueError("Value contains a database-unsafe null character")
        return value

    @field_validator("clock_drift_seconds")
    @classmethod
    def validate_clock_drift(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("Clock drift must be a finite number")
        return value

    @field_validator("raw_event")
    @classmethod
    def validate_raw_event_storage(cls, value: dict[str, Any]) -> dict[str, Any]:
        def safe(item: object) -> bool:
            if item is None or isinstance(item, bool):
                return True
            if isinstance(item, str):
                return "\x00" not in item
            if isinstance(item, int):
                return -(2**63) <= item <= 2**63 - 1
            if isinstance(item, float):
                return math.isfinite(item)
            if isinstance(item, list):
                return all(safe(child) for child in item)
            if isinstance(item, dict):
                return all(
                    isinstance(key, str)
                    and "\x00" not in key
                    and safe(child)
                    for key, child in item.items()
                )
            return False

        if not safe(value):
            raise ValueError("Raw event contains a value that cannot be persisted as JSON")
        return value


class AttendanceBatchRequest(BaseModel):
    batch_id: str = Field(min_length=1, max_length=120)
    payload_digest: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    events: list[AttendanceEventIn] = Field(min_length=1, max_length=100)

    @field_validator("batch_id")
    @classmethod
    def validate_batch_id_storage(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Batch ID contains a database-unsafe null character")
        return value


ReconciliationScope = Literal["ALL_RECORDS", "ACTIVE_USERS", "RECENT_TAIL"]


class ReconciliationCreateRequest(BaseModel):
    """One-click start. reason, confirmation and password are accepted from an
    older open tab and ignored."""

    scope: ReconciliationScope = "ALL_RECORDS"
    reason: str | None = Field(default=None, max_length=500)
    confirmation: str | None = Field(default=None, max_length=180)
    password: str | None = Field(default=None, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class ReconciliationBulkCreateRequest(BaseModel):
    connector_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=100
    )
    scope: ReconciliationScope = "ALL_RECORDS"
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("connector_ids")
    @classmethod
    def require_distinct_connectors(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("connector_ids must be distinct")
        return value


class ReconciliationArchiveRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)


class ReconciliationClearHistoryRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)


class ReconciliationRetryExpectedState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["NEEDS_ATTENTION"]
    phase: str = Field(max_length=80)
    error_code: str | None = Field(max_length=120)
    wait_reason: str | None = Field(max_length=255)
    source_epoch: str = Field(min_length=36, max_length=36)
    terminal_serial: str = Field(min_length=1, max_length=120)
    terminal_generation: int = Field(ge=1)
    committed_next_ordinal: int = Field(ge=0)
    cutoff_count: int = Field(ge=0)
    chain_digest: str | None = Field(min_length=64, max_length=64)
    retry_count: int = Field(ge=0)
    updated_at: datetime
    connector_boot_id: str = Field(min_length=1, max_length=100)
    firmware_version: str = Field(min_length=1, max_length=80)
    application_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    diagnostics_sample_sequence: int = Field(ge=0)
    diagnostics_at: datetime
    # terminal_generation is the job's own generation. The connector's binding
    # generation can be newer after a re-onboard; pin it separately if wanted.
    connector_generation: int | None = Field(default=None, ge=1)

    @field_validator("updated_at", "diagnostics_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Retry evidence timestamps require a timezone")
        return value


class ReconciliationControlRequest(BaseModel):
    """One-click control. reason and password are accepted and ignored."""

    reason: str | None = Field(default=None, max_length=500)
    password: str | None = Field(default=None, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)
    expected_state: ReconciliationRetryExpectedState | None = None


class SourceEpochRequest(BaseModel):
    # Optional on the legacy wire; required by service validation for the
    # 2.6.16 reader bridge and 2.7+ ZKT firmware. Never the database integer ID.
    source_epoch: str | None = Field(
        default=None, strict=True, min_length=36, max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )


class ReconciliationAnchorRequest(SourceEpochRequest):
    job_id: str = Field(min_length=36, max_length=36)
    generation: int = Field(ge=1)
    terminal_serial: str = Field(min_length=1, max_length=120)
    terminal_generation: int = Field(ge=1)
    cutoff_count: int = Field(ge=0)
    latest_terminal_count: int = Field(ge=0)
    record_size: Literal[8, 16, 40]
    source_total_bytes: int = Field(ge=4, le=128 * 1024 * 1024)
    first_anchor_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    identity_snapshot_id: str | None = Field(default=None, max_length=120)


class ReconciliationSourceRecord(BaseModel):
    ordinal: int = Field(ge=0)
    raw_record_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    terminal_record_key: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    occurrence_index: int = Field(default=1, ge=1)
    disposition: Literal[
        "EVENT",
        "BLOCKED_IDENTITY",
        "IDENTITY_UNRESOLVED",
        "INVALID_TIME",
        "MALFORMED",
        "TERMINAL_DUPLICATE",
        "RAW_PRESERVED",
    ]
    event: AttendanceEventIn | None = None
    raw_record_b64: str = Field(min_length=4, max_length=512)
    error_code: str | None = Field(default=None, max_length=120)
    raw_timestamp: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    observed_uid: str | None = Field(default=None, max_length=40)
    observed_user_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_record_evidence(self):
        if self.disposition == "RAW_PRESERVED" and any(value is not None for value in (
            self.event, self.raw_timestamp, self.observed_uid, self.observed_user_id, self.error_code,
        )):
            raise ValueError("Raw custody cannot claim decoded time, identity, attendance or invalid source data")
        if self.disposition == "IDENTITY_UNRESOLVED" and self.event is not None:
            raise ValueError("Unresolved source identity cannot claim an attendance event")
        if self.disposition in {"EVENT", "BLOCKED_IDENTITY"} and self.event is None:
            raise ValueError("Parsed reconciliation rows require an attendance event")
        return self


class SourceProbeResultRequest(SourceEpochRequest):
    job_id: str = Field(min_length=36, max_length=36)
    generation: int = Field(ge=1)
    terminal_serial: str = Field(min_length=1, max_length=120)
    latest_terminal_count: int = Field(ge=0)
    record_size: Literal[8, 16, 40]
    ordinal: int = Field(ge=0)
    record: ReconciliationSourceRecord


class ReconciliationChunkRequest(SourceEpochRequest):
    assignment_id: str | None = Field(default=None, min_length=36, max_length=36)
    job_id: str = Field(min_length=36, max_length=36)
    generation: int = Field(ge=1)
    sequence: int = Field(ge=0)
    start_ordinal: int = Field(ge=0)
    end_ordinal: int = Field(ge=1)
    chunk_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    previous_chain_digest: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    resulting_chain_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    records: list[ReconciliationSourceRecord] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_contiguous_chunk(self):
        if self.end_ordinal - self.start_ordinal != len(self.records):
            raise ValueError("Chunk range must exactly match its record count")
        expected = list(range(self.start_ordinal, self.end_ordinal))
        if [row.ordinal for row in self.records] != expected:
            raise ValueError("Chunk records must be ordered and contiguous")
        return self


class ReconciliationManifestRequest(SourceEpochRequest):
    job_id: str = Field(min_length=36, max_length=36)
    generation: int = Field(ge=1)
    terminal_serial: str = Field(min_length=1, max_length=120)
    terminal_generation: int = Field(ge=1)
    cutoff_count: int = Field(ge=0)
    latest_terminal_count: int = Field(ge=0)
    final_chain_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )


class SourceTailChunkRequest(SourceEpochRequest):
    terminal_serial: str = Field(min_length=1, max_length=120)
    terminal_generation: int = Field(ge=1)
    record_size: Literal[8, 16, 40]
    start_ordinal: int = Field(ge=0)
    end_ordinal: int = Field(ge=1)
    latest_terminal_count: int = Field(ge=1)
    chunk_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    previous_chain_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    resulting_chain_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    records: list[ReconciliationSourceRecord] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_contiguous_tail(self):
        if self.end_ordinal - self.start_ordinal != len(self.records):
            raise ValueError("Tail range must exactly match its record count")
        if self.end_ordinal > self.latest_terminal_count:
            raise ValueError("Tail range cannot exceed the observed terminal count")
        expected = list(range(self.start_ordinal, self.end_ordinal))
        if [row.ordinal for row in self.records] != expected:
            raise ValueError("Tail records must be ordered and contiguous")
        return self


class SourceExceptionActionRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class ReconciliationAssignmentReleaseRequest(SourceEpochRequest):
    assignment_id: str = Field(min_length=36, max_length=36)
    job_id: str = Field(min_length=36, max_length=36)
    generation: int = Field(ge=1)
    committed_next_ordinal: int = Field(ge=0)
    reason: Literal[
        "COMMAND_PENDING",
        "LEASE_EXPIRING",
        "HEAP_PRESSURE",
        "DISCONNECTING",
        "TRANSIENT_STEP_FAILED",
    ]


class OracleReceiptBatchRequest(BaseModel):
    confirmation_path: Literal[
        "FIRMWARE_LIVE",
        "FIRMWARE_BULK",
        "FIRMWARE_RECONCILE",
    ]
    oracle_observed_at: datetime
    event_uids: list[
        str
    ] = Field(
        min_length=1,
        max_length=100,
    )

    @field_validator("event_uids")
    @classmethod
    def validate_event_uids(cls, values: list[str]) -> list[str]:
        for value in values:
            if len(value) != 64 or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(
                    "Each event_uid must be a 64-character lowercase hex digest"
                )
        if len(values) != len(set(values)):
            raise ValueError("Each event_uid may be confirmed only once per batch")
        return values


class DeviceLogIn(BaseModel):
    boot_id: str
    sequence: int
    level: Literal["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"]
    subsystem: str
    code: str | None = None
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
    device_time: datetime | None = None


class LogBatchRequest(BaseModel):
    logs: list[DeviceLogIn]


class UserCreateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    cnic: str = Field(min_length=13, max_length=15)
    shift_worker: bool = False
    user_id_override: str | None = Field(default=None, min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("cnic")
    @classmethod
    def validate_cnic(cls, value: str) -> str:
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("CNIC must contain exactly 13 digits")
        return digits

    @field_validator("user_id_override")
    @classmethod
    def validate_user_id(cls, value: str | None) -> str | None:
        if value is not None and not value.isdigit():
            raise ValueError("Employee/user ID override must be numeric")
        return value


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    cnic: str | None = Field(default=None, min_length=13, max_length=15)
    shift_worker: bool | None = None
    privilege: Literal[0, 14] | None = None
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=120)
    password: str = Field(min_length=1, max_length=512)
    reason: str | None = Field(default=None, min_length=10, max_length=500)
    typed_confirmation: str | None = Field(default=None, max_length=300)

    @field_validator("cnic")
    @classmethod
    def validate_optional_cnic(cls, value: str | None) -> str | None:
        if value is None:
            return None
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("CNIC must contain exactly 13 digits")
        return digits

    @model_validator(mode="after")
    def require_change(self):
        if all(
            value is None
            for value in (self.display_name, self.cnic, self.shift_worker, self.privilege)
        ):
            raise ValueError("At least one user field must be changed")
        return self


class UserDeleteRequest(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=120)
    typed_confirmation: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=512)


class TerminalSerialConfirmRequest(BaseModel):
    observed_serial: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,79}$")
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class TerminalSerialReplaceRequest(BaseModel):
    current_serial: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,79}$")
    observed_serial: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,79}$")
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class CommKeyChangeRequest(BaseModel):
    new_key: SecretStr
    mode: Literal["ESP_ONLY", "ESP_AND_TERMINAL"]
    expected_revision: int = Field(ge=0)
    expected_terminal_serial: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,79}$")
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: SecretStr
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("new_key")
    @classmethod
    def validate_new_key(cls, value: SecretStr) -> SecretStr:
        material = value.get_secret_value()
        if not re.fullmatch(r"[1-9][0-9]{0,9}", material):
            raise ValueError(
                "COMM Key must be a canonical non-zero decimal value without leading zeros"
            )
        if int(material) > 4_294_967_295:
            raise ValueError("COMM Key exceeds the ESP uint32 protocol range")
        return value


class CommKeyRevealRequest(BaseModel):
    password: SecretStr
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)


class BulkUserDeleteTarget(BaseModel):
    user_key: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)


class UserSelectionValidationRequest(BaseModel):
    user_keys: list[str] = Field(min_length=1, max_length=500)

    @field_validator("user_keys")
    @classmethod
    def validate_user_keys(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 36 for value in normalized):
            raise ValueError(
                "Each terminal user key must contain between 1 and 36 characters"
            )
        if len(normalized) != len(set(normalized)):
            raise ValueError("Each terminal user may be selected only once")
        return normalized


class BulkUserDeleteRequest(BaseModel):
    targets: list[BulkUserDeleteTarget] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def unique_targets(self):
        keys = [target.user_key for target in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("Each terminal user may be selected only once")
        return self


class BulkUserDeleteCancelRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class IdentityConflictMemberConfirmation(BaseModel):
    user_key: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)


class IdentityConflictResolveRequest(BaseModel):
    group_token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    members: list[IdentityConflictMemberConfirmation] = Field(min_length=2, max_length=20)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: Literal["SAME EMPLOYEE"]
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def unique_members(self):
        keys = [member.user_key for member in self.members]
        if len(keys) != len(set(keys)):
            raise ValueError("Each conflict member must be confirmed exactly once")
        return self


class IdentityConflictRevokeRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: Literal["REVOKE RESOLUTION"]
    password: str = Field(min_length=1, max_length=512)


class HistoricalIdentityAliasRequest(BaseModel):
    source_user_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    source_cnic: str = Field(min_length=13, max_length=15)
    target_user_key: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("source_cnic")
    @classmethod
    def validate_source_cnic(cls, value: str) -> str:
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("Historical source CNIC must contain exactly 13 digits")
        return digits


class HistoricalDirectoryIdentityRequest(BaseModel):
    source_user_key: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)
    source_cnic: str = Field(min_length=13, max_length=15)
    directory_employee_id: str = Field(min_length=1, max_length=40)
    directory_service_number: str = Field(min_length=1, max_length=40)
    directory_employee_name: str = Field(min_length=2, max_length=255)
    directory_zone_code: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("source_cnic")
    @classmethod
    def validate_directory_cnic(cls, value: str) -> str:
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("Directory CNIC must contain exactly 13 digits")
        return digits

    @field_validator("directory_employee_id")
    @classmethod
    def validate_directory_employee_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.isdigit():
            raise ValueError("Directory employee ID must contain only digits")
        return normalized

    @field_validator("directory_service_number")
    @classmethod
    def validate_directory_service_number(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
            raise ValueError(
                "Directory service number contains unsupported characters"
            )
        return normalized


class HistoricalEventGroupIdentityRequest(BaseModel):
    group_token: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    source_user_id: str = Field(min_length=1, max_length=100)
    source_uid: str = Field(default="", max_length=40)
    source_cnic: str = Field(min_length=13, max_length=15)
    directory_employee_id: str = Field(min_length=1, max_length=40)
    directory_service_number: str = Field(min_length=1, max_length=40)
    directory_employee_name: str = Field(min_length=2, max_length=255)
    directory_zone_code: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("source_cnic")
    @classmethod
    def validate_event_group_cnic(cls, value: str) -> str:
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("Directory CNIC must contain exactly 13 digits")
        return digits

    @field_validator("directory_employee_id")
    @classmethod
    def validate_event_group_employee_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.isdigit():
            raise ValueError("Directory employee ID must contain only digits")
        return normalized

    @field_validator("directory_service_number")
    @classmethod
    def validate_event_group_service_number(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
            raise ValueError(
                "Directory service number contains unsupported characters"
            )
        return normalized


class HistoricalCurrentIdentityRequest(BaseModel):
    group_token: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    source_user_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    source_uid: str = Field(default="", max_length=40)
    target_user_key: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)
    source_cnic: str = Field(min_length=13, max_length=15)
    verified_employee_name: str = Field(min_length=2, max_length=255)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("source_cnic")
    @classmethod
    def validate_current_identity_cnic(cls, value: str) -> str:
        digits = "".join(character for character in value if character.isdigit())
        if len(digits) != 13:
            raise ValueError("Authoritative CNIC must contain exactly 13 digits")
        return digits


class AdminLeaseRequest(BaseModel):
    uid: str
    idempotency_key: str = Field(min_length=8, max_length=120)
    password: str


class RestartRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=120)
    password: str


class DeviceSpareUpdateRequest(BaseModel):
    spare: bool


class CommandUpdate(BaseModel):
    command_id: str
    status: Literal[
        "ACKNOWLEDGED",
        "WAITING_FOR_DEVICE",
        "WAITING_FOR_ZKT",
        "RETRYING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "EXPIRED",
    ]
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class AlertAcknowledgeRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class AlertResolveRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class HealthCleanupPreviewRequest(BaseModel):
    connector_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)


class HealthCleanupApplyRequest(BaseModel):
    connector_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    alert_ids: list[int] = Field(default_factory=list, max_length=200)
    error_fix_connector_ids: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(min_length=10, max_length=500)
    typed_confirmation: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class DeviceErrorClearRequest(BaseModel):
    expected_code: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=10, max_length=500)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)


class HikvisionWriteExceptionRequest(BaseModel):
    terminal_serial: str = Field(min_length=1, max_length=120)
    enabled: bool = False
    permanent: bool = False
    reason: str = Field(min_length=10, max_length=500)
    password: SecretStr
    idempotency_key: str = Field(min_length=8, max_length=120)


class HikvisionPolicyRequest(BaseModel):
    terminal_serial: str = Field(min_length=1, max_length=120)
    source_epoch: str = Field(min_length=1, max_length=64)
    profile_id: str = Field(min_length=1, max_length=80)
    success_codes: list[list[StrictInt]] = Field(min_length=1, max_length=32)
    excluded_codes: list[list[StrictInt]] = Field(default_factory=list, max_length=256)
    enabled: bool = False
    profile_commands_enabled: bool = False
    reason: str = Field(min_length=10, max_length=500)
    password: SecretStr
    idempotency_key: str = Field(min_length=8, max_length=120)


class AttendanceRecoveryFilter(BaseModel):
    """Bounded, serializable filter used to freeze recovery candidates."""

    connector_id: str | None = Field(default=None, max_length=100)
    zone_id: str | None = Field(default=None, max_length=100)
    firmware_family: Literal["zkt", "hikvision"] | None = None
    lane: Literal[
        "SAFE_DELIVERY",
        "IDENTITY_HELD",
        "SOURCE_CORRECTION",
        "PERMANENT_REVIEW",
    ] | None = None
    statuses: list[str] = Field(default_factory=list, max_length=20)
    source: str | None = Field(default=None, max_length=40)
    terminal_serial: str | None = Field(default=None, max_length=120)
    from_at: datetime | None = None
    to_at: datetime | None = None
    stale_only: bool = False
    event_ids: list[int] = Field(default_factory=list, max_length=5000)
    source_epoch: str | None = Field(default=None, max_length=64)
    generation: int | None = Field(default=None, ge=0)
    retry_age_minutes: int | None = Field(default=None, ge=0, le=52560000)
    ords_error_category: str | None = Field(default=None, max_length=120)
    provenance: Literal["VERIFIED", "MISSING"] | None = None

    @field_validator("statuses")
    @classmethod
    def normalize_statuses(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            value = value.strip().upper()
            if value and value not in normalized:
                normalized.append(value)
        return normalized

    @model_validator(mode="after")
    def validate_range(self):
        if self.from_at and self.to_at and self.from_at > self.to_at:
            raise ValueError("from_at must be before to_at")
        return self


class AttendanceRecoveryPreviewRequest(BaseModel):
    action: Literal[
        "RETRY_DELIVERY",
        "REBUILD_OUTBOX",
        "RECOVER_STALE_IN_FLIGHT",
    ] = "RETRY_DELIVERY"
    filters: AttendanceRecoveryFilter = Field(default_factory=AttendanceRecoveryFilter)
    reason: str = Field(min_length=10, max_length=500)


class AttendanceRecoveryCreateRequest(AttendanceRecoveryPreviewRequest):
    candidate_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)
    preview_expires_at: datetime
    preview_signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class AttendanceRecoveryControlRequest(BaseModel):
    action: Literal["pause", "resume", "cancel", "retry"]
    reason: str = Field(min_length=10, max_length=500)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    typed_confirmation: str = Field(min_length=1, max_length=300)


class AttendanceSourceCorrectionInput(BaseModel):
    source_kind: Literal["ZKT_MANIFEST", "HIKVISION_EVIDENCE"]
    source_ref: str = Field(min_length=1, max_length=160)
    corrected_device_time: datetime

    @field_validator("corrected_device_time")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("A corrected timestamp must include an explicit timezone offset")
        return value


class AttendanceSourceCorrectionPreviewRequest(BaseModel):
    corrections: list[AttendanceSourceCorrectionInput] = Field(
        min_length=1, max_length=500
    )
    reason: str = Field(min_length=10, max_length=500)


class AttendanceSourceCorrectionCreateRequest(AttendanceSourceCorrectionPreviewRequest):
    candidate_digest: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    typed_confirmation: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=120)
    preview_expires_at: datetime
    preview_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
