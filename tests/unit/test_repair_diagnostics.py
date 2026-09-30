from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json

import httpx
import pytest


@pytest.fixture()
def diagnostics():
    path = Path(__file__).resolve().parents[2] / "scripts" / "attendance_repair_diagnostics.py"
    spec = spec_from_file_location("repair_diagnostics_probe", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
