import base64
import hashlib
import json

import pytest
from sqlalchemy import func, select

from test_reconciliation import reconciliation_db as reconciliation_db
from zk_add.crypto import encrypt_text
from zk_add.models import AuditEvent, TerminalRecordManifest
from zk_add.zkt_source_evidence import list_evidence, reveal_evidence


def sample(db, connector, ordinal=0, **updates):
    raw = bytes([7, 0, 0, 0, 42, 0, 0, 0, 1, 2, 0, 0, 0, 0, 0, 0])
    values = dict(connector_id=connector.id, zkt_device_id=connector.zkt_device.id,
        terminal_serial=connector.zkt_device.serial, generation=1, ordinal=ordinal,
        source_kind="TAIL", canonical_source=True, record_size=len(raw),
        raw_record_digest=hashlib.sha256(raw).hexdigest(), terminal_record_key="a" * 64,
        disposition="EVENT", protected_raw_record=encrypt_text(base64.b64encode(raw).decode()),
        observed_user_id="protected-identity", observed_uid="777")
    row = TerminalRecordManifest(**{**values, **updates})
    db.add(row)
    db.flush()
    return row, raw


def test_listing_is_paginated_and_has_no_raw_or_identity_fields(reconciliation_db):
    db, connector = reconciliation_db
    sample(db, connector)
    sample(db, connector, 1, disposition="MALFORMED")
    first = list_evidence(db, connector, limit=1)
    assert len(first["rows"]) == 1 and first["next_cursor"] == first["rows"][0]["id"]
    second = list_evidence(db, connector, limit=1, before=first["next_cursor"])
    assert len(second["rows"]) == 1 and second["next_cursor"] is None
    assert len(list_evidence(db, connector, disposition="EVENT")["rows"]) == 1
    text = json.dumps(first, default=str)
    assert "protected-identity" not in text and "raw_record_b64" not in text and "observed_uid" not in text
    assert first["qualification"] == "NOT_ASSERTED" and first["model_at_capture"] == "NOT_RECORDED"


def test_reveal_preserves_original_disposition_and_commits_an_idempotent_audit(reconciliation_db):
    db, connector = reconciliation_db
    row, raw = sample(db, connector, disposition="INVALID_TIME", error_code="ORIGINAL_FAILURE")
    original = row.protected_raw_record
    arguments = dict(actor="operator", reason="Protocol fixture investigation", idempotency_key="fixture-one")
    revealed = reveal_evidence(db, connector, row.id, **arguments)
    db.commit()
    assert base64.b64decode(revealed["raw_record_b64"]) == raw
    assert revealed["original_error_code"] == "ORIGINAL_FAILURE"
    assert revealed["original_disposition"] == "INVALID_TIME"
    assert revealed["qualification"] == "NOT_ASSERTED"
    assert reveal_evidence(db, connector, row.id, **arguments) == revealed
    assert db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == "ZKT_SOURCE_EVIDENCE_REVEALED")) == 1
    assert row.protected_raw_record == original and row.disposition == "INVALID_TIME"
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "ZKT_SOURCE_EVIDENCE_REVEALED"))
    assert "raw_record_b64" not in audit.after and "protected-identity" not in json.dumps(audit.after)


@pytest.mark.parametrize("change", ["digest", "size", "encrypted", "missing"])
def test_bad_evidence_never_becomes_a_verified_fixture(reconciliation_db, change):
    db, connector = reconciliation_db
    row, _ = sample(db, connector)
    if change == "digest":
        row.raw_record_digest = "0" * 64
    if change == "size":
        row.record_size = 40
    if change == "encrypted":
        row.protected_raw_record = "invalid encrypted data"
    if change == "missing":
        row.protected_raw_record = None
    db.flush()
    with pytest.raises(ValueError, match="SOURCE_EVIDENCE_"):
        reveal_evidence(db, connector, row.id, actor="operator", reason="Investigating evidence",
                        idempotency_key="fixture-invalid")
    assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action == "ZKT_SOURCE_EVIDENCE_REVEALED"))


def test_cross_device_and_hikvision_evidence_are_not_returned(reconciliation_db):
    db, connector = reconciliation_db
    row, _ = sample(db, connector)
    # The record must satisfy both ownership keys, not just a guessed manifest ID.
    row.zkt_device_id += 100
    db.flush()
    assert not list_evidence(db, connector)["rows"]
    assert reveal_evidence(db, connector, row.id, actor="operator", reason="Investigating evidence",
                           idempotency_key="fixture-scope") is None
    connector.firmware_family = "hikvision"
    with pytest.raises(ValueError, match="ZKT_SOURCE_REQUIRED"):
        list_evidence(db, connector)


def test_protected_evidence_api_requires_session_csrf_and_password(reconciliation_db, monkeypatch):
    from fastapi.testclient import TestClient
    from zk_add import web
    from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password
    from zk_add.settings import settings

    db, connector = reconciliation_db
    row, raw = sample(db, connector)
    db.commit()
    monkeypatch.setattr(settings, "admin_username", "fixture-admin")
    monkeypatch.setattr(settings, "admin_password_hash", hash_admin_password("fixture-test-password"))
    def override_db():
        yield db
    previous = web.app.dependency_overrides.copy()
    web.app.dependency_overrides[web.get_db] = override_db
    try:
        client = TestClient(web.app)
        root = f"/api/v1/devices/{connector.connector_id}/source-evidence"
        path = f"{root}/{row.id}/reveal"
        body = dict(password="fixture-test-password", reason="Protocol fixture investigation",
                    idempotency_key="api-fixture-one")
        assert client.get(root).status_code == 401
        assert client.post(path, json=body).status_code == 401
        token, admin = create_admin_session(db, username="fixture-admin", ip_address="127.0.0.1", user_agent="pytest")
        db.commit()
        client.cookies.set(ADMIN_COOKIE, token)
        assert client.get(root).status_code == 200
        assert client.get(root, params={"limit": 51}).status_code == 422
        assert client.post(path, json=body).status_code == 403
        client.headers["X-CSRF-Token"] = admin.csrf_token
        assert client.post(path, json={**body, "password": "wrong"}).status_code == 403
        assert not db.scalar(select(AuditEvent.id).where(AuditEvent.action == "ZKT_SOURCE_EVIDENCE_REVEALED"))
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        assert base64.b64decode(response.json()["raw_record_b64"]) == raw
        assert "no-store" in response.headers["Cache-Control"]
        assert "fixture-test-password" not in response.text and "cnic" not in response.text
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == "ZKT_SOURCE_EVIDENCE_REVEALED"))
        assert client.post(f"{root}/999999/reveal", json=body).status_code == 404
        # Evidence integrity failure is a safe category, not ciphertext/key/error details.
        row.protected_raw_record = "private-corruption-marker"
        db.commit()
        invalid = client.post(path, json={**body, "idempotency_key": "api-fixture-bad"})
        assert invalid.status_code == 409 and "private-corruption-marker" not in invalid.text
    finally:
        web.app.dependency_overrides.clear()
        web.app.dependency_overrides.update(previous)


def test_audit_failure_cannot_return_protected_evidence(reconciliation_db, monkeypatch):
    from zk_add import zkt_source_evidence
    db, connector = reconciliation_db
    row, _ = sample(db, connector)
    def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(zkt_source_evidence, "append_audit", unavailable)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        reveal_evidence(db, connector, row.id, actor="operator", reason="Protocol investigation",
                        idempotency_key="unavailable-audit")
