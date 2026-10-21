"""Legacy dump punches held at ingest release on their batch and retained identity.

A 2.6.x reconnect/startup dump reaches ADD before the next roster snapshot, so
ingest holds every row. The next snapshot may release one only when a retained,
fingerprint-bound interval covering the punch carries the user's current CNIC.
Older holds wait for the owner's audited per-zone release.
"""

import base64
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from test_add_backend import (
    CNIC,
    SERIAL,
    connector_fixture,
    db as db,
    make_writable,
)
from test_reconciliation import (
    _raw_record,
    reconciliation_db as reconciliation_db,
)
from zk_add import attendance_identity_backlog as backlog
from zk_add.attendance_identity_evidence import identity_evidence
from zk_add.attendance_safe_repair import delivery_proof_valid
from zk_add.crypto import decrypt_cnic
from zk_add.models import (
    AttendanceEvent,
    AttendanceIdentityHistory,
    AttendanceRecoveryJob,
    AuditEvent,
    DeviceUser,
    OrdsOutbox,
    TerminalRecordManifest,
)
from zk_add.reconciliation import (
    apply_reconciliation_anchor,
    apply_reconciliation_chunk,
    create_reconciliation_job,
    reconciliation_chain_digest,
    reconciliation_chunk_digest,
)
from zk_add.schemas import (
    AttendanceEventIn,
    ReconciliationAnchorRequest,
    ReconciliationChunkRequest,
    ReconciliationSourceRecord,
    UserSnapshotRequest,
    UserSnapshotRow,
)
from zk_add.service import (
    ingest_attendance,
    replace_user_snapshot,
    repair_attendance_delivery_backlog,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now

FP = "a" * 64
OTHER_CNIC = "3520212345672"


def _connector(db):
    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    connector.zkt_device.confirmed_serial = connector.zkt_device.serial
    return connector


def _snapshot(db, connector, key, observed_at, *, cnic=CNIC, stable=True):
    replace_user_snapshot(
        db, connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id=key, complete=True, stable=stable, observed_at=observed_at,
            users=[UserSnapshotRow(
                uid="69", user_id="11", name=f"Synced Employee-{cnic}",
                terminal_identity_fingerprint=FP,
            )],
        ),
    )
    db.flush()


def _dump(seed, when, captured, *, uid="69", fingerprint=FP, source="DUMP_RECONNECT",
          record_uid="29139"):
    return AttendanceEventIn(
        event_uid=hashlib.sha256(seed.encode()).hexdigest(), uid=uid, user_id="11",
        terminal_serial=SERIAL, terminal_identity_fingerprint=fingerprint,
        device_event_time=when, captured_at=captured, source=source,
        clock_quality="OK", punch=0, raw_event={"attendance_record_uid": record_uid},
    )


def _ingest_held(db, connector, incoming):
    assert ingest_attendance(db, connector=connector, events=[incoming]) == (
        [incoming.event_uid], []
    )
    db.flush()
    row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == incoming.event_uid))
    # The dump reached ADD after the last roster snapshot: held, no identity.
    assert row.ords_status == "BLOCKED_IDENTITY"
    assert row.identity_resolution_status == "BLOCKED_PROVENANCE"
    assert row.cnic_lookup_hash is None and row.manual_release_required
    return row


def _outbox_count(db, row):
    return db.scalar(select(func.count(OrdsOutbox.id)).where(
        OrdsOutbox.attendance_event_id == row.id
    ))


def _audits(db, action):
    return db.scalars(
        select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.id)
    ).all()


@pytest.mark.parametrize("punch", ["after_last_snapshot", "between_snapshots"])
@pytest.mark.parametrize("source", ["DUMP_RECONNECT", "DUMP_STARTUP"])
def test_held_dump_row_is_released_by_the_next_snapshot(db, punch, source):
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(minutes=40))
    _snapshot(db, connector, "s1", now - timedelta(minutes=25))
    when = now - timedelta(minutes=20 if punch == "after_last_snapshot" else 35)
    row = _ingest_held(
        db, connector, _dump("kh-1", when, now - timedelta(minutes=1), source=source)
    )

    _snapshot(db, connector, "s2", utc_now())

    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "11"))
    assert row.ords_status == "PENDING"
    assert row.identity_resolution_status == "RESOLVED_SYNCED_CNIC"
    assert row.identity_repair_reason == "VERIFIED_SYNCED_CNIC"
    assert row.device_user_id == user.id
    assert decrypt_cnic(row.cnic_encrypted) == CNIC
    assert not row.manual_release_required
    assert row.raw_event == {"attendance_record_uid": "29139"}
    assert _outbox_count(db, row) == 1
    evidence = identity_evidence(db, row, connector, allow_reconcile_batch=True)
    assert evidence.proof["kind"] == "RETAINED_INTERVAL"
    assert evidence.proof["source"]["kind"] == "AUTHENTICATED_RECONCILE_BATCH"
    # Without the opt-in, no other caller sees a proof for this row.
    assert identity_evidence(db, row, connector) is None
    # The pre-delivery recheck accepts the same proof.
    assert delivery_proof_valid(db, row, connector)
    [audit] = _audits(db, "ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED")
    assert audit.actor == "system:identity-dump-auto-release"
    assert audit.target_id == connector.connector_id
    assert audit.after["event_ids"] == [row.id]
    assert audit.after["proof"] == "AUTHENTICATED_RECONCILE_BATCH"


@pytest.mark.parametrize(
    "blocker", [
        "wrong_fingerprint", "wrong_uid", "record_uid", "interval_gap", "revoked",
        "recreated_user", "receipt_lag", "legacy_continuity_only", "cnic_changed",
        "foreign_saved_record", "partial_snapshot_gap",
    ],
)
def test_held_dump_row_without_retained_proof_stays_held(db, blocker):
    connector = _connector(db)
    zkt = connector.zkt_device
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(minutes=40))
    _snapshot(db, connector, "s1", now - timedelta(minutes=30))
    # Inside the s0-s1 interval unless the blocker is about coverage itself.
    gap = {"interval_gap", "partial_snapshot_gap", "receipt_lag", "legacy_continuity_only"}
    when = now - timedelta(minutes=20 if blocker in gap else 35)
    captured = now - timedelta(minutes=15 if blocker == "receipt_lag" else 1)
    incoming = _dump(
        f"held-{blocker}", when, captured,
        uid={"wrong_uid": "99", "record_uid": "29139"}.get(blocker, "69"),
        fingerprint="b" * 64 if blocker == "wrong_fingerprint" else FP,
    )
    row = _ingest_held(db, connector, incoming)
    history = db.scalar(select(AttendanceIdentityHistory).where(
        AttendanceIdentityHistory.user_id == "11"
    ))
    if blocker == "interval_gap":
        # A roster read that could not be trusted ended the interval at s1.
        history.closed = True
    elif blocker == "partial_snapshot_gap":
        _snapshot(db, connector, "unstable", now - timedelta(minutes=10), stable=False)
    elif blocker == "revoked":
        history.revoked = True
    elif blocker == "recreated_user":
        # The interval covering the punch belongs to an earlier device user.
        former = DeviceUser(
            zkt_device_id=zkt.id, uid="69", user_id="11", display_name="Former",
            terminal_identity_fingerprint=FP, lifecycle_state="DELETED", present=False,
        )
        db.add(former)
        db.flush()
        history.device_user_id = former.id
    elif blocker == "legacy_continuity_only":
        # Whole-roster continuity still spans the punch; no per-user interval does.
        db.delete(history)
    elif blocker == "foreign_saved_record":
        other = connector_fixture(db, hardware_id="e0:72:a1:d6:f3:99", expected_serial="OTHER")
        db.add(TerminalRecordManifest(
            connector_id=other.id, zkt_device_id=other.zkt_device.id,
            terminal_serial=SERIAL, generation=1, ordinal=0, canonical_source=True,
            record_size=40, raw_record_digest="b" * 64, terminal_record_key="c" * 64,
            attendance_event_id=row.id, disposition="EVENT",
            observed_uid="29139", observed_user_id="11",
        ))
    db.flush()
    if blocker == "legacy_continuity_only":
        assert ensure_utc(zkt.last_identity_change_at) <= when

    _snapshot(
        db, connector, "s2", utc_now(), cnic=OTHER_CNIC if blocker == "cnic_changed" else CNIC
    )

    assert row.ords_status == "BLOCKED_IDENTITY"
    assert row.cnic_lookup_hash is None
    assert row.manual_release_required
    assert _outbox_count(db, row) == 0
    assert not _audits(db, "ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED")
    # The owner's backlog release uses the same proof and keeps it held too.
    job = backlog.create_release(
        db, actor="StateHealthAdmin", zone_id=connector.zone_id, reason="Backlog check",
        typed_confirmation=f"RELEASE {connector.zone_id}", idempotency_key=f"key-{blocker}",
    )
    assert backlog.advance_once(db) == 1
    assert row.ords_status == "BLOCKED_IDENTITY" and row.cnic_lookup_hash is None
    assert job.succeeded_count == 0 and job.skipped_count == 1


def test_cnic_changed_after_the_punch_keeps_the_punch_time_identity(db):
    """Toheed: the fingerprint stays the same when only the CNIC is edited."""
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(minutes=40))
    _snapshot(db, connector, "s1", now - timedelta(minutes=30))
    row = _ingest_held(
        db, connector, _dump("toheed", now - timedelta(minutes=35), now - timedelta(minutes=1))
    )
    _snapshot(db, connector, "s2", utc_now(), cnic=OTHER_CNIC)
    histories = db.scalars(select(AttendanceIdentityHistory).where(
        AttendanceIdentityHistory.user_id == "11"
    ).order_by(AttendanceIdentityHistory.id)).all()
    assert [h.fingerprint for h in histories] == [FP, FP]
    assert histories[0].cnic_lookup_hash != histories[1].cnic_lookup_hash
    evidence = identity_evidence(db, row, connector, allow_reconcile_batch=True)
    # The punch-time interval exists, but its CNIC is not the current one.
    assert evidence is not None and evidence.cnic_hash == histories[0].cnic_lookup_hash
    assert row.ords_status == "BLOCKED_IDENTITY" and row.cnic_lookup_hash is None


def test_auto_release_is_capped_per_connector_snapshot(db, monkeypatch):
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_batch", 2)
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(minutes=40))
    _snapshot(db, connector, "s1", now - timedelta(minutes=25))
    rows = [
        _ingest_held(db, connector, _dump(
            f"cap-{index}", now - timedelta(minutes=20 - index), now - timedelta(minutes=1)
        ))
        for index in range(3)
    ]
    _snapshot(db, connector, "s2", utc_now())
    assert [row.ords_status for row in rows] == ["PENDING", "PENDING", "BLOCKED_IDENTITY"]
    _snapshot(db, connector, "s3", utc_now())
    assert [row.ords_status for row in rows] == ["PENDING"] * 3
    audits = _audits(db, "ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED")
    assert [audit.after["event_ids"] for audit in audits] == [
        [rows[0].id, rows[1].id], [rows[2].id]
    ]


def test_old_hold_waits_for_the_owner_backlog_release(db):
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(days=5))
    captured = now - timedelta(days=4)
    row = _ingest_held(
        db, connector, _dump("old", captured - timedelta(minutes=3), captured)
    )
    # ADD received this dump four days ago, before the rollout window.
    row.received_at = captured + timedelta(seconds=20)
    db.flush()

    _snapshot(db, connector, "s1", utc_now())
    assert row.ords_status == "BLOCKED_IDENTITY" and row.cnic_lookup_hash is None
    assert not _audits(db, "ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED")
    # The periodic delivery sweep never uses the dump proof either.
    repair_attendance_delivery_backlog(db, limit=100)
    assert row.ords_status == "BLOCKED_IDENTITY"

    preview = backlog.preview_page(db, zone_id=connector.zone_id)
    assert preview["held_total"] == 1 and preview["next_after_id"] is None
    group = next(
        g for g in preview["groups"] if g["source"] == "DUMP_RECONNECT" and g["held"]
    )
    assert group == {"source": "DUMP_RECONNECT", "age": "UP_TO_7_DAYS", "held": 1, "provable": 1}
    assert row.ords_status == "BLOCKED_IDENTITY"  # the preview is read-only

    job = backlog.create_release(
        db, actor="StateHealthAdmin", zone_id=connector.zone_id, reason="Owner backlog",
        typed_confirmation=f"RELEASE {connector.zone_id}", idempotency_key="old-hold-1",
    )
    assert job.status == "RELEASING" and job.requested_count == 1
    assert backlog.advance_once(db) == 1
    assert row.ords_status == "PENDING" and row.identity_resolution_status == "RESOLVED_SYNCED_CNIC"
    assert decrypt_cnic(row.cnic_encrypted) == CNIC and _outbox_count(db, row) == 1
    assert delivery_proof_valid(db, row, connector)
    assert backlog.advance_once(db) == 0
    assert job.status == "COMPLETED" and job.succeeded_count == 1
    [approved] = _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_APPROVED")
    assert approved.target_id == connector.zone_id and approved.after["held"] == 1
    [batch] = _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_BATCH")
    assert batch.after["event_ids"] == [row.id] and batch.actor == "StateHealthAdmin"
    assert _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_COMPLETED")

    # Delivery re-held it after a later snapshot: not new backlog.
    row.ords_status = "BLOCKED_IDENTITY"
    db.flush()
    _snapshot(db, connector, "s2", utc_now())
    assert row.ords_status == "PENDING"


def test_zero_window_turns_automatic_release_off(db, monkeypatch):
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_hours", 0)
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(minutes=40))
    _snapshot(db, connector, "s1", now - timedelta(minutes=25))
    row = _ingest_held(
        db, connector, _dump("off", now - timedelta(minutes=20), now - timedelta(minutes=1))
    )
    _snapshot(db, connector, "s2", utc_now())
    assert row.ords_status == "BLOCKED_IDENTITY"
    preview = backlog.preview_page(db, zone_id=connector.zone_id)
    assert {"source": "DUMP_RECONNECT", "age": "UP_TO_7_DAYS", "held": 1, "provable": 1} in (
        preview["groups"]
    )


def test_preview_pages_are_bounded_and_read_only(db):
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(minutes=40))
    _snapshot(db, connector, "s1", now - timedelta(minutes=25))
    rows = [
        _ingest_held(db, connector, _dump(
            f"page-{index}", now - timedelta(minutes=20 - index), now - timedelta(minutes=1),
            source=source,
        ))
        for index, source in enumerate(("DUMP_RECONNECT", "DUMP_STARTUP"))
    ]
    first = backlog.preview_page(db, zone_id=connector.zone_id, limit=1)
    assert first["scanned"] == 1 and first["next_after_id"] == rows[0].id
    assert first["held_total"] == 2
    second = backlog.preview_page(
        db, zone_id=connector.zone_id, after_id=first["next_after_id"], limit=1
    )
    assert second["scanned"] == 1 and second["next_after_id"] is None
    assert "totals" not in second
    # Before the next snapshot no interval covers these punches yet.
    for page in (first, second):
        assert sum(g["provable"] for g in page["groups"]) == 0
        assert sum(g["held"] for g in page["groups"] if g["age"] == "AUTOMATIC_WINDOW") == 1
    assert [row.ords_status for row in rows] == ["BLOCKED_IDENTITY"] * 2
    assert not db.dirty and not db.new
    with pytest.raises(backlog.RecoveryError):
        backlog.preview_page(db, zone_id="NO-SUCH-ZONE")


def _admin_client(db):
    from fastapi.testclient import TestClient

    from zk_add.security import ADMIN_COOKIE, create_admin_session
    from zk_add.web import app, get_db

    raw_session, admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest"
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw_session)
    return client, {"X-CSRF-Token": admin.csrf_token}


def test_backlog_release_api_requires_owner_step_up_and_confirmation(db):
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(days=5))
    captured = now - timedelta(days=4)
    row = _ingest_held(db, connector, _dump("api", captured - timedelta(minutes=3), captured))
    row.received_at = captured + timedelta(seconds=20)
    _snapshot(db, connector, "s1", utc_now())
    assert row.ords_status == "BLOCKED_IDENTITY"
    client, headers = _admin_client(db)
    zone = connector.zone_id
    root = "/api/v2/attendance-identity-backlog"
    body = {
        "zone_id": zone, "reason": "Release the reviewed Karachi backlog",
        "typed_confirmation": f"RELEASE {zone}", "password": "correct-password",
        "idempotency_key": "api-release-0001",
    }

    preview = client.get(f"{root}/preview", params={"zone_id": zone})
    assert preview.status_code == 200 and preview.json()["held_total"] == 1
    assert client.post(f"{root}/releases", json=body).status_code == 403  # no CSRF
    assert client.post(
        f"{root}/releases", json={**body, "password": "wrong"}, headers=headers
    ).status_code == 403
    mistyped = client.post(
        f"{root}/releases", json={**body, "typed_confirmation": "RELEASE"}, headers=headers
    )
    assert mistyped.status_code == 409
    assert mistyped.json()["detail"]["code"] == "CONFIRMATION_MISMATCH"
    assert not db.scalar(select(func.count(AttendanceRecoveryJob.id)))

    started = client.post(f"{root}/releases", json=body, headers=headers)
    assert started.status_code == 202, started.text
    job = started.json()
    assert job["status"] == "RELEASING" and job["held_at_approval"] == 1
    again = client.post(f"{root}/releases", json=body, headers=headers)
    assert again.json()["job_id"] == job["job_id"]
    second = client.post(
        f"{root}/releases", json={**body, "idempotency_key": "api-release-0002"}, headers=headers
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "RELEASE_ALREADY_RUNNING"
    listed = client.get(f"{root}/releases", params={"zone_id": zone}).json()
    assert [item["job_id"] for item in listed["rows"]] == [job["job_id"]]
    assert listed["confirmation"] == f"RELEASE {zone}"

    stop_url = f"{root}/releases/{job['job_id']}/stop"
    assert client.post(stop_url, json={"password": "wrong"}, headers=headers).status_code == 403
    stopped = client.post(stop_url, json={"password": "correct-password"}, headers=headers)
    assert stopped.status_code == 200 and stopped.json()["status"] == "STOPPED"
    assert backlog.advance_once(db) == 0
    assert row.ords_status == "BLOCKED_IDENTITY"
    assert client.get(f"{root}/releases/{job['job_id']}").json()["status"] == "STOPPED"
    assert _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_STOPPED")

    restarted = client.post(
        f"{root}/releases", json={**body, "idempotency_key": "api-release-0003"}, headers=headers
    )
    assert restarted.status_code == 202
    assert backlog.advance_once(db) == 1
    assert row.ords_status == "PENDING"


def _chunk_record(ordinal, event, *, observed_uid="7"):
    raw = _raw_record(uid=7, minute=ordinal)
    return ReconciliationSourceRecord(
        ordinal=ordinal,
        raw_record_digest=hashlib.sha256(raw).hexdigest(),
        terminal_record_key=hashlib.sha256(f"chunk-record-{ordinal}".encode()).hexdigest(),
        occurrence_index=1,
        disposition="EVENT",
        raw_record_b64=base64.b64encode(raw).decode(),
        observed_uid=observed_uid,
        observed_user_id="1007",
        event=event,
    )


def _apply_baseline(db, connector, records):
    job = create_reconciliation_job(
        db, connector=connector, actor="operator", reason="Link saved terminal records.",
        confirmation="RECONCILE 1 FROM START", idempotency_key="chunk-release",
    )
    first = base64.b64decode(records[0].raw_record_b64)
    apply_reconciliation_anchor(
        db, connector=connector,
        payload=ReconciliationAnchorRequest(
            job_id=job.job_id, generation=job.terminal_generation,
            terminal_serial=SERIAL, terminal_generation=job.terminal_generation,
            cutoff_count=len(records), latest_terminal_count=len(records), record_size=40,
            source_total_bytes=4 + 40 * len(records),
            first_anchor_digest=hashlib.sha256(first).hexdigest(),
        ),
    )
    draft = ReconciliationChunkRequest(
        job_id=job.job_id, generation=job.terminal_generation, sequence=0,
        start_ordinal=0, end_ordinal=len(records), chunk_digest="0" * 64,
        previous_chain_digest=None, resulting_chain_digest="0" * 64, records=records,
    )
    digest = reconciliation_chunk_digest(draft)
    _, chunk, _ = apply_reconciliation_chunk(db, connector=connector, payload=draft.model_copy(
        update={
            "chunk_digest": digest,
            "resulting_chain_digest": reconciliation_chain_digest(
                None, start_ordinal=0, end_ordinal=len(records), chunk_digest=digest,
            ),
        }
    ))
    db.flush()
    return chunk


def test_baseline_chunk_releases_covered_history_and_held_dump_rows(reconciliation_db, monkeypatch):
    db, connector = reconciliation_db
    db.autoflush = False
    # Only the saved-record proof may act at chunk time.
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_hours", 0)
    start = utc_now() - timedelta(minutes=5)
    for index in range(2):
        replace_user_snapshot(
            db, connector=connector,
            snapshot=UserSnapshotRequest(
                snapshot_id=f"chunk-{index}", complete=True, stable=True,
                observed_at=start + timedelta(minutes=index),
                users=[UserSnapshotRow(
                    uid="7", user_id="1007", name=f"Ayesha-{CNIC}",
                    terminal_identity_fingerprint=FP,
                )],
            ),
        )

    def event(seed, second, source, captured_delay, when=None):
        when = when or start + timedelta(seconds=10 + second)
        return AttendanceEventIn(
            event_uid=hashlib.sha256(seed.encode()).hexdigest(), uid="7", user_id="1007",
            terminal_serial=SERIAL, terminal_identity_fingerprint=FP,
            device_event_time=when, captured_at=when + captured_delay, source=source,
            clock_quality="OK", punch=0, raw_event={"attendance_record_uid": "7"},
        )

    # A legacy dump already held this punch; the baseline reads it again.
    dump = event("chunk-dump", 1, "DUMP_RECONNECT", timedelta(minutes=2))
    ingest_attendance(db, connector=connector, events=[dump])
    db.flush()
    held_dump = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == dump.event_uid))
    assert held_dump.ords_status == "BLOCKED_IDENTITY"
    history = event("chunk-history", 2, "FULL_HISTORY", timedelta(minutes=3))
    replay = event("chunk-dump", 1, "FULL_HISTORY", timedelta(minutes=3))
    # Punched before the first roster observation: no interval covers it.
    uncovered = event(
        "chunk-uncovered", 0, "FULL_HISTORY", timedelta(minutes=14),
        when=datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc),
    )
    chunk = _apply_baseline(db, connector, [
        _chunk_record(0, history), _chunk_record(1, replay), _chunk_record(2, uncovered),
    ])

    rows = {
        row.event_uid: row
        for row in db.scalars(select(AttendanceEvent)).all()
    }
    released_history = rows[history.event_uid]
    assert released_history.source == "FULL_HISTORY"
    assert released_history.ords_status == "PENDING"
    assert released_history.identity_resolution_status == "RESOLVED_SYNCED_CNIC"
    assert held_dump.ords_status == "PENDING"
    assert held_dump.source == "DUMP_RECONNECT"
    assert decrypt_cnic(held_dump.cnic_encrypted) == CNIC
    still_held = rows[uncovered.event_uid]
    assert still_held.ords_status == "BLOCKED_IDENTITY" and still_held.cnic_lookup_hash is None
    assert chunk.blocked_identity_count == 1
    assert chunk.already_present_count == 1
