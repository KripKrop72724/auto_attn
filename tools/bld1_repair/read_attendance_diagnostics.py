"""Fixed-device, database-enforced read-only attendance diagnostics. No raw PII output."""
import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select, text

from zk_add.db import SessionLocal
from zk_add.models import AttendanceEvent, Connector
from zk_add.crypto import decrypt_text
from zk_add.hikvision_delivery import HikvisionPolicy
from zk_add.hikvision_evidence import HikvisionEvidence
from zk_add.hikvision_probe import decode_body

with SessionLocal() as session:
    session.execute(text("SET TRANSACTION READ ONLY"))
    connector = session.scalar(select(Connector).where(
        Connector.connector_id == "dacdee7c-f824-40cc-b4c0-3d873653f8db"))
    assert connector and connector.hardware_id.lower() == "ac:27:6e:a3:0a:58"
    terminal = connector.zkt_device
    assert terminal and terminal.serial == terminal.confirmed_serial == terminal.expected_serial == "G04600476"
    policy = session.get(HikvisionPolicy, connector.id)
    print(json.dumps({"policy": None if not policy else {
        "enabled": policy.enabled, "source_epoch": policy.source_epoch,
        "profile_id": policy.profile_id, "success_codes": policy.success_codes,
        "excluded_codes": policy.excluded_codes}}))
    print(json.dumps({"evidence_counts": [list(row) for row in session.execute(
        select(HikvisionEvidence.channel, HikvisionEvidence.disposition, func.count())
        .where(HikvisionEvidence.connector_id == connector.id)
        .group_by(HikvisionEvidence.channel, HikvisionEvidence.disposition))]}))
    print(json.dumps({"attendance_counts": [list(row) for row in session.execute(
        select(AttendanceEvent.ords_status, func.count())
        .where(AttendanceEvent.connector_id == connector.id).group_by(AttendanceEvent.ords_status))]}))
    records = list(session.scalars(select(HikvisionEvidence).where(
        HikvisionEvidence.connector_id == connector.id,
        HikvisionEvidence.source_event_id == "593441").limit(5)))
    records += list(session.scalars(select(HikvisionEvidence).where(
        HikvisionEvidence.connector_id == connector.id).order_by(HikvisionEvidence.id.desc()).limit(8)))
    for evidence in records:
        raw = decrypt_text(evidence.raw_encrypted)
        data = decode_body(raw.encode())
        root = data.get("EventNotificationAlert", data)
        event = root.get("AccessControllerEvent", root)
        allowed = ("serialNo", "major", "minor", "majorEventType", "subEventType",
                   "time", "currentEvent", "currentVerifyMode", "attendanceStatus")
        stable = None
        if "time" in event and all(key in event for key in ("serialNo", "major", "minor")):
            instant = int(datetime.fromisoformat(event["time"]).timestamp())
            stable = hashlib.sha256((f'HIK_ANCHOR_V2\n{event["serialNo"]}\n{event["major"]}\n{event["minor"]}\n{event.get("employeeNoString") or ""}\n{instant}').encode()).hexdigest()
        print(json.dumps({"evidence_id": evidence.id, "source_epoch": evidence.source_epoch,
            "channel": evidence.channel, "disposition": evidence.disposition,
            "observation_sha256": evidence.observation_sha256,
            "stable_anchor_v2": stable,
            "raw_sha256_matches": hashlib.sha256(raw.encode()).hexdigest() == evidence.observation_sha256,
            "root_time": root.get("dateTime"), "fields": sorted(event),
            "metadata": {key: event[key] for key in allowed if key in event}}))
    session.rollback()
