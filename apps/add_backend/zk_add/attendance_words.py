"""Codes for the attendance tools' reason sentences (UI/UX revamp WORDS-8, backend half).

Read side only. The tools already store English sentences; the page shows
its own plain words keyed by a code and keeps the stored sentence for
Technical details. Where a row carries only a sentence, these maps give the
code. Stored codes and sentences are never renamed.
"""

from __future__ import annotations

from functools import lru_cache

# The plain fallback for a code the tools have no sentence for. It replaces
# the old title-cased code ("Sync Expired"), so the API never sends one.
UNKNOWN_REASON = "This check-in needs a closer look before it can be sent."
UNKNOWN_REASON_CODE = "UNKNOWN"

# Force release: two fixed sentences written by the worker besides MESSAGES
# (attendance_force_release.py, the queued and confirmed steps).
FORCE_FIXED_REASON_CODES = {
    "Saved for delivery. Waiting for Oracle confirmation.": "WAITING_ORACLE",
    "Oracle confirmed the approved attendance.": "CONFIRMED",
}


@lru_cache(maxsize=1)
def _force_messages() -> dict[str, str]:
    # Imported here: attendance_force_release imports this module.
    from zk_add.attendance_force_release import MESSAGES

    return dict(MESSAGES)


@lru_cache(maxsize=1)
def _force_code_by_sentence() -> dict[str, str]:
    return {sentence: code for code, sentence in _force_messages().items()}


# Recover attendance rows: every sentence _event_lane returns.
LANE_REASON_CODES = {
    "Oracle has acknowledged this event.": "CONFIRMED",
    "Verified terminal provenance is missing.": "MACHINE_NOT_CONFIRMED",
    "The event UID is invalid.": "RECORD_NUMBER_INVALID",
    "The delivery state is unknown and is held fail-closed.": "STATE_UNKNOWN",
    "The event has a terminal review outcome.": "NEEDS_REVIEW",
    "The preserved source timestamp requires correction review.": "TIME_NEEDS_FIX",
    "The event is pinned because terminal identity reuse was detected.": "USER_NUMBER_REUSED",
    "The durable outbox has an acknowledged, unknown, or permanent review outcome.": "SEND_STATE_FINAL",
    "Exact terminal identity and authoritative CNIC evidence are now verified.": "IDENTITY_VERIFIED",
    "Identity evidence is incomplete or reused.": "IDENTITY_INCOMPLETE",
    "The event can be retried through the idempotent outbox.": "CAN_RETRY",
}


def force_item_reason_code(error_code: str | None, stored_reason: str | None) -> str:
    """The code behind one force-release item's reason sentence."""

    if stored_reason in FORCE_FIXED_REASON_CODES:
        return FORCE_FIXED_REASON_CODES[stored_reason]
    by_sentence = _force_code_by_sentence()
    if stored_reason in by_sentence:
        return by_sentence[stored_reason]  # type: ignore[index]
    if error_code in _force_messages():
        return error_code  # type: ignore[return-value]
    return UNKNOWN_REASON_CODE


def lane_reason_code(reason: str | None) -> str:
    return LANE_REASON_CODES.get(reason or "", UNKNOWN_REASON_CODE)
