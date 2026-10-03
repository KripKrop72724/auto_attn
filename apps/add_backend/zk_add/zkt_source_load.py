"""Read a bounded source-occurrence workload without counting delivery attempts.

This is observed load, not profile/time qualification or an OTA authorization.
No protected record, identity or attendance payload is decrypted or returned.
"""
from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import BigInteger, case, cast, func, or_, select, text, true
from sqlalchemy.exc import DBAPIError

from zk_add.models import Connector, ReconciliationCoverage, TerminalRecordManifest, TerminalSourceEpoch, ZKTDevice
from zk_add.time_utils import utc_now
from zk_add.zkt_decode import DecodeError, PAKISTAN_TIME, decode_time, encode_time

WINDOW_DAYS = 30
MINUTE_GROUP_LIMIT = 33 * 24 * 60  # Packed ZKT calendars can include nonexistent dates.


def _snapshot(session, connector_id):
    rows = session.execute(select(
        Connector.firmware_family.label("family"), Connector.onboarding_generation.label("binding_generation"),
        ZKTDevice.id.label("device_id"), ZKTDevice.confirmed_serial.label("confirmed_serial"),
        ZKTDevice.serial.label("live_serial"), ZKTDevice.attendance_count.label("terminal_count"),
        ReconciliationCoverage.id.label("coverage_id"), ReconciliationCoverage.source_epoch_id.label("epoch_pk"),
        ReconciliationCoverage.terminal_serial.label("serial"),
        ReconciliationCoverage.terminal_generation.label("generation"),
        ReconciliationCoverage.source_committed_cursor.label("cursor"),
        ReconciliationCoverage.source_committed_chain_digest.label("chain"),
        ReconciliationCoverage.capture_state.label("capture_state"),
        TerminalSourceEpoch.epoch_id.label("epoch"), TerminalSourceEpoch.state.label("epoch_state"),
        TerminalSourceEpoch.zkt_device_id.label("epoch_device_id"),
        TerminalSourceEpoch.terminal_generation.label("epoch_generation"),
    ).select_from(Connector).join(ZKTDevice, ZKTDevice.connector_id == Connector.id)
        .join(ReconciliationCoverage, ReconciliationCoverage.zkt_device_id == ZKTDevice.id)
        .outerjoin(TerminalSourceEpoch, TerminalSourceEpoch.id == ReconciliationCoverage.source_epoch_id)
        .where(Connector.id == connector_id, ReconciliationCoverage.active.is_(True)).limit(2)).mappings().all()
    return dict(rows[0]) if len(rows) == 1 else None


def source_load_baseline(session, connector, *, now: datetime | None = None):
    # Keep this manual diagnostic from delaying live custody. PostgreSQL local
    # settings are restored on success and rolled back with the savepoint on
    # error; they do not leak into the caller's remaining transaction.
    try:
        with session.begin_nested():
            postgres = session.get_bind().dialect.name == "postgresql"
            if postgres:
                previous = session.execute(text("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")).one()
                session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
                session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            result = _measure_source_load(session, connector, now=now)
            if postgres:
                session.execute(text("SELECT set_config('statement_timeout', :statement, true), set_config('lock_timeout', :lock, true)"),
                                {"statement": previous[0], "lock": previous[1]})
            return result
    except DBAPIError as exc:
        code = getattr(exc.orig, "sqlstate", None)
        reason = "BASELINE_QUERY_DEADLINE" if code in {"57014", "55P03"} else "BASELINE_STORAGE_UNAVAILABLE"
        raise ValueError(reason) from exc


def _measure_source_load(session, connector, *, now: datetime | None = None):
    if connector.firmware_family != "zkt" or connector.zkt_device is None:
        raise ValueError("ZKT_SOURCE_REQUIRED")
    sampled = now or utc_now()
    if sampled.tzinfo is None:
        raise ValueError("BASELINE_TIMEZONE_REQUIRED")
    end = sampled.astimezone(PAKISTAN_TIME).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=WINDOW_DAYS)
    lower, upper = encode_time(start), encode_time(end)
    result = {"schema_version": 1, "connector_id": connector.connector_id,
        "sampled_at": sampled, "timezone": "Asia/Karachi", "window_start": start, "window_end_exclusive": end,
        "requested_days": WINDOW_DAYS, "grain": "CANONICAL_SOURCE_ORDINAL",
        "baseline_complete": False, "qualification": "NOT_ASSERTED",
        "source_snapshot": None, "source_inventory_complete": False, "coherent_snapshot": False,
        "observed_occurrences": None, "peak_daily_observed_occurrences": None,
        "peak_minute_observed_occurrences": None, "daily": [], "reasons": []}
    reasons = result["reasons"]
    initial = _snapshot(session, connector.id)
    if initial is None:
        reasons.append("ACTIVE_SOURCE_COVERAGE_UNAVAILABLE")
        return result
    if (initial["family"] != "zkt" or not initial["confirmed_serial"]
            or initial["serial"] != initial["confirmed_serial"]
            or (initial["live_serial"] and initial["live_serial"] != initial["confirmed_serial"])
            or initial["generation"] != max(1, initial["binding_generation"] or 0)
            or initial["epoch_device_id"] != initial["device_id"]
            or initial["epoch_generation"] != initial["generation"]
            or initial["epoch_state"] != "ACTIVE"):
        reasons.append("SOURCE_BINDING_OR_EPOCH_UNVERIFIED")
        return result
    cursor, chain = initial["cursor"], initial["chain"]
    if (cursor is None or cursor < 0 or not isinstance(chain, str) or len(chain) != 64
            or any(value not in "0123456789abcdef" for value in chain)):
        reasons.append("SOURCE_CURSOR_OR_CHAIN_INVALID")
        return result
    result["source_snapshot"] = {"epoch": initial["epoch"], "generation": initial["generation"],
        "committed_cursor": cursor, "chain_digest": chain}
    manifest = TerminalRecordManifest
    scope = (manifest.connector_id == connector.id, manifest.zkt_device_id == initial["device_id"],
        manifest.source_epoch_id == initial["epoch_pk"], manifest.terminal_serial == initial["serial"],
        manifest.generation == initial["generation"], manifest.canonical_source.is_(True),
        manifest.ordinal >= 0, manifest.ordinal < cursor)
    unusable = or_(manifest.raw_timestamp.is_(None), manifest.raw_timestamp < 0,
                   manifest.raw_timestamp >= 100 * 12 * 31 * 86400,
                   manifest.disposition.in_(["INVALID_TIME", "MALFORMED"]),
                   manifest.record_size.is_(None), manifest.record_size.not_in([8, 16, 40]))
    inventory_query = select(func.count(manifest.id).label("inventory_count"),
        func.min(manifest.ordinal).label("first_ordinal"), func.max(manifest.ordinal).label("last_ordinal"),
        func.coalesce(func.sum(case((unusable, 1), else_=0)), 0).label("unusable_count"),
        func.coalesce(func.sum(case((or_(manifest.protected_raw_record.is_(None),
                                         manifest.protected_raw_record == ""), 1), else_=0)), 0).label("missing_count")
        ).where(*scope).subquery()
    # Both aggregates use one database statement snapshot. Comparing only the
    # coverage cursor would not detect a concurrent manifest correction between
    # separate inventory and workload queries.
    minute = cast(func.floor(manifest.raw_timestamp / 60), BigInteger)
    minute_query = select(minute.label("encoded_minute"), func.count(manifest.id).label("occurrences"))\
        .where(*scope, ~unusable, manifest.raw_timestamp >= lower, manifest.raw_timestamp < upper)\
        .group_by(minute).order_by(minute).limit(MINUTE_GROUP_LIMIT + 1).subquery()
    measured = session.execute(select(inventory_query, minute_query)
        .select_from(inventory_query.outerjoin(minute_query, true()))).all()
    count, first, last, invalid, missing = measured[0][:5]
    groups = [(row[5], row[6]) for row in measured if row[5] is not None]
    inventory = count == cursor and (cursor == 0 or (first == 0 and last == cursor - 1))
    result["source_inventory_complete"] = inventory
    result["inventoried_occurrences"] = count
    result["unusable_source_records"] = invalid
    result["missing_raw_evidence_records"] = missing
    if not inventory:
        reasons.append("SOURCE_ORDINALS_MISSING")
    if initial["terminal_count"] is None or initial["terminal_count"] != cursor:
        reasons.append("TERMINAL_COUNT_NOT_COVERED")
    if initial["capture_state"] not in {"SOURCE_CAPTURE_CERTIFIED", "SOURCE_CAPTURE_CERTIFIED_WITH_EXCEPTIONS"}:
        reasons.append("SOURCE_CUSTODY_UNCERTIFIED")
    if invalid:
        reasons.append("UNUSABLE_SOURCE_TIMESTAMPS_OR_LAYOUTS")
    if missing:
        reasons.append("RAW_SOURCE_EVIDENCE_MISSING")

    # Group only by source time, never employee, event UID, outbox or attempt.
    # Distinct canonical ordinals with identical bytes still contribute twice.
    if len(groups) > MINUTE_GROUP_LIMIT:
        reasons.append("SOURCE_MINUTE_GROUP_LIMIT")
        return result
    days = {(start + timedelta(days=offset)).date(): 0 for offset in range(WINDOW_DAYS)}
    peak_minute = invalid_calendar = 0
    for encoded_minute, occurrences in groups:
        try:
            local, _ = decode_time(encoded_minute * 60)
        except DecodeError:
            invalid_calendar += occurrences
            continue
        if local.date() not in days:
            invalid_calendar += occurrences
            continue
        days[local.date()] += occurrences
        peak_minute = max(peak_minute, occurrences)
    if invalid_calendar:
        reasons.append("INVALID_CALENDAR_RECORDS_IN_WINDOW")
    result["invalid_calendar_records_in_window"] = invalid_calendar
    result["daily"] = [{"date": day.isoformat(), "observed_occurrences": count} for day, count in days.items()]
    result["observed_occurrences"] = sum(days.values())
    result["peak_daily_observed_occurrences"] = max(days.values())
    result["peak_minute_observed_occurrences"] = peak_minute
    result["coherent_snapshot"] = _snapshot(session, connector.id) == initial
    if not result["coherent_snapshot"]:
        reasons.append("SOURCE_CHANGED_DURING_MEASUREMENT")
    # A full ordinal inventory and plausible timestamps do not independently
    # prove the model, clock history or closure of the requested calendar window.
    reasons.extend(["PROTOCOL_PROFILE_QUALIFICATION_REQUIRED", "SOURCE_CLOCK_AND_WINDOW_CLOSURE_REQUIRED"])
    material = {"source": result["source_snapshot"], "window_start": start.isoformat(),
        "window_end_exclusive": end.isoformat(), "daily": result["daily"], "peak_minute": peak_minute,
        "inventoried": count, "unusable": invalid, "missing_raw": missing, "invalid_calendar": invalid_calendar}
    result["measurement_digest"] = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result
