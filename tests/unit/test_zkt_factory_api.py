"""Factory proof routes exercise HMAC authentication and committed receipts."""
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from test_hil_scope import hil_session  # noqa: F401
from test_zkt_factory_trial import factory, installed, proof  # noqa: F401
from zk_add.models import Base, ConnectorCredential
from zk_add.ota import FirmwareEvent
from zk_add.protocol import body_sha256, sign_request
from zk_add.security import connector_token_hash
from zk_add.time_utils import utc_now
from zk_add import zkt_factory_trial as trial


@pytest.fixture
def api(factory, monkeypatch, tmp_path):  # noqa: F811
    from zk_add import web
    # Importing the complete web application registers optional family tables.
    Base.metadata.create_all(factory[0].bind)
    deployment, reservation = installed(factory)
    value = proof(factory, deployment, reservation)
    device = factory[2][0]
    token = "isolated-factory-test-token"
    factory[0].add(ConnectorCredential(connector_id=device.id, token_hash=connector_token_hash(token),
                                      token_last4="oken", active=True))
    factory[0].commit()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'factory-api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with engine.begin() as db:
        for table in Base.metadata.sorted_tables:
            rows = [dict(row) for row in factory[0].execute(select(table)).mappings()]
            if rows:
                db.execute(table.insert(), rows)
    sessions = sessionmaker(engine)
    def get_db():
        with sessions() as db:
            yield db
    monkeypatch.setitem(web.app.dependency_overrides, web.get_db, get_db)
    client = TestClient(web.app, raise_server_exceptions=False)
    path = f"/device/v2/firmware/deployments/{deployment.deployment_id}/factory-trial"
    def request(method, value=None, *, bad_signature=False, nonce=None):
        body = b"" if value is None else json.dumps(value, separators=(",", ":")).encode()
        timestamp, nonce = utc_now().isoformat(), nonce or uuid4().hex
        digest = body_sha256(body)
        signature = sign_request(token=token, method=method, path=path,
                                 timestamp=timestamp, nonce=nonce, body_hash=digest)
        return client.request(method, path, content=body, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "X-ADD-Connector-Id": device.connector_id, "X-ADD-Timestamp": timestamp,
            "X-ADD-Nonce": nonce, "X-ADD-Body-SHA256": digest,
            "X-ADD-Signature": "0" * 64 if bad_signature else signature})
    try:
        yield sessions, client, path, request, value
    finally:
        client.close()
        engine.dispose()


def test_actual_routes_require_signed_auth_and_commit_exact_receipt_before_success(api):
    sessions, client, path, request, value = api
    assert client.get(path).status_code == 401
    assert request("GET", bad_signature=True).status_code == 401
    context = request("GET")
    assert context.status_code == 200 and context.headers["cache-control"] == "no-store"
    assert context.json()["trial_id"] == value["trial_id"]
    assert context.json()["challenge"] == value["challenge"]
    assert context.json()["onboarding_generation"] == value["onboarding_generation"]
    result = request("POST", value)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json() == trial._receipt(value)
    # This separate connection cannot see uncommitted response evidence.
    with sessions() as db:
        event = db.scalar(select(FirmwareEvent).where(FirmwareEvent.state == "FACTORY_FALLBACK_REVOKED"))
        assert event is not None and trial._verified(event.details)["proof"] == value
    replay = request("POST", value)
    assert replay.status_code == 200 and replay.json() == result.json()
    with sessions() as db:
        assert len(list(db.scalars(select(FirmwareEvent).where(FirmwareEvent.state.in_(trial.PROOF_EVENTS))))) == 1


def test_wire_rejection_nonce_replay_and_wrong_challenge_never_create_a_receipt(api):
    sessions, _client, _path, request, value = api
    nonce = uuid4().hex
    assert request("GET", nonce=nonce).status_code == 200
    assert request("GET", nonce=nonce).status_code == 409
    assert request("POST", {**value, "unexpected": "not accepted"}).status_code == 422
    assert request("POST", {**value, "checkpoint_verified": 1}).status_code == 422
    assert request("POST", {**value, "challenge": "0" * 64}).status_code == 409
    with sessions() as db:
        assert not db.scalar(select(FirmwareEvent.id).where(FirmwareEvent.state.in_(trial.PROOF_EVENTS)))


def test_failed_database_commit_never_returns_an_accepted_factory_receipt(api, monkeypatch):
    from sqlalchemy.orm import Session
    sessions, _client, _path, request, value = api
    original = Session.commit
    def fail_trial_commit(self):
        if self.scalar(select(FirmwareEvent.id).where(FirmwareEvent.state == "FACTORY_FALLBACK_REVOKED")):
            raise RuntimeError("isolated commit failure")
        return original(self)
    monkeypatch.setattr(Session, "commit", fail_trial_commit)
    assert request("POST", value).status_code == 500
    with sessions() as db:
        assert not db.scalar(select(FirmwareEvent.id).where(FirmwareEvent.state.in_(trial.PROOF_EVENTS)))


@pytest.mark.parametrize("change", ["terminal", "generation", "campaign", "release"])
def test_proof_rechecks_previously_cached_bindings_against_committed_database_changes(api, change):
    from zk_add.models import Connector
    from zk_add.ota import FirmwareCampaign, FirmwareDeployment, FirmwareRelease
    sessions, _client, _path, _request, value = api
    with sessions() as stale:
        connector = stale.scalar(select(Connector).where(Connector.connector_id == trial.FACTORY_TARGETS[0]["connector_id"]))
        terminal = connector.zkt_device
        deployment = stale.scalar(select(FirmwareDeployment).where(FirmwareDeployment.deployment_id == value["deployment_id"]))
        campaign = stale.get(FirmwareCampaign, deployment.campaign_id)
        release = stale.get(FirmwareRelease, deployment.release_id)
        assert terminal.confirmed_serial and campaign.status == "ACTIVE" and release.state == "HIL_ONLY"
        with sessions.begin() as concurrent:
            if change == "terminal":
                concurrent.get(type(terminal), terminal.id).confirmed_serial = "replaced-terminal"
            elif change == "generation":
                concurrent.get(Connector, connector.id).onboarding_generation += 1
            elif change == "campaign":
                concurrent.get(FirmwareCampaign, campaign.id).status = "CANCELLED"
            else:
                concurrent.get(FirmwareRelease, release.id).state = "REVOKED"
        with pytest.raises(ValueError):
            trial.accept_proof(stale, connector, deployment.deployment_id, value)
        stale.rollback()
    with sessions() as db:
        assert not db.scalar(select(FirmwareEvent.id).where(FirmwareEvent.state.in_(trial.PROOF_EVENTS)))
