"""People read-side API: sorting, paging, totals, quick views, damaged entries, summary and search."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import event, select

from test_add_backend import CNIC, connector_fixture, db as db  # noqa: F401
from test_device_health import admin_client
from zk_add import dashboard_cache, device_names, people_read, web
from zk_add.models import DeviceUser, DeviceUserSnapshot, TemporaryAdminLease
from zk_add.schemas import UserSnapshotRequest, UserSnapshotRow
from zk_add.service import replace_user_snapshot
from zk_add.time_utils import utc_now

OTHER_CNIC = "4230111028815"


@pytest.fixture(autouse=True)
def _clean():
    device_names.forget_fleet()
    dashboard_cache.invalidate()
    yield
    device_names.forget_fleet()
    dashboard_cache.invalidate()


def _people(db, connector, users, snapshot_id="people"):
    replace_user_snapshot(db, connector=connector, snapshot=UserSnapshotRequest(
        snapshot_id=snapshot_id, complete=True, observed_at=utc_now(), users=users))
    db.commit()


def _machine(db, *, code="ZONE-MULTAN-01", hardware="e0:72:a1:d6:f5:01", serial="PEOPLE-0001"):
    connector = connector_fixture(db, hardware_id=hardware, expected_serial=serial)
    connector.display_name = connector.zone_id = connector.zone_name = code
    db.commit()
    _people(db, connector, [
        UserSnapshotRow(uid="1", user_id="1001", name=f"Zara-{CNIC}"),
        UserSnapshotRow(uid="2", user_id="1002", name="Ali Khan"),  # no CNIC
        UserSnapshotRow(uid="3", user_id="1003", name="Bilal-42301-1102881-5"),  # typed wrongly
        UserSnapshotRow(uid="4", user_id="1004", name=f"Admin-{OTHER_CNIC}", privilege=14),
        UserSnapshotRow(uid="5", user_id="10", name=f"Saba-S-{'3' * 13}"),
        UserSnapshotRow(uid="6", user_id="123456789012", name="Ghost"),  # damaged: ID too long
        UserSnapshotRow(uid="7", user_id="1802", name="1802"),  # damaged: name is the ID
    ], snapshot_id=f"{code}-people")
    return connector


def _rows(client, connector, query=""):
    response = client.get(f"/api/v2/devices/{connector.connector_id}/users{query}")
    assert response.status_code == 200, response.text
    return response.json()


def test_totals_and_default_order_stay_for_old_clients(db):
    connector = _machine(db)
    client, _headers = admin_client(db)
    body = _rows(client, connector, "?limit=3")
    assert [row["uid"] for row in body["rows"]] == ["1", "2", "3"]
    assert body["next_cursor"] is not None and body["next_offset"] is None
    assert (body["total"], body["filtered_total"]) == (7, 7)
    filtered = _rows(client, connector, "?q=Ali")
    assert (filtered["total"], filtered["filtered_total"]) == (7, 1)


def test_sort_name_pages_by_offset_without_repeats(db):
    connector = _machine(db)
    client, _headers = admin_client(db)
    seen = []
    offset = 0
    while offset is not None:
        body = _rows(client, connector, f"?sort=name&limit=3&offset={offset}")
        seen += [row["display_name"] for row in body["rows"]]
        offset = body["next_offset"]
    assert seen == sorted(seen, key=str.casefold) and len(seen) == len(set(seen)) == 7
    ids = [row["user_id"] for row in _rows(client, connector, "?sort=employee_id")["rows"]]
    assert ids == ["10", "1001", "1002", "1003", "1004", "1802", "123456789012"]
    problems = [row["uid"] for row in _rows(client, connector, "?sort=problems")["rows"]]
    # People to fix (no CNIC, typed wrongly, damaged names without CNIC) come first.
    assert set(problems[:4]) == {"2", "3", "6", "7"}
    assert client.get(f"/api/v2/devices/{connector.connector_id}/users?sort=other").status_code == 422


def test_no_cnic_and_typed_wrongly_are_separate(db):
    connector = _machine(db)
    client, _headers = admin_client(db)
    no_cnic = {row["uid"] for row in _rows(client, connector, "?identity=NO_CNIC")["rows"]}
    typed = {row["uid"] for row in _rows(client, connector, "?identity=TYPED_WRONG")["rows"]}
    assert typed == {"3"}
    assert "3" not in no_cnic and {"2", "6", "7"} <= no_cnic


def test_shift_workers_admins_and_changes_filters(db):
    connector = _machine(db)
    client, _headers = admin_client(db)
    assert [row["uid"] for row in _rows(client, connector, "?shift_worker=true")["rows"]] == ["5"]
    assert [row["uid"] for row in _rows(client, connector, "?privilege=14")["rows"]] == ["4"]
    assert _rows(client, connector, "?changing=true")["rows"] == []
    integrity = _rows(client, connector)["identity_integrity"]
    assert (integrity["admins"], integrity["shift_workers"], integrity["added_recently"]) == (1, 1, 0)


def test_added_recently_needs_a_day_after_the_first_list_read(db):
    connector = _machine(db)
    client, _headers = admin_client(db)
    # The first list was read just now: nobody counts as added.
    assert _rows(client, connector, "?added_within_days=7")["rows"] == []
    first = db.scalar(select(DeviceUserSnapshot).where(DeviceUserSnapshot.zkt_device_id == connector.zkt_device.id))
    first.received_at = utc_now() - timedelta(days=2)
    db.commit()
    for snapshot in db.scalars(select(DeviceUserSnapshot)):
        snapshot.received_at = utc_now() - timedelta(days=2)
    late = db.scalar(select(DeviceUser).where(DeviceUser.uid == "2"))
    late.created_at = utc_now() - timedelta(hours=1)
    for user in db.scalars(select(DeviceUser).where(DeviceUser.uid != "2")):
        user.created_at = utc_now() - timedelta(days=2)
    db.commit()
    body = _rows(client, connector, "?added_within_days=7")
    assert [row["uid"] for row in body["rows"]] == ["2"]
    assert body["rows"][0]["added_recently"] is True
    assert body["identity_integrity"]["added_recently"] == 1
    newest = _rows(client, connector, "?sort=newest")["rows"]
    assert newest[0]["uid"] == "2" and newest[0]["created_at"]


def test_rows_say_which_entries_look_damaged(db):
    connector = _machine(db)
    client, _headers = admin_client(db)
    rows = {row["uid"]: row for row in _rows(client, connector)["rows"]}
    assert {uid for uid, row in rows.items() if row["looks_damaged"]} == {"6", "7"}
    for row in rows.values():
        assert {"created_at", "added_recently", "current_command_id", "looks_damaged"} <= set(row)


def test_hikvision_entries_are_never_called_damaged(db):
    connector = _machine(db)
    connector.firmware_family = "hikvision"
    db.commit()
    user = db.scalar(select(DeviceUser).where(DeviceUser.uid == "6"))
    assert people_read.looks_damaged(user, hikvision=True) is False
    assert people_read.looks_damaged(user, hikvision=False) is True
    tt = DeviceUser(user_id="55", display_name="tt")
    assert people_read.looks_damaged(tt, hikvision=False) is True


def test_running_change_has_its_public_command_id(db):
    from zk_add.service import create_command

    connector = _machine(db)
    user = db.scalar(select(DeviceUser).where(DeviceUser.uid == "2"))
    command = create_command(db, connector=connector, command_type="REFRESH_USERS", payload={}, expected_state={}, desired_state={},
                             idempotency_key="people-test-refresh", actor="test")
    user.current_command_id = command.id
    db.commit()
    client, _headers = admin_client(db)
    rows = _rows(client, connector, "?changing=true")["rows"]
    assert [(row["uid"], row["current_command_id"]) for row in rows] == [("2", command.command_id)]


def test_lease_names_the_person(db):
    connector = _machine(db)
    user = db.scalar(select(DeviceUser).where(DeviceUser.uid == "4"))
    db.add(TemporaryAdminLease(lease_id="lease-people-1", zkt_device_id=connector.zkt_device.id,
                               device_user_id=user.id, state="ACTIVE", expires_at=utc_now() + timedelta(minutes=9)))
    db.commit()
    client, _headers = admin_client(db)
    lease = client.get(f"/api/v1/devices/{connector.connector_id}").json()["active_lease"]
    assert (lease["user_id"], lease["user_display_name"]) == ("1004", user.display_name)


def test_people_summary_matches_each_list_and_hides_cnics(db):
    connector = _machine(db)
    user = db.scalar(select(DeviceUser).where(DeviceUser.uid == "4"))
    db.add(TemporaryAdminLease(lease_id="lease-people-2", zkt_device_id=connector.zkt_device.id,
                               device_user_id=user.id, state="ACTIVE", expires_at=utc_now() + timedelta(minutes=9)))
    db.commit()
    client, _headers = admin_client(db)
    body = client.get("/api/v2/people/summary").json()
    text = json.dumps(body)
    assert CNIC not in text and OTHER_CNIC not in text and "cnic_lookup_hash" not in text
    row = next(row for row in body["rows"] if row["connector_id"] == connector.connector_id)
    integrity = _rows(client, connector)["identity_integrity"]
    assert row["people"] == integrity["total_users"] == 7
    assert row["with_cnic"] == integrity["with_cnic"]
    assert row["needs_fixing"] == integrity["missing_cnic"] + integrity["unresolved_duplicate_users"]
    assert row["cnic_typed_wrongly"] == integrity["cnic_format_suspect_users"] == 1
    assert row["no_cnic"] == integrity["missing_cnic"] - 1
    assert row["admins"] == 1
    assert row["admin_access"]["user_id"] == "1004"


def test_people_summary_is_shared_for_a_minute(db, monkeypatch):
    _machine(db)
    calls = []

    def counted(_session, _integrity):
        calls.append(1)
        return {"rows": [], "computed_at": utc_now()}

    monkeypatch.setattr(web, "_dashboard_cache_applies", lambda _db: True)
    monkeypatch.setattr(web.people_read, "people_summary", counted)
    monkeypatch.setattr(web, "SessionLocal", lambda: _Closing(db))
    client, _headers = admin_client(db)
    assert client.get("/api/v2/people/summary").status_code == 200
    assert client.get("/api/v2/people/summary").status_code == 200
    assert len(calls) == 1


class _Closing:
    def __init__(self, session):
        self.session = session

    def __getattr__(self, name):
        return getattr(self.session, name)

    def close(self):
        pass


def test_people_search_across_devices(db):
    first = _machine(db)
    second = _machine(db, code="LF-ZONE-BLD9-03", hardware="e0:72:a1:d6:f5:02", serial="PEOPLE-0002")
    client, headers = admin_client(db)
    response = client.post("/api/v2/people/search", json={"q": "ali"}, headers=headers)
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert {row["connector_id"] for row in rows} == {first.connector_id, second.connector_id}
    assert {(row["user_id"], tuple(row["needs"])) for row in rows} == {("1002", ("no_cnic",))}
    life = next(row for row in rows if row["connector_id"] == second.connector_id)
    assert (life["division"], life["friendly_name"]) == ("life", "Building 9 · Device 3")
    assert set(life) == {"connector_id", "friendly_name", "division", "is_spare", "user_id", "display_name",
                         "needs"}

    by_id = client.post("/api/v2/people/search", json={"q": "1003"}, headers=headers).json()["rows"]
    assert {tuple(row["needs"]) for row in by_id} == {("cnic_typed_wrongly",)}


def test_people_search_by_cnic_never_returns_it(db):
    _machine(db)
    client, headers = admin_client(db)
    dashed = f"{CNIC[:5]}-{CNIC[5:12]}-{CNIC[12]}"
    response = client.post("/api/v2/people/search", json={"q": dashed}, headers=headers)
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert [row["user_id"] for row in rows] == ["1001"]
    assert CNIC not in response.text and CNIC[-4:] not in json.dumps(rows)


def test_people_search_is_bounded_and_needs_the_csrf_token(db):
    connector = connector_fixture(db)
    _people(db, connector, [UserSnapshotRow(uid=str(n), user_id=str(5000 + n), name=f"Usman {n}")
                            for n in range(1, 31)])
    client, headers = admin_client(db)
    assert client.post("/api/v2/people/search", json={"q": "usman"}).status_code == 403
    assert len(client.post("/api/v2/people/search", json={"q": "usman"}, headers=headers).json()["rows"]) == 20
    assert client.post("/api/v2/people/search", json={"q": "u"}, headers=headers).status_code == 422


def test_people_list_reads_commands_in_one_query(db):
    connector = _machine(db)
    client, _headers = admin_client(db)
    statements = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        _rows(client, connector)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert sum("FROM add_device_commands" in statement for statement in statements) <= 1
