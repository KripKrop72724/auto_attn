"""Read-side helpers for the Problems list (UI/UX revamp PRB-1, PRB-2).

``GET /api/v1/alerts`` keeps every existing parameter, key and order. These
helpers add: the SEEN / LEGACY_SEEN queues, Life / Health, a severity list,
type and "affects attendance" filters, a search over device names, codes
and plain titles, date windows, newest / oldest / fixed_newest orders with
their own cursors, the ``counts`` object, plain device names, and the
"can it be marked as fixed" rule on each open row. Nothing here writes.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from zk_add import connector_naming, problem_words
from zk_add.device_health import alert_reason, duplicate_serial_claimed, evaluate_health_batch, operator_policy
from zk_add.models import Connector, DeviceAlert
from zk_add.time_utils import parse_datetime, utc_now

SEVERITIES = ("CRITICAL", "HIGH", "WARNING", "MEDIUM", "INFO")
ORDERS = ("serious", "newest", "oldest", "fixed_newest")
OPEN_STATES = ("OPEN", "ACKNOWLEDGED")


class AlertFilterError(ValueError):
    pass


def seen_filters() -> list:
    """Seen by someone, still open (legacy ACKNOWLEDGED rows excluded)."""

    return [DeviceAlert.state == "OPEN", DeviceAlert.acknowledged_at.is_not(None)]


def legacy_seen_filters() -> list:
    """Rows acknowledged before acknowledgement became an annotation."""

    return [DeviceAlert.state == "ACKNOWLEDGED"]


def parse_list(values: Iterable[str] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    items: list[str] = []
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if part and part not in items:
                items.append(part)
    return items


def severity_values(severity: str | None) -> list[str]:
    """``CRITICAL`` or ``CRITICAL,HIGH``; unknown values simply match nothing."""

    return [value.upper() for value in parse_list(severity)]


def type_codes(types: list[str]) -> list[str]:
    unknown = [value for value in types if value not in problem_words.PROBLEM_TYPES]
    if unknown:
        raise AlertFilterError("Choose a known problem type.")
    return problem_words.codes_for_types(types)


def search_condition(db: Session, q: str):
    """Friendly name (any device), device code, problem code or plain title."""

    text = q.strip()
    like = f"%{text}%"
    needle = text.lower()
    named = [
        connector.id
        for connector in db.scalars(select(Connector).where(Connector.is_spare.is_(False))).all()
        if needle in connector_naming.friendly_name(connector).lower()
    ]
    conditions = [
        Connector.display_name.ilike(like),
        Connector.zone_id.ilike(like),
        DeviceAlert.code.ilike(like),
    ]
    if named:
        conditions.append(Connector.id.in_(named))
    codes = problem_words.codes_matching(text)
    if codes:
        conditions.append(DeviceAlert.code.in_(codes))
    return or_(*conditions)


def date_condition(column, from_time: str | None, to_time: str | None) -> list:
    conditions = []
    try:
        if from_time:
            conditions.append(column >= parse_datetime(from_time))
        if to_time:
            conditions.append(column < parse_datetime(to_time))
    except ValueError as error:
        raise AlertFilterError("Choose a valid date range.") from error
    return conditions


def encode_cursor(payload: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()


def decode_cursor(cursor: str, order: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        if payload.get("order") != order:
            raise KeyError("order")
        return parse_datetime(str(payload["at"])), int(payload["id"])
    except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError, binascii.Error) as error:
        raise AlertFilterError("Alert cursor is invalid or expired.") from error


def time_order(order: str):
    """(column, descending) for the newest / oldest / fixed_newest orders."""

    if order == "fixed_newest":
        return DeviceAlert.resolved_at, True
    return DeviceAlert.first_seen_at, order == "newest"


def time_page_filter(order: str, cursor: str):
    column, descending = time_order(order)
    at, row_id = decode_cursor(cursor, order)
    if descending:
        return or_(column < at, and_(column == at, DeviceAlert.id < row_id))
    return or_(column > at, and_(column == at, DeviceAlert.id > row_id))


def time_next_cursor(order: str, row: DeviceAlert) -> str | None:
    column, _descending = time_order(order)
    value = getattr(row, column.key)
    if value is None:
        return None
    return encode_cursor({"order": order, "at": value.isoformat(), "id": row.id})


def _division_of(db: Session, connector_pks: Iterable[int]) -> dict[int, str]:
    connector_pks = set(connector_pks)
    if not connector_pks:
        return {}
    return {
        row.id: connector_naming.code_division(row.display_name or row.zone_id)
        for row in db.execute(
            select(Connector.id, Connector.display_name, Connector.zone_id).where(Connector.id.in_(connector_pks))
        ).all()
    }


def counts(
    db: Session,
    *,
    base: dict[str, list],
    division: str | None,
    first_seen_window: list,
    resolved_window: list,
    page_window: list,
) -> dict:
    """The ``counts`` object: each count ignores only the filter it counts.

    ``base`` holds the filter lists by name (scope, severity, type,
    attention, search, state); tab counts ignore the queue, severity counts
    ignore ``severity``, type counts ignore ``type``. ``by_division`` covers
    both divisions whatever the division filter.
    """

    def filters(*skip: str) -> list:
        return [condition for name, items in base.items() if name not in skip for condition in items]

    def joined(*columns):
        return select(*columns).select_from(DeviceAlert).join(Connector, Connector.id == DeviceAlert.connector_id)

    queue_rows = db.execute(
        joined(DeviceAlert.connector_id, DeviceAlert.state, DeviceAlert.acknowledged_at.is_(None),
               DeviceAlert.code, func.count(DeviceAlert.id))
        .where(*filters("state"), or_(
            and_(DeviceAlert.state != "RESOLVED", *first_seen_window),
            and_(DeviceAlert.state == "RESOLVED", *resolved_window),
        ))
        .group_by(DeviceAlert.connector_id, DeviceAlert.state, DeviceAlert.acknowledged_at.is_(None),
                  DeviceAlert.code)
    ).all()
    severity_rows = db.execute(
        joined(DeviceAlert.connector_id, DeviceAlert.severity, func.count(DeviceAlert.id))
        .where(*filters("severity"), *page_window)
        .group_by(DeviceAlert.connector_id, DeviceAlert.severity)
    ).all()
    type_rows = db.execute(
        joined(DeviceAlert.connector_id, DeviceAlert.code, func.count(DeviceAlert.id))
        .where(*filters("type"), *page_window)
        .group_by(DeviceAlert.connector_id, DeviceAlert.code)
    ).all()
    divisions = _division_of(db, {row[0] for row in (*queue_rows, *severity_rows, *type_rows)})

    def wanted(connector_pk: int) -> bool:
        return division is None or divisions.get(connector_pk) == division

    result = {
        "open": 0, "seen": 0, "legacy_seen": 0, "fixed": 0,
        "by_division": {name: {"open": 0, "seen": 0, "fixed": 0} for name in connector_naming.DIVISIONS},
        "by_severity": {name: 0 for name in SEVERITIES},
        "by_type": {name: 0 for name in problem_words.PROBLEM_TYPES},
        "legacy_by_code": {},
    }
    for connector_pk, state, unseen, code, count in queue_rows:
        count = int(count)
        if state == "OPEN":
            key = "open" if unseen else "seen"
        elif state == "ACKNOWLEDGED":
            key = "legacy_seen"
        elif state == "RESOLVED":
            key = "fixed"
        else:
            continue
        bucket = result["by_division"][divisions.get(connector_pk, "health")]
        if key in bucket:
            bucket[key] += count
        if not wanted(connector_pk):
            continue
        result[key] += count
        if key == "legacy_seen":
            result["legacy_by_code"][code] = result["legacy_by_code"].get(code, 0) + int(count)
    for connector_pk, severity, count in severity_rows:
        if wanted(connector_pk) and severity in result["by_severity"]:
            result["by_severity"][severity] += int(count)
    for connector_pk, code, count in type_rows:
        kind = problem_words.problem_type(code)
        if wanted(connector_pk) and kind is not None:
            result["by_type"][kind] += int(count)
    result["legacy_by_code"] = dict(sorted(result["legacy_by_code"].items()))
    return result


def operators_for_page(db: Session, page: list[tuple[DeviceAlert, Connector]]) -> dict[int, dict]:
    """Whether each open row can be marked as fixed, as the resolve action decides.

    One health evaluation for the page's connectors; the duplicate-serial
    lookup runs only for that code (the path of device_health_actions).
    """

    active = [(row, connector) for row, connector in page if row.state in OPEN_STATES]
    if not active:
        return {}
    now = utc_now()
    connectors = {connector.id: connector for _row, connector in active}
    health = evaluate_health_batch(db, connectors.values(), now=now)
    result = {}
    for row, connector in active:
        state = health[connector.id]
        current = next((item for item in state.reasons + state.other_active_alerts if item.alert_id == row.id), None)
        current = current or alert_reason(row, connector, now=now, link=state.terminal_link)
        policy = operator_policy(current, duplicate_claimed=(
            duplicate_serial_claimed(db, connector) if row.code == "QUARANTINED_DUPLICATE_SERIAL" else None))
        result[row.id] = {
            "resolvable": bool(policy["resolvable"]),
            "refusal_code": policy["refusal_code"],
            "refusal": policy["refusal"],
        }
    return result


def device_fields(connector: Connector) -> dict:
    return connector_naming.naming(connector)
