"""Five plain status groups for one check-in, for the Attendance and Home pages.

Read side only. The stored ``ords_status`` codes are never renamed and the
delivery worker never reads these groups; they only decide which chip a row
shows and which rows a status filter returns. The sets come from
``ords_states`` so a status counted as "waiting" here is exactly a status
the delivery worker and reconciliation still treat as active.

* ``sent``: the HR system (Oracle) confirmed it.
* ``waiting``: on its way (queued, sending, being re-checked).
* ``not_matched``: held because the person is not known safely yet.
* ``set_aside``: kept but not sent (impossible time, damaged record,
  machine not confirmed, two people on one Employee ID).
* ``problem``: the HR system refused it, or a status this page has no
  group for. Fail-closed: an unknown or missing status is never "waiting".

``trying_again`` is a sub-count of ``waiting`` (a send failed and will be
tried again).
"""

from __future__ import annotations

from typing import Iterable, Literal

from sqlalchemy import and_, false, or_

from zk_add.models import AttendanceEvent
from zk_add.ords_states import (
    ORDS_ACKNOWLEDGED_STATUSES,
    ORDS_IDENTITY_HELD_STATUSES,
    ORDS_RECONCILIATION_PENDING_STATUSES,
)

DeliveryGroup = Literal["sent", "waiting", "not_matched", "set_aside", "problem"]
DELIVERY_GROUP_ORDER: tuple[DeliveryGroup, ...] = (
    "sent", "waiting", "not_matched", "set_aside", "problem",
)

SET_ASIDE_STATUSES = frozenset({
    "QUARANTINED_INVALID_DEVICE_TIME",
    "QUARANTINED_INVALID_EVENT_UID",
    "QUARANTINED_MISSING_TERMINAL_PROVENANCE",
    "QUARANTINED_IDENTITY_CONFLICT",
})
REFUSED_STATUSES = frozenset({"QUARANTINED_ORDS_REJECTED"})
TRYING_AGAIN_STATUSES = frozenset({"FAILED_RETRYABLE", "RETRYING"})

DELIVERY_GROUPS: dict[DeliveryGroup, frozenset[str]] = {
    "sent": ORDS_ACKNOWLEDGED_STATUSES,
    "waiting": ORDS_RECONCILIATION_PENDING_STATUSES,
    "not_matched": ORDS_IDENTITY_HELD_STATUSES,
    "set_aside": SET_ASIDE_STATUSES,
    # Plus every status outside the sets above.
    "problem": REFUSED_STATUSES,
}
_GROUP_BY_STATUS: dict[str, DeliveryGroup] = {
    status: group for group, statuses in DELIVERY_GROUPS.items() for status in statuses
}
# Every stored status that has a group of its own (problem's catch-all aside).
KNOWN_STATUSES = frozenset(_GROUP_BY_STATUS)
# A fixed list for the Technical status filter: no DISTINCT scan per request.
STATUS_OPTIONS: tuple[str, ...] = tuple(sorted(KNOWN_STATUSES))


def delivery_group(status: str | None) -> DeliveryGroup:
    """The group of one stored ``ords_status`` (exact value, as SQL compares it)."""

    return _GROUP_BY_STATUS.get(status or "", "problem")


def parse_groups(values: Iterable[str]) -> list[DeliveryGroup]:
    """Accept repeated or comma-separated group names; reject unknown ones."""

    groups: list[DeliveryGroup] = []
    for value in values:
        for part in str(value).split(","):
            name = part.strip().lower()
            if not name:
                continue
            if name not in DELIVERY_GROUPS:
                raise ValueError(f"unknown status group {name!r}")
            if name not in groups:
                groups.append(name)  # type: ignore[arg-type]
    return groups


def statuses_for_groups(groups: Iterable[str]) -> frozenset[str]:
    """The stored statuses of the named groups (problem's catch-all excluded)."""

    statuses: set[str] = set()
    for group in groups:
        statuses |= DELIVERY_GROUPS[group]  # type: ignore[index]
    return frozenset(statuses)


def group_condition(groups: Iterable[str]):
    """SQL condition for rows in any of the named groups."""

    groups = list(groups)
    if not groups:
        return false()
    conditions = []
    named = statuses_for_groups(group for group in groups if group != "problem")
    if named:
        conditions.append(AttendanceEvent.ords_status.in_(sorted(named)))
    if "problem" in groups:
        conditions.append(or_(
            AttendanceEvent.ords_status.is_(None),
            AttendanceEvent.ords_status.in_(sorted(REFUSED_STATUSES)),
            and_(
                AttendanceEvent.ords_status.is_not(None),
                AttendanceEvent.ords_status.not_in(sorted(KNOWN_STATUSES)),
            ),
        ))
    return or_(*conditions)


def empty_totals() -> dict:
    return {
        "received": 0, "sent": 0, "waiting": 0, "trying_again": 0,
        "not_matched": 0, "set_aside": 0, "problem": 0,
    }


def add_to_totals(totals: dict, status: str | None, count: int) -> None:
    """Fold one ``(ords_status, count)`` pair into a totals dict."""

    totals["received"] += count
    totals[delivery_group(status)] += count
    if (status or "") in TRYING_AGAIN_STATUSES:
        totals["trying_again"] += count


def totals_from_status_counts(counts: dict[str | None, int]) -> dict:
    """Group a ``{ords_status: count}`` map (as ``event_ords_status_counts`` gives)."""

    totals = empty_totals()
    for status, count in counts.items():
        add_to_totals(totals, status, int(count))
    return totals


def fixture_rows() -> list[dict]:
    """The rows of ``apps/add_frontend/src/features/attendance/deliveryGroups.fixture.json`` (shared with Vitest)."""

    rows = [{"ords_status": status, "delivery_group": delivery_group(status)}
            for status in sorted(KNOWN_STATUSES)]
    rows.append({"ords_status": None, "delivery_group": "problem"})
    rows.append({"ords_status": "MISSING_STATUS", "delivery_group": "problem"})
    rows.append({"ords_status": "SOME_FUTURE_STATUS", "delivery_group": "problem"})
    return rows
