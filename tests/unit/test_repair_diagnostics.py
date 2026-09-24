from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


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
