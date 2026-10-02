"""Normal Oracle diagnostics recheck custody and never activate a receipt."""

from datetime import timedelta
import base64
import hashlib
import json
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from test_add_backend import CNIC, SERIAL, connector_fixture, db as db, make_writable
from test_repair_diagnostics import diagnostics as diagnostics
from zk_add.crypto import encrypt_cnic, encrypt_text
from zk_add.models import AttendanceEvent, DeviceUser, TerminalRecordManifest, TerminalSourceEpoch
from zk_add.schemas import AttendanceEventIn, UserSnapshotRequest, UserSnapshotRow
from zk_add.service import ingest_attendance, replace_user_snapshot
from zk_add.time_utils import utc_now


@pytest.fixture()
def normal_row(db):
    db.autoflush = False
    connector = connector_fixture(db)
    make_writable(connector)
    connector.zkt_device.confirmed_serial = SERIAL
    connector.zkt_device.terminal_binding_state = "CONFIRMED"
    start = utc_now().replace(microsecond=0) - timedelta(minutes=5)
    for index in range(2):
        replace_user_snapshot(db, connector=connector, snapshot=UserSnapshotRequest(
            snapshot_id=f"normal-check-{index}", complete=True, stable=True,
            observed_at=start + timedelta(minutes=index), users=[UserSnapshotRow(
                uid="7", user_id="1007", name=f"Synthetic Employee-{CNIC}",
                terminal_identity_fingerprint="a" * 64,
            )],
        ))
    when = start + timedelta(seconds=30)
    ingest_attendance(db, connector=connector, events=[AttendanceEventIn(
        event_uid="b" * 64, uid="7", user_id="1007", terminal_serial=SERIAL,
        terminal_identity_fingerprint="a" * 64,
        device_event_time=when, captured_at=when + timedelta(minutes=3),
        source="CURRENT_RECONCILE", clock_quality="OK", status=1, punch=255,
        raw_event={"reconciliation_source": "VERIFIED_TERMINAL_SOURCE"},
    )])
    row = db.scalar(select(AttendanceEvent))
    assert row.identity_resolution_status == "RESOLVED_SYNCED_CNIC"
    # Model the repaired dump cohort after its normal identity release; source
    # custody is independently provided by the exact protected manifest below.
    row.source = "DUMP_RECONNECT"
    row.captured_cnic_lookup_hash = row.cnic_lookup_hash
    row.raw_event = {
        **row.raw_event, "attendance_record_uid": "29139",
        "terminal_provenance": "VERIFIED_SOURCE_REPLAY",
    }
    local = when.astimezone(ZoneInfo("Asia/Karachi"))
    packed_time = (((((local.year - 2000) * 12 + local.month - 1) * 31 + local.day - 1)
                    * 24 + local.hour) * 60 + local.minute) * 60 + local.second
    raw = bytearray(40)
    raw[0:2] = int("29139").to_bytes(2, "little")
    raw[2:6] = b"1007"
    raw[26] = 1
    raw[27:31] = packed_time.to_bytes(4, "little")
    raw[31] = 255
    epoch = TerminalSourceEpoch(
        zkt_device_id=connector.zkt_device.id, terminal_generation=1, sequence=1, state="ACTIVE",
    )
    db.add(epoch)
    db.flush()
    db.add(TerminalRecordManifest(
        connector_id=connector.id, zkt_device_id=connector.zkt_device.id, terminal_serial=SERIAL,
        generation=1, source_epoch_id=epoch.id, ordinal=0, canonical_source=True, record_size=40,
        raw_record_digest=hashlib.sha256(raw).hexdigest(), terminal_record_key="d" * 64,
        attendance_event_id=row.id, disposition="EVENT", raw_timestamp=packed_time,
        observed_uid="29139", observed_user_id="1007",
        protected_raw_record=encrypt_text(base64.b64encode(raw).decode()),
    ))
    row.ords_status = "ACKED"
    row.oracle_confirmed_at = utc_now()
    db.commit()
    return db, connector, row


def test_normal_check_constructs_current_proved_identity_without_writing(diagnostics, normal_row):
    db, connector, row = normal_row
    before = (row.ords_status, row.oracle_confirmed_at, row.device_serial, row.cnic_encrypted)
    guards, check = diagnostics.normal_content_check(db, row)
    assert check is not None
    assert guards["delivery_proof_valid"] is True
    assert guards["guard_evaluation_failed"] is False
    assert check["connector_id"] == connector.connector_id
    assert check["items"][0]["desired_identity"]["cnic"] == CNIC
    assert check["items"][0]["immutable_facts"]["device_serial"] == SERIAL
    assert check["items"][0]["immutable_facts"]["punch"] == "255"
    from zk_add.attendance_identity_evidence import source_evidence
    assert source_evidence(db, row, connector) is None
    assert guards["bound_source_proven"] is True
    assert (row.ords_status, row.oracle_confirmed_at, row.device_serial, row.cnic_encrypted) == before
    assert not db.new and not db.dirty and not db.deleted
    assert CNIC not in json.dumps(guards) and row.event_uid not in json.dumps(guards)


@pytest.mark.parametrize("blocker", [
    "stale_pending_snapshot", "saved_cnic_changed", "current_cnic_changed", "manual_override",
    "source_missing", "wrong_owner", "missing_serial", "identity_hold", "invalid_timestamp",
])
def test_disputed_normal_rows_never_construct_a_check(diagnostics, normal_row, blocker):
    db, connector, row = normal_row
    if blocker == "stale_pending_snapshot":
        row.identity_snapshot_id = None
        row.ords_status = "PENDING"
    elif blocker == "saved_cnic_changed":
        row.cnic_encrypted = encrypt_cnic("3520212345672")
    elif blocker == "current_cnic_changed":
        db.get(DeviceUser, row.device_user_id).cnic_encrypted = encrypt_cnic("3520212345672")
    elif blocker == "manual_override":
        row.manual_release_required = True
    elif blocker == "source_missing":
        row.raw_event = {}
    elif blocker == "wrong_owner":
        row.connector_id = connector.id + 10000
    elif blocker == "missing_serial":
        row.device_serial = None
    elif blocker == "identity_hold":
        row.ords_status = "BLOCKED_IDENTITY"
    elif blocker == "invalid_timestamp":
        row.device_event_time = row.device_event_time.replace(microsecond=123456)
    dirty_before = set(db.dirty)
    guards, check = diagnostics.normal_content_check(db, row)
    assert check is None
    assert set(db.dirty) == dirty_before and not db.new and not db.deleted
    assert all(isinstance(value, bool) for value in guards.values())
    assert CNIC not in json.dumps(guards) and row.event_uid not in json.dumps(guards)


def test_acknowledged_check_preserves_old_pin_after_unchanged_roster_refresh(diagnostics, normal_row):
    db, connector, row = normal_row
    old_pin = row.identity_snapshot_id
    db.get(DeviceUser, row.device_user_id).display_name = "Later corrected name"
    connector.zkt_device.identity_snapshot_id = old_pin + 10000
    guards, check = diagnostics.normal_content_check(db, row)
    assert check is not None
    assert guards["snapshot_pin_matches"] is False
    assert guards["delivery_proof_valid"] is False
    assert guards["acknowledged_identity_proven"] is True
    assert check["items"][0]["desired_identity"]["employee_name"] == row.display_name
    assert row.identity_snapshot_id == old_pin


@pytest.mark.parametrize("contradiction", [
    "wrong_user", "wrong_record_uid", "wrong_status", "wrong_punch", "wrong_time",
    "digest_changed", "epoch_inactive", "epoch_generation_changed", "owner_changed",
])
def test_canonical_raw_source_contradictions_skip_check(diagnostics, normal_row, contradiction):
    db, _connector, row = normal_row
    manifest = db.scalar(select(TerminalRecordManifest))
    epoch = db.get(TerminalSourceEpoch, manifest.source_epoch_id)
    if contradiction == "wrong_user":
        row.user_id = "different"
    elif contradiction == "wrong_record_uid":
        row.raw_event = {**row.raw_event, "attendance_record_uid": "different"}
    elif contradiction == "wrong_status":
        row.status = "15"
    elif contradiction == "wrong_punch":
        row.punch = "0"
    elif contradiction == "wrong_time":
        row.device_event_time += timedelta(seconds=1)
    elif contradiction == "digest_changed":
        manifest.raw_record_digest = "f" * 64
    elif contradiction == "epoch_inactive":
        epoch.state = "SUPERSEDED"
    elif contradiction == "epoch_generation_changed":
        epoch.terminal_generation += 1
    elif contradiction == "owner_changed":
        manifest.connector_id += 10000
    db.flush()
    guards, check = diagnostics.normal_content_check(db, row)
    assert check is None and guards["bound_source_proven"] is False


def test_exact_normal_scope_rolls_back_before_only_read_only_oracle_check(
    diagnostics, normal_row, monkeypatch,
):
    import sqlalchemy.orm
    import zk_add.db
    from zk_add.settings import settings

    db, _connector, row = normal_row
    calls = []

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): calls.append("connection_closed")
        def execute(self, statement): calls.append(str(statement))
        def rollback(self): calls.append("rollback")

    class Engine:
        def connect(self): return Connection()

    class ReadSession:
        def __init__(self, *, bind, autoflush): assert autoflush is False
        def __enter__(self): return db
        def __exit__(self, *args): calls.append("session_closed")

    monkeypatch.setattr(zk_add.db, "engine", Engine())
    monkeypatch.setattr(sqlalchemy.orm, "Session", ReadSession)
    monkeypatch.setattr(settings, "ords_base_url", "https://example.invalid/ords")
    monkeypatch.setattr(settings, "attendance_repair_ords_username", "PRIVATE-USERNAME")
    monkeypatch.setattr(settings, "attendance_repair_ords_password", "PRIVATE-PASSWORD")

    def respond(request):
        assert calls[-1] == "connection_closed" and "rollback" in calls
        assert request.method == "POST"
        assert request.url.path == "/ords/raw-captures/identity-repairs/check"
        check = json.loads(request.content)
        assert check["items"][0]["event_uid"] == row.event_uid
        calls.append("oracle_check")
        return httpx.Response(200, json={"success": True, "results": [{
            "event_uid": row.event_uid, "classification": "MATCH",
            "current_content_token": "c" * 64,
            "employee_name": "PRIVATE-RESPONSE-NAME", "cnic": CNIC,
        }]})

    client_type = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    result = diagnostics.oracle_normal_event_diagnostics([row.id, row.id, row.id + 10000])
    assert calls[0] == "SET TRANSACTION READ ONLY"
    assert calls.index("session_closed") < calls.index("rollback") < calls.index("oracle_check")
    assert calls.count("oracle_check") == 1
    assert [item["event_id"] for item in result["rows"]] == [row.id, row.id + 10000]
    assert result["rows"][0]["content_match"] is True
    assert result["rows"][1]["classification"] == "SKIPPED"
    assert row.ords_status == "ACKED" and not db.new and not db.dirty
    for private in (CNIC, SERIAL, row.event_uid, "c" * 64, row.display_name,
                    "PRIVATE-USERNAME", "PRIVATE-PASSWORD", "PRIVATE-RESPONSE-NAME"):
        assert private not in json.dumps(result)


@pytest.mark.parametrize("opt_in", [False, True])
def test_normal_cli_network_requires_explicit_opt_in(diagnostics, monkeypatch, capsys, opt_in):
    calls = []
    monkeypatch.setattr(diagnostics, "database_report", lambda run, ids: {})
    monkeypatch.setattr(diagnostics, "oracle_normal_event_diagnostics", lambda ids: calls.append(ids) or {})
    argv = ["diagnostics", "--attendance-event-id", "101,103"]
    if opt_in:
        argv.append("--normal-oracle-content-check")
    monkeypatch.setattr("sys.argv", argv)
    diagnostics.main()
    assert calls == ([[101, 103]] if opt_in else [])
    assert json.loads(capsys.readouterr().out) == ({"normal_oracle_check": {}} if opt_in else {})


@pytest.mark.parametrize("args", [
    [], ["--attendance-event-id", ",".join(str(value) for value in range(1, 32))],
    ["--attendance-event-id", "101", "--direct-run-id", "11111111-2222-4333-8444-555555555555"],
])
def test_normal_cli_rejects_unbounded_or_mixed_modes_before_database(diagnostics, monkeypatch, args):
    monkeypatch.setattr(diagnostics, "database_report", lambda *_: pytest.fail("unexpected database access"))
    monkeypatch.setattr("sys.argv", ["diagnostics", "--normal-oracle-content-check", *args])
    with pytest.raises(SystemExit) as failure:
        diagnostics.main()
    assert failure.value.code == 2


def test_normal_function_rejects_more_than_thirty_exact_rows_before_access(diagnostics, monkeypatch):
    import zk_add.db

    monkeypatch.setattr(zk_add.db, "engine", None)
    assert diagnostics.oracle_normal_event_diagnostics(list(range(1, 32))) == {"failed": True, "rows": []}


def test_normal_workflow_option_is_default_off_and_explicit(diagnostics):
    from pathlib import Path

    workflow = (Path(__file__).resolve().parents[2]
                / ".github/workflows/add-repair-diagnostics.yml").read_text()
    option = workflow.split("normal_oracle_content_check:\n", 1)[1].split("firmware_campaign_id:", 1)[0]
    assert "default: false" in option and "type: boolean" in option
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "$env:NORMAL_ORACLE_CONTENT_CHECK -eq 'true'" in workflow
    assert "$diagnosticArgs += '--normal-oracle-content-check'" in workflow
