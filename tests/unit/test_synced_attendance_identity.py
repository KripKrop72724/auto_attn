"""Reconciliation resolves a synced employee using retained punch-time proof."""

from datetime import timedelta

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
    RAW_RECORD,
    _source_record,
    reconciliation_db as reconciliation_db,
)
from zk_add.crypto import decrypt_cnic
from zk_add.models import AttendanceEvent, DeviceUser, OrdsOutbox
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
    UserSnapshotRequest,
    UserSnapshotRow,
)
from zk_add.service import ingest_attendance, replace_user_snapshot
from zk_add.time_utils import ensure_utc, utc_now


def test_reconciliation_resolves_unchanged_user_across_other_roster_change(db):
    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    zkt = connector.zkt_device
    zkt.confirmed_serial = zkt.serial
    start = utc_now() - timedelta(minutes=5)
    for index in range(2):
        replace_user_snapshot(
            db, connector=connector,
            snapshot=UserSnapshotRequest(
                snapshot_id=f"swat-continuity-{index}", complete=True, stable=True,
                observed_at=start + timedelta(minutes=index),
                users=[
                    UserSnapshotRow(
                        uid="69", user_id="11", name=f"Synced Employee-{CNIC}",
                        terminal_identity_fingerprint="a" * 64,
                    ),
                    UserSnapshotRow(
                        uid="70", user_id="12", name=f"Other {index}-3520212345672",
                        terminal_identity_fingerprint=f"{index + 1}" * 64,
                    ),
                ],
            ),
        )
    when = start + timedelta(seconds=30)
    assert ensure_utc(zkt.last_identity_change_at) > when
    incoming = AttendanceEventIn(
        event_uid="1" * 64, uid="69", user_id="11", raw_name=None,
        terminal_serial=SERIAL, terminal_identity_fingerprint="a" * 64,
        device_event_time=when, captured_at=when + timedelta(minutes=3),
        source="CURRENT_RECONCILE", clock_quality="OK", punch=0,
        raw_event={"attendance_record_uid": "29139"},
    )

    assert ingest_attendance(db, connector=connector, events=[incoming]) == (
        [incoming.event_uid], []
    )
    row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == incoming.event_uid))
    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "11"))
    assert row.ords_status == "PENDING"
    assert row.identity_resolution_status == "RESOLVED_SYNCED_CNIC"
    assert row.device_user_id == user.id
    assert row.display_name == "Synced Employee"
    assert decrypt_cnic(row.cnic_encrypted) == CNIC
    assert not row.manual_release_required
    assert row.raw_event == {"attendance_record_uid": "29139"}
    assert ingest_attendance(db, connector=connector, events=[incoming]) == (
        [], [incoming.event_uid]
    )
    assert db.scalar(select(func.count(OrdsOutbox.id)).where(
        OrdsOutbox.attendance_event_id == row.id
    )) == 1


def test_new_snapshot_releases_punch_in_same_transaction_after_other_user_edit(db):
    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    connector.zkt_device.confirmed_serial = connector.zkt_device.serial
    start = utc_now() - timedelta(minutes=5)

    def snapshot(index):
        replace_user_snapshot(
            db, connector=connector,
            snapshot=UserSnapshotRequest(
                snapshot_id=f"snapshot-release-{index}", complete=True, stable=True,
                observed_at=start + timedelta(minutes=index),
                users=[
                    UserSnapshotRow(
                        uid="69", user_id="11", name=f"Synced Employee-{CNIC}",
                        terminal_identity_fingerprint="a" * 64,
                    ),
                    UserSnapshotRow(
                        uid="70", user_id="12", name=f"Other {index}-3520212345672",
                        terminal_identity_fingerprint=f"{index + 1}" * 64,
                    ),
                ],
            ),
        )

    snapshot(0)
    when = start + timedelta(seconds=30)
    incoming = AttendanceEventIn(
        event_uid="3" * 64, uid="69", user_id="11", terminal_serial=SERIAL,
        terminal_identity_fingerprint="a" * 64,
        device_event_time=when, captured_at=when + timedelta(seconds=20),
        source="CURRENT_RECONCILE", clock_quality="OK", raw_event={},
    )
    ingest_attendance(db, connector=connector, events=[incoming])
    row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == incoming.event_uid))
    assert row.ords_status == "BLOCKED_IDENTITY"

    snapshot(1)
    db.flush()
    assert row.ords_status == "PENDING"
    assert row.display_name == "Synced Employee"
    assert not row.manual_release_required
    assert db.scalar(select(func.count(OrdsOutbox.id)).where(
        OrdsOutbox.attendance_event_id == row.id
    )) == 1


@pytest.mark.parametrize(
    "blocker", [
        "no_cnic", "late_first_observation", "changed_cnic", "conflicting_capture",
        "wrong_uid", "wrong_fingerprint", "wrong_serial", "invalid_clock",
    ],
)
def test_reconciliation_keeps_unproven_identity_held(db, blocker):
    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    connector.zkt_device.confirmed_serial = connector.zkt_device.serial
    start = utc_now() - timedelta(minutes=5)
    for index in range(2):
        name = "Unlinked Employee" if blocker == "no_cnic" else f"Synced Employee-{CNIC}"
        if blocker == "changed_cnic" and index == 1:
            name = "New Person-3520212345672"
        replace_user_snapshot(
            db, connector=connector,
            snapshot=UserSnapshotRequest(
                snapshot_id=f"held-{blocker}-{index}", complete=True, stable=True,
                observed_at=start + timedelta(minutes=index),
                users=[UserSnapshotRow(
                    uid="69", user_id="11", name=name,
                    terminal_identity_fingerprint=(
                        "b" * 64 if blocker == "changed_cnic" and index == 1 else "a" * 64
                    ),
                )],
            ),
        )
    when = start + timedelta(seconds=30)
    if blocker == "late_first_observation":
        when = start - timedelta(seconds=1)
    incoming = AttendanceEventIn(
        event_uid="2" * 64, uid="99" if blocker == "wrong_uid" else "69",
        user_id="11",
        raw_name="Different-3520212345672" if blocker == "conflicting_capture" else None,
        terminal_serial="OTHER-SERIAL" if blocker == "wrong_serial" else SERIAL,
        terminal_identity_fingerprint="b" * 64 if blocker == "wrong_fingerprint" else "a" * 64,
        device_event_time=when, captured_at=when + timedelta(minutes=3),
        source="CURRENT_RECONCILE",
        clock_quality="INVALID" if blocker == "invalid_clock" else "OK",
        raw_event={"attendance_record_uid": "29139"},
    )
    ingest_attendance(db, connector=connector, events=[incoming])
    row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == incoming.event_uid))
    assert row.ords_status == "BLOCKED_IDENTITY"
    assert row.cnic_lookup_hash is None
    assert row.manual_release_required
    assert db.scalar(select(func.count(OrdsOutbox.id)).where(
        OrdsOutbox.attendance_event_id == row.id
    )) == 0


@pytest.mark.parametrize("manifest_uid", ["7", "99"])
def test_reconciliation_waits_for_manifest_before_releasing_synced_identity(
    reconciliation_db, manifest_uid,
):
    import hashlib

    db, connector = reconciliation_db
    db.autoflush = False
    start = utc_now() - timedelta(minutes=5)
    for index in range(2):
        replace_user_snapshot(
            db, connector=connector,
            snapshot=UserSnapshotRequest(
                snapshot_id=f"source-proof-{index}", complete=True, stable=True,
                observed_at=start + timedelta(minutes=index),
                users=[
                    UserSnapshotRow(
                        uid="7", user_id="1007", name=f"Ayesha-{CNIC}",
                        terminal_identity_fingerprint="a" * 64,
                    ),
                    UserSnapshotRow(
                        uid="8", user_id="1008", name=f"Other {index}-3520212345672",
                        terminal_identity_fingerprint=f"{index + 1}" * 64,
                    ),
                ],
            ),
        )
    job = create_reconciliation_job(
        db, connector=connector, actor="operator", reason="Check exact source identity.",
        confirmation="RECONCILE 1 FROM START", idempotency_key=f"source-proof-{manifest_uid}",
    )
    apply_reconciliation_anchor(
        db, connector=connector,
        payload=ReconciliationAnchorRequest(
            job_id=job.job_id, generation=job.terminal_generation,
            terminal_serial=SERIAL, terminal_generation=job.terminal_generation,
            cutoff_count=1, latest_terminal_count=1, record_size=len(RAW_RECORD), source_total_bytes=4 + len(RAW_RECORD),
            first_anchor_digest=hashlib.sha256(RAW_RECORD).hexdigest(),
        ),
    )
    source = _source_record()
    when = start + timedelta(seconds=30)
    source = source.model_copy(update={
        "disposition": "BLOCKED_IDENTITY", "observed_uid": manifest_uid,
        "observed_user_id": "1007",
        "event": source.event.model_copy(update={
            "raw_name": None, "source": "CURRENT_RECONCILE",
            "device_event_time": when, "captured_at": when + timedelta(minutes=2),
        }),
    })
    draft = ReconciliationChunkRequest(
        job_id=job.job_id, generation=job.terminal_generation, sequence=0,
        start_ordinal=0, end_ordinal=1, chunk_digest="0" * 64,
        previous_chain_digest=None, resulting_chain_digest="0" * 64, records=[source],
    )
    digest = reconciliation_chunk_digest(draft)
    request = draft.model_copy(update={
        "chunk_digest": digest,
        "resulting_chain_digest": reconciliation_chain_digest(
            None, start_ordinal=0, end_ordinal=1, chunk_digest=digest,
        ),
    })
    _, chunk, _ = apply_reconciliation_chunk(db, connector=connector, payload=request)
    db.flush()
    row = db.scalar(select(AttendanceEvent).where(
        AttendanceEvent.event_uid == source.event.event_uid
    ))
    if manifest_uid == "7":
        assert row.ords_status == "PENDING"
        assert row.display_name == "Ayesha"
        assert chunk.blocked_identity_count == 0
    else:
        assert row.ords_status == "BLOCKED_IDENTITY"
        assert row.device_user_id is None
        assert row.cnic_lookup_hash is None
        assert chunk.blocked_identity_count == 1
