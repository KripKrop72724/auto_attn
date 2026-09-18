import json

import pytest
from sqlalchemy import select, func

from zk_add.hikvision_profiles import accept_profile_page, HikvisionProfileScan
from zk_add.models import DeviceUser
from test_hikvision_evidence import db as evidence_db


@pytest.fixture
def db(monkeypatch):
    yield from evidence_db.__wrapped__(monkeypatch)


def page(phase=1, position=0, total=1, employee="00111", name="Test-1234512345671", **changes):
    raw = json.dumps({"employeeNo": employee, "name": name,
                      "userType": "normal", "localUIRight": False}, separators=(",", ":"))
    return dict(snapshot_id="a" * 32, terminal_serial="terminal", phase=phase,
                position=position, total=total, records=[] if not total else [raw], **changes)


def test_profiles_are_published_only_after_matching_complete_scans(db):
    session, connector = db
    request = page()
    assert not accept_profile_page(session, connector, request)["published"]
    session.commit()
    assert session.scalar(select(func.count()).select_from(DeviceUser)) == 0
    scan = session.get(HikvisionProfileScan, connector.id)
    assert "1234512345671" not in scan.data_encrypted
    assert not accept_profile_page(session, connector, request)["published"]
    assert accept_profile_page(session, connector, page(phase=2))["published"]
    session.commit()
    user = session.scalar(select(DeviceUser))
    assert user.user_id == user.uid == "00111"
    assert user.display_name == "Test" and user.present and user.privilege == 0
    version = user.row_version
    assert accept_profile_page(session, connector, page(phase=2))["published"]
    assert user.row_version == version


def test_changed_or_duplicate_inventory_cannot_publish(db):
    session, connector = db
    accept_profile_page(session, connector, page())
    session.commit()
    with pytest.raises(ValueError, match="SCAN_CHANGED"):
        accept_profile_page(session, connector, page(phase=2, name="Changed"))
    session.rollback()
    assert session.scalar(select(func.count()).select_from(DeviceUser)) == 0
    accept_profile_page(session, connector, page(phase=2))
    session.commit()
    first = page(total=2)
    first["snapshot_id"] = "b" * 32
    accept_profile_page(session, connector, first)
    session.commit()
    with pytest.raises(ValueError, match="INVALID_OR_DUPLICATE"):
        accept_profile_page(session, connector, {**first, "position": 1})
    session.rollback()
    assert session.scalar(select(DeviceUser)).present


def test_empty_first_pass_does_not_delete_existing_profiles(db):
    session, connector = db
    accept_profile_page(session, connector, page())
    accept_profile_page(session, connector, page(phase=2))
    session.commit()
    request = page(total=0)
    request["snapshot_id"] = "b" * 32
    accept_profile_page(session, connector, request)
    session.commit()
    assert session.scalar(select(DeviceUser)).present
    assert accept_profile_page(session, connector, {**request, "phase": 2})["published"]
    assert not session.scalar(select(DeviceUser)).present


def test_employee_reservations_preserve_strings_and_never_reuse_tombstones(db):
    from zk_add.service import allocate_device_identifiers

    session, connector = db
    accept_profile_page(session, connector, page(employee="00065536"))
    accept_profile_page(session, connector, page(phase=2, employee="00065536"))
    session.commit()
    user = session.scalar(select(DeviceUser))
    user.present = False
    user.lifecycle_state = "DELETED"
    session.flush()
    zkt = connector.zkt_device
    assert allocate_device_identifiers(session, zkt=zkt, user_id_override=None) == ("65537", "65537")
    employee = "0" * 31 + "1"
    assert allocate_device_identifiers(session, zkt=zkt, user_id_override=employee) == (employee, employee)
    with pytest.raises(ValueError, match="already been used"):
        allocate_device_identifiers(session, zkt=zkt, user_id_override="00065536")
    for invalid in ("", "1" * 33, "１２３", "12a"):
        with pytest.raises(ValueError, match="ASCII digits"):
            allocate_device_identifiers(session, zkt=zkt, user_id_override=invalid)


def test_hikvision_cannot_inherit_zkt_write_certification(db):
    from zk_add.service import auto_certify_zkt

    session, connector = db
    terminal = connector.zkt_device
    terminal.online = True
    terminal.certification_state = "CERTIFIED"
    terminal.capability_profile = {"observed_user_record_bytes": 72, "user_write": True,
                                   "create_user": True, "admin_lease": True}
    auto_certify_zkt(session, connector, terminal)
    assert terminal.certification_state == "READ_ONLY"
    assert terminal.writes_disabled_reason == "HIKVISION_WRITE_QUALIFICATION_PENDING"
    assert not terminal.capability_profile["create_user"]
    assert not terminal.capability_profile["admin_lease"]


def test_zkt_employee_length_limit_remains_unchanged(db):
    from zk_add.service import allocate_device_identifiers

    session, connector = db
    connector.firmware_family = "zkt"
    with pytest.raises(ValueError, match="24 characters"):
        allocate_device_identifiers(session, zkt=connector.zkt_device, user_id_override="1" * 25)


def test_profile_pilot_requires_add_approval_firmware_and_complete_snapshot(db):
    from zk_add.hikvision_delivery import configure_policy
    from zk_add.service import auto_certify_zkt, require_writable_user_profile

    session, connector = db
    terminal = connector.zkt_device
    profile = 'ds-k1t342efwx-v3.3.5-220310-poll5-pilot-v1'
    terminal.online = True
    terminal.terminal_binding_state = 'CONFIRMED'
    terminal.snapshot_complete = terminal.identity_snapshot_stable = True
    terminal.capability_profile = {'hikvision_health': {
        'capability_profile': profile, 'profile_command_version': 1,
    }}
    auto_certify_zkt(session, connector, terminal)
    assert terminal.certification_state == 'READ_ONLY'
    configure_policy(session, connector, terminal_serial='terminal', source_epoch='epoch',
                     profile_id=profile, success_codes=[[5, 75]], excluded_codes=[], enabled=True,
                     profile_commands_enabled=True, actor='test', reason='Qualified disposable profile operations',
                     idempotency_key='profile-pilot-approval')
    auto_certify_zkt(session, connector, terminal)
    assert terminal.certification_state == 'PROFILE_PILOT'
    assert require_writable_user_profile(connector, 'user_write') is terminal
    assert terminal.capability_profile['user_role_write']
    assert not terminal.capability_profile['admin_lease']
    with pytest.raises(ValueError, match='read-only'):
        require_writable_user_profile(connector, 'admin_lease')
    terminal.identity_snapshot_stable = False
    auto_certify_zkt(session, connector, terminal)
    assert not terminal.capability_profile['user_write']
    terminal.identity_snapshot_stable = True
    terminal.capability_profile = {**terminal.capability_profile, 'hikvision_health': {'capability_profile': profile}}
    auto_certify_zkt(session, connector, terminal)
    assert terminal.certification_state == 'READ_ONLY'


def test_unknown_hikvision_profile_cannot_receive_write_approval(db):
    from zk_add.hikvision_delivery import configure_policy

    session, connector = db
    with pytest.raises(ValueError, match='NOT_QUALIFIED'):
        configure_policy(session, connector, terminal_serial='terminal', source_epoch='epoch',
                         profile_id='unqualified', success_codes=[[5, 75]], excluded_codes=[], enabled=True,
                         profile_commands_enabled=True, actor='test', reason='Invalid profile approval attempt',
                         idempotency_key='profile-pilot-invalid')


@pytest.mark.parametrize('operation', ['CREATE_USER', 'UPDATE_USER', 'DELETE_USER'])
@pytest.mark.parametrize('evidence', ['valid', 'missing', 'wrong_terminal', 'missing_postcondition'])
def test_hikvision_commands_require_bound_readback_receipts(db, operation, evidence):
    import hashlib
    from zk_add.service import create_command, apply_command_update

    session, connector = db
    command = create_command(session, connector=connector, command_type=operation,
                             payload={'user_id': '000123'}, expected_state={'serial': 'terminal'},
                             desired_state={}, idempotency_key='receipt', actor='test')
    session.flush()
    result = {
        'verified': True,
        'verified_terminal_identity_fingerprint': hashlib.sha256(b'terminal\n000123').hexdigest(),
        'verified_terminal_state_fingerprint': 'a' * 64,
        'user_absent': True,
    }
    if evidence == 'missing':
        result = {}
    elif evidence == 'wrong_terminal':
        result['verified_terminal_identity_fingerprint'] = 'b' * 64
    elif evidence == 'missing_postcondition':
        result.pop('user_absent' if operation == 'DELETE_USER' else 'verified_terminal_state_fingerprint')
    applied = apply_command_update(session, connector=connector, command_id=command.command_id,
                                   status='SUCCEEDED', result=result, error_code=None, error_message=None)
    assert applied.status == ('SUCCEEDED' if evidence == 'valid' else 'FAILED')
