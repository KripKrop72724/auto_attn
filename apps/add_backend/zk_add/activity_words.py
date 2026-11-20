"""Plain words for device activity (log rows), from 02-words Appendix B.

A row's kind and group depend on (code, level, subsystem) only, never on the
message, so the SQL filters (kind_clause, topic_clause) and the sentence a
row gets always agree. The message and context only fill number and date
slots, through fixed name=value patterns; raw text is never echoed.

Display kinds (04-device): problem, review (handled on another page),
retried (routine that tries again), good (the fix line of a pair), routine.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Iterable, Mapping

from sqlalchemy import and_, false, or_

from zk_add.activity_words_data import WORDS

logger = logging.getLogger(__name__)

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")
TOPICS = ("machine", "link_box", "hr_system", "people", "software", "storage")
KINDS = ("problem", "review", "retried", "good", "routine")

# B3: any code not in the dictionary, by level.
FALLBACK = {
    "DEBUG": ("Other activity from the link box.", "routine", "no", None),
    "INFO": ("Other activity from the link box.", "routine", "no", None),
    "WARN": ("The link box reported a small issue and is handling it.", "routine", "if_repeats", None),
    "ERROR": ("The link box reported a problem.", "problem", "if_repeats", None),
    "CRITICAL": ("The link box reported a serious problem.", "problem", "yes", "Tell IT (details below)."),
}
FALLBACK_GROUP_SENTENCE = "{n} routine updates from the link box."
SUBSYSTEM_TOPICS = {
    "zkt": "machine", "hikvision": "machine", "reconcile": "machine", "zkt_diagnostic": "machine",
    "configuration": "machine", "attendance": "machine", "ords": "hr_system", "identity": "people",
    "users": "people", "enrollment": "people", "storage": "storage", "live": "storage", "ota": "software",
}

# B5 pairs: failure code -> the codes whose row marks it fixed.
FIX_PAIRS: dict[str, frozenset[str]] = {
    "ZKT_CONNECTION_LOST": frozenset({"ZKT_STABLE", "ZKT_SESSION_REFRESHED"}),
    "ZKT_TIME_SYNC_FAILED": frozenset({"ZKT_TIME_SYNC_OK"}),
    "ZKT_TIME_SYNC_READBACK_FAILED": frozenset({"ZKT_TIME_SYNC_OK"}),
    "HIK_LIVE_STREAM_RETRY": frozenset({"HIK_LIVE_STREAM_CONNECTED"}),
    "HIK_HISTORY_RETRY": frozenset({"HIK_HISTORY_PAGE_ACCEPTED"}),
    "HIK_IP_SAVE_PENDING": frozenset({"HIK_IP_REDISCOVERED"}),
    "ORDS_RECONCILE_TRANSPORT_RETRY": frozenset({"ORDS_RECONCILE_TRANSPORT_RECOVERED", "ORACLE_RECONCILE_ACCEPTED"}),
    "ORDS_RECONCILE_HTTP_FAILED": frozenset({"ORDS_RECONCILE_TRANSPORT_RECOVERED", "ORACLE_RECONCILE_ACCEPTED"}),
    "ORDS_RECONCILE_BACKOFF": frozenset({"ORDS_RECONCILE_TRANSPORT_RECOVERED", "ORACLE_RECONCILE_ACCEPTED"}),
    "TRUTH_READ_RETRY_SESSION": frozenset({"LIGHT_RECONCILE_OK", "ORACLE_RECONCILE_DELEGATED_TO_ADD"}),
    "TRUTH_READ_RETRY_EXHAUSTED": frozenset({"LIGHT_RECONCILE_OK", "ORACLE_RECONCILE_DELEGATED_TO_ADD"}),
    "FULL_RECONCILE_FAILED": frozenset({"LIGHT_RECONCILE_OK", "ORACLE_RECONCILE_DELEGATED_TO_ADD"}),
    "IDENTITY_CATALOG_STAGE_FAILED": frozenset({"IDENTITY_CATALOG_APPLIED"}),
    "IDENTITY_CATALOG_CHUNK_REJECTED": frozenset({"IDENTITY_CATALOG_APPLIED"}),
    "IDENTITY_CATALOG_COMMIT_REJECTED": frozenset({"IDENTITY_CATALOG_APPLIED"}),
    "LEASE_REVOKE_FAILED": frozenset({"LEASE_REVOKED_LOCAL"}),
    "ZKT_CREDENTIAL_POLICY_FAILED": frozenset({"ZKT_CREDENTIAL_POLICY_OK"}),
    "CURRENT_TAIL_CHECKPOINT_RETRY": frozenset({"CURRENT_TAIL_SOURCE_COMMITTED"}),
    "CURRENT_TAIL_AUDIT_RETRY": frozenset({"CURRENT_TAIL_SOURCE_COMMITTED"}),
    "ADD_SOURCE_CHECKPOINT_RETRY": frozenset({"CURRENT_TAIL_SOURCE_COMMITTED"}),
    "ZKT_SECURE_HANDSHAKE_FAILED": frozenset({"ZKT_SECURE_SESSION"}),
}
# HIK_POLL_STATUS WARN is fixed by HIK_POLL_STATUS INFO (same code, by level).
SAME_CODE_FIXES = frozenset({"HIK_POLL_STATUS"})
FIX_CODES = frozenset().union(*FIX_PAIRS.values())
# B5 rule 6: "If it repeats" escalates at this many in 30 minutes (default 5).
ESCALATE_AT = {"LIVE_DIRECT_ACK_FALLBACK_FAILED": 3, "ADD_DIRECT_ACK_FALLBACK_FAILED": 3,
               "LIVE_EVENT_DURABILITY_DEFERRED": 3}
ESCALATION_TITLES = {
    "ZKT_CONNECTION_LOST": "Lost the connection to the machine {n} times in 30 minutes.",
    "ADD_MESSAGE_PROCESSING_FAILED": "The dashboard is struggling to save data from this link box. Tell IT.",
    "LIVE_EVENT_DURABILITY_DEFERRED": ("Check-ins keep failing to save in the link box. Tell IT; the link box memory "
                                       "may need repair."),
    "LIVE_DIRECT_ACK_FALLBACK_FAILED": ("Check-ins keep failing to save in the link box. Tell IT; the link box memory "
                                        "may need repair."),
    "ADD_DIRECT_ACK_FALLBACK_FAILED": ("Check-ins keep failing to save in the link box. Tell IT; the link box memory "
                                       "may need repair."),
}
DEFAULT_ESCALATION_TITLE = "This happened {n} times in 30 minutes."

# B4 words.
CONNECTION_LOST_REASONS = (
    ("before the stability window", "The connection dropped right after connecting. Trying again."),
    ("did not find the assigned", "Can't find the machine on the network. Trying again."),
)
HIK_REASONS = {
    "ok": "all fine",
    "invalid_configuration": "the link box settings for this machine are incomplete",
    "network_timeout_or_unreachable": "the machine isn't answering on the network",
    "authentication_rejected": "the machine refused the password",
    "http_error": "the machine replied with an error",
    "response_too_large": "the machine's reply was too large",
    "identity_or_history_changed": "the machine's saved records changed",
    "invalid_response": "the machine's reply didn't make sense",
    "durable_storage_failed": "couldn't save check-ins in the link box",
    "unknown": "unknown reason",
}
HIK_LIGHT_STATES = {
    "WAITING": ("Record check waiting for its turn.", "routine", "no"),
    "SCANNING": ("Checking records saved on the machine ({n} so far).", "routine", "no"),
    "WAITING_DELIVERY": ("Record check paused until waiting check-ins are sent.", "routine", "no"),
    "WAITING_TERMINAL": ("Record check paused until the machine answers.", "routine", "if_repeats"),
    "RETRYING": ("Record check hit a problem ({reason}); trying again.", "routine", "if_repeats"),
    "COMPLETE": ("Finished checking records saved on the machine.", "routine", "no"),
    "BLOCKED": ("The record check is stuck ({reason}) and needs IT.", "problem", "yes"),
}
MESSAGE_TYPES = {
    "heartbeat": "a status report", "command_update": "a reply to a request", "log": "an activity message",
    "user_snapshot": "the list of people", "zkt_observation_batch": "raw records from the machine",
    "attendance_batch": "a batch of check-ins", "queue_evidence": "a backup of saved check-ins",
    "oracle_receipt_batch": "HR system receipts", "reconcile_anchor": "the start of a record check",
    "reconcile_chunk": "part of a record check", "reconcile_source_manifest": "a record-check summary",
    "reconcile_assignment_release": "the end of a record check",
    "source_probe_result": "the answer to a record lookup", "source_tail_chunk": "the newest-records check",
    "hikvision_profile_page": "a page of people from the machine",
    "hikvision_history_page": "a page of older records", "hikvision_observation": "a check-in",
}
MESSAGE_TYPE_OTHER = "some information"
REJECTION_CATEGORIES = {
    "SCHEMA_INVALID": "it was in the wrong format",
    "EVIDENCE_INVALID": "it didn't match what the dashboard expected",
    "CONSTRAINT_CONFLICT": "it clashed with data already saved",
    "DATABASE_UNAVAILABLE": "the dashboard's database was busy",
    "OPERATION_TIMEOUT": "it took too long",
    "INTERNAL_ERROR": "of an unexpected dashboard error",
}
REJECTION_CATEGORY_OTHER = "of an unexpected dashboard error"
# Slots that are filled with a word when the row has no value for them.
SLOT_DEFAULTS = {"n": "some", "dates": "some days", "m": "several", "type": MESSAGE_TYPE_OTHER,
                 "category": REJECTION_CATEGORY_OTHER, "reason": "unknown reason"}
# Where a slot's number is read from: context keys, then message name=value keys.
NUMBER_KEYS = ("count", "n", "events", "records", "rows", "files", "batches", "exceptions", "exception_count",
               "queued", "failures", "tries", "round", "quarantined", "unreadable")
SLOT_KEYS = {
    "rows": ("rows",), "s": ("seconds",), "p": ("percent", "used_percent", "pct"), "time": ("verified_pst", "time"),
}
_PAIR = re.compile(r"([A-Za-z_]+)=([^;,\s]+)")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SLOT = re.compile(r"\{([a-z_ ]+)\}")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_unknown_seen: set[str] = set()
unknown_code_count = 0  # activity_words_unknown_code counter


def _level(level: str | None) -> str:
    value = (level or "INFO").upper()
    return value if value in LEVELS else "INFO"


def _entry(code: str | None) -> dict | None:
    return WORDS.get(code or "")


def topic_of(code: str | None, subsystem: str | None) -> str:
    entry = _entry(code)
    if entry is not None:
        return entry["group"]
    return SUBSYSTEM_TOPICS.get((subsystem or "").lower(), "link_box")


def classify(code: str | None, level: str | None) -> tuple[str, str, str | None]:
    """(base kind routine|problem, act, action) for a code at a level."""
    level = _level(level)
    entry = _entry(code)
    if entry is None:
        _sentence, kind, act, action = FALLBACK[level]
        return kind, act, action
    kind = entry.get("kind_by_level", {}).get(level, entry["kind"])
    act = entry.get("act_by_level", {}).get(level, entry["act"])
    action = entry.get("action_by_level", {}).get(level, entry.get("action"))
    return kind, act, action


# 04-device's event table: the display kind of each live code (binding for Activity).
_LIVE_KINDS = {
    "retried": ("ADD_DIRECT_ACK_FALLBACK_FAILED", "ADD_DIRECT_ACK_FALLBACK_SUCCEEDED", "ADD_MESSAGE_PROCESSING_FAILED",
                "IDENTITY_CATALOG_MEMORY_FALLBACK", "LEGACY_QUEUE_READ_FAILED", "LIVE_DIRECT_ACK_FALLBACK_FAILED",
                "LIVE_DIRECT_ACK_FALLBACK_SUCCEEDED", "LIVE_EVENT_DURABILITY_DEFERRED",
                "ORDS_DRAIN_STORAGE_BACKPRESSURE", "OTA_IMAGE_UNVERIFIED", "STORAGE_REPAIR_EXCEPTION_REFUSED",
                "ZKT_CONNECTION_LOST", "ZKT_TIME_SYNC_READBACK_FAILED"),
    "routine": ("ADD_RECONNECTED", "ADD_SOURCE_CHECKPOINT_RETRY", "ADD_SOURCE_COVERAGE_APPLIED",
                "CURRENT_TAIL_AUDIT_RETRY", "CURRENT_TAIL_CHECKPOINT_RETRY", "CURRENT_TAIL_SOURCE_COMMITTED",
                "FULL_RECONCILE", "IDENTITY_CATALOG_APPLIED", "LIGHT_RECONCILE_OK", "LIVE_OUTBOX_LOCK_RECOVERED",
                "ORACLE_RECONCILE_DELEGATED_TO_ADD", "ORDS_LEGACY_LOCK_SLOW", "SCHEDULED_RESTART",
                "STORAGE_REPAIR_CHECKED", "STORAGE_REPAIR_GAP", "STORAGE_REPAIR_STARTED", "ZKT_CREDENTIAL_POLICY_OK",
                "ZKT_FRAME_SESSION_MISMATCH", "ZKT_SESSION_REFRESHED", "ZKT_TIME_SYNC_DEFERRED", "ZKT_TIME_SYNC_OK"),
    "good": ("ADD_SOURCE_COVERAGE_CERTIFIED", "LEASE_REVOKED_LOCAL", "LIVE_LOCAL_STORAGE_RECOVERED", "ZKT_STABLE"),
    "review": ("ATTENDANCE_TIMESTAMP_QUARANTINED", "CURRENT_TAIL_SOURCE_EXCEPTIONS_COMMITTED",
               "TRUTH_IDENTITY_INCOMPLETE"),
    "problem": ("BLOCKED_IDENTITY_RECOVERY_DEFERRED", "DEVICE_MESSAGE_REJECTED", "FULL_RECONCILE_FAILED",
                "STORAGE_FILE_UNREADABLE", "ZKT_SERIAL_MISMATCH"),
}
LIVE_KINDS: dict[str, str] = {code: kind for kind, codes in _LIVE_KINDS.items() for code in codes}
LIVE_KINDS_BY_LEVEL = {
    "HIK_CLOCK_SAMPLE": {"INFO": "routine", "WARN": "retried"},
    "HIK_POLL_STATUS": {"INFO": "routine", "WARN": "retried"},
    "STORAGE_INVENTORY": {"INFO": "routine", "WARN": "problem"},
    "STORAGE_REPAIR_DONE": {"INFO": "good", "WARN": "retried", "ERROR": "problem"},
}


def display_kind(code: str | None, level: str | None) -> str:
    by_level = LIVE_KINDS_BY_LEVEL.get(code or "", {})
    if _level(level) in by_level:
        return by_level[_level(level)]
    if code in LIVE_KINDS:
        return LIVE_KINDS[code]
    kind, act, _action = classify(code, level)
    if kind == "problem":
        return "problem"
    if act == "list":
        return "review"
    if act == "if_repeats":
        return "retried"
    if code in FIX_CODES:
        return "good"
    return "routine"


def _values(message: str | None, context: Mapping | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in _PAIR.findall(message or ""):
        values.setdefault(key.lower(), value)
    for key, value in (context or {}).items():
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            values[str(key).lower()] = str(value)
    return values


def _number(values: Mapping[str, str], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = values.get(key)
        if value is not None and re.fullmatch(r"-?\d+(\.\d+)?", value):
            return value
    return None


def _day(value: str) -> str | None:
    match = _DATE.search(value)
    if not match:
        return None
    try:
        day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    return f"{day.day} {MONTHS[day.month - 1]}"


def _dates(message: str | None, values: Mapping[str, str]) -> str | None:
    found = [_day(match.group(0)) for match in _DATE.finditer(message or "")]
    found = [value for value in found if value]
    if not found:
        return None
    first, last = found[0], found[-1]
    return first if first == last else f"{first} to {last}"


def fill(template: str, slots: Mapping[str, str | None]) -> str:
    """Fill {x} slots; a bracket whose slot has no value is dropped, other slots get a word."""
    def missing(name: str) -> bool:
        return slots.get(name) in (None, "")

    text = re.sub(r"\s*\(([^()]*)\)", lambda m: "" if any(missing(n) for n in _SLOT.findall(m.group(1)))
                  else m.group(0), template)
    text = _SLOT.sub(lambda m: str(slots.get(m.group(1)) or SLOT_DEFAULTS.get(m.group(1), "")), text)
    text = re.sub(r"\s{2,}", " ", text).replace(" .", ".").replace(" ;", ";").replace(" ,", ",").strip()
    return text[:1].upper() + text[1:] if text else text


def _slots(code: str | None, message: str | None, context: Mapping | None) -> dict[str, str | None]:
    values = _values(message, context)
    slots: dict[str, str | None] = {"n": _number(values, NUMBER_KEYS), "dates": _dates(message, values)}
    for slot, keys in SLOT_KEYS.items():
        slots[slot] = _number(values, keys) if slot != "time" else None
    if code == "ORDS_LEGACY_LOCK_SLOW":
        held = re.search(r"(\d+)\s*ms", message or "")
        slots["s"] = f"{int(held.group(1)) / 1000:.1f}".rstrip("0").rstrip(".") if held else None
    if code == "CURRENT_TAIL_SOURCE_EXCEPTIONS_COMMITTED":
        match = re.search(r"with (\d+) fail-closed", message or "")
        slots["n"] = match.group(1) if match else slots["n"]
    if code == "HIK_CLOCK_SAMPLE":
        slots["n"] = _number(values, ("drift", "drift_seconds"))
    if code in {"ADD_MESSAGE_PROCESSING_FAILED", "DEVICE_MESSAGE_REJECTED"}:
        match = re.match(r"\s*(?:ADD could not process an? )?([a-z_]+) message", message or "")
        kind = match.group(1) if match else str((context or {}).get("message_type") or "")
        slots["type"] = MESSAGE_TYPES.get(kind, MESSAGE_TYPE_OTHER)
        category = re.search(r"\(([A-Z_]+)", message or "")
        name = category.group(1) if category else str((context or {}).get("category") or "")
        slots["category"] = REJECTION_CATEGORIES.get(name, REJECTION_CATEGORY_OTHER)
    reason = values.get("reason")
    slots["reason"] = HIK_REASONS.get((reason or "").lower()) if reason else None
    return slots


def _variant(code: str | None, level: str, message: str | None, context: Mapping | None):
    """B4: a sentence (and kind, act) chosen from the message; None for the dictionary's own."""
    text = message or ""
    if code == "ZKT_CONNECTION_LOST":
        for needle, sentence in CONNECTION_LOST_REASONS:
            if needle in text:
                return sentence, None, None
    if code == "FULL_RECONCILE_FAILED" and ("unsafe or rejected" in text or "fail-closed" in text):
        return ("A record check stopped because some records couldn't be matched safely. Nothing was changed in "
                "the HR system; it will try again tonight."), None, "list"
    if code == "FULL_RECONCILE" and "historical=true" in text.lower():
        return "Started checking older records (overnight).", None, None
    if code == "HIK_LIGHT_RECONCILE":
        state = re.search(r"Light audit ([A-Z_]+)", text)
        if state and state.group(1) in HIK_LIGHT_STATES:
            return HIK_LIGHT_STATES[state.group(1)]
    if code == "ZKT_CREDENTIAL_POLICY_FAILED" and "duplicate" in text.lower():
        return ("Two people share the same number on the machine, so PIN or card check-ins couldn't be removed.",
                "problem", "yes")
    if code == "ZKT_TIME_SYNC_FAILED" and "pakistan" in text.lower() and "could not" in text.lower():
        return "Couldn't work out Pakistan time to set the machine's clock. Trying again.", None, None
    return None


def plain_log(row) -> dict:
    """The plain sentence of one stored log row, with its kind, topic and what to do.

    ``row`` is a DeviceLog (or anything with code, level, subsystem, message, context).
    """
    global unknown_code_count
    code = row.code
    level = _level(row.level)
    entry = _entry(code)
    kind = display_kind(code, level)
    base_kind, act, action = classify(code, level)
    if entry is None:
        sentence = FALLBACK[level][0]
        if code and code not in _unknown_seen:
            _unknown_seen.add(code)
            unknown_code_count += 1
            logger.info("activity_words_unknown_code %s", code)
    else:
        sentence = entry.get("by_level", {}).get(level, entry["sentence"])
    variant = _variant(code, level, row.message, row.context)
    if variant is not None:
        sentence, variant_kind, variant_act = variant
        if variant_kind == "problem":
            kind = "problem"
        if variant_act == "list":
            kind, act = "review", "list"
        elif variant_act:
            act = variant_act
    slots = _slots(code, row.message, row.context)
    title = fill(sentence, slots)
    if code == "HIK_POLL_STATUS" and level == "INFO":
        queued = _number(_values(row.message, row.context), ("queued",))
        if queued and queued != "0":
            title += f" {queued} check-ins waiting to be sent."
    return {
        "kind": kind,
        "serious": level == "CRITICAL" and kind == "problem",
        "topic": topic_of(code, row.subsystem),
        "title": title,
        "act": act,
        "action": fill(action, slots) if action else None,
        "known": entry is not None,
    }


def group_title(code: str | None, level: str | None, count: int, sample=None) -> str:
    """The sentence of a folded run of one code."""
    entry = _entry(code)
    template = entry.get("group_sentence") if entry else None
    if template:
        return fill(template, {"n": str(count)})
    if entry is None and display_kind(code, level) == "routine":
        return fill(FALLBACK_GROUP_SENTENCE, {"n": str(count)})
    single = plain_log(sample)["title"] if sample is not None else (
        entry.get("by_level", {}).get(_level(level), entry["sentence"]) if entry else FALLBACK[_level(level)][0])
    return f"{fill(single, {})} ({count} times)"


def escalation_title(code: str | None, count: int) -> str:
    return fill(ESCALATION_TITLES.get(code or "", DEFAULT_ESCALATION_TITLE), {"n": str(count)})


def escalates_at(code: str | None) -> int:
    return ESCALATE_AT.get(code or "", 5)


def _pairs_for(kinds: set[str]) -> tuple[list[tuple[str, str]], set[str]]:
    """(code, level) pairs of known codes whose display kind is in ``kinds``."""
    pairs = []
    for code in WORDS:
        for level in LEVELS:
            if display_kind(code, level) in kinds:
                pairs.append((code, level))
    return pairs, {level for level in LEVELS if display_kind(None, level) in kinds}


def kind_clause(kinds: Iterable[str], code_column, level_column):
    """SQL: rows whose display kind is one of ``kinds`` (exactly what plain_log labels).

    B4 message variants (FULL_RECONCILE_FAILED, HIK_LIGHT_RECONCILE BLOCKED,
    the ZKT_CREDENTIAL_POLICY_FAILED duplicate) can raise a row's kind; their
    codes are included whenever their raised kind is asked for, and
    device_activity drops rows whose final kind is not wanted.
    """
    wanted = set(kinds)
    pairs, fallback_levels = _pairs_for(wanted)
    by_code: dict[str, list[str]] = {}
    for code, level in pairs:
        by_code.setdefault(code, []).append(level)
    terms = []
    every_level = [code for code, levels in by_code.items() if len(levels) == len(LEVELS)]
    if every_level:
        terms.append(code_column.in_(sorted(every_level)))
    for code, levels in sorted(by_code.items()):
        if len(levels) != len(LEVELS):
            terms.append(and_(code_column == code, level_column.in_(levels)))
    if wanted & {"problem", "review"}:
        terms.append(code_column.in_(sorted(VARIANT_CODES)))
    if fallback_levels:
        terms.append(and_(or_(code_column.is_(None), code_column.not_in(sorted(WORDS))),
                          level_column.in_(sorted(fallback_levels))))
    return or_(*terms) if terms else false()


VARIANT_CODES = frozenset({"FULL_RECONCILE_FAILED", "HIK_LIGHT_RECONCILE", "ZKT_CREDENTIAL_POLICY_FAILED"})


def topic_clause(topics: Iterable[str], code_column, subsystem_column):
    """SQL: rows whose group is one of ``topics`` (dictionary group, else the subsystem fallback)."""
    wanted = set(topics)
    codes = sorted(code for code, entry in WORDS.items() if entry["group"] in wanted)
    subsystems = sorted(name for name, topic in SUBSYSTEM_TOPICS.items() if topic in wanted)
    unknown = or_(code_column.is_(None), code_column.not_in(sorted(WORDS)))
    terms = []
    if codes:
        terms.append(code_column.in_(codes))
    if "link_box" in wanted:
        terms.append(and_(unknown, or_(subsystem_column.is_(None), subsystem_column.not_in(sorted(SUBSYSTEM_TOPICS)))))
    if subsystems:
        terms.append(and_(unknown, subsystem_column.in_(subsystems)))
    return or_(*terms) if terms else false()
