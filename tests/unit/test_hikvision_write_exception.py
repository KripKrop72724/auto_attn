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
@pytest.mark.parametrize("permanent", [False, True])
def test_other_connector_cannot_be_approved(target, field, value, permanent):
    session, connector, terminal = target
    setattr(connector, field, value)
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        grant(session, connector, permanent=permanent)
    assert not exception.active(connector, terminal)


@pytest.mark.parametrize("field,value", [("serial", "wrong"), ("expected_serial", "wrong"),
    ("confirmed_serial", "wrong"), ("terminal_binding_state", "PENDING")])
@pytest.mark.parametrize("permanent", [False, True])
def test_other_terminal_cannot_be_approved(target, field, value, permanent):
    session, connector, terminal = target
    setattr(terminal, field, value)
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        grant(session, connector, permanent=permanent)


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


@pytest.mark.parametrize("version,capability", [("3.1.1-bld1fix4", 3), ("3.1.1-bld1fix5", 4)])
def test_signed_repair_heartbeat_is_accepted_and_keeps_approval(target, version, capability):
    from test_hikvision_clock import heartbeat
    from zk_add.schemas import HeartbeatPayload
    from zk_add.service import update_heartbeat

    session, connector, terminal = target
    grant(session, connector, permanent=capability == 4)
    data = heartbeat().model_dump()
    data["firmware_version"] = "zone-lite-" + version
    data["terminal"].update(serial=exception.SERIAL, capability_profile=exception.PROFILE,
                            profile_command_version=capability)
    update_heartbeat(session, connector=connector, boot_id="test-boot", sequence=1,
                     payload=HeartbeatPayload.model_validate(data))
    assert terminal.capability_profile["hikvision_write_exception"]["enabled"]
    assert terminal.certification_state == "PROFILE_EXCEPTION"
    assert require_writable_user_profile(connector, "user_write") is terminal
    assert terminal.capability_profile["hikvision_health"]["qualification_state"] == "NOT_QUALIFIED"
    data["terminal"]["profile_command_version"] = 5
    with pytest.raises(ValueError):
        HeartbeatPayload.model_validate(data)


def test_permanent_grant_has_no_date_limit_on_verified_permanent_firmware(target, monkeypatch):
    session, connector, terminal = target
    request = grant(session, connector, permanent=True)
    assert request["permanent"] and request["expires_epoch"] is None
    # The old image stays honestly time-limited until the replacement is installed.
    auto_certify_zkt(session, connector, terminal)
    assert terminal.capability_profile["write_exception_pending_firmware"]
    assert not terminal.capability_profile["write_exception_permanent"]
    assert terminal.capability_profile["write_exception_expires_at"]
    monkeypatch.setattr(exception, "now_epoch", lambda: 4102444800)
    assert not exception.active(connector, terminal)
    connector.firmware_version = "zone-lite-3.1.1-bld1fix5"
    terminal.capability_profile["hikvision_health"]["profile_command_version"] = 4
    auto_certify_zkt(session, connector, terminal)
    assert exception.permanent_active(connector, terminal)
    assert require_writable_user_profile(connector, "user_write") is terminal
    assert terminal.capability_profile["write_exception_permanent"]
    assert not terminal.capability_profile["write_exception_pending_firmware"]
    assert terminal.capability_profile["write_exception_expires_at"] is None
    assert not terminal.capability_profile["user_role_write"]
    assert not terminal.capability_profile["admin_lease"]
    assert terminal.capability_profile["hikvision_health"]["qualification_state"] == "NOT_QUALIFIED"


def test_permanent_grant_can_be_created_and_revoked_after_old_expiry(target, monkeypatch):
    session, connector, terminal = target
    monkeypatch.setattr(exception, "now_epoch", lambda: exception.EXPIRES + 86400)
    connector.firmware_version = "zone-lite-3.1.1-bld1fix5"
    terminal.capability_profile["hikvision_health"]["profile_command_version"] = 4
    grant(session, connector, permanent=True)
    assert exception.active(connector, terminal)
    grant(session, connector, permanent=True, enabled=False, idempotency_key="revoke-permanent")
    auto_certify_zkt(session, connector, terminal)
    assert not exception.active(connector, terminal)
    assert not terminal.capability_profile["user_write"]
    assert not terminal.capability_profile["write_exception_pending_firmware"]


@pytest.mark.parametrize("family,count,expected", [
    ("zkt", 184, 120), ("hikvision", None, 600), ("hikvision", 184, 600),
    ("hikvision", 1000, 1620), ("hikvision", 3000, 3600),
])
def test_hikvision_user_sync_has_time_for_two_complete_scans(family, count, expected):
    from types import SimpleNamespace
    from zk_add.service import user_refresh_timeout_seconds
    connector = SimpleNamespace(firmware_family=family, zkt_device=SimpleNamespace(user_count=count))
    assert user_refresh_timeout_seconds(connector) == expected


def test_user_refresh_endpoint_uses_the_hikvision_scan_window(target, monkeypatch):
    from types import SimpleNamespace
    from zk_add import web
    session, connector, terminal = target
    terminal.user_count = 184
    captured = {}
    original = web.create_command

    def record_command(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(web, "create_command", record_command)
    result = web.refresh_users(
        connector.connector_id,
        web.UserRefreshRequest(idempotency_key="test-manual-hikvision-sync"),
        (session, SimpleNamespace(username="test")),
    )
    assert captured["command_type"] == "REFRESH_USERS"
    assert captured["expires_in_seconds"] == 600
    assert result["type"] == "REFRESH_USERS"
