"""Identity-resolution states that are safe from automatic re-enrichment.

Keep this list in one module.  Both the regular ORDS worker and attendance
repair activation use it, so a verified historical decision cannot later be
silently replaced by a best-effort snapshot match.
"""

from __future__ import annotations


PINNED_IDENTITY_RESOLUTION_STATUSES = frozenset(
    {
        "RESOLVED_DIRECTORY_EVIDENCE",
        "RESOLVED_DIRECTORY_EVENT_GROUP",
        "RESOLVED_CURRENT_IDENTITY_EVIDENCE",
        "RESOLVED_HISTORICAL_ALIAS",
        "RESOLVED_TOMBSTONE",
        "RESOLVED_ATTENDANCE_REPAIR",
    }
)

VERIFIED_IDENTITY_RESOLUTION_STATUSES = PINNED_IDENTITY_RESOLUTION_STATUSES | frozenset(
    {
        "RESOLVED",
        "RESOLVED_CURRENT_SNAPSHOT",
    }
)
