from __future__ import annotations

from typing import Literal


# These sets are shared by the delivery worker and reconciliation assurance so
# a durable outbox state cannot be "active" in one subsystem and terminal in
# another.
ORDS_DELIVERY_ACTIVE_STATUSES = frozenset({"PENDING", "FAILED_RETRYABLE", "IN_FLIGHT"})
ORDS_FIRMWARE_UNVERIFIED_STATUSES = frozenset(
    {
        "ACKED_FIRMWARE",
        "FIRMWARE_RECEIPT_UNVERIFIED",
        "FIRMWARE_RECEIPT_VERIFYING",
    }
)
ORDS_MEMBERSHIP_REVERIFY_STATUSES = frozenset(
    {"MEMBERSHIP_REVERIFYING", "MEMBERSHIP_REVERIFY_RETRY"}
)
ORDS_ACTIVE_STATUSES = (
    ORDS_DELIVERY_ACTIVE_STATUSES
    | ORDS_FIRMWARE_UNVERIFIED_STATUSES
    | ORDS_MEMBERSHIP_REVERIFY_STATUSES
)
ORDS_RECONCILIATION_PENDING_STATUSES = ORDS_ACTIVE_STATUSES | frozenset(
    {
        # Compatibility with attendance rows written before the durable outbox
        # status was renamed to FAILED_RETRYABLE.
        "RETRYING",
    }
)
ORDS_ACKNOWLEDGED_STATUSES = frozenset({"ACKED", "ACKED_CHECK"})
ORDS_IDENTITY_HELD_STATUSES = frozenset({"BLOCKED_IDENTITY", "QUARANTINED_IDENTITY_REUSE"})
ORDS_TERMINAL_REVIEW_STATUSES = frozenset(
    {
        "QUARANTINED_INVALID_DEVICE_TIME",
        "QUARANTINED_INVALID_EVENT_UID",
        "QUARANTINED_ORDS_REJECTED",
    }
)

OrdsAssuranceOutcome = Literal[
    "CONFIRMED",
    "IDENTITY_HELD",
    "PENDING",
    "REVIEW_REQUIRED",
]


def normalize_ords_status(value: str | None) -> str:
    normalized = (value or "").strip().upper()
    return normalized or "MISSING_STATUS"


def classify_ords_assurance_status(value: str | None) -> OrdsAssuranceOutcome:
    """Classify every ORDS state without treating unknown states as retryable.

    A new or malformed state is fail-closed as REVIEW_REQUIRED. This prevents a
    future terminal state from silently producing an endless reconciliation
    ETA while still protecting the Oracle membership certificate.
    """

    status = normalize_ords_status(value)
    if status in ORDS_ACKNOWLEDGED_STATUSES:
        return "CONFIRMED"
    if status in ORDS_IDENTITY_HELD_STATUSES:
        return "IDENTITY_HELD"
    if status in ORDS_RECONCILIATION_PENDING_STATUSES:
        return "PENDING"
    return "REVIEW_REQUIRED"
