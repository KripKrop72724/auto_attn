import json
from pathlib import Path
import urllib.request

import pytest


def diagnostic():
    source = (Path(__file__).resolve().parents[2] /
              ".github/workflows/zone-lite-device-provisioning.yml").read_text()
    block = source.split("  inspect-ords-contract:", 1)[1].split("  attest-hmac:", 1)[0]
    script = block.split("$script = @'\n", 1)[1].split("          '@", 1)[0]
    return "\n".join(line[10:] for line in script.splitlines())


def run_probe(monkeypatch, body):
    monkeypatch.setenv("ADD_ORDS_BASE_URL", "https://example.invalid/ords")
    monkeypatch.setenv("ADD_ATTENDANCE_REPAIR_ORDS_USERNAME", "private-username")
    monkeypatch.setenv("ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD", "private-password")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, size):
            assert size == 65537
            return body[:size]

    class Client:
        def open(self, request, timeout):
            assert request.get_method() == "GET"
            assert timeout == 15
            assert request.full_url.endswith("/raw-captures/identity-repairs/capabilities")
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Client())
    exec(compile(diagnostic(), "ords-diagnostic", "exec"), {})


def test_capability_diagnostic_never_exports_unselected_fields(monkeypatch, capsys):
    run_probe(monkeypatch, json.dumps({"contract_version": "1", "add_only_auth": True,
                                     "execution_ready": False, "batch_limit": 100,
                                     "password": "must-not-leak", "body": "private"}).encode())
    result = json.loads(capsys.readouterr().out)
    assert result == {"http_status": 200, "contract_version_matches_v1": True,
                      "add_only_auth": True, "execution_ready": False, "batch_limit": 100}


@pytest.mark.parametrize("body", [b"[]", b"not-json-private", b"x" * 65537])
def test_invalid_contract_prints_only_error_type(monkeypatch, capsys, body):
    with pytest.raises(SystemExit, match="diagnostic failed: (ValueError|JSONDecodeError)"):
        run_probe(monkeypatch, body)
    assert capsys.readouterr().out == ""


def test_wrong_types_cannot_be_logged_as_credentials(monkeypatch, capsys):
    run_probe(monkeypatch, json.dumps(dict.fromkeys(
        ["contract_version", "add_only_auth", "execution_ready", "batch_limit"], "private-password"
    )).encode())
    assert "private-password" not in capsys.readouterr().out
