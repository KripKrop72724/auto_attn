"""A device's activity in plain words: log rows folded, paired and escalated,
plus dashboard requests and problem events for the same time window.

Read-only. It returns at most the stored, redacted log row (``raw`` is
exactly serialize_log); command payloads are never returned.

Rules (02-words Appendix B5):
* consecutive rows with the same code and sentence fold into one group;
* "On its list" rows fold once per Pakistan day per code;
* a failure followed by its fix line gets fixed_at;
* "If it repeats" rows escalate to a problem at 5 (some codes 3) in
  30 minutes with no fix line between them;
* paging is by raw row ("<received_at>|<id>" cursors), so pages never
  repeat or skip a row; a fold never spans two pages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zk_add import activity_words
from zk_add.models import Connector, DeviceAlert, DeviceCommand, DeviceLog
from zk_add.problem_words import plain as problem_plain
from zk_add.time_utils import ensure_utc, iso_utc, utc_now

RAW_PAGE_ROWS = 2000
DEFAULT_WINDOW = timedelta(hours=24)
RETENTION_DAYS = 14
ESCALATION_WINDOW = timedelta(minutes=30)
PKT = timezone(timedelta(hours=5))
PROBLEM_KINDS = frozenset({"problem", "review"})
COMMAND_WORDS = {
    "REFRESH_USERS": ("refreshed the people list.", "machine"),
    "RESTART_ZKT": ("restarted the machine.", "machine"),
    "CREATE_USER": ("added a person to the machine.", "people"),
    "UPDATE_USER": ("changed a person on the machine.", "people"),
    "DELETE_USER": ("removed a person from the machine.", "people"),
    "GRANT_TEMP_ADMIN": ("gave temporary admin access.", "people"),
    "REVOKE_TEMP_ADMIN": ("ended temporary admin access.", "people"),
    "PIN_TERMINAL_SERIAL": ("confirmed the new machine.", "machine"),
    "APPLY_CONFIG": ("changed the machine password.", "machine"),
}
COMMAND_OTHER = ("sent a request to the device.", "link_box")
DONE_STATES = {"SUCCEEDED": "Confirmed.", "COMPLETED": "Confirmed."}
FAILED_STATES = frozenset({"FAILED", "REJECTED", "EXPIRED", "CANCELED", "CANCELLED", "TIMED_OUT"})


class ActivityCursorError(ValueError):
    pass


def parse_cursor(value: str | None) -> tuple[datetime | None, int] | None:
    """(received_at, log id) of a "<received_at>|<id>" cursor."""
    if not value:
        return None
    moment, _bar, raw_id = value.rpartition("|")
    try:
        log_id = int(raw_id)
        at = ensure_utc(datetime.fromisoformat(moment.replace("Z", "+00:00"))) if moment else None
    except ValueError as error:
        raise ActivityCursorError("The activity cursor is not valid.") from error
    return at, log_id


def _cursor(row: DeviceLog) -> str:
    return f"{iso_utc(row.received_at)}|{row.id}"


def _day(value: datetime) -> str:
    return ensure_utc(value).astimezone(PKT).date().isoformat()


@dataclass
class _Row:
    log: DeviceLog
    words: dict
    fixed_at: datetime | None = None
    escalated: bool = False
    burst: list = field(default_factory=list)


def _classify(rows: Iterable[DeviceLog]) -> list[_Row]:
    return [_Row(row, activity_words.plain_log(row)) for row in rows]


def _pair_fixes(rows: list[_Row]) -> None:
    """Oldest to newest: a fix line marks every open failure it fixes."""
    open_failures: dict[str, list[_Row]] = {}
    for item in reversed(rows):
        code, level = item.log.code, (item.log.level or "").upper()
        same_code_fix = code in activity_words.SAME_CODE_FIXES and level == "INFO"
        for failure_code, fixes in activity_words.FIX_PAIRS.items():
            if code in fixes:
                for failure in open_failures.pop(failure_code, []):
                    failure.fixed_at = item.log.received_at
        if same_code_fix:
            for failure in open_failures.pop(code, []):
                failure.fixed_at = item.log.received_at
        elif code in activity_words.FIX_PAIRS or (code in activity_words.SAME_CODE_FIXES and level != "INFO"):
            open_failures.setdefault(code, []).append(item)


def _escalate(rows: list[_Row]) -> None:
    """Mark bursts of "If it repeats" rows (no fix line between) as escalated."""
    runs: dict[str, list[_Row]] = {}
    for item in reversed(rows):  # oldest first
        code = item.log.code or ""
        level = (item.log.level or "").upper()
        for failure_code, fixes in activity_words.FIX_PAIRS.items():
            if code in fixes:
                runs.pop(failure_code, None)
        if code in activity_words.SAME_CODE_FIXES and level == "INFO":
            runs.pop(code, None)
            continue
        if item.words["kind"] != "retried":
            continue
        run = runs.setdefault(code, [])
        run.append(item)
        start = item.log.received_at - ESCALATION_WINDOW
        while run and ensure_utc(run[0].log.received_at) < ensure_utc(start):
            run.pop(0)
        if len(run) >= activity_words.escalates_at(code):
            # One escalation per burst, shown at its newest row.
            for member in run:
                member.escalated = True
                member.burst = []
            run[-1].burst = list(run)


def _log_item(item: _Row) -> dict:
    words = item.words
    return {
        "type": "event", "id": item.log.id, "at": item.log.received_at, "kind": words["kind"],
        "serious": words["serious"], "topic": words["topic"], "title": words["title"],
        "action": words["action"] if words["kind"] in {"problem", "review"} or words["act"] == "yes" else None,
        "link": None, "fixed_at": item.fixed_at, "code": item.log.code, "raw": None,
    }


def _group_item(members: list[_Row]) -> dict:
    newest, oldest = members[0], members[-1]
    words = newest.words
    return {
        "type": "group", "kind": words["kind"], "topic": words["topic"], "code": newest.log.code,
        "count": len(members), "first_at": oldest.log.received_at, "last_at": newest.log.received_at,
        "first_id": oldest.log.id, "last_id": newest.log.id,
        "title": activity_words.group_title(newest.log.code, newest.log.level, len(members), newest.log),
        "action": words["action"] if words["kind"] == "review" else None,
        "fixed_at": newest.fixed_at,
    }


def _escalation_item(burst: list[_Row]) -> dict:
    newest, oldest = burst[-1], burst[0]
    return {
        "type": "escalation", "kind": "problem", "topic": newest.words["topic"], "code": newest.log.code,
        "count": len(burst), "first_at": oldest.log.received_at, "last_at": newest.log.received_at,
        "first_id": oldest.log.id, "last_id": newest.log.id,
        "title": activity_words.escalation_title(newest.log.code, len(burst)),
        "action": newest.words["action"],
        "fixed_at": newest.fixed_at,
    }


def fold(rows: list[_Row], serialize_log: Callable[[DeviceLog], dict]) -> list[dict]:
    """Newest first: rows into event, group and escalation items."""
    items: list[dict] = []
    burst_members = {id(member) for item in rows for member in item.burst}
    daily: dict[tuple[str, str], dict] = {}
    run: list[_Row] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:
            item = _log_item(run[0])
            item["raw"] = serialize_log(run[0].log)
            items.append(item)
        else:
            items.append(_group_item(list(run)))
        run.clear()

    for item in rows:
        if item.burst:
            flush()
            items.append(_escalation_item(item.burst))
            continue
        if id(item) in burst_members:
            continue  # shown inside its escalation
        words = item.words
        if words["act"] == "list" and words["kind"] == "review":
            flush()
            key = (item.log.code or "", _day(item.log.received_at))
            folded = daily.get(key)
            if folded is None:
                folded = daily[key] = {"members": []}
                folded["members"].append(item)
                folded["item"] = None
                items.append(folded)  # replaced below
            else:
                folded["members"].append(item)
            continue
        if run and (run[-1].log.code != item.log.code or run[-1].words["title"] != words["title"]
                    or words["kind"] == "problem" or run[-1].fixed_at != item.fixed_at):
            flush()
        run.append(item)
        if words["kind"] == "problem":
            flush()
    flush()
    result = []
    for item in items:
        if "members" in item:
            members = item["members"]
            if len(members) == 1:
                single = _log_item(members[0])
                single["raw"] = serialize_log(members[0].log)
                result.append(single)
            else:
                result.append(_group_item(members))
        else:
            result.append(item)
    return result


def _counts(db: Session, connector: Connector, since: datetime, until: datetime) -> dict:
    rows = db.execute(
        select(DeviceLog.code, DeviceLog.level, DeviceLog.subsystem, func.count(DeviceLog.id))
        .where(DeviceLog.connector_id == connector.id, DeviceLog.received_at >= since, DeviceLog.received_at < until)
        .group_by(DeviceLog.code, DeviceLog.level, DeviceLog.subsystem)
    ).all()
    counts = {"problems": 0, "retried": 0, "routine": 0, "by_topic": {topic: 0 for topic in activity_words.TOPICS}}
    for code, level, subsystem, count in rows:
        kind = activity_words.display_kind(code, level)
        if kind in PROBLEM_KINDS:
            counts["problems"] += count
            counts["by_topic"][activity_words.topic_of(code, subsystem)] += count
        elif kind == "retried":
            counts["retried"] += count
        else:
            counts["routine"] += count
    return counts


def _command_items(db: Session, connector: Connector, low: datetime, high: datetime, topics) -> list[dict]:
    rows = db.scalars(
        select(DeviceCommand).where(
            DeviceCommand.connector_id == connector.id,
            DeviceCommand.created_at >= low, DeviceCommand.created_at < high,
        ).order_by(DeviceCommand.created_at.desc()).limit(200)
    ).all()
    items = []
    for row in rows:
        words, topic = COMMAND_WORDS.get(row.command_type, COMMAND_OTHER)
        if topics and topic not in topics:
            continue
        status = (row.status or "").upper()
        if status in DONE_STATES:
            outcome = DONE_STATES[status]
        elif status in FAILED_STATES:
            outcome = "Didn't work."
        else:
            outcome = "Still waiting."
        items.append({
            "type": "action", "at": row.created_at, "command_id": row.command_id,
            "command_type": row.command_type, "status": row.status, "actor": row.actor,
            "title": f"{row.actor or 'Someone'} {words} {outcome}", "topic": topic, "kind": "action",
        })
    return items


def _alert_items(db: Session, connector: Connector, low: datetime, high: datetime, topics) -> list[dict]:
    rows = db.scalars(
        select(DeviceAlert).where(DeviceAlert.connector_id == connector.id).where(
            (DeviceAlert.first_seen_at.between(low, high))
            | (DeviceAlert.acknowledged_at.between(low, high))
            | (DeviceAlert.resolved_at.between(low, high))
        ).limit(200)
    ).all()
    items = []
    for row in rows:
        words = problem_plain(row.code, row.details or {}, severity=row.severity)
        topic = words["group"]
        if topics and topic not in topics:
            continue
        title = words["plain_title"]
        details = row.details or {}
        events = [("opened", row.first_seen_at, None, f"Problem started: {title}.")]
        if row.acknowledged_at is not None:
            actor = details.get("acknowledged_by")
            events.append(("seen", row.acknowledged_at, actor,
                           f"{actor or 'Someone'} marked “{title}” as seen."))
        if row.resolved_at is not None:
            resolution = details.get("resolution") if isinstance(details.get("resolution"), dict) else {}
            actor = resolution.get("actor") or resolution.get("resolved_by")
            if resolution.get("kind") == "OPERATOR" or actor:
                events.append(("fixed", row.resolved_at, actor,
                               f"{actor or 'Someone'} marked “{title}” as fixed."))
            else:
                events.append(("cleared", row.resolved_at, None, f"Fixed by itself: {title}."))
        for event, at, actor, text in events:
            if at is not None and ensure_utc(low) <= ensure_utc(at) <= ensure_utc(high):
                items.append({"type": "problem_event", "at": at, "alert_id": row.id, "event": event,
                              "actor": actor, "title": text, "topic": topic, "kind": "problem"})
    return items


def build_activity(
    db: Session,
    connector: Connector,
    *,
    serialize_log: Callable[[DeviceLog], dict],
    show: str = "all",
    topics: Iterable[str] = (),
    since: datetime | None = None,
    until: datetime | None = None,
    before: str | None = None,
    after: int | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> dict:
    now = now or utc_now()
    until = ensure_utc(until) if until else now
    since = ensure_utc(since) if since else until - DEFAULT_WINDOW
    topics = [topic for topic in topics if topic in activity_words.TOPICS]
    statement = select(DeviceLog).where(
        DeviceLog.connector_id == connector.id,
        DeviceLog.received_at >= since,
        DeviceLog.received_at < until,
    )
    cursor = None if after is not None else parse_cursor(before)
    if after is not None:
        statement = statement.where(DeviceLog.id > after)
    elif cursor is not None:
        statement = statement.where(DeviceLog.id < cursor[1])
    if topics:
        statement = statement.where(activity_words.topic_clause(topics, DeviceLog.code, DeviceLog.subsystem))
    if show == "problems":
        statement = statement.where(activity_words.kind_clause(
            {"problem", "review", "retried"}, DeviceLog.code, DeviceLog.level))
    raw = db.scalars(statement.order_by(DeviceLog.id.desc()).limit(RAW_PAGE_ROWS + 1)).all()
    more_raw = len(raw) > RAW_PAGE_ROWS
    raw = list(raw[:RAW_PAGE_ROWS])
    classified = _classify(raw)
    _pair_fixes(classified)
    _escalate(classified)

    # Take raw rows until the page holds ``limit`` items, never splitting a fold
    # decision across the cut: fold the prefix, and cut at a raw boundary.
    consumed = len(classified)
    items = fold(classified, serialize_log)
    if len(items) > limit:
        low, high = 0, len(classified)
        while low < high:  # the longest prefix that folds into at most ``limit`` items
            middle = (low + high + 1) // 2
            if len(fold(classified[:middle], serialize_log)) <= limit:
                low = middle
            else:
                high = middle - 1
        consumed = max(low, 1)
        items = fold(classified[:consumed], serialize_log)
    has_more = more_raw or consumed < len(classified)
    next_cursor = _cursor(classified[consumed - 1].log) if has_more and consumed else None

    if show == "problems":
        items = [item for item in items if item["kind"] in PROBLEM_KINDS or item["type"] == "escalation"]

    # Requests and problem events for the time this page covers.
    # A later page starts where the previous one stopped, so nothing is skipped.
    page_high = cursor[0] if cursor is not None and cursor[0] is not None else until
    page_low = ensure_utc(classified[consumed - 1].log.received_at) if has_more and consumed else since
    extra = _alert_items(db, connector, page_low, page_high, topics)
    if show != "problems":
        extra += _command_items(db, connector, page_low, page_high, topics)
    items.extend(extra)
    items.sort(key=lambda item: ensure_utc(item.get("at") or item.get("last_at")), reverse=True)

    newest_id = db.scalar(select(func.max(DeviceLog.id)).where(DeviceLog.connector_id == connector.id))
    return {
        "items": items,
        "next_cursor": next_cursor,
        "newest_id": newest_id,
        "counts": _counts(db, connector, since, until),
        "window": {"since": since, "until": until},
        "retention_days": RETENTION_DAYS,
    }


