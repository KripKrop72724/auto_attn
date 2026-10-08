from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from reader_matrix_fixtures import pinned  # noqa: F401
from test_add_backend import db  # noqa: F401
from test_hil_observation import full_rows, observed, prepared, source_store, store  # noqa: F401
from zk_add.models import AuditEvent
from zk_add.ota import FirmwareEvent, FirmwareHilRun
from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password
from zk_add.settings import settings
from zk_add.web import app, get_db


def test_hil_observation_routes_require_admin_and_csrf(db):  # noqa: F811
    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    body = {
        "deployment_id": "not-installed",
        "idempotency_key": "observation-key",
        "target": {
            "connector_id": "one",
            "mac": "e0:72:a1:d6:3c:7c",
            "terminal_serial": "PGB1261200077",
        },
    }
    assert client.post("/api/v1/firmware/hil-runs", json=body).status_code == 401
    assert client.get("/api/v1/firmware/hil-runs/unknown").status_code == 401
    interruption = {"password": "synthetic-test-password", "idempotency_key": "outage-key"}
    assert client.post("/api/v1/firmware/hil-runs/unknown/interrupt-add", json=interruption).status_code == 401
    assert client.post("/api/v1/firmware/hil-runs/unknown/reboot-esp", json=interruption).status_code == 401
    completion = {"password": "synthetic-test-password"}
    assert client.post("/api/v1/firmware/hil-runs/unknown/complete-full", json=completion).status_code == 401
    token, admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest"
    )
    db.commit()
    client.cookies.set(ADMIN_COOKIE, token)
    assert client.post("/api/v1/firmware/hil-runs", json=body).status_code == 403
    assert client.post("/api/v1/firmware/hil-runs/unknown/cancel").status_code == 403
    assert client.post("/api/v1/firmware/hil-runs/unknown/interrupt-add", json=interruption).status_code == 403
    assert client.post("/api/v1/firmware/hil-runs/unknown/reboot-esp", json=interruption).status_code == 403
    assert client.post("/api/v1/firmware/hil-runs/unknown/complete-full", json=completion).status_code == 403
    headers = {"X-CSRF-Token": admin.csrf_token}
    assert client.post("/api/v1/firmware/hil-runs/unknown/interrupt-add",
        json={**interruption, "duration_seconds": 3600}, headers=headers).status_code == 422
    assert client.post("/api/v1/firmware/hil-runs/unknown/interrupt-add",
        json=interruption, headers=headers).status_code == 403
    assert client.post("/api/v1/firmware/hil-runs/unknown/reboot-esp",
        json={**interruption, "duration_seconds": 3600}, headers=headers).status_code == 422
    assert client.post("/api/v1/firmware/hil-runs/unknown/reboot-esp",
        json=interruption, headers=headers).status_code == 403
    assert client.post("/api/v1/firmware/hil-runs/unknown/complete-full",
        json={**completion, "outcome": "PASS"}, headers=headers).status_code == 422
    assert client.post("/api/v1/firmware/hil-runs/unknown/complete-full",
        json=completion, headers=headers).status_code == 403
    result = client.post("/api/v1/firmware/hil-runs", json=body, headers=headers)
    assert result.status_code == 409 and "not found" in result.json()["detail"]
    assert (
        client.post("/api/v1/firmware/hil-runs/unknown/cancel", headers=headers).status_code == 404
    )
    assert client.get("/api/v1/firmware/hil-runs/unknown").status_code == 404


@pytest.mark.parametrize("full_rows", [True], indirect=True)
@pytest.mark.parametrize("missing_witness", [False, True])
def test_complete_full_route_commits_only_server_collected_result(full_rows, monkeypatch, missing_witness):  # noqa: F811
    sessions, _ = full_rows
    password = "synthetic-completion-password"
    monkeypatch.setattr(settings, "admin_username", "StateHealthAdmin")
    monkeypatch.setattr(settings, "admin_password_hash", hash_admin_password(password))
    with sessions() as session:
        run = session.scalar(select(FirmwareHilRun))
        run_id = run.run_id
        if missing_witness:
            run.result = {key: value for key, value in run.result.items() if key != "esp_reboot"}
        token, admin = create_admin_session(session, username=settings.admin_username,
            ip_address="127.0.0.1", user_agent="pytest")
        csrf = admin.csrf_token
        session.commit()

    def get_session():
        with sessions() as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_db, get_session)
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, token)
    headers, body = {"X-CSRF-Token": csrf}, {"password": password}
    path = f"/api/v1/firmware/hil-runs/{run_id}/complete-full"
    for submitted in ({"outcome": "PASS"}, {"verdict": "PASS"}, {"evidence": {}}, {"result": {"outcome": "PASS"}}):
        assert client.post(path, json={**body, **submitted}, headers=headers).status_code == 422
    assert client.post(path, json={"password": "wrong"}, headers=headers).status_code == 403
    # The fixture contains a real boot transition. Removing its controlled
    # witness makes that an unplanned reset, which is a failure, not merely a
    # missing test. The route must preserve the collector's classification.
    expected = "HIL_FAILED" if missing_witness else "HIL_ACCEPTED"
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected
    with sessions() as session:
        run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == run_id))
        digest = run.result["evidence_sha256"]
        assert run.status == expected and run.result["actor"] == settings.admin_username
        if missing_witness:
            assert "UNPLANNED_RESET" in run.result["reasons"]
        assert run.result["evidence"]["physical_power_cut"] == "NOT_PERFORMED"
        audits = list(session.scalars(select(AuditEvent).where(AuditEvent.action == "FIRMWARE_FULL_OBSERVATION_COMPLETED")))
        assert len(audits) == 1 and audits[0].outcome == expected
    assert client.post(path, json=body, headers=headers).json()["status"] == expected
    with sessions() as session:
        run = session.scalar(select(FirmwareHilRun).where(FirmwareHilRun.run_id == run_id))
        assert run.result["evidence_sha256"] == digest
        assert len(list(session.scalars(select(FirmwareEvent).where(FirmwareEvent.state == expected)))) == 1
    unknown = client.post("/api/v1/firmware/hil-runs/unknown/complete-full", json=body, headers=headers)
    assert unknown.status_code == 409 and "not found" in unknown.json()["detail"]


def test_complete_full_openapi_has_password_only_input():
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/firmware/hil-runs/{run_id}/complete-full"]["post"]
    body = operation["requestBody"]
    assert body["required"]
    name = body["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
    contract = schema["components"]["schemas"][name]
    assert contract["additionalProperties"] is False
    assert set(contract["properties"]) == {"password"} and contract["required"] == ["password"]
    assert contract["properties"]["password"]["writeOnly"] is True
