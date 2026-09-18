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
