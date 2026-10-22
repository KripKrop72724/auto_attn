"""Firmware truth passes that re-send settled batches with a new capture time.

Production shape (22 Oct 2026): ZONE-LAHORE-02 and ZONE-MULTAN-02 (2.6.15),
ZONE-PESHAWAR-06 (2.7.5) and ZONE-PESHAWAR-02 (2.5.2) re-run a forced truth
reconcile every 15-30 minutes. Each pass rebuilds the month as
"truth-<first event_uid>" batches of ten rows whose only change is
``captured_at``. ADD quarantined every pass as ATTENDANCE_BATCH_ID_CONFLICT
(1,577 rows per pass on Lahore-02, ~38,000 rows that day fleet-wide).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import os
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from zk_add import attendance_batches, web
from zk_add.attendance_batches import (
    _ReplayMeter,
    attendance_batch_replay_metrics,
    payload_digest,
    replay_content_digest,
    replay_meter,
    settle_attendance_batch,
)
from zk_add.crypto import decrypt_json
from zk_add.db import Base
from zk_add.models import (
    AttendanceBatchItem,
    AttendanceBatchReceipt,
    AttendanceEvent,
    Connector,
)
from zk_add.schemas import Envelope
from zk_add.service import onboard_connector
from zk_add.settings import settings
from zk_add.time_utils import utc_now


SERIAL = "CQZ7232161091"
GENERATIONS = ("2.4.12", "2.5.2", "2.6.15", "2.7.5")


def _postgres_url() -> str | None:
    url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL")
    if not url and os.environ.get("CI"):
        url = os.environ.get("ADD_DATABASE_URL")
    return url if url and url.startswith("postgresql") else None


@pytest.fixture(scope="module")
def postgres_engine():
    url = _postgres_url()
    if url is None:
        yield None
        return
    schema = "batch_replay_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url,
        connect_args={
            "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000"
        },
    )
    try:
        metadata = MetaData()
        for table in Base.metadata.tables.values():
            table.to_metadata(metadata)
        metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def sessions(request, monkeypatch, postgres_engine):
    monkeypatch.setattr(settings, "pii_fernet_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "pii_lookup_key", "test-lookup-key-with-enough-entropy")
    monkeypatch.setattr(
        settings, "fleet_root_secret", "test-fleet-root-secret-with-enough-entropy"
    )
    replay_meter.reset()
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        try:
            yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        finally:
            engine.dispose()
        return
    if postgres_engine is None:
        pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
    # One disposable schema per module; every test starts from empty tables.
    tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    with postgres_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    yield sessionmaker(bind=postgres_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture()
def device(sessions):
    with sessions() as db:
        connector, _token, _created = onboard_connector(
            db,
            hardware_id="e0:72:a1:00:00:" + uuid4().hex[:2],
            zone_id="ZONE-LAHORE-02",
            zone_name="ZONE-LAHORE-02",
            device_id="2",
            firmware_version="2.6.15",
            expected_serial=SERIAL,
            actor="test",
            ip_address="127.0.0.1",
        )
        db.commit()
        return connector.id


def _connector(db: Session, connector_pk: int) -> Connector:
    connector = db.get(Connector, connector_pk)
    assert connector is not None
    return connector


def _uid(user_id: str, when: str, punch: int = 0) -> str:
    # Same material as firmware build_event_uid().
    material = (
        f'{{"device_event_time":"{when}","device_serial":"{SERIAL}",'
        f'"punch":"{punch}","user_id":"{user_id}"}}'
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def firmware_row(
    generation: str,
    *,
    index: int,
    captured_at: datetime,
    source: str = "DUMP_RECONNECT",
    uid: str | None = None,
    day: int = 21,
) -> dict:
    """One row exactly as add_attendance_json_row() builds it for a dump."""

    user_id = str(1000 + index)
    when = _iso(datetime(2026, 10, day, 3, 0, tzinfo=timezone.utc) + timedelta(minutes=index))
    terminal_uid = uid or str(40 + index)
    row = {
        "event_uid": _uid(user_id, when),
        "uid": terminal_uid,
        "terminal_identity_fingerprint": hashlib.sha256(
            f"{SERIAL}:{terminal_uid}:{user_id}".encode()
        ).hexdigest(),
        "user_id": user_id,
        "device_event_time": when,
        "captured_at": _iso(captured_at),
        "source": source,
        "status": 0,
        "punch": 0,
        "raw_punch": False,
        "clock_quality": "OK",
        "raw_event": {"terminal_uid": terminal_uid, "attendance_record_uid": str(500 + index)},
    }
    if generation != "2.4.12":
        # 2.5.2 and later add the bound terminal serial; 2.4.12 never did.
        row["terminal_serial"] = SERIAL
    return row


def truth_batch(
    generation: str,
    *,
    captured_at: datetime,
    count: int = 10,
    first: int = 0,
    source: str = "DUMP_RECONNECT",
    uid_overrides: dict[int, str] | None = None,
) -> dict:
    """zone_lite.c add_enqueue_reconcile_events(): batch_id is the first row's UID."""

    rows = [
        firmware_row(
            generation,
            index=index,
            captured_at=captured_at,
            source=source,
            uid=(uid_overrides or {}).get(index),
        )
        for index in range(first, first + count)
    ]
    return {"batch_id": f"truth-{rows[0]['event_uid']}", "events": rows}


def firmware_accepts(generation: str, payload: dict, ack: dict, message_id: str) -> bool:
    """Port of the deployed ACK checks.

    2.4.12 retires a payload on any ``ack`` for its message id. 2.5.2 through
    2.7.6 also require add_connector.c attendance_settlement_matches_payload()
    (byte-identical in all four of those generations).
    """

    if ack.get("type") != "ack" or ack.get("message_id") != message_id:
        return False
    if generation == "2.4.12":
        return True
    if ack.get("message_type") != "attendance_batch":
        return False
    numbers = [ack.get(name) for name in ("accepted", "duplicates", "quarantined")]
    if not (
        isinstance(ack.get("receipt_id"), str) and len(ack["receipt_id"]) == 36
        and isinstance(ack.get("batch_id"), str) and 0 < len(ack["batch_id"]) <= 120
        and isinstance(ack.get("payload_digest"), str) and len(ack["payload_digest"]) == 64
        and ack.get("outcome") in {
            "COMMITTED", "COMMITTED_WITH_DUPLICATES", "COMMITTED_WITH_QUARANTINE", "QUARANTINED",
        }
        and all(isinstance(value, int) and 0 <= value <= 100 for value in numbers)
    ):
        return False
    accepted, duplicates, quarantined = numbers
    expected = "COMMITTED"
    if quarantined and not accepted and not duplicates:
        expected = "QUARANTINED"
    elif quarantined:
        expected = "COMMITTED_WITH_QUARANTINE"
    elif duplicates:
        expected = "COMMITTED_WITH_DUPLICATES"
    reported = payload.get("payload_digest")
    digest_matches = (
        not isinstance(reported, str)
        or reported == ack["payload_digest"]
        or ack["outcome"] == "QUARANTINED"
    )
    return (
        payload["batch_id"] == ack["batch_id"]
        and accepted + duplicates + quarantined == len(payload["events"])
        and ack["outcome"] == expected
        and digest_matches
    )


def settle(sessions, connector_pk: int, payload: dict):
    with sessions() as db:
        settlement = settle_attendance_batch(
            db, connector=_connector(db, connector_pk), payload=payload
        )
        db.commit()
        return settlement


def counts(sessions) -> tuple[int, int, int, int]:
    with sessions() as db:
        return (
            db.scalar(select(func.count()).select_from(AttendanceBatchReceipt)),
            db.scalar(select(func.count()).select_from(AttendanceBatchItem)),
            db.scalar(
                select(func.count())
                .select_from(AttendanceBatchItem)
                .where(AttendanceBatchItem.disposition == "QUARANTINED")
            ),
            db.scalar(select(func.count()).select_from(AttendanceEvent)),
        )


def forget_content_digests(sessions) -> None:
    """Make every receipt look like one committed before 22 Oct 2026."""

    with sessions() as db:
        for receipt in db.scalars(select(AttendanceBatchReceipt)).all():
            receipt.content_digest = None
        db.commit()


PASS_START = datetime(2026, 10, 22, 6, 33, 40, tzinfo=timezone.utc)


@pytest.mark.parametrize("generation", GENERATIONS)
def test_truth_pass_replays_repeat_the_original_settlement(sessions, device, generation):
    original = truth_batch(generation, captured_at=PASS_START)
    first = settle(sessions, device, original)
    assert first.outcome == "COMMITTED"
    assert first.accepted_count == 10
    baseline = counts(sessions)

    # Production cadence: the same 10 punches every ~27 minutes, each pass
    # with its own serialisation time.
    for minutes in (27, 54, 81, 108):
        replay = truth_batch(generation, captured_at=PASS_START + timedelta(minutes=minutes))
        assert payload_digest(replay["events"]) != payload_digest(original["events"])
        message_id = f"msg-{minutes}"
        settlement = settle(sessions, device, replay)
        ack = settlement.ack(message_id=message_id, sequence=minutes)

        assert settlement.duplicate_batch is True
        assert settlement.replay == "CAPTURE_REPLAY"
        assert settlement.receipt_id == first.receipt_id
        assert settlement.outcome == "COMMITTED"
        assert settlement.accepted_event_uids == first.accepted_event_uids
        assert settlement.quarantined_count == 0
        assert ack["replay"] == "CAPTURE_REPLAY"
        assert firmware_accepts(generation, replay, ack, message_id)

    assert counts(sessions) == baseline
    with sessions() as db:
        receipt = db.scalar(select(AttendanceBatchReceipt))
        assert receipt.observation_count == 5
        assert receipt.content_digest == replay_content_digest(original["events"])
    assert attendance_batch_replay_metrics()[
        _connector_id(sessions, device)
    ] == {"CAPTURE_REPLAY_OF_COMMITTED": 4}


def _connector_id(sessions, connector_pk: int) -> str:
    with sessions() as db:
        return _connector(db, connector_pk).connector_id


@pytest.mark.parametrize("generation", GENERATIONS)
def test_live_first_then_truth_replays_keep_duplicate_outcome(sessions, device, generation):
    batch = truth_batch(generation, captured_at=PASS_START)
    for row in batch["events"]:
        live = {**row, "source": "LIVE"}
        assert settle(sessions, device, {"batch_id": row["event_uid"], "events": [live]}).outcome == "COMMITTED"
    truth = settle(sessions, device, batch)
    assert truth.outcome == "COMMITTED_WITH_DUPLICATES"
    assert truth.duplicate_count == 10
    baseline = counts(sessions)

    replay = truth_batch(generation, captured_at=PASS_START + timedelta(minutes=30))
    settlement = settle(sessions, device, replay)
    ack = settlement.ack(message_id="replay", sequence=9)

    assert settlement.receipt_id == truth.receipt_id
    assert settlement.outcome == "COMMITTED_WITH_DUPLICATES"
    assert settlement.duplicate_event_uids == truth.duplicate_event_uids
    assert firmware_accepts(generation, replay, ack, "replay")
    assert counts(sessions) == baseline


@pytest.mark.parametrize("generation", GENERATIONS)
def test_reused_batch_id_with_different_punches_stays_quarantined_once(
    sessions, device, generation
):
    # The current day's last chunk grows between passes: same first punch,
    # one more row. That is different content and stays custody evidence.
    settle(sessions, device, truth_batch(generation, captured_at=PASS_START, count=7))
    grown = truth_batch(generation, captured_at=PASS_START + timedelta(minutes=30), count=8)
    conflict = settle(sessions, device, grown)

    assert conflict.outcome == "QUARANTINED"
    assert conflict.replay is None
    assert {row["code"] for row in conflict.rejected} == {"ATTENDANCE_BATCH_ID_CONFLICT"}
    assert conflict.quarantined_count == 8
    with sessions() as db:
        preserved = db.scalars(
            select(AttendanceBatchItem)
            .join(AttendanceBatchReceipt)
            .where(AttendanceBatchReceipt.receipt_id == conflict.receipt_id)
            .order_by(AttendanceBatchItem.item_index)
        ).all()
        assert [decrypt_json(row.protected_payload)["event_uid"] for row in preserved] == [
            row["event_uid"] for row in grown["events"]
        ]
        assert db.scalar(select(func.count()).select_from(AttendanceEvent)) == 7
    after_conflict = counts(sessions)

    # The same grown chunk on the next pass repeats that preserved quarantine.
    again = truth_batch(generation, captured_at=PASS_START + timedelta(minutes=60), count=8)
    message_id = "grown-again"
    repeat = settle(sessions, device, again)
    assert repeat.receipt_id == conflict.receipt_id
    assert repeat.replay == "CAPTURE_REPLAY"
    assert repeat.outcome == "QUARANTINED"
    assert firmware_accepts(generation, again, repeat.ack(message_id=message_id, sequence=3), message_id)
    assert counts(sessions) == after_conflict


@pytest.mark.parametrize(
    "change",
    ["uid", "fingerprint", "raw_name_cnic", "status", "source", "record_uid"],
)
def test_any_change_besides_capture_time_stays_quarantined(sessions, device, change):
    original = truth_batch("2.6.15", captured_at=PASS_START)
    settle(sessions, device, original)
    replay = truth_batch("2.6.15", captured_at=PASS_START + timedelta(minutes=30))
    row = replay["events"][3]
    if change == "uid":
        # The terminal user ID now belongs to another enrollment: never
        # attribute the settled punch to it, keep the evidence.
        row["uid"] = "999"
        row["raw_event"]["terminal_uid"] = "999"
    elif change == "fingerprint":
        row["terminal_identity_fingerprint"] = "f" * 64
    elif change == "raw_name_cnic":
        row["raw_name"] = "Someone Else-3520212345671"
    elif change == "status":
        row["status"] = 1
    elif change == "source":
        row["source"] = "DUMP_STARTUP"
    else:
        row["raw_event"]["attendance_record_uid"] = "77"

    settlement = settle(sessions, device, replay)

    assert settlement.outcome == "QUARANTINED"
    assert settlement.replay is None
    assert {item["code"] for item in settlement.rejected} == {"ATTENDANCE_BATCH_ID_CONFLICT"}
    receipts, _items, quarantined, events = counts(sessions)
    assert (receipts, quarantined, events) == (2, 10, 10)


@pytest.mark.parametrize("generation", GENERATIONS)
def test_pre_deploy_conflict_history_is_matched_from_its_own_evidence(
    sessions, device, generation
):
    settle(sessions, device, truth_batch(generation, captured_at=PASS_START))
    forget_content_digests(sessions)
    # Before this release every replay became a new conflict receipt, built by
    # the same quarantine helper, and no receipt carried a content digest.
    with sessions() as db:
        for minutes in (27, 54):
            replayed = truth_batch(generation, captured_at=PASS_START + timedelta(minutes=minutes))
            legacy = AttendanceBatchReceipt(
                connector_id=device,
                zkt_device_id=_connector(db, device).zkt_device.id,
                batch_id=replayed["batch_id"],
                payload_digest=payload_digest(replayed["events"]),
                reported_digest_key=attendance_batches._reported_digest_key(None, valid=False),
                outcome="PENDING",
                item_count=10,
            )
            db.add(legacy)
            db.flush()
            for index, raw in enumerate(replayed["events"]):
                attendance_batches._quarantine_item(
                    db,
                    receipt=legacy,
                    item_index=index,
                    raw=raw,
                    error_code="ATTENDANCE_BATCH_ID_CONFLICT",
                    error_path="batch_id",
                    evidence_context={"batch_id": replayed["batch_id"], "payload_digest": None},
                )
            legacy.outcome = "QUARANTINED"
            legacy.quarantined_count = 10
        db.commit()
    baseline = counts(sessions)
    assert baseline[2] == 20

    replay = truth_batch(generation, captured_at=PASS_START + timedelta(minutes=81))
    settlement = settle(sessions, device, replay)
    message_id = "after-deploy"

    assert settlement.replay == "CAPTURE_REPLAY"
    assert settlement.outcome == "QUARANTINED"
    assert firmware_accepts(
        generation, replay, settlement.ack(message_id=message_id, sequence=1), message_id
    )
    assert counts(sessions) == baseline
    with sessions() as db:
        newest = db.scalar(
            select(AttendanceBatchReceipt).order_by(AttendanceBatchReceipt.id.desc()).limit(1)
        )
        assert newest.receipt_id == settlement.receipt_id
        assert newest.content_digest == replay_content_digest(replay["events"])
        assert newest.observation_count == 2

    # Later passes hit the stored digest directly.
    later = settle(sessions, device, truth_batch(generation, captured_at=PASS_START + timedelta(minutes=108)))
    assert later.receipt_id == settlement.receipt_id
    assert counts(sessions) == baseline


def test_pre_deploy_original_without_conflict_history_is_quarantined_once(sessions, device):
    original = settle(sessions, device, truth_batch("2.6.15", captured_at=PASS_START))
    forget_content_digests(sessions)

    # A committed receipt kept no raw rows, so its content cannot be proven
    # equal: the first replay is preserved once, exactly as before.
    first = settle(sessions, device, truth_batch("2.6.15", captured_at=PASS_START + timedelta(minutes=30)))
    assert first.outcome == "QUARANTINED"
    assert first.replay is None
    after_first = counts(sessions)
    second = settle(sessions, device, truth_batch("2.6.15", captured_at=PASS_START + timedelta(minutes=60)))
    assert second.receipt_id == first.receipt_id != original.receipt_id
    assert second.replay == "CAPTURE_REPLAY"
    assert counts(sessions) == after_first


def test_device_reported_digest_keeps_the_existing_conflict_rule(sessions, device):
    original = truth_batch("2.7.5", captured_at=PASS_START)
    settle(sessions, device, original)
    replay = truth_batch("2.7.5", captured_at=PASS_START + timedelta(minutes=30))
    replay["payload_digest"] = payload_digest(replay["events"])

    settlement = settle(sessions, device, replay)

    assert settlement.outcome == "QUARANTINED"
    assert settlement.replay is None
    assert counts(sessions)[2] == 10


def test_invalid_replayed_row_cannot_repeat_an_accepted_settlement(sessions, device):
    settle(sessions, device, truth_batch("2.6.15", captured_at=PASS_START))
    replay = truth_batch("2.6.15", captured_at=PASS_START + timedelta(minutes=30))
    replay["events"][0]["captured_at"] = "not-a-timestamp"

    settlement = settle(sessions, device, replay)

    assert settlement.outcome == "QUARANTINED"
    assert settlement.replay is None


def test_exact_retry_is_counted_and_records_its_content_digest(sessions, device):
    original = truth_batch("2.5.2", captured_at=PASS_START)
    first = settle(sessions, device, original)
    forget_content_digests(sessions)

    retry = settle(sessions, device, original)

    assert retry.receipt_id == first.receipt_id
    assert retry.replay == "EXACT_RETRY"
    assert retry.response()["replay"] == "EXACT_RETRY"
    with sessions() as db:
        receipt = db.scalar(select(AttendanceBatchReceipt))
        assert receipt.content_digest == replay_content_digest(original["events"])
        assert receipt.observation_count == 2
    assert first.response().get("replay") is None


def test_replays_on_another_connector_are_not_matched(sessions, device):
    settle(sessions, device, truth_batch("2.6.15", captured_at=PASS_START))
    with sessions() as db:
        other, _token, _created = onboard_connector(
            db, hardware_id="e0:72:a1:00:01:99", zone_id="ZONE-MULTAN-02",
            zone_name="ZONE-MULTAN-02", device_id="2", firmware_version="2.6.15",
            expected_serial="OTHER-SERIAL", actor="test", ip_address="127.0.0.1",
        )
        db.commit()
        other_pk = other.id
    settle(sessions, other_pk, truth_batch("2.6.15", captured_at=PASS_START + timedelta(minutes=1)))
    # Same batch id on the second connector: its own first send, not a replay.
    with sessions() as db:
        rows = db.scalars(select(AttendanceBatchReceipt).order_by(AttendanceBatchReceipt.id)).all()
        assert [row.connector_id for row in rows] == [device, other_pk]
        assert all(row.observation_count == 1 for row in rows)


@pytest.mark.parametrize("generation", GENERATIONS)
def test_websocket_ack_for_a_replayed_truth_batch(sessions, device, generation, monkeypatch):
    @contextmanager
    def scope():
        with sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setattr(web, "session_scope", scope)
    connector_id = _connector_id(sessions, device)
    sequence = 0

    def send(payload: dict):
        nonlocal sequence
        sequence += 1
        envelope = Envelope(
            schema_version="2", message_id=f"m-{sequence}", connector_id=connector_id,
            boot_id="boot-1", seq=sequence, sent_at=utc_now(), type="attendance_batch",
            payload=payload,
        )
        return envelope, web.persist_envelope(device, envelope)

    first_payload = truth_batch(generation, captured_at=PASS_START)
    _envelope, first = send(first_payload)
    assert firmware_accepts(generation, first_payload, first.ack, "m-1")
    baseline = counts(sessions)
    for minutes in (27, 54, 81):
        payload = truth_batch(generation, captured_at=PASS_START + timedelta(minutes=minutes))
        envelope, outcome = send(payload)
        assert outcome.ack["receipt_id"] == first.ack["receipt_id"]
        assert outcome.ack["duplicate_batch"] is True
        assert firmware_accepts(generation, payload, outcome.ack, envelope.message_id)
    assert counts(sessions) == baseline


def test_replay_meter_logs_at_most_once_per_interval(caplog):
    meter = _ReplayMeter(interval_seconds=300)
    with caplog.at_level(logging.WARNING, logger="zk_add.attendance_batches"):
        assert meter.record("c-1", "CAPTURE_REPLAY_OF_COMMITTED", batch_id="truth-a", now=0.0)
        for second in range(1, 200):
            assert not meter.record(
                "c-1", "CAPTURE_REPLAY_OF_COMMITTED", batch_id="truth-a", now=float(second)
            )
        assert meter.record("c-2", "BATCH_ID_CONFLICT_QUARANTINED", batch_id="truth-b", now=5.0)
        assert meter.record("c-1", "EXACT_RETRY", batch_id="truth-c", now=300.0)
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 3
    assert "counts={'CAPTURE_REPLAY_OF_COMMITTED': 199, 'EXACT_RETRY': 1}" in lines[2]
    assert meter.snapshot() == {
        "c-1": {"CAPTURE_REPLAY_OF_COMMITTED": 200, "EXACT_RETRY": 1},
        "c-2": {"BATCH_ID_CONFLICT_QUARANTINED": 1},
    }


def test_content_digest_ignores_only_the_capture_time():
    base = [firmware_row("2.6.15", index=0, captured_at=PASS_START)]
    later = [firmware_row("2.6.15", index=0, captured_at=PASS_START + timedelta(hours=1))]
    assert replay_content_digest(base) == replay_content_digest(later)
    changed = [{**later[0], "source": "DUMP_STARTUP"}]
    assert replay_content_digest(base) != replay_content_digest(changed)
    assert replay_content_digest(base) != replay_content_digest(base + base)
    assert replay_content_digest({"events": base}) is None


def test_migration_adds_the_column_once_and_keeps_it_on_downgrade(sessions):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    path = (
        Path(__file__).resolve().parents[2]
        / "apps/add_backend/migrations/versions/20261022_0054_duplicate_batch_resend.py"
    )
    spec = importlib.util.spec_from_file_location("batch_replay_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "20261012_0053"
    with sessions() as db:
        engine = db.get_bind()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE add_attendance_batch_receipts DROP COLUMN content_digest"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        migration.downgrade()
        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("add_attendance_batch_receipts")
        }
    assert columns["content_digest"]["nullable"] is True


def test_admin_can_read_replay_counters(sessions, device, monkeypatch):
    from fastapi.testclient import TestClient

    from zk_add.security import ADMIN_COOKIE, create_admin_session

    monkeypatch.setattr(settings, "admin_username", "StateHealthAdmin")
    settle(sessions, device, truth_batch("2.6.15", captured_at=PASS_START))
    settle(sessions, device, truth_batch("2.6.15", captured_at=PASS_START + timedelta(minutes=30)))
    with sessions() as db:
        raw_session, _admin = create_admin_session(
            db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest"
        )
        db.commit()

    def request_session():
        with sessions() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setitem(web.app.dependency_overrides, web.get_db, request_session)
    client = TestClient(web.app)
    assert client.get("/api/v1/attendance-batch-replays").status_code == 401
    client.cookies.set(ADMIN_COOKIE, raw_session)
    response = client.get("/api/v1/attendance-batch-replays")

    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "PROCESS_SINCE_START"
    assert body["rows"] == [
        {
            "connector_id": _connector_id(sessions, device),
            "display_name": "ZONE-LAHORE-02",
            "counts": {"CAPTURE_REPLAY_OF_COMMITTED": 1},
        }
    ]
