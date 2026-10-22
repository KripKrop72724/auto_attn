"""Legacy dump punches held at ingest release on their batch and retained identity.

A 2.6.x reconnect/startup dump reaches ADD before the next roster snapshot, so
ingest holds every row. The next snapshot may release one only when a retained,
fingerprint-bound interval covering the punch carries the user's current CNIC.
Older holds wait for the owner's audited per-zone release.
"""

import base64
import hashlib
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import func, select, update

from test_add_backend import (
    CNIC,
    SERIAL,
    connector_fixture,
    db as db,
    make_writable,
)
from test_reconciliation import (
    RAW_RECORD,
    _certify_one_record_baseline,
    _raw_record,
    reconciliation_db as reconciliation_db,
)
from zk_add import attendance_identity_backlog as backlog
from zk_add import worker
from zk_add.attendance_identity_evidence import identity_evidence
from zk_add.attendance_safe_repair import delivery_proof_valid
from zk_add.crypto import decrypt_cnic
from zk_add.models import (
    AttendanceEvent,
    AttendanceIdentityHistory,
    AttendanceRecoveryItem,
    AttendanceRecoveryJob,
    AuditEvent,
    DeviceUser,
    OrdsOutbox,
    TerminalRecordManifest,
    ZKTDevice,
)
from zk_add.reconciliation import (
    apply_reconciliation_anchor,
    apply_reconciliation_chunk,
    apply_source_tail_chunk,
    create_reconciliation_job,
    reconciliation_chain_digest,
    reconciliation_chunk_digest,
)
from zk_add.schemas import (
    AttendanceEventIn,
    ReconciliationAnchorRequest,
    ReconciliationChunkRequest,
    ReconciliationSourceRecord,
    SourceTailChunkRequest,
    UserSnapshotRequest,
    UserSnapshotRow,
)
from zk_add.service import (
    DumpIdentityRelease,
    dump_release_outlook,
    synced_cnic_proof_kind,
    ingest_attendance,
    release_synced_cnic_attendance_event,
    replace_user_snapshot,
    repair_attendance_delivery_backlog,
)
from zk_add.settings import settings
from zk_add.time_utils import ensure_utc, utc_now

FP = "a" * 64
OTHER_CNIC = "3520212345672"
ADMIN = "StateHealthAdmin"


def _connector(db, **kwargs):
    db.autoflush = False
    connector = connector_fixture(db, **kwargs)
    make_writable(connector)
    connector.zkt_device.confirmed_serial = connector.zkt_device.serial
    return connector


@pytest.fixture()
def auto_zone(monkeypatch):
    """The owner enabled automatic release for the test zone."""
    monkeypatch.setattr(
        settings, "attendance_dump_identity_auto_release_zones", "ZONE-SLICTOWER-3FL"
    )


def _snapshot(db, connector, key, observed_at, *, cnic=CNIC, stable=True, state=False):
    """state: send the per-user state fingerprint, which every firmware
    generation derives from the user record including its name (and CNIC);
    a string also stands for another state field, such as the privilege."""
    replace_user_snapshot(
        db, connector=connector,
        snapshot=UserSnapshotRequest(
            snapshot_id=key, complete=True, stable=stable, observed_at=observed_at,
            users=[UserSnapshotRow(
                uid="69", user_id="11", name=f"Synced Employee-{cnic}",
                terminal_identity_fingerprint=FP,
                terminal_state_fingerprint=(
                    hashlib.sha256(f"{cnic}{'' if state is True else state}".encode()).hexdigest()
                    if state else None
                ),
            )],
        ),
    )
    db.flush()


def _dump(seed, when, captured, *, uid="69", fingerprint=FP, source="DUMP_RECONNECT",
          record_uid="29139", serial=SERIAL):
    return AttendanceEventIn(
        event_uid=hashlib.sha256(seed.encode()).hexdigest(), uid=uid, user_id="11",
        terminal_serial=serial, terminal_identity_fingerprint=fingerprint,
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


def _outbox(db, row):
    return db.scalar(select(OrdsOutbox).where(OrdsOutbox.attendance_event_id == row.id))


def _old_hold(db, connector, seed, *, days=4, source="DUMP_RECONNECT", serial=SERIAL):
    """A dump ADD received and held `days` ago, before any rollout window."""
    captured = utc_now() - timedelta(days=days)
    row = _ingest_held(db, connector, _dump(
        seed, captured - timedelta(minutes=3), captured, source=source, serial=serial,
    ))
    row.received_at = captured + timedelta(seconds=20)
    db.flush()
    return row


def _check(db, zone_id, *, actor=ADMIN, limit=500, now=None):
    """Run the owner's zone check page by page; return its first page and sums."""
    first = backlog.preview_page(db, zone_id=zone_id, actor=actor, limit=limit, now=now)
    page, totals = first, {}
    while True:
        for group in page["groups"]:
            entry = totals.setdefault(
                (group["connector_id"], group["source"], group["age"]),
                {"held": 0, "provable": 0},
            )
            entry["held"] += group["held"]
            entry["provable"] += group["provable"]
        if page["next_after_id"] is None:
            return first, totals
        page = backlog.preview_page(
            db, zone_id=zone_id, actor=actor, after_id=page["next_after_id"], limit=limit,
            as_of=first["as_of"], until_id=first["until_id"],
            window_hours=first["window_hours"],
        )


def _approve(db, zone_id, *, key, check=None, groups=None, approved=None, held=None,
             actor=ADMIN, **overrides):
    first, totals = check or _check(db, zone_id, actor=actor)
    chosen = list(totals) if groups is None else groups
    approval = first["approval"]
    arguments = {
        "actor": actor, "zone_id": zone_id, "reason": "Owner backlog release",
        "typed_confirmation": f"RELEASE {zone_id}", "idempotency_key": key,
        "scope": approval["scope"],
        "expires_at": datetime.fromisoformat(approval["expires_at"]),
        "signature": approval["signature"],
        "groups": [
            {"connector_id": connector_id, "source": source, "age": age}
            for connector_id, source, age in chosen
        ],
        "approved_count": approved if approved is not None else max(
            1, sum(totals[group]["provable"] for group in chosen)
        ),
        "previewed_held": held if held is not None else max(
            1, sum(totals[group]["held"] for group in chosen)
        ),
        **overrides,
    }
    return backlog.create_release(db, **arguments)


def _deliver(db, monkeypatch, limit=10):
    """Run one delivery claim against the test session."""
    monkeypatch.setattr(worker, "session_scope", lambda: nullcontext(db))
    claims = worker.claim_ords_batch(limit)
    db.flush()
    return claims


def _acknowledge(db, *rows):
    for row in rows:
        row.ords_status = "ACKED"
        _outbox(db, row).status = "ACKED"
    db.flush()


def _run(db, job):
    return backlog.serialize(db, job)


@pytest.mark.parametrize("punch", ["after_last_snapshot", "between_snapshots"])
@pytest.mark.parametrize("source", ["DUMP_RECONNECT", "DUMP_STARTUP"])
def test_held_dump_row_is_released_by_the_next_snapshot(db, auto_zone, punch, source):
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(minutes=40))
    _snapshot(db, connector, "s1", now - timedelta(minutes=25))
    when = now - timedelta(minutes=20 if punch == "after_last_snapshot" else 35)
    row = _ingest_held(
        db, connector, _dump("kh-1", when, now - timedelta(minutes=1), source=source)
    )
    # Ingest never uses the batch proof; the outlook says what will release it.
    # A punch after the last snapshot waits for the next one to cover it.
    outlook = dump_release_outlook(db, row)
    assert outlook["release_path"] == "AUTOMATIC_SNAPSHOT_RELEASE"
    assert outlook["proof_passes"] is (punch == "between_snapshots")
    assert outlook["awaiting_next_snapshot"] is (punch == "after_last_snapshot")

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
    # Recovered history uses the background lane, never ahead of live punches.
    assert _outbox(db, row).delivery_type == "FULL_HISTORY"
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
    assert audit.after["rereleased_event_ids"] == []
    assert audit.after["zone_id"] == connector.zone_id
    assert audit.after["proof"] == "AUTHENTICATED_RECONCILE_BATCH"


@pytest.mark.parametrize("zones", ["", "ZONE-ELSEWHERE"])
def test_automatic_release_is_off_until_the_owner_lists_the_zone(db, monkeypatch, zones):
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_zones", zones)
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(minutes=40))
    _snapshot(db, connector, "s1", now - timedelta(minutes=25))
    row = _ingest_held(
        db, connector, _dump("dark", now - timedelta(minutes=20), now - timedelta(minutes=1))
    )
    _snapshot(db, connector, "s2", utc_now())
    assert row.ords_status == "BLOCKED_IDENTITY" and row.cnic_lookup_hash is None
    assert not _audits(db, "ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED")
    # The evidence shows that only the owner's zone release can send it.
    outlook = dump_release_outlook(db, row)
    assert outlook["proof_passes"] and outlook["proof"] == "AUTHENTICATED_RECONCILE_BATCH"
    assert outlook["release_path"] == "OWNER_BACKLOG_RELEASE"
    assert not outlook["automatic_zone_enabled"]

    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_zones", "*")
    assert dump_release_outlook(db, row)["release_path"] == "AUTOMATIC_SNAPSHOT_RELEASE"
    _snapshot(db, connector, "s3", utc_now())
    assert row.ords_status == "PENDING"


@pytest.mark.parametrize(
    "blocker", [
        "wrong_fingerprint", "wrong_uid", "record_uid", "interval_gap", "revoked",
        "recreated_user", "receipt_lag", "legacy_continuity_only", "cnic_changed",
        "foreign_saved_record", "partial_snapshot_gap",
    ],
)
def test_held_dump_row_without_retained_proof_stays_held(db, auto_zone, blocker):
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
    assert not dump_release_outlook(db, row)["proof_passes"]
    # The owner's check counts it as held, not releasable. Even an approval
    # forced through for it re-checks the same proof and keeps it held.
    check = _check(db, connector.zone_id)
    assert sum(entry["provable"] for entry in check[1].values()) == 0
    assert sum(entry["held"] for entry in check[1].values()) == 1
    job = _approve(db, connector.zone_id, key=f"key-{blocker}", check=check, approved=1)
    assert backlog.advance_once(db) == 1
    assert row.ords_status == "BLOCKED_IDENTITY" and row.cnic_lookup_hash is None
    assert backlog.advance_once(db) == 0
    run = _run(db, job)
    assert job.status == "COMPLETED"
    assert (run["released"], run["kept_held"], run["delivered"]) == (0, 1, 0)


def test_cnic_changed_after_the_punch_keeps_the_punch_time_identity(db, auto_zone):
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


@pytest.mark.parametrize("source", ["DUMP_RECONNECT", "FULL_HISTORY", "RECONCILE_15M"])
@pytest.mark.parametrize("reassigned", [True, False])
def test_a_pre_26_record_never_takes_the_new_holders_cnic(db, source, reassigned):
    """Safety review: 2.4.12/2.5.2 fill a dump or reconcile record's CNIC from
    the user table when they read the log. If user 11 changed hands since the
    punch, that captured CNIC is the new holder's and the punch stays held."""
    connector = _connector(db)
    now = utc_now()
    later_cnic = OTHER_CNIC if reassigned else CNIC
    # Otherwise the same holder's privilege changed: the roster still changed
    # after the punch, so ingest holds the dump either way.
    later_state = True if reassigned else "privilege"
    _snapshot(db, connector, "s0", now - timedelta(minutes=60), state=True)
    _snapshot(db, connector, "s1", now - timedelta(minutes=45), state=True)
    _snapshot(db, connector, "s2", now - timedelta(minutes=30), cnic=later_cnic, state=later_state)
    incoming = _dump(
        f"pre26-{source}", now - timedelta(minutes=55), now - timedelta(minutes=1),
        source=source,
    ).model_copy(update={"raw_name": f"Synced Employee-{later_cnic}"})
    row = _ingest_held(db, connector, incoming)
    assert row.captured_cnic_lookup_hash is not None
    _snapshot(db, connector, "s3", utc_now(), cnic=later_cnic, state=later_state)
    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "11"))
    if reassigned:
        assert row.ords_status == "BLOCKED_IDENTITY" and row.cnic_lookup_hash is None
        assert synced_cnic_proof_kind(
            db, connector, connector.zkt_device, user, row, allow_reconcile_batch=True
        ) is None
    else:
        # The same holder throughout: released on its captured CNIC, as before.
        assert row.ords_status == "PENDING"
        assert row.identity_resolution_status == "RESOLVED_SYNCED_CNIC"
        assert decrypt_cnic(row.cnic_encrypted) == CNIC
        assert delivery_proof_valid(db, row, connector)


def test_a_released_pre_26_record_is_held_again_before_oracle_once_its_user_id_changes_hands(
    db, monkeypatch,
):
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(minutes=60), state=True)
    _snapshot(db, connector, "s1", now - timedelta(minutes=45), state=True)
    row = _ingest_held(db, connector, _dump(
        "pre26-inflight", now - timedelta(minutes=55), now - timedelta(minutes=1),
    ).model_copy(update={"raw_name": f"Synced Employee-{OTHER_CNIC}"}))
    # Captured with OTHER_CNIC while the roster still shows CNIC: never released.
    _snapshot(db, connector, "s2", now - timedelta(minutes=30), state=True)
    assert row.ords_status == "BLOCKED_IDENTITY"
    # Released on an earlier build that trusted the captured CNIC alone.
    _snapshot(db, connector, "s3", now - timedelta(minutes=20), cnic=OTHER_CNIC, state=True)
    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "11"))
    row.device_user_id, row.identity_snapshot_id = user.id, connector.zkt_device.identity_snapshot_id
    row.identity_resolution_status, row.identity_repair_reason = (
        "RESOLVED_SYNCED_CNIC", "VERIFIED_SYNCED_CNIC"
    )
    row.cnic_encrypted, row.cnic_lookup_hash = user.cnic_encrypted, user.cnic_lookup_hash
    row.ords_status, row.manual_release_required = "PENDING", False
    db.flush()
    assert not delivery_proof_valid(db, row, connector)


def test_auto_release_is_capped_per_connector_snapshot(db, auto_zone, monkeypatch):
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


def test_a_reheld_release_has_its_own_cap_and_never_overrides_manual_custody(
    db, auto_zone, monkeypatch,
):
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_batch", 1)
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(days=5))
    # Evaluated first, so a refusal there must not be what spends the cap.
    manual, reheld = (_old_hold(db, connector, seed) for seed in ("manual", "reheld"))
    _snapshot(db, connector, "s1", now - timedelta(minutes=25))
    new = [
        _ingest_held(db, connector, _dump(
            f"new-{index}", now - timedelta(minutes=20 - index), now - timedelta(minutes=1)
        ))
        for index in range(2)
    ]
    # The owner released both old holds; delivery then re-held them because
    # a newer roster snapshot arrived before Oracle acknowledged them.
    job = _approve(db, connector.zone_id, key="cap-split",
                   groups=[(connector.connector_id, "DUMP_RECONNECT", "UP_TO_7_DAYS")])
    backlog.advance_once(db)
    for row in (reheld, manual):
        assert row.ords_status == "PENDING"
        row.ords_status = _outbox(db, row).status = "BLOCKED_IDENTITY"
    backlog.stop(db, job, actor=ADMIN)
    db.flush()
    # An administrator has taken one of them into a manual send.
    from zk_add import attendance_manual_guard

    real = attendance_manual_guard.decision_for
    monkeypatch.setattr(
        attendance_manual_guard, "decision_for",
        lambda session, row: object() if row.id == manual.id else real(session, row),
    )

    _snapshot(db, connector, "s2", utc_now())

    # The re-hold does not use up the new-hold cap of one.
    assert reheld.ords_status == "PENDING"
    assert [row.ords_status for row in new] == ["PENDING", "BLOCKED_IDENTITY"]
    assert manual.ords_status == "BLOCKED_IDENTITY"
    [audit] = _audits(db, "ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED")
    assert audit.after["event_ids"] == [new[0].id]
    assert audit.after["rereleased_event_ids"] == [reheld.id]


def test_turning_the_proof_off_withdraws_it_before_oracle_and_pauses_releases(
    db, auto_zone, monkeypatch,
):
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(days=5))
    old = _old_hold(db, connector, "switch-old")
    _snapshot(db, connector, "s1", now - timedelta(minutes=25))
    recent = _ingest_held(
        db, connector, _dump("switch-new", now - timedelta(minutes=20), now - timedelta(minutes=1))
    )
    _snapshot(db, connector, "s2", utc_now())
    assert recent.ords_status == "PENDING"
    job = _approve(db, connector.zone_id, key="switch-1",
                   groups=[(connector.connector_id, "DUMP_RECONNECT", "UP_TO_7_DAYS")])

    monkeypatch.setattr(settings, "attendance_dump_identity_release_enabled", False)

    # Released but not yet sent: the pre-send check now refuses it.
    assert not delivery_proof_valid(db, recent, connector)
    assert _deliver(db, monkeypatch) == []
    assert recent.ords_status == "BLOCKED_IDENTITY"
    # The owner's running release pauses; nothing new is approved.
    assert backlog.advance_once(db) == 0
    assert old.ords_status == "BLOCKED_IDENTITY"
    assert _run(db, job)["paused"] is True
    with pytest.raises(backlog.RecoveryError) as refused:
        _approve(db, connector.zone_id, key="switch-2")
    assert refused.value.code == "RELEASE_PROOF_DISABLED"
    # Snapshots no longer release on the dump proof, even a re-held row.
    _snapshot(db, connector, "s3", utc_now())
    assert recent.ords_status == "BLOCKED_IDENTITY"
    assert dump_release_outlook(db, recent)["proof"] is None

    monkeypatch.setattr(settings, "attendance_dump_identity_release_enabled", True)
    assert backlog.advance_once(db) == 1
    assert old.ords_status == "PENDING"


def test_old_hold_waits_for_the_owner_backlog_release(db, auto_zone, monkeypatch):
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(days=5))
    row = _old_hold(db, connector, "old")

    _snapshot(db, connector, "s1", utc_now())
    assert row.ords_status == "BLOCKED_IDENTITY" and row.cnic_lookup_hash is None
    assert not _audits(db, "ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED")
    assert dump_release_outlook(db, row)["release_path"] == "OWNER_BACKLOG_RELEASE"
    # The periodic delivery sweep never uses the dump proof either.
    repair_attendance_delivery_backlog(db, limit=100)
    assert row.ords_status == "BLOCKED_IDENTITY"

    first, totals = check = _check(db, connector.zone_id)
    assert first["held_total"] == 1 and first["next_after_id"] is None
    assert first["until_id"] == row.id and first["approval"]["signature"]
    assert totals == {
        (connector.connector_id, "DUMP_RECONNECT", "UP_TO_7_DAYS"): {"held": 1, "provable": 1}
    }
    assert row.ords_status == "BLOCKED_IDENTITY"  # the check is read-only

    job = _approve(db, connector.zone_id, key="old-hold-1", check=check)
    assert job.status == "RELEASING" and job.requested_count == 1
    assert backlog.advance_once(db) == 1
    assert row.ords_status == "PENDING" and row.identity_resolution_status == "RESOLVED_SYNCED_CNIC"
    assert decrypt_cnic(row.cnic_encrypted) == CNIC and _outbox_count(db, row) == 1
    assert _outbox(db, row).delivery_type == "FULL_HISTORY"
    assert delivery_proof_valid(db, row, connector)
    # Released is not delivered: the run stays open until Oracle confirms it.
    assert backlog.advance_once(db) == 0
    assert job.status == "RELEASING"
    assert _run(db, job)["released"] == 1 and _run(db, job)["waiting_oracle"] == 1
    [claim] = _deliver(db, monkeypatch)
    _acknowledge(db, row)
    backlog.advance_once(db)
    assert job.status == "COMPLETED"
    run = _run(db, job)
    assert (run["approved"], run["released"], run["delivered"], run["waiting_oracle"]) == (
        1, 1, 1, 0
    )
    [approved] = _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_APPROVED")
    assert approved.target_id == connector.zone_id
    assert approved.after["approved_count"] == 1 and approved.after["held_at_approval"] == 1
    assert approved.after["until_event_id"] == row.id
    [batch] = _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_BATCH")
    assert batch.after["event_ids"] == [row.id] and batch.actor == ADMIN
    [completed] = _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_COMPLETED")
    assert completed.after["delivered"] == 1


def test_backlog_follows_a_punch_reheld_by_a_newer_snapshot(db, monkeypatch):
    """Automatic release is off here: only the run itself can send it again."""
    # Automatic release would act on these punches; this test covers zones without it.
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_zones", "")
    connector = _connector(db)
    zkt = connector.zkt_device
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    row = _old_hold(db, connector, "rehold")
    _snapshot(db, connector, "s1", utc_now())
    job = _approve(db, connector.zone_id, key="rehold-1")
    assert backlog.advance_once(db) == 1
    assert row.ords_status == "PENDING"

    # The routine roster snapshot arrives before Oracle delivery.
    _snapshot(db, connector, "s2", utc_now())
    assert _deliver(db, monkeypatch) == []
    assert row.ords_status == "BLOCKED_IDENTITY"
    _snapshot(db, connector, "s3", utc_now())
    assert row.ords_status == "BLOCKED_IDENTITY"  # the zone is not enabled

    backlog.advance_once(db)
    assert row.ords_status == "PENDING"
    assert row.identity_snapshot_id == zkt.identity_snapshot_id
    assert job.status == "RELEASING"
    [claim] = _deliver(db, monkeypatch)
    _acknowledge(db, row)
    backlog.advance_once(db)
    assert job.status == "COMPLETED"
    run = _run(db, job)
    assert (run["released"], run["delivered"], run["kept_held"]) == (1, 1, 0)
    batches = _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_BATCH")
    assert [audit.after["requeued_event_ids"] for audit in batches] == [[], [row.id]]
    item = db.scalar(select(AttendanceRecoveryItem).where(
        AttendanceRecoveryItem.attendance_event_id == row.id
    ))
    assert item.status == "DELIVERED" and item.attempt_count == 2


def test_a_reheld_punch_whose_proof_no_longer_passes_is_kept_held(db, monkeypatch):
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    row = _old_hold(db, connector, "lost-proof")
    _snapshot(db, connector, "s1", utc_now())
    job = _approve(db, connector.zone_id, key="lost-1")
    backlog.advance_once(db)
    # HR changed the CNIC before Oracle delivery; the punch-time CNIC differs.
    for attempt in range(backlog.REHOLD_ATTEMPTS):
        _snapshot(db, connector, f"s-cnic-{attempt}", utc_now(), cnic=OTHER_CNIC)
        _deliver(db, monkeypatch)
        assert row.ords_status == "BLOCKED_IDENTITY"
        backlog.advance_once(db)
        assert row.ords_status == "BLOCKED_IDENTITY"
    assert job.status == "COMPLETED"
    run = _run(db, job)
    assert (run["released"], run["delivered"], run["kept_held"]) == (1, 0, 1)


def test_backlog_paces_releases_to_oracle_acknowledgements(db, monkeypatch):
    monkeypatch.setattr(settings, "attendance_identity_backlog_in_flight_limit", 2)
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    rows = [_old_hold(db, connector, f"pace-{index}") for index in range(5)]
    _snapshot(db, connector, "s1", utc_now())
    job = _approve(db, connector.zone_id, key="pace-1")
    assert job.requested_count == 5
    assert backlog.advance_once(db) == 2
    assert backlog.advance_once(db) == 0  # two still wait for Oracle
    assert [row.ords_status for row in rows].count("PENDING") == 2
    assert len(_deliver(db, monkeypatch)) == 2
    _acknowledge(db, *[row for row in rows if row.ords_status == "PENDING"])
    assert backlog.advance_once(db) == 2
    assert [row.ords_status for row in rows].count("BLOCKED_IDENTITY") == 1


def test_released_backlog_never_queues_ahead_of_a_live_punch(db, monkeypatch):
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    rows = [_old_hold(db, connector, f"lane-{index}") for index in range(3)]
    _snapshot(db, connector, "s1", utc_now())
    _approve(db, connector.zone_id, key="lane-1")
    backlog.advance_once(db)
    assert [row.ords_status for row in rows] == ["PENDING"] * 3
    live = AttendanceEventIn(
        event_uid=hashlib.sha256(b"lane-live").hexdigest(), uid="69", user_id="11",
        terminal_serial=SERIAL, terminal_identity_fingerprint=FP,
        raw_name=f"Synced Employee-{CNIC}",
        device_event_time=utc_now() - timedelta(seconds=5), captured_at=utc_now(),
        source="LIVE", clock_quality="OK", punch=0, raw_event={},
    )
    ingest_attendance(db, connector=connector, events=[live])
    db.flush()
    live_row = db.scalar(select(AttendanceEvent).where(AttendanceEvent.event_uid == live.event_uid))
    assert live_row.ords_status == "PENDING"
    assert {_outbox(db, row).delivery_type for row in rows} == {"FULL_HISTORY"}
    [claim] = _deliver(db, monkeypatch, limit=1)
    assert db.get(OrdsOutbox, claim[0]).attendance_event_id == live_row.id


def test_a_held_punch_with_a_claim_in_flight_is_never_requeued(db):
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    row = _old_hold(db, connector, "in-flight")
    _snapshot(db, connector, "s1", utc_now())
    outbox = OrdsOutbox(attendance_event_id=row.id, status="BLOCKED_IDENTITY",
                        delivery_type="CURRENT_RECONCILE")
    db.add(outbox)
    db.flush()
    # What a stale read sees after another transaction released and claimed it.
    outbox.status = "IN_FLIGHT"
    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "11"))
    assert not release_synced_cnic_attendance_event(
        db, zkt=connector.zkt_device, user=user, row=row,
        dump_release=DumpIdentityRelease(held_since=None, remaining=1, rereleases_remaining=1),
    )
    assert row.ords_status == "BLOCKED_IDENTITY" and _outbox(db, row).status == "IN_FLIGHT"


def test_approval_is_bound_to_the_signed_check(db, monkeypatch):
    # Automatic release would act on these punches; this test covers zones without it.
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_zones", "")
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=45))
    week = _old_hold(db, connector, "bound-week")
    month = _old_hold(db, connector, "bound-month", days=40)
    startup = _old_hold(db, connector, "bound-startup", source="DUMP_STARTUP")
    _snapshot(db, connector, "s1", utc_now() - timedelta(minutes=5))
    check = _check(db, connector.zone_id)
    first, totals = check
    key = connector.connector_id
    assert totals == {
        (key, "DUMP_RECONNECT", "OLDER"): {"held": 1, "provable": 1},
        (key, "DUMP_RECONNECT", "UP_TO_7_DAYS"): {"held": 1, "provable": 1},
        (key, "DUMP_STARTUP", "UP_TO_7_DAYS"): {"held": 1, "provable": 1},
    }

    def refused(code, **arguments):
        with pytest.raises(backlog.RecoveryError) as error:
            _approve(db, connector.zone_id, check=check, **arguments)
        assert error.value.code == code

    scope = first["approval"]["scope"]
    refused("PREVIEW_INVALID", key="bound-a", scope={**scope, "until_id": scope["until_id"] + 9})
    refused("PREVIEW_INVALID", key="bound-b", actor="SomeoneElse")
    # The check counted fewer held punches in these groups than are held now.
    refused("ZONE_CHANGED", key="bound-c", held=1, approved=1)
    refused("APPROVED_COUNT_INVALID", key="bound-d", approved=4)
    stale = _check(db, connector.zone_id, now=utc_now() - backlog.PREVIEW_VALID)
    with pytest.raises(backlog.RecoveryError) as expired:
        _approve(db, connector.zone_id, key="bound-e", check=stale)
    assert expired.value.code == "PREVIEW_EXPIRED"

    # A reconnect dump held after the check; the next snapshot makes it
    # provable, but it is outside the approved check.
    later = _ingest_held(db, connector, _dump(
        "bound-later", utc_now() - timedelta(seconds=30), utc_now(),
    ))
    _snapshot(db, connector, "s2", utc_now())
    assert later.ords_status == "BLOCKED_IDENTITY"
    assert dump_release_outlook(db, later)["proof_passes"]
    chosen = [
        (key, "DUMP_RECONNECT", "UP_TO_7_DAYS"),
        (key, "DUMP_RECONNECT", "AUTOMATIC_WINDOW"),
        (key, "DUMP_STARTUP", "UP_TO_7_DAYS"),
    ]
    job = _approve(db, connector.zone_id, key="bound-ok", check=check, groups=chosen,
                   approved=2, held=2)
    assert [group["age"] for group in job.scope["groups"]] == [
        "AUTOMATIC_WINDOW", "UP_TO_7_DAYS", "UP_TO_7_DAYS"
    ]
    while backlog.advance_once(db):
        pass
    # The over-30-days group was not chosen; the later hold is past the check.
    assert week.ords_status == "PENDING" and startup.ords_status == "PENDING"
    assert month.ords_status == "BLOCKED_IDENTITY"
    assert later.ords_status == "BLOCKED_IDENTITY"
    assert job.cursor == first["until_id"] and job.eligible_count == 2


def test_backlog_stops_at_the_approved_count(db, monkeypatch):
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    rows = [_old_hold(db, connector, f"count-{index}") for index in range(3)]
    _snapshot(db, connector, "s1", utc_now())
    first, totals = check = _check(db, connector.zone_id)
    assert sum(entry["provable"] for entry in totals.values()) == 3
    job = _approve(db, connector.zone_id, key="count-1", check=check, approved=2)
    while backlog.advance_once(db):
        pass
    assert [row.ords_status for row in rows] == ["PENDING", "PENDING", "BLOCKED_IDENTITY"]
    _deliver(db, monkeypatch)
    _acknowledge(db, *rows[:2])
    backlog.advance_once(db)
    assert job.status == "COMPLETED" and rows[2].ords_status == "BLOCKED_IDENTITY"
    assert _run(db, job)["delivered"] == 2 and job.cursor == first["until_id"]


def test_zone_terminal_changes_refuse_the_approval_or_hold_the_moved_punches(db):
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    row = _old_hold(db, connector, "moved")
    _snapshot(db, connector, "s1", utc_now())
    check = _check(db, connector.zone_id)
    # A terminal joined the zone after the check.
    added = connector_fixture(db, hardware_id="e0:72:a1:d6:f3:77", expected_serial="ADDED")
    with pytest.raises(backlog.RecoveryError) as changed:
        _approve(db, connector.zone_id, key="moved-a", check=check)
    assert changed.value.code == "ZONE_CHANGED"
    added.zone_id = "ZONE-ELSEWHERE"
    db.flush()
    job = _approve(db, connector.zone_id, key="moved-b", check=check)
    # The approved terminal moved to another zone before the batch ran.
    connector.zone_id = "ZONE-ELSEWHERE"
    db.flush()
    assert backlog.advance_once(db) == 1
    assert row.ords_status == "BLOCKED_IDENTITY"
    assert job.status == "COMPLETED" and _run(db, job)["kept_held"] == 1


def test_a_multi_terminal_zone_releases_only_the_chosen_terminal(db):
    first_connector = _connector(db)
    second = _connector(db, hardware_id="e0:72:a1:d6:f3:55", expected_serial="ADZV211860254")
    second.zkt_device.serial = second.zkt_device.confirmed_serial = "ADZV211860254"
    terminals = {
        first_connector.id: (first_connector, SERIAL, CNIC),
        second.id: (second, "ADZV211860254", OTHER_CNIC),
    }
    for connector, _serial, cnic in terminals.values():
        _snapshot(db, connector, f"{connector.id}-s0", utc_now() - timedelta(days=5), cnic=cnic)
    rows = {
        connector.id: _old_hold(db, connector, f"multi-{connector.id}", serial=serial)
        for connector, serial, _cnic in terminals.values()
    }
    for connector, _serial, cnic in terminals.values():
        _snapshot(db, connector, f"{connector.id}-s1", utc_now(), cnic=cnic)
    first, totals = check = _check(db, first_connector.zone_id)
    assert first["approval"]["scope"]["connectors"] == sorted(
        [first_connector.connector_id, second.connector_id]
    )
    assert {group[0] for group in totals} == {first_connector.connector_id, second.connector_id}
    assert all(entry == {"held": 1, "provable": 1} for entry in totals.values())
    chosen = [group for group in totals if group[0] == second.connector_id]
    job = _approve(db, first_connector.zone_id, key="multi-1", check=check, groups=chosen)
    backlog.advance_once(db)
    assert rows[second.id].ords_status == "PENDING"
    assert rows[first_connector.id].ords_status == "BLOCKED_IDENTITY"
    assert job.scope["connector_keys"] == [second.connector_id]


def test_zero_window_turns_automatic_release_off(db, auto_zone, monkeypatch):
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
    _first, totals = _check(db, connector.zone_id)
    assert totals == {
        (connector.connector_id, "DUMP_RECONNECT", "UP_TO_7_DAYS"): {"held": 1, "provable": 1}
    }


def test_preview_pages_are_bounded_frozen_and_read_only(db, monkeypatch):
    # Automatic release would act on these punches; this test covers zones without it.
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_zones", "")
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
    first = backlog.preview_page(db, zone_id=connector.zone_id, actor=ADMIN, limit=1)
    assert first["scanned"] == 1 and first["next_after_id"] == rows[0].id
    assert first["held_total"] == 2 and first["until_id"] == rows[1].id
    # A punch held after the check started is not part of it.
    _ingest_held(db, connector, _dump("page-later", now - timedelta(minutes=2), now))
    second = backlog.preview_page(
        db, zone_id=connector.zone_id, actor=ADMIN, after_id=first["next_after_id"], limit=1,
        as_of=first["as_of"], until_id=first["until_id"], window_hours=first["window_hours"],
    )
    assert second["scanned"] == 1 and second["next_after_id"] is None
    assert "totals" not in second and "approval" not in second
    # Before the next snapshot no interval covers these punches yet.
    for page in (first, second):
        assert sum(g["provable"] for g in page["groups"]) == 0
        assert sum(g["held"] for g in page["groups"] if g["age"] == "AUTOMATIC_WINDOW") == 1
    assert not first["automatic_zone_enabled"] and first["release_enabled"]
    assert [row.ords_status for row in rows] == ["BLOCKED_IDENTITY"] * 2
    db.flush()
    assert not db.dirty and not db.new
    with pytest.raises(backlog.RecoveryError):
        backlog.preview_page(db, zone_id="NO-SUCH-ZONE", actor=ADMIN)


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


def test_backlog_release_api_requires_owner_step_up_and_the_signed_check(db):
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    row = _old_hold(db, connector, "api")
    _snapshot(db, connector, "s1", utc_now())
    assert row.ords_status == "BLOCKED_IDENTITY"
    client, headers = _admin_client(db)
    zone = connector.zone_id
    root = "/api/v2/attendance-identity-backlog"

    first = client.get(f"{root}/preview", params={"zone_id": zone})
    assert first.status_code == 200, first.text
    preview = first.json()
    assert preview["held_total"] == 1 and preview["next_after_id"] is None
    [group] = preview["groups"]
    assert group == {
        "connector_id": connector.connector_id, "source": "DUMP_RECONNECT",
        "age": "UP_TO_7_DAYS", "held": 1, "provable": 1, "next_snapshot": 0,
    }
    page = client.get(f"{root}/preview", params={
        "zone_id": zone, "after_id": 0, "as_of": preview["as_of"],
        "until_id": preview["until_id"], "window_hours": preview["window_hours"],
    })
    assert page.status_code == 200 and "approval" not in page.json()
    approval = preview["approval"]
    body = {
        "zone_id": zone, "reason": "Release the reviewed Karachi backlog",
        "typed_confirmation": f"RELEASE {zone}", "password": "correct-password",
        "idempotency_key": "api-release-0001",
        "scope": approval["scope"], "expires_at": approval["expires_at"],
        "signature": approval["signature"],
        "groups": [{key: group[key] for key in ("connector_id", "source", "age")}],
        "approved_count": 1, "previewed_held": 1,
    }

    assert client.post(f"{root}/releases", json=body).status_code == 403  # no CSRF
    assert client.post(
        f"{root}/releases", json={**body, "password": "wrong"}, headers=headers
    ).status_code == 403
    for change, code in (
        ({"typed_confirmation": "RELEASE"}, "CONFIRMATION_MISMATCH"),
        ({"scope": {**approval["scope"], "until_id": 0}}, "PREVIEW_INVALID"),
        ({"signature": "0" * 64}, "PREVIEW_INVALID"),
    ):
        refused = client.post(f"{root}/releases", json={**body, **change}, headers=headers)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == code
    missing = {key: value for key, value in body.items() if key != "scope"}
    assert client.post(f"{root}/releases", json=missing, headers=headers).status_code == 422
    assert not db.scalar(select(func.count(AttendanceRecoveryJob.id)))

    started = client.post(f"{root}/releases", json=body, headers=headers)
    assert started.status_code == 202, started.text
    job = started.json()
    assert job["status"] == "RELEASING" and job["approved"] == 1 and job["released"] == 0
    again = client.post(f"{root}/releases", json=body, headers=headers)
    assert again.json()["job_id"] == job["job_id"]
    conflict = client.post(
        f"{root}/releases", json={**body, "approved_count": 1, "groups": []}, headers=headers
    )
    assert conflict.status_code == 422
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
    run = client.get(f"{root}/releases/{restarted.json()['job_id']}").json()
    assert (run["released"], run["waiting_oracle"], run["delivered"]) == (1, 1, 0)


def test_identity_evidence_report_is_left_to_the_release_safety_branch(db, monkeypatch):
    """The endpoint is unchanged here; dump_release_outlook is the helper for it."""
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    row = _old_hold(db, connector, "evidence")
    _snapshot(db, connector, "s1", utc_now())
    client, _headers = _admin_client(db)
    report = client.get(f"/api/v2/attendance/{row.id}/identity-evidence").json()
    assert report["auto_release_eligible"] is False and "dump_release" not in report
    outlook = dump_release_outlook(db, row)
    assert outlook["proof_passes"] is True
    assert outlook["release_path"] == "OWNER_BACKLOG_RELEASE"
    # Even with every zone and a week-long window, ADD saw no roster between
    # five days ago and now: only the owner can release this punch.
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_zones", "*")
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_hours", 7 * 24)
    assert dump_release_outlook(db, row)["release_path"] == "OWNER_BACKLOG_RELEASE"


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


def _newest_slot_junk(ordinal):
    """A terminal's unwritten newest slot reads as zero bytes (see
    test_source_newest_slot.py); real baselines end on one."""
    raw = bytes(40)
    digest = hashlib.sha256(raw).hexdigest()
    return ReconciliationSourceRecord(
        ordinal=ordinal, raw_record_digest=digest,
        terminal_record_key=hashlib.sha256(f"{SERIAL}:{digest}".encode()).hexdigest(),
        occurrence_index=ordinal + 1, disposition="INVALID_TIME", event=None,
        raw_record_b64=base64.b64encode(raw).decode(), error_code="IMPLAUSIBLE_TERMINAL_TIME",
        raw_timestamp=0, observed_uid="0", observed_user_id=None,
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


@pytest.mark.parametrize("history_limit", [25, 1])
def test_baseline_chunk_releases_covered_history_and_held_dump_rows(
    reconciliation_db, monkeypatch, history_limit,
):
    db, connector = reconciliation_db
    db.autoflush = False
    # Only the saved-record proof may act at chunk time.
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_hours", 0)
    monkeypatch.setattr(settings, "attendance_chunk_history_release_limit", history_limit)
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
        # The baseline's final ordinal is the unwritten newest slot.
        _newest_slot_junk(3),
    ])

    rows = {
        row.event_uid: row
        for row in db.scalars(select(AttendanceEvent)).all()
    }
    released_history = rows[history.event_uid]
    if history_limit == 1:
        # The device waits for the chunk's acknowledgement, so the chunk
        # releases only the first held row; the next snapshot does the rest.
        assert held_dump.ords_status == "PENDING"
        assert released_history.ords_status == "BLOCKED_IDENTITY"
        assert chunk.blocked_identity_count == 2
        replace_user_snapshot(
            db, connector=connector,
            snapshot=UserSnapshotRequest(
                snapshot_id="chunk-2", complete=True, stable=True,
                observed_at=start + timedelta(minutes=2),
                users=[UserSnapshotRow(
                    uid="7", user_id="1007", name=f"Ayesha-{CNIC}",
                    terminal_identity_fingerprint=FP,
                )],
            ),
        )
        db.flush()
    else:
        assert chunk.blocked_identity_count == 1
    assert released_history.source == "FULL_HISTORY"
    assert released_history.ords_status == "PENDING"
    assert released_history.identity_resolution_status == "RESOLVED_SYNCED_CNIC"
    assert held_dump.ords_status == "PENDING"
    assert held_dump.source == "DUMP_RECONNECT"
    assert decrypt_cnic(held_dump.cnic_encrypted) == CNIC
    still_held = rows[uncovered.event_uid]
    assert still_held.ords_status == "BLOCKED_IDENTITY" and still_held.cnic_lookup_hash is None
    assert chunk.already_present_count == 1


def test_tail_chunk_releases_a_held_dump_row_it_links(reconciliation_db):
    db, connector = reconciliation_db
    coverage = _certify_one_record_baseline(db, connector)
    db.autoflush = False
    start = utc_now() - timedelta(minutes=5)
    for index in range(2):
        replace_user_snapshot(
            db, connector=connector,
            snapshot=UserSnapshotRequest(
                snapshot_id=f"tail-{index}", complete=True, stable=True,
                observed_at=start + timedelta(minutes=index),
                users=[UserSnapshotRow(
                    uid="7", user_id="1007", name=f"Ayesha-{CNIC}",
                    terminal_identity_fingerprint=FP,
                )],
            ),
        )
    when = start + timedelta(seconds=11)

    def event(source, captured_at):
        return AttendanceEventIn(
            event_uid=hashlib.sha256(b"tail-dump").hexdigest(), uid="7", user_id="1007",
            terminal_serial=SERIAL, terminal_identity_fingerprint=FP,
            device_event_time=when, captured_at=captured_at, source=source,
            clock_quality="OK", punch=0, raw_event={"attendance_record_uid": "7"},
        )

    # A legacy dump held this punch; the certified tail reads it again.
    ingest_attendance(
        db, connector=connector, events=[event("DUMP_RECONNECT", when + timedelta(minutes=2))]
    )
    db.flush()
    held_dump = db.scalar(select(AttendanceEvent).where(
        AttendanceEvent.event_uid == hashlib.sha256(b"tail-dump").hexdigest()
    ))
    assert held_dump.ords_status == "BLOCKED_IDENTITY"
    raw = _raw_record(uid=7, hour=14, minute=1)
    # A later record exists, so ordinal 1 is not the terminal's newest slot,
    # which ADD never releases until a later read confirms it.
    connector.zkt_device.attendance_count = 3
    draft = SourceTailChunkRequest(
        terminal_serial=SERIAL, terminal_generation=coverage.terminal_generation,
        record_size=len(RAW_RECORD), start_ordinal=1, end_ordinal=2, latest_terminal_count=3,
        chunk_digest="0" * 64, previous_chain_digest=coverage.source_committed_chain_digest,
        resulting_chain_digest="0" * 64,
        records=[ReconciliationSourceRecord(
            ordinal=1, raw_record_digest=hashlib.sha256(raw).hexdigest(),
            terminal_record_key=hashlib.sha256(b"tail-dump-record").hexdigest(),
            occurrence_index=1, disposition="EVENT",
            raw_record_b64=base64.b64encode(raw).decode(), raw_timestamp=1,
            observed_uid="7", observed_user_id="1007",
            event=event("CURRENT_RECONCILE", utc_now()),
        )],
    )
    digest = reconciliation_chunk_digest(draft)
    _coverage, chunk, duplicate, error_code = apply_source_tail_chunk(
        db, connector=connector, payload=draft.model_copy(update={
            "chunk_digest": digest,
            "resulting_chain_digest": reconciliation_chain_digest(
                coverage.source_committed_chain_digest, start_ordinal=1, end_ordinal=2,
                chunk_digest=digest,
            ),
        }),
    )
    db.flush()
    assert error_code is None and chunk is not None and not duplicate
    assert db.scalar(select(TerminalRecordManifest.id).where(
        TerminalRecordManifest.attendance_event_id == held_dump.id
    )) is not None
    # Released in the tail's own transaction on its saved record, not later.
    assert held_dump.ords_status == "PENDING"
    assert held_dump.identity_resolution_status == "RESOLVED_SYNCED_CNIC"
    assert decrypt_cnic(held_dump.cnic_encrypted) == CNIC
    # The saved-record proof keeps its usual lane; only the batch proof moves.
    assert _outbox(db, held_dump).delivery_type == "CURRENT_RECONCILE"


@pytest.mark.parametrize("case", ["outage", "old_punch"])
def test_a_dump_of_outage_or_old_punches_waits_for_the_owner(db, auto_zone, case):
    """A new hold is not a new punch: an ESP back from an outage uploads old ones."""
    connector = _connector(db)
    now = utc_now()
    if case == "outage":
        # The last roster ADD saw before the ESP went down, 50 hours ago. The
        # identity interval still continues across the outage.
        when = now - timedelta(hours=49)
        _snapshot(db, connector, "s0", now - timedelta(hours=50))
    else:
        # Rosters closely around a punch older than the 72-hour window.
        when = now - timedelta(days=4)
        _snapshot(db, connector, "s0", when - timedelta(minutes=5))
        _snapshot(db, connector, "s0b", when + timedelta(minutes=5))
    # The startup dump reaches ADD now: the hold itself is brand new.
    row = _ingest_held(db, connector, _dump(
        f"late-{case}", when, now - timedelta(minutes=1), source="DUMP_STARTUP",
    ))
    _snapshot(db, connector, "s1", utc_now())

    assert row.ords_status == "BLOCKED_IDENTITY" and row.cnic_lookup_hash is None
    assert not _audits(db, "ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED")
    outlook = dump_release_outlook(db, row)
    assert outlook["proof_passes"] and outlook["release_path"] == "OWNER_BACKLOG_RELEASE"
    # The proof itself passes, so the owner can release it after checking.
    first, totals = check = _check(db, connector.zone_id)
    assert totals == {
        (connector.connector_id, "DUMP_STARTUP", "AUTOMATIC_WINDOW"): {"held": 1, "provable": 1}
    }
    _approve(db, connector.zone_id, key=f"late-{case}", check=check)
    assert backlog.advance_once(db) == 1
    assert row.ords_status == "PENDING"


def test_auto_release_is_capped_per_connector_hour_across_reconnects(
    db, auto_zone, monkeypatch,
):
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_hourly_cap", 2)
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(minutes=40))
    _snapshot(db, connector, "s1", now - timedelta(minutes=25))
    rows = [
        _ingest_held(db, connector, _dump(
            f"hour-{index}", now - timedelta(minutes=20 - index), now - timedelta(minutes=1)
        ))
        for index in range(3)
    ]
    # A reconnect storm: 2.6.x sends a roster snapshot on every reconnect.
    for index in range(3):
        _snapshot(db, connector, f"storm-{index}", utc_now())
    assert [row.ords_status for row in rows] == ["PENDING", "PENDING", "BLOCKED_IDENTITY"]
    [audit] = _audits(db, "ATTENDANCE_DUMP_IDENTITY_AUTO_RELEASED")
    assert (audit.after["released"], audit.after["hourly_cap"]) == (2, 2)
    assert audit.after["max_snapshot_gap_minutes"] == 60
    # An hour later the connector's budget is available again.
    audit.created_at = ensure_utc(audit.created_at) - timedelta(minutes=61)
    db.flush()
    _snapshot(db, connector, "later", utc_now())
    assert rows[2].ords_status == "PENDING"


def test_owner_release_takes_only_punches_held_and_never_released_at_the_check(
    db, monkeypatch,
):
    """A punch on its way to Oracle at the check is not taken in if delivery re-holds it."""
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    earlier, checked = (_old_hold(db, connector, seed) for seed in ("earlier", "checked"))
    _snapshot(db, connector, "s1", utc_now())
    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "11"))
    # Released before the check (for example when the zone was enabled).
    assert release_synced_cnic_attendance_event(
        db, zkt=connector.zkt_device, user=user, row=earlier,
        dump_release=DumpIdentityRelease(held_since=None, remaining=1, rereleases_remaining=1),
    )
    db.flush()
    first, totals = check = _check(db, connector.zone_id)
    assert first["held_total"] == 1 and first["reheld_outside_check"] == 0
    # A newer snapshot reaches ADD before Oracle; delivery holds it again.
    _snapshot(db, connector, "s2", utc_now())
    assert _deliver(db, monkeypatch) == []
    assert earlier.ords_status == "BLOCKED_IDENTITY"
    job = _approve(db, connector.zone_id, key="frozen-1", check=check)
    assert job.scope["held_at_approval"] == 1
    while backlog.advance_once(db):
        pass
    assert checked.ords_status == "PENDING"
    assert earlier.ords_status == "BLOCKED_IDENTITY"  # not this run's punch
    assert _run(db, job)["released"] == 1
    assert [audit.after["event_ids"] for audit in _audits(
        db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_BATCH"
    )] == [[checked.id]]
    again, _totals = _check(db, connector.zone_id)
    assert again["held_total"] == 0 and again["reheld_outside_check"] == 1


def test_owner_release_is_frozen_to_the_punches_held_at_the_approval(db):
    """Review finding: membership was re-evaluated live, so a punch that joined
    the hold after the approval could be released in place of a checked one."""
    connector = _connector(db)
    zkt = connector.zkt_device
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    outside, elsewhere, approved, repaired = (
        _old_hold(db, connector, f"frozen-{name}")
        for name in ("outside", "elsewhere", "approved", "repaired")
    )
    _snapshot(db, connector, "s1", utc_now())
    # Another proof (safe repair here) released this one before and delivery
    # held it again: it belongs to that release, not to this check.
    repaired.identity_resolution_status = "RESOLVED_RETAINED_IDENTITY"
    repaired.identity_repaired_at = utc_now() - timedelta(hours=1)
    # Out of the hold during the check and the approval.
    outside.ords_status = "QUARANTINED_MISSING_TERMINAL_PROVENANCE"
    db.flush()
    first, totals = check = _check(db, connector.zone_id)
    assert first["held_total"] == 2 and first["reheld_outside_check"] == 1
    job = _approve(db, connector.zone_id, key="frozen-2", check=check)
    assert job.requested_count == 2 and job.scope["held_at_approval"] == 2
    assert backlog._members_after(job.scope["members"], 0, 10) == [elsewhere.id, approved.id]
    [approval] = _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_APPROVED")
    assert approval.after["members_digest"] == job.scope["members_digest"]

    # After the approval: the quarantined punch is held again and its proof
    # passes, and a roster-snapshot release reaches an approved punch first.
    outside.ords_status = "BLOCKED_IDENTITY"
    db.flush()
    assert dump_release_outlook(db, outside)["proof_passes"]
    user = db.scalar(select(DeviceUser).where(DeviceUser.user_id == "11"))
    assert release_synced_cnic_attendance_event(
        db, zkt=zkt, user=user, row=elsewhere,
        dump_release=DumpIdentityRelease(held_since=None, remaining=1, rereleases_remaining=1),
    )
    db.flush()
    while backlog.advance_once(db):
        pass
    assert approved.ords_status == "PENDING"
    assert outside.ords_status == "BLOCKED_IDENTITY"  # never part of this run
    assert repaired.ords_status == "BLOCKED_IDENTITY"
    run = _run(db, job)
    assert (run["released"], run["no_longer_held"], run["kept_held"]) == (1, 1, 0)
    [batch] = _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_BATCH")
    assert batch.after["event_ids"] == [approved.id]
    assert batch.after["no_longer_held_event_ids"] == [elsewhere.id]


def test_frozen_members_are_stored_as_id_runs():
    runs = backlog._id_ranges([8, 1, 2, 3, 5, 7])
    assert runs == [[1, 3], [5, 5], [7, 8]]
    assert backlog._members_after(runs, 0, 2) == [1, 2]
    assert backlog._members_after(runs, 2, 3) == [3, 5, 7]
    assert backlog._members_after(runs, 6, 10) == [7, 8]
    assert backlog._members_after(runs, 8, 10) == []
    assert backlog._members_after([], 0, 10) == []


def test_owner_release_uses_only_the_dump_proof(db):
    """A punch another proof releases is left to the roster snapshot that applies it."""
    connector = _connector(db)
    zkt = connector.zkt_device
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    saved, dump_only = (_old_hold(db, connector, seed) for seed in ("saved", "dump-only"))
    _snapshot(db, connector, "s1", utc_now())
    # A later terminal read linked the terminal's own saved record.
    db.add(TerminalRecordManifest(
        connector_id=connector.id, zkt_device_id=zkt.id, terminal_serial=SERIAL,
        generation=1, ordinal=0, canonical_source=True, record_size=40,
        raw_record_digest="b" * 64, terminal_record_key="c" * 64,
        attendance_event_id=saved.id, disposition="EVENT",
        observed_uid="69", observed_user_id="11",
    ))
    db.flush()
    assert saved.ords_status == "BLOCKED_IDENTITY"
    first = backlog.preview_page(db, zone_id=connector.zone_id, actor=ADMIN)
    assert first["groups"] == [{
        "connector_id": connector.connector_id, "source": "DUMP_RECONNECT",
        "age": "UP_TO_7_DAYS", "held": 2, "provable": 1, "next_snapshot": 1,
    }]
    job = _approve(db, connector.zone_id, key="dump-only-1", approved=1, held=2)
    while backlog.advance_once(db):
        pass
    assert dump_only.ords_status == "PENDING"
    assert saved.ords_status == "BLOCKED_IDENTITY"
    run = _run(db, job)
    assert (run["released"], run["kept_held"], run["left_to_snapshot"]) == (1, 0, 1)
    batches = [audit.after for audit in _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_BATCH")]
    assert [batch["left_to_snapshot_event_ids"] for batch in batches] == [[saved.id], []]
    assert [batch["event_ids"] for batch in batches] == [[], [dump_only.id]]
    assert {batch["proof"] for batch in batches} == {"AUTHENTICATED_RECONCILE_BATCH"}
    # The next roster snapshot releases it on its saved record, as before.
    _snapshot(db, connector, "s2", utc_now())
    assert saved.ords_status == "PENDING"
    assert saved.identity_resolution_status == "RESOLVED_SYNCED_CNIC"
    # The saved-record proof keeps its usual lane.
    assert _outbox(db, saved).delivery_type == "CURRENT_RECONCILE"


def test_a_partial_roster_pauses_the_owner_release_instead_of_passing_punches(db):
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    rows = [_old_hold(db, connector, f"partial-{index}") for index in range(3)]
    _snapshot(db, connector, "s1", utc_now())
    job = _approve(db, connector.zone_id, key="partial-1")
    assert job.requested_count == 3
    # The terminal sends one partial roster (common on 2.6.x) during the run.
    replace_user_snapshot(db, connector=connector, snapshot=UserSnapshotRequest(
        snapshot_id="partial", complete=False, stable=True, observed_at=utc_now(), users=[],
    ))
    db.flush()
    assert not connector.zkt_device.snapshot_complete
    assert backlog.advance_once(db) == 0
    assert job.status == "RELEASING" and job.cursor == 0
    assert _run(db, job)["waiting_reason"] == "WAITING_FOR_COMPLETE_ROSTER"
    assert [row.ords_status for row in rows] == ["BLOCKED_IDENTITY"] * 3
    _snapshot(db, connector, "s2", utc_now())
    while backlog.advance_once(db):
        pass
    assert [row.ords_status for row in rows] == ["PENDING"] * 3
    run = _run(db, job)
    assert (run["released"], run["kept_held"], run["waiting_reason"]) == (3, 0, None)


def test_a_roster_replaced_while_a_punch_is_checked_is_checked_again(db, monkeypatch):
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    row = _old_hold(db, connector, "raced")
    _snapshot(db, connector, "s1", utc_now())
    job = _approve(db, connector.zone_id, key="raced-1")
    real_release = backlog.release_synced_cnic_attendance_event
    calls = []

    def stale_release(session, **arguments):
        # Another transaction commits the routine snapshot mid-check, so the
        # installed guard refuses the release against the newer roster.
        calls.append(arguments["row"].id)
        return False if len(calls) == 1 else real_release(session, **arguments)

    monkeypatch.setattr(backlog, "release_synced_cnic_attendance_event", stale_release)
    monkeypatch.setattr(backlog, "_roster_changed", lambda session, zkt: True)
    assert backlog.advance_once(db) == 0
    assert job.cursor == 0 and row.ords_status == "BLOCKED_IDENTITY"
    run = _run(db, job)
    assert run["kept_held"] == 0 and run["waiting_reason"] == "WAITING_FOR_ROSTER_UPDATE"
    assert backlog.advance_once(db) == 1
    assert row.ords_status == "PENDING" and _run(db, job)["kept_held"] == 0
    assert calls == [row.id, row.id]


def test_roster_changed_reads_the_terminal_past_the_session(db):
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now())
    zkt = connector.zkt_device
    assert not backlog._roster_changed(db, zkt)
    # What a snapshot committed by another transaction looks like here.
    db.execute(
        update(ZKTDevice).where(ZKTDevice.id == zkt.id)
        .values(identity_snapshot_revision=zkt.identity_snapshot_revision + 1)
        .execution_options(synchronize_session=False)
    )
    assert backlog._roster_changed(db, zkt)


def test_stop_releases_nothing_more_but_follows_released_punches_to_oracle(db, monkeypatch):
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    released, waiting = (_old_hold(db, connector, f"stop-{index}") for index in range(2))
    _snapshot(db, connector, "s1", utc_now())
    job = _approve(db, connector.zone_id, key="stop-follow")
    assert backlog.advance_once(db, limit=1) == 1
    assert released.ords_status == "PENDING" and waiting.ords_status == "BLOCKED_IDENTITY"
    backlog.stop(db, job, actor=ADMIN)
    assert job.status == "STOPPED_FOLLOWING" and job.completed_at is None
    [stopped] = _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_STOPPED")
    assert stopped.after["still_following"] == 1

    # A newer snapshot reaches ADD before Oracle; delivery holds it again.
    _snapshot(db, connector, "s2", utc_now())
    assert _deliver(db, monkeypatch) == []
    assert released.ords_status == "BLOCKED_IDENTITY"
    _snapshot(db, connector, "s3", utc_now())
    assert backlog.advance_once(db) == 0  # nothing new is checked
    assert released.ords_status == "PENDING"  # but the released punch is followed
    assert waiting.ords_status == "BLOCKED_IDENTITY"
    with pytest.raises(backlog.RecoveryError) as running:
        _approve(db, connector.zone_id, key="stop-follow-2")
    assert running.value.code == "RELEASE_ALREADY_RUNNING"

    [_claim] = _deliver(db, monkeypatch)
    _acknowledge(db, released)
    backlog.advance_once(db)
    assert job.status == "STOPPED" and job.completed_at is not None
    run = _run(db, job)
    assert (run["released"], run["delivered"], run["waiting_oracle"]) == (1, 1, 0)
    [completed] = _audits(db, "ATTENDANCE_IDENTITY_BACKLOG_RELEASE_COMPLETED")
    assert completed.after["stopped"] is True and completed.outcome == "STOPPED"
    assert waiting.ords_status == "BLOCKED_IDENTITY"


def test_a_punch_after_the_last_snapshot_shows_it_awaits_the_next_one(db, monkeypatch):
    # Automatic release would act on these punches; this test covers zones without it.
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_zones", "")
    connector = _connector(db)
    now = utc_now()
    _snapshot(db, connector, "s0", now - timedelta(minutes=40))
    _snapshot(db, connector, "s1", now - timedelta(minutes=25))
    row = _ingest_held(
        db, connector, _dump("newest", now - timedelta(minutes=20), now - timedelta(minutes=1))
    )
    outlook = dump_release_outlook(db, row)
    assert outlook["proof"] == "AUTHENTICATED_RECONCILE_BATCH"
    assert outlook["awaiting_next_snapshot"] and not outlook["proof_passes"]
    assert outlook["release_path"] == "OWNER_BACKLOG_RELEASE"  # zone not enabled
    monkeypatch.setattr(settings, "attendance_dump_identity_auto_release_zones", "*")
    assert dump_release_outlook(db, row)["release_path"] == "AUTOMATIC_SNAPSHOT_RELEASE"
    # A changed fingerprint never shows a path.
    row.identity_terminal_fingerprint = "b" * 64
    db.flush()
    assert dump_release_outlook(db, row)["release_path"] is None


def test_a_snapshot_reads_no_outbox_for_held_punches_it_cannot_release(db):
    connector = _connector(db)
    _snapshot(db, connector, "s0", utc_now() - timedelta(days=5))
    for index in range(20):
        _old_hold(db, connector, f"cost-{index}")
    statements = []

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db.get_bind()
    sa_event.listen(engine, "before_cursor_execute", count)
    try:
        _snapshot(db, connector, "s1", utc_now())
    finally:
        sa_event.remove(engine, "before_cursor_execute", count)
    assert statements
    assert not [statement for statement in statements if "FROM add_ords_outbox" in statement]
