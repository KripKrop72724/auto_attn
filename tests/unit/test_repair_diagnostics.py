from importlib.util import module_from_spec, spec_from_file_location
from copy import deepcopy
from pathlib import Path
import json
import re
import subprocess
import sys

import httpx
import pytest
from sqlalchemy import select

from test_attendance_force_release import store as store
from test_attendance_repair import repair_store as repair_store, CORRECT_CNIC, WRONG_CNIC


@pytest.fixture()
def diagnostics():
    path = Path(__file__).resolve().parents[2] / "scripts" / "attendance_repair_diagnostics.py"
    spec = spec_from_file_location("repair_diagnostics_probe", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def queued_direct_approval(store):
    from zk_add import attendance_direct_ords as direct
    from zk_add.attendance_direct_ords_schemas import DirectOrdsStartRequest
    from zk_add.models import AttendanceEvent

    sessions, _connector_id, _event_uid = store
    with sessions() as db:
        event = db.scalar(select(AttendanceEvent))
        event.display_name = event.cnic_encrypted = event.cnic_lookup_hash = None
        event_id = event.id
        direct.create(db, actor="test", request=DirectOrdsStartRequest(
            event_ids=[event_id], reason="Test saved approval", password="test-password",
            idempotency_key="diagnostic-approval",
        ))
        db.commit()
    with sessions() as db:
        direct.advance_once(db)
        db.commit()
    return sessions, event_id


@pytest.mark.parametrize("change, failing_flag", [
    ("none", None),
    ("missing_user", "known_current_user"),
    ("changed_cnic", "current_cnic_hash_matches"),
    ("changed_user_key", "current_user_key_matches"),
    ("changed_name", "frozen_payload_matches_current_payload"),
    ("changed_saved_source", "source_digest_matches"),
    ("changed_payload_digest", "payload_digest_matches"),
])
def test_saved_approval_diagnostics_distinguish_guards_without_exporting_identity(
    diagnostics, queued_direct_approval, change, failing_flag,
):
    from zk_add.crypto import cnic_lookup, encrypt_cnic
    from zk_add.models import AttendanceEvent, Connector, DeviceUser, AttendanceForceReleaseDecision

    sessions, event_id = queued_direct_approval
    with sessions() as db:
        event = db.get(AttendanceEvent, event_id)
        user = db.scalar(select(DeviceUser))
        decision = db.scalar(select(AttendanceForceReleaseDecision))
        if change == "missing_user":
            user.present = False
        elif change == "changed_cnic":
            user.cnic_encrypted = encrypt_cnic(WRONG_CNIC)
            user.cnic_lookup_hash = cnic_lookup(WRONG_CNIC)
        elif change == "changed_user_key":
            user.user_key = "22222222-2222-4222-8222-222222222222"
        elif change == "changed_name":
            user.display_name = "PRIVATE-CHANGED-NAME"
        elif change == "changed_saved_source":
            event.display_name = "PRIVATE-SOURCE-NAME"
        elif change == "changed_payload_digest":
            decision.payload_digest = "0" * 64
        db.flush()
        shape = diagnostics.direct_approval_guard_shape(
            db, event, db.get(Connector, event.connector_id), decision,
        )
        assert shape["approval_authorized"] is (change == "none")
        assert shape["guard_evaluation_failed"] is False
        if failing_flag:
            assert shape[failing_flag] is False
        if change in {"changed_name", "changed_user_key", "changed_cnic"}:
            assert shape["source_digest_matches"] is True
        if change == "changed_saved_source":
            assert shape["immutable_facts_match"] is True
            assert shape["frozen_payload_matches_current_payload"] is True
        assert all(isinstance(value, bool) for value in shape.values())
        rendered = json.dumps(shape)
        for private in (CORRECT_CNIC, WRONG_CNIC, user.display_name, user.user_key,
                        event.event_uid, decision.payload_digest):
            assert private not in rendered


def test_diagnostic_cli_keeps_both_run_and_event_filters(diagnostics, monkeypatch, capsys):
    run_id = "11111111-2222-4333-8444-555555555555"
    event_ids = [101, 103, 107]
    calls = []
    monkeypatch.setattr(diagnostics, "database_report", lambda run, ids: calls.append(
        ("database", run, ids),
    ) or {})
    monkeypatch.setattr(diagnostics, "oracle_direct_run_diagnostics", lambda run, ids: calls.append(
        ("oracle", run, ids),
    ) or {"checks": []})
    monkeypatch.setattr("sys.argv", [
        "diagnostics", "--direct-run-id", run_id,
        "--attendance-event-id", ",".join(map(str, event_ids)),
    ])
    diagnostics.main()
    assert calls == [("database", run_id, event_ids), ("oracle", run_id, event_ids)]
    assert json.loads(capsys.readouterr().out) == {"direct_oracle_check": {"checks": []}}


@pytest.mark.parametrize("event_ids", [None, [101, 103, 107]])
def test_saved_oracle_checks_bind_exact_event_scope_inside_run(diagnostics, monkeypatch, event_ids):
    import zk_add.db

    calls = []

    class Result:
        def mappings(self): return self
        def all(self): return []

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def rollback(self): calls.append(("rollback",))
        def execute(self, statement, params=None):
            calls.append((str(statement), params))
            return Result()

    class Engine:
        def connect(self): return Connection()

    monkeypatch.setattr(zk_add.db, "engine", Engine())
    monkeypatch.setattr(diagnostics, "probe_saved_direct_checks", lambda checks: {"checks": checks})
    result = diagnostics.oracle_direct_run_diagnostics("test-run", event_ids)
    query, params = next(call for call in calls if len(call) == 2 and "FROM add_attendance_force" in call[0])
    assert params == {"run_id": "test-run", "filter_events": bool(event_ids), "event_ids": event_ids or []}
    assert query.index("e.id=ANY(:event_ids)") < query.index("LIMIT 3")
    assert "j.job_id=:run_id" in query and "j.action='MANUAL_DIRECT_ORDS'" in query
    assert result == {"checks": []}
    assert calls[0][0] == "SET TRANSACTION READ ONLY"


@pytest.mark.parametrize("runtime", ["deployed", "legacy"])
@pytest.mark.parametrize("missing_serial", [None, ""])
def test_saved_check_uses_runtime_missing_serial_adapter_without_changing_approval(
    diagnostics, repair_store, monkeypatch, runtime, missing_serial,
):
    import zk_add.db
    from zk_add import attendance_force_delivery as delivery
    from zk_add.attendance_repair import _protected_digest
    from zk_add.crypto import decrypt_json, encrypt_json
    from zk_add.settings import settings

    facts = {
        "event_uid": "a" * 64, "device_serial": missing_serial,
        "source_uid": "7", "source_user_id": "1007",
        "device_event_time": "2026-09-30T04:31:07+00:00",
        "punch": "0", "status": "1", "raw_punch": "F", "source": "DUMP_STARTUP",
    }
    proof = {
        "policy": "manual-direct-ords-v1", "terminal": missing_serial,
        "immutable_facts": facts,
    }
    payload = {
        "event_uid": facts["event_uid"], "device_serial": "unknown",
        "employee_name": "Synthetic employee", "cnic": CORRECT_CNIC,
    }
    row = {
        "payload_encrypted": encrypt_json(payload), "proof": proof,
        "connector_id": "11111111-2222-4333-8444-555555555555",
        "device_serial": missing_serial,
    }
    original = deepcopy(row)
    calls, requests = [], []

    class Result:
        def __init__(self, rows): self.rows = rows
        def mappings(self): return self
        def all(self): return self.rows

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def rollback(self): calls.append("rollback")
        def execute(self, statement, params=None):
            sql = str(statement)
            calls.append(sql)
            return Result([row] if "FROM add_attendance_force" in sql else [])

    class Engine:
        def connect(self): return Connection()

    monkeypatch.setattr(zk_add.db, "engine", Engine())
    if runtime == "legacy":
        monkeypatch.delattr(delivery, "content_check_facts")
    monkeypatch.setattr(settings, "ords_base_url", "https://example.invalid/ords")
    monkeypatch.setattr(settings, "attendance_repair_ords_username", "PRIVATE-USERNAME")
    monkeypatch.setattr(settings, "attendance_repair_ords_password", "PRIVATE-PASSWORD")

    def respond(request):
        assert calls[-1] == "rollback"
        assert request.method == "POST"
        assert request.url.path == "/ords/raw-captures/identity-repairs/check"
        check = json.loads(request.content)
        requests.append(check)
        # Oracle requires a nonempty immutable serial even for a read-only check.
        if not check["items"][0]["immutable_facts"]["device_serial"]:
            return httpx.Response(400, json={"success": False, "error_code": "INVALID_CHECK_ITEM"})
        return httpx.Response(200, json={"success": True, "results": [{
            "event_uid": payload["event_uid"], "classification": "MATCH",
            "current_content_token": "c" * 64,
        }]})

    client_type = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    result = diagnostics.oracle_direct_run_diagnostics(
        "22222222-2222-4222-8222-222222222222", [101],
    )

    assert len(requests) == 1
    expected_serial = "unknown" if runtime == "deployed" else missing_serial
    check = requests[0]
    checked_facts = {**facts, "device_serial": expected_serial}
    assert check["terminal_serial"] == expected_serial
    assert check["items"][0]["immutable_facts"] == checked_facts
    assert check["items"][0]["immutable_facts_digest"] == _protected_digest(checked_facts)
    assert row == original
    assert decrypt_json(row["payload_encrypted"]) == payload
    assert calls[0] == "SET TRANSACTION READ ONLY"
    summary = result["checks"][0]
    assert summary["shape"]["device_serial_present"] is (runtime == "deployed")
    assert summary["http_status"] == (200 if runtime == "deployed" else 400)
    assert summary["error_code"] == (None if runtime == "deployed" else "INVALID_CHECK_ITEM")
    assert summary["classification"] == ("MATCH" if runtime == "deployed" else None)
    assert summary["token_format_valid"] is (runtime == "deployed")
    for private in (CORRECT_CNIC, payload["employee_name"], payload["event_uid"],
                    row["payload_encrypted"], "PRIVATE-USERNAME", "PRIVATE-PASSWORD", "c" * 64):
        assert private not in json.dumps(result)


def saved_check():
    return {"contract_version": "1", "items": [{
        "event_uid": "a" * 64,
        "immutable_facts": {
            "device_serial": "PRIVATE-SERIAL", "source_user_id": "PRIVATE-USER",
            "device_event_time": "2026-09-30T04:31:07+00:00", "raw_punch": "F",
        },
        "desired_identity": {"employee_name": "PRIVATE-NAME", "cnic": "3520212345671"},
    }]}


@pytest.mark.parametrize("server_code, expected", [
    ("INVALID_CHECK_ITEM", "INVALID_CHECK_ITEM"),
    ("CHECK_VALIDATION_FAILED", "CHECK_VALIDATION_FAILED"),
    ("PRIVATE-NAME-3520212345671", "UNRECOGNIZED_ERROR_CODE"),
    ({"password": "PRIVATE-PASSWORD"}, "UNRECOGNIZED_ERROR_CODE"),
])
def test_oracle_check_diagnostic_exports_only_allowlisted_error_and_booleans(
    diagnostics, server_code, expected,
):
    response = httpx.Response(400, json={
        "success": False, "error_code": server_code,
        "payload": saved_check(), "password": "PRIVATE-PASSWORD",
    })
    result = diagnostics.check_response_summary(response, saved_check())
    assert result["http_status"] == 400 and result["error_code"] == expected
    assert all(isinstance(value, bool) for value in result["shape"].values())
    rendered = json.dumps(result)
    for private in ("PRIVATE-NAME", "3520212345671", "PRIVATE-PASSWORD", "PRIVATE-USER", "PRIVATE-SERIAL", "a" * 64):
        assert private not in rendered


def test_check_diagnostic_detects_fractional_timestamp_without_exporting_it(diagnostics):
    check = saved_check()
    assert diagnostics.check_payload_shape(check)["timestamp_oracle_format_valid"]
    check["items"][0]["immutable_facts"]["device_event_time"] = "2026-09-30T04:31:07.123456+00:00"
    assert not diagnostics.check_payload_shape(check)["timestamp_oracle_format_valid"]


@pytest.mark.parametrize("classification, expected", [
    ("MATCH", "MATCH"), ("MISSING", "MISSING"), ("MISMATCH", "MISMATCH"),
    ("IMMUTABLE_MISMATCH", "IMMUTABLE_MISMATCH"),
    ("CROSS_DEVICE_UID_COLLISION", "CROSS_DEVICE_UID_COLLISION"),
    ("LEGACY_SOURCE_MATCH", "LEGACY_SOURCE_MATCH"),
    ("LEGACY_SOURCE_MISSING", "LEGACY_SOURCE_MISSING"),
    ("LEGACY_SOURCE_CONFLICT", "LEGACY_SOURCE_CONFLICT"),
    ("LEGACY_SOURCE_AMBIGUOUS", "LEGACY_SOURCE_AMBIGUOUS"),
    ("LEGACY_SOURCE_PRIVATE-NAME-3520212345671", "UNRECOGNIZED_CLASSIFICATION"),
    ({"cnic": "3520212345671"}, "UNRECOGNIZED_CLASSIFICATION"),
    (None, None),
])
def test_check_diagnostics_allowlist_classification_and_export_token_boolean_only(
    diagnostics, classification, expected,
):
    check = saved_check()
    token = "f" * 64
    response = httpx.Response(200, json={"success": True, "results": [{
        "event_uid": check["items"][0]["event_uid"],
        "classification": classification, "current_content_token": token,
        "employee_name": "PRIVATE-NAME", "cnic": "3520212345671",
    }]})
    result = diagnostics.check_response_summary(response, check)
    assert result["classification"] == expected
    assert result["token_format_valid"] is True
    assert result["response_shape_valid"] is True
    for private in (token, "PRIVATE-NAME", "3520212345671", check["items"][0]["event_uid"]):
        assert private not in json.dumps(result)


def test_check_diagnostics_reject_malformed_token_without_exporting_it(diagnostics):
    check = saved_check()
    response = httpx.Response(200, json={"success": True, "results": [{
        "event_uid": check["items"][0]["event_uid"],
        "classification": "MATCH", "current_content_token": "PRIVATE-TOKEN",
    }]})
    result = diagnostics.check_response_summary(response, check)
    assert result["token_format_valid"] is False
    assert "PRIVATE-TOKEN" not in json.dumps(result)


def test_direct_diagnostic_calls_only_bounded_read_only_verifier(diagnostics, monkeypatch):
    from zk_add.settings import settings

    monkeypatch.setattr(settings, "ords_base_url", "https://example.invalid/ords")
    monkeypatch.setattr(settings, "attendance_repair_ords_username", "PRIVATE-USERNAME")
    monkeypatch.setattr(settings, "attendance_repair_ords_password", "PRIVATE-PASSWORD")
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/ords/raw-captures/identity-repairs/check"
        assert json.loads(request.content) == saved_check()
        return httpx.Response(400, json={"error_code": "CHECK_VALIDATION_FAILED"})

    client_type = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    result = diagnostics.probe_saved_direct_checks([saved_check()] * 10)
    assert len(requests) == 3 and len(result["checks"]) == 3
    assert "PRIVATE" not in json.dumps(result)


def test_direct_diagnostic_does_not_follow_or_export_redirect(diagnostics, monkeypatch):
    from zk_add.settings import settings

    monkeypatch.setattr(settings, "ords_base_url", "https://example.invalid/ords")
    monkeypatch.setattr(settings, "attendance_repair_ords_username", "PRIVATE-USERNAME")
    monkeypatch.setattr(settings, "attendance_repair_ords_password", "PRIVATE-PASSWORD")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://private.invalid/PRIVATE-PASSWORD"})

    client_type = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    result = diagnostics.probe_saved_direct_checks([saved_check()])
    assert len(requests) == 1
    assert result["checks"][0]["http_status"] == 302
    assert "PRIVATE" not in json.dumps(result)


def test_workflow_streams_script_with_short_argv_and_keeps_raw_logs_in_memory(tmp_path):
    workflow = (Path(__file__).resolve().parents[2]
                / ".github/workflows/add-repair-diagnostics.yml").read_text(encoding="utf-8")
    summary = workflow.split("- name: Summarize HTTP failures without exposing raw logs\n", 1)[1]
    lines = [line.strip() for line in summary.splitlines()]
    programs = re.findall(r"python -c '([^']*)' \$scriptPath", summary)
    installer = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
    remover = "import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)"
    assert programs == [installer, remover]
    assert "base64" not in summary.lower() and "$runner" not in summary and "$encoded" not in summary
    assert "Get-Content -Raw -Encoding UTF8 'scripts/attendance_repair_diagnostics.py'" in summary
    assert '"/tmp/add-repair-diagnostics-$env:GITHUB_RUN_ID-$env:GITHUB_RUN_ATTEMPT.py"' in summary
    assert "$OutputEncoding = New-Object System.Text.UTF8Encoding($false)" in summary
    install_line = f"$script | & docker exec -i $api python -c '{installer}' $scriptPath"
    remove_line = f"& docker exec $api python -c '{remover}' $scriptPath"
    capture_line = '$raw = @(& cmd /c "docker logs --since 15m --tail 5000 $container 2>&1")'
    summarize_line = "$raw | & docker exec -i $api python $scriptPath --logs"
    assert [line for line in lines if "$raw" in line] == [capture_line, summarize_line]
    assert lines.index("try {") < lines.index(install_line)
    assert lines.index("} finally {") < lines.index(remove_line)
    assert lines.index("$OutputEncoding = $previousEncoding") < lines.index(
        "if ($LASTEXITCODE -ne 0 -and -not $summaryFailed) { throw 'Could not remove the temporary diagnostic script.' }"
    )
    for command, error in (
        (install_line, "Could not install the temporary diagnostic script."),
        (capture_line, "Could not read bounded container logs."),
        (summarize_line, "Log summarization failed."),
    ):
        assert lines[lines.index(command) + 1] == f"if ($LASTEXITCODE -ne 0) {{ throw '{error}' }}"
    assert "$summaryFailed = $true\n            throw" in summary
    assert lines[lines.index(remove_line) + 1].startswith(
        "if ($LASTEXITCODE -ne 0 -and -not $summaryFailed)"
    )
    assert [line for line in lines if "& docker exec" in line] == [
        install_line, summarize_line, remove_line,
    ]

    # Exercise the exact fixed installer with UTF-8 source exceeding Windows' argv limit.
    source = ("# Synthetic diagnostic source with UTF-8: caf\u00e9\n" * 2000).encode("utf-8")
    assert len(source) > 32768
    installed = tmp_path / "add-repair-diagnostics-101-1.py"
    copied = subprocess.run(
        [sys.executable, "-c", installer, str(installed)], input=source, capture_output=True,
        check=True,
    )
    assert installed.read_bytes() == source
    assert copied.stdout == copied.stderr == b""
    for _ in range(2):
        removed = subprocess.run(
            [sys.executable, "-c", remover, str(installed)], capture_output=True, check=True,
        )
        assert removed.stdout == removed.stderr == b"" and not installed.exists()


def test_diagnostics_discard_payloads_credentials_and_dynamic_paths():
    path = Path(__file__).resolve().parents[2] / "scripts" / "attendance_repair_diagnostics.py"
    spec = spec_from_file_location("repair_diagnostics", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.summarize_logs(
        [
            'INFO: 1.2.3.4 "POST /api/v2/attendance-recovery/checks?password=secret HTTP/1.1" 502 Bad Gateway',
            'INFO: 1.2.3.4 "GET /api/v1/auth/session HTTP/1.1" 200 OK',
            'INFO: 1.2.3.4 "GET /api/v1/users/private-person HTTP/1.1" 500 Error',
            'INFO: 1.2.3.4 "POST /device/v2/firmware/capability HTTP/1.1" 401 Unauthorized',
            'INFO: 1.2.3.4 "GET /device/v2/firmware/assignment HTTP/1.1" 204 No Content',
            "sqlalchemy.exc.OperationalError: private sql parameters and protected identity",
            '  File "/app/zk_add/attendance_safe_repair.py", line 181, in create_check',
            "password=secret employee=private-person cnic=3520212345671",
            "ADD event loop lag detected lag_seconds=42.000 private employee information",
            "WARNING OTA_AUTH_REJECTED connector_fp=012345abcdef reason=TIMESTAMP_INVALID",
        ]
    )
    assert output == {
        "http_counts": {
            "POST attendance-recovery 502": 1,
            "GET auth-session 200": 1,
            "GET other 500": 1,
            "POST firmware-capability 401": 1,
            "GET firmware-assignment 204": 1,
        },
        "exception_types": {"OperationalError": 1},
        "code_frames": {"attendance_safe_repair.py:181:create_check": 1},
        "proxy_errors": {},
        "runtime_signals": {"ADD event loop lag detected": 1},
        "firmware_auth_rejections": {"012345abcdef TIMESTAMP_INVALID": 1},
    }
