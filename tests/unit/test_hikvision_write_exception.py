import pytest
from sqlalchemy import select, func
from zk_add.models import AuditEvent
from zk_add import hikvision_write_exception as exception
from zk_add.service import auto_certify_zkt, require_writable_user_profile
from test_hikvision_evidence import db as evidence_db


@pytest.fixture
def target(monkeypatch):
    monkeypatch.setattr(exception, "now_epoch", lambda: 1792000000)
    for session, connector in evidence_db.__wrapped__(monkeypatch):
        connector.connector_id = exception.CONNECTOR
        connector.hardware_id = exception.MAC
        connector.zone_id = exception.ZONE
        connector.device_id = "1"
        connector.firmware_version = "zone-lite-3.1.1-bld1fix4"
        terminal = connector.zkt_device
        terminal.serial = terminal.expected_serial = terminal.confirmed_serial = exception.SERIAL
        terminal.terminal_binding_state = "CONFIRMED"
        terminal.snapshot_complete = terminal.identity_snapshot_stable = True
        terminal.online = True
        terminal.capability_profile = {"hikvision_health": {
            "capability_profile": exception.PROFILE, "profile_command_version": 3,
            "qualification_state": "NOT_QUALIFIED"}}
        session.flush()
        yield session, connector, terminal


def grant(session, connector, **changes):
    values = dict(
        terminal_serial=exception.SERIAL, enabled=True, reason="Owner requested urgent single-terminal exception",
        idempotency_key="test-0001", actor="test")
    values.update(changes)
    return exception.configure(session, connector, **values)


def test_exception_requires_approval_and_keeps_identity_checks(target):
    session, connector, terminal = target
    auto_certify_zkt(session, connector, terminal)
    assert not terminal.capability_profile["user_write"]
    grant(session, connector)
    auto_certify_zkt(session, connector, terminal)
    assert terminal.certification_state == "PROFILE_EXCEPTION"
    for capability in ("user_write", "create_user", "delete_user"):
        assert require_writable_user_profile(connector, capability) is terminal
    assert not terminal.capability_profile["user_role_write"]
    assert not terminal.capability_profile["admin_lease"]
    assert terminal.capability_profile["hikvision_health"]["qualification_state"] == "NOT_QUALIFIED"
    grant(session, connector)
    assert session.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.action == "HIKVISION_WRITE_EXCEPTION_CONFIGURED")) == 1


@pytest.mark.parametrize("field,value", [
    ("hardware_id", "ac:27:6e:a3:0a:59"), ("connector_id", "other"),
    ("zone_id", "other"), ("device_id", "2"), ("firmware_family", "zkt"),
    ("lifecycle_state", "QUARANTINED_DUPLICATE_SERIAL")])
def test_other_connector_cannot_be_approved(target, field, value):
    session, connector, terminal = target
    setattr(connector, field, value)
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        grant(session, connector)
    assert not exception.active(connector, terminal)


@pytest.mark.parametrize("field,value", [("serial", "wrong"), ("expected_serial", "wrong"),
    ("confirmed_serial", "wrong"), ("terminal_binding_state", "PENDING")])
def test_other_terminal_cannot_be_approved(target, field, value):
    session, connector, terminal = target
    setattr(terminal, field, value)
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        grant(session, connector)


def test_expiry_is_checked_for_every_command_not_only_heartbeats(target, monkeypatch):
    session, connector, terminal = target
    grant(session, connector)
    auto_certify_zkt(session, connector, terminal)
    monkeypatch.setattr(exception, "now_epoch", lambda: exception.EXPIRES)
    with pytest.raises(ValueError, match="read-only"):
        require_writable_user_profile(connector, "user_write")
    with pytest.raises(ValueError, match="EXPIRED"):
        grant(session, connector)
    auto_certify_zkt(session, connector, terminal)
    assert not terminal.capability_profile["user_write"]


@pytest.mark.parametrize("field,value", [("snapshot_complete", False), ("identity_snapshot_stable", False)])
def test_incomplete_snapshot_cannot_write(target, field, value):
    session, connector, terminal = target
    grant(session, connector)
    setattr(terminal, field, value)
    auto_certify_zkt(session, connector, terminal)
    assert not terminal.capability_profile["user_write"]


def test_old_firmware_cannot_gain_writes(target):
    session, connector, terminal = target
    grant(session, connector)
    connector.firmware_version = "zone-lite-3.1.1-bld1fix3"
    auto_certify_zkt(session, connector, terminal)
    assert not terminal.capability_profile["user_write"]
    connector.firmware_version = "zone-lite-3.1.1-bld1fix4"
    terminal.capability_profile["hikvision_health"]["profile_command_version"] = 2
    auto_certify_zkt(session, connector, terminal)
    assert not terminal.capability_profile["user_write"]


def test_revocation_does_not_change_attendance_policy_or_qualification(target):
    session, connector, terminal = target
    grant(session, connector)
    exception.configure(session, connector, terminal_serial=exception.SERIAL, enabled=False,
        reason="Operator revoked temporary exception", idempotency_key="test-0002", actor="test")
    auto_certify_zkt(session, connector, terminal)
    assert terminal.certification_state == "READ_ONLY"
    assert not terminal.capability_profile["user_write"]


def test_signed_repair_heartbeat_is_accepted_and_keeps_approval(target):
    from test_hikvision_clock import heartbeat
    from zk_add.schemas import HeartbeatPayload
    from zk_add.service import update_heartbeat

    session, connector, terminal = target
    grant(session, connector)
    data = heartbeat().model_dump()
    data["firmware_version"] = "zone-lite-3.1.1-bld1fix4"
    data["terminal"].update(serial=exception.SERIAL, capability_profile=exception.PROFILE,
                            profile_command_version=3)
    update_heartbeat(session, connector=connector, boot_id="test-boot", sequence=1,
                     payload=HeartbeatPayload.model_validate(data))
    assert terminal.capability_profile["hikvision_write_exception"]["enabled"]
    assert terminal.certification_state == "PROFILE_EXCEPTION"
    assert require_writable_user_profile(connector, "user_write") is terminal
    assert terminal.capability_profile["hikvision_health"]["qualification_state"] == "NOT_QUALIFIED"
    data["terminal"]["profile_command_version"] = 4
    with pytest.raises(ValueError):
        HeartbeatPayload.model_validate(data)
