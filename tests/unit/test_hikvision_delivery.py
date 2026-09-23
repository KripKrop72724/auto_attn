import hashlib
import json

from sqlalchemy import select, func

from zk_add.crypto import decrypt_cnic
from zk_add.hikvision_delivery import HikvisionPolicy
import zk_add.hikvision_profiles  # noqa: F401
from zk_add.hikvision_evidence import ObservationIn, preserve_observation, HikvisionEvidence
from zk_add.models import AttendanceEvent, OrdsOutbox
import pytest
from test_hikvision_evidence import db as evidence_db


@pytest.fixture
def db(monkeypatch):
    yield from evidence_db.__wrapped__(monkeypatch)


def payload(serial=123, employee="00111", **changes):
    raw = json.dumps(
        {
            "serialNo": serial,
            "major": 5,
            "minor": 75,
            "employeeNoString": employee,
            "name": "Test-1234512345671",
            "time": "2026-09-17T10:00:00+05:00",
            **changes,
        }
    )
    return ObservationIn(
        schema_version=1,
        source_protocol="hikvision-isapi-v1",
        terminal_serial="terminal",
        source_epoch="verified-epoch",
        observation_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        channel="POLL",
        raw=raw,
        captured_epoch=1790000000,
    )


def policy(session, connector):
    session.add(
        HikvisionPolicy(
            connector_id=connector.id,
            enabled=True,
            source_epoch="verified-epoch",
            terminal_serial="terminal",
            profile_id="test",
            success_codes=[[5, 75]],
            excluded_codes=[[5, 21]],
        )
    )
    session.flush()


def test_add_custody_and_oracle_work_commit_together_and_retry_independently(db):
    session, connector = db
    policy(session, connector)
    message = payload()
    receipt = preserve_observation(session, connector, message)
    session.commit()
    row = session.scalar(select(AttendanceEvent))
    outbox = session.scalar(select(OrdsOutbox))
    assert row.source == "LIVE_POLL" and row.user_id == "00111"
    assert row.identity_resolution_status == "RESOLVED_HIKVISION_NAME"
    assert row.punch is None and row.status is None
    assert outbox.status == "PENDING" and row.oracle_confirmed_at is None
    outbox.status = "FAILED_RETRYABLE"
    outbox.attempt_count = 1
    session.commit()
    assert preserve_observation(session, connector, message) == receipt
    assert session.scalar(select(func.count()).select_from(AttendanceEvent)) == 1
    assert outbox.status == "FAILED_RETRYABLE" and outbox.attempt_count == 1
    # A second representation shares source identity without a second Oracle job.
    preserve_observation(session, connector, payload(name="Changed-1234512345671"))
    assert session.scalar(select(func.count()).select_from(OrdsOutbox)) == 1


def test_no_name_derived_cnic_or_numeric_identifier_guess(db):
    session, connector = db
    policy(session, connector)
    preserve_observation(session, connector, payload(employee="111", name="Test 1234512345671"))
    row = session.scalar(select(AttendanceEvent))
    assert row.cnic_encrypted is None and row.device_user_id is None
    assert row.ords_status == "BLOCKED_IDENTITY"
    assert session.scalar(select(OrdsOutbox)).status == "BLOCKED_IDENTITY"


def publish_profile(session, connector, employee="00111"):
    from zk_add.hikvision_profiles import accept_profile_page
    raw = json.dumps({"employeeNo": employee, "name": "Test-S-1234512345671",
                      "userType": "normal", "localUIRight": False}, separators=(",", ":"))
    request = dict(snapshot_id="b" * 32, terminal_serial="terminal", position=0, total=1, records=[raw])
    accept_profile_page(session, connector, {**request, "phase": 1})
    accept_profile_page(session, connector, {**request, "phase": 2})
    session.commit()


def test_plain_historical_name_uses_exact_verified_employee_profile(db):
    session, connector = db
    policy(session, connector)
    publish_profile(session, connector)
    preserve_observation(session, connector, payload(name="Plain historic name"))
    row = session.scalar(select(AttendanceEvent))
    assert row.ords_status == "PENDING" and row.raw_punch
    assert row.raw_event["identity_source"] == "VERIFIED_PROFILE_NAME_CNIC"
    assert row.identity_snapshot_id == connector.zkt_device.identity_snapshot_id
    assert decrypt_cnic(row.cnic_encrypted) == "1234512345671"


def test_profile_arrival_releases_only_previously_unmapped_undelivered_punches(db):
    from zk_add.hikvision_delivery import repair_profile_identity_holds
    session, connector = db
    policy(session, connector)
    preserve_observation(session, connector, payload(name="Plain historic name"))
    preserve_observation(session, connector, payload(serial=124, employee="111", name="Same display"))
    session.commit()
    first = session.scalar(select(AttendanceEvent).where(AttendanceEvent.sequence == 123))
    uid = first.event_uid
    publish_profile(session, connector)
    assert repair_profile_identity_holds(session) == 0
    assert first.ords_status == "BLOCKED_IDENTITY" and first.event_uid == uid
    assert first.manual_release_required
    # Leading zero identifiers are distinct; names never supply the join.
    assert session.scalar(select(AttendanceEvent).where(AttendanceEvent.sequence == 124)).ords_status == "BLOCKED_IDENTITY"
    assert repair_profile_identity_holds(session) == 0


def test_detected_identifier_reuse_blocks_profile_fallback(db):
    from zk_add.models import DeviceUser
    session, connector = db
    policy(session, connector)
    publish_profile(session, connector)
    session.add(DeviceUser(zkt_device_id=connector.zkt_device.id, uid="00111", user_id="00111",
                           display_name="Old lifecycle", lifecycle_state="DELETED", present=False))
    session.flush()
    preserve_observation(session, connector, payload(name="Plain historic name"))
    assert session.scalar(select(AttendanceEvent)).ords_status == "BLOCKED_IDENTITY"


def test_encoded_name_does_not_require_a_separate_mapping_and_shift_is_preserved(db):
    session, connector = db
    policy(session, connector)
    preserve_observation(session, connector, payload(name="Test-S-1234512345671"))
    row = session.scalar(select(AttendanceEvent))
    assert row.ords_status == "PENDING" and row.raw_punch
    assert decrypt_cnic(row.cnic_encrypted) == "1234512345671"


def test_oracle_worker_claims_encoded_identity_without_snapshot_or_mapping(db, monkeypatch):
    from contextlib import contextmanager
    from zk_add import worker
    from zk_add.settings import settings

    session, connector = db
    policy(session, connector)
    preserve_observation(session, connector, payload(name="Test-S-1234512345671"))
    preserve_observation(session, connector, payload(serial=124, name="Unmapped"))
    session.commit()
    monkeypatch.setattr(settings, "identity_snapshot_gate_enabled", True)

    @contextmanager
    def local_session():
        yield session
        session.commit()

    monkeypatch.setattr(worker, "session_scope", local_session)
    claims = worker.claim_ords_batch(10)
    assert len(claims) == 1
    valid = session.scalar(select(AttendanceEvent).where(AttendanceEvent.sequence == 123))
    held = session.scalar(select(AttendanceEvent).where(AttendanceEvent.sequence == 124))
    assert claims[0][1]["event_uid"] == valid.event_uid
    assert valid.device_user_id is None
    assert session.get(OrdsOutbox, claims[0][0]).status == "IN_FLIGHT"
    assert held.ords_status == "BLOCKED_IDENTITY"


def test_same_source_with_a_different_encoded_cnic_is_held(db):
    session, connector = db
    policy(session, connector)
    preserve_observation(session, connector, payload())
    preserve_observation(session, connector, payload(name="Test-1234512345672"))
    assert session.scalar(select(AttendanceEvent)).ords_status == "QUARANTINED_IDENTITY_CONFLICT"
    assert session.scalar(select(OrdsOutbox)).status == "QUARANTINED_IDENTITY_CONFLICT"


def test_source_conflict_quarantines_existing_oracle_work(db):
    session, connector = db
    policy(session, connector)
    preserve_observation(session, connector, payload())
    preserve_observation(session, connector, payload(employee="222"))
    assert session.scalar(select(AttendanceEvent)).ords_status == "QUARANTINED_SOURCE_CONFLICT"
    assert session.scalar(select(OrdsOutbox)).status == "QUARANTINED_SOURCE_CONFLICT"
    assert {r.disposition for r in session.scalars(select(HikvisionEvidence))} == {
        "SOURCE_FACT_CONFLICT"
    }


def test_unknown_and_explicitly_excluded_codes_are_not_attendance(db):
    session, connector = db
    policy(session, connector)
    preserve_observation(session, connector, payload(minor=21))
    preserve_observation(session, connector, payload(serial=124, minor=999))
    assert session.scalar(select(func.count()).select_from(AttendanceEvent)) == 0
    assert {r.disposition for r in session.scalars(select(HikvisionEvidence))} == {
        "NON_ATTENDANCE",
        "UNCLASSIFIED_SOURCE",
    }


def test_transaction_failure_leaves_no_partial_custody_or_outbox(db):
    session, connector = db
    policy(session, connector)
    session.commit()
    preserve_observation(session, connector, payload())
    session.rollback()
    for model in (AttendanceEvent, OrdsOutbox, HikvisionEvidence):
        assert session.scalar(select(func.count()).select_from(model)) == 0
    assert preserve_observation(session, connector, payload())["durable"]
    session.commit()
    assert session.scalar(select(func.count()).select_from(OrdsOutbox)) == 1


def test_enabling_policy_then_replaying_custody_creates_oracle_work_once(db):
    session, connector = db
    message = payload()
    preserve_observation(session, connector, message)
    session.commit()
    assert session.scalar(select(func.count()).select_from(AttendanceEvent)) == 0
    policy(session, connector)
    preserve_observation(session, connector, message)
    preserve_observation(session, connector, message)
    assert session.scalar(select(func.count()).select_from(AttendanceEvent)) == 1
    assert session.scalar(select(func.count()).select_from(OrdsOutbox)) == 1


def test_policy_cannot_rebind_source_epoch_or_confuse_idempotent_changes(db):
    from zk_add.hikvision_delivery import configure_policy
    session, connector = db
    request = dict(terminal_serial='terminal', source_epoch='epoch-1', profile_id='qualified-test',
                   success_codes=[[5, 75]], excluded_codes=[[5, 21]], enabled=True,
                   actor='test', reason='Controlled qualification completed', idempotency_key='policy-test-1')
    first = configure_policy(session, connector, **request)
    session.commit()
    assert configure_policy(session, connector, **request).mapping_revision == first.mapping_revision
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        configure_policy(session, connector, **{**request, 'enabled': False})
    with pytest.raises(ValueError, match='SOURCE_EPOCH_CHANGE_REQUIRES_REVIEW'):
        configure_policy(session, connector, **{**request, 'source_epoch': 'epoch-2', 'idempotency_key': 'policy-test-2'})
