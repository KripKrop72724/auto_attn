from fastapi.testclient import TestClient

from test_add_backend import db  # noqa: F401
from zk_add.security import ADMIN_COOKIE, create_admin_session
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
    token, admin = create_admin_session(
        db, username="StateHealthAdmin", ip_address="127.0.0.1", user_agent="pytest"
    )
    db.commit()
    client.cookies.set(ADMIN_COOKIE, token)
    assert client.post("/api/v1/firmware/hil-runs", json=body).status_code == 403
    assert client.post("/api/v1/firmware/hil-runs/unknown/cancel").status_code == 403
    headers = {"X-CSRF-Token": admin.csrf_token}
    result = client.post("/api/v1/firmware/hil-runs", json=body, headers=headers)
    assert result.status_code == 409 and "not found" in result.json()["detail"]
    assert (
        client.post("/api/v1/firmware/hil-runs/unknown/cancel", headers=headers).status_code == 404
    )
    assert client.get("/api/v1/firmware/hil-runs/unknown").status_code == 404
