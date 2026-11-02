"""/device/v2/onboard answers 409, not 500, when a MAC belongs to the other family."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from zk_add import web
from zk_add.db import Base, create_database_engine
from zk_add.models import Connector, OnboardingNonce, Site
from zk_add.onboarding import derive_bootstrap_secret
from zk_add.protocol import body_sha256, sign_request
from zk_add.settings import settings
from zk_add.time_utils import utc_now

MAC = "ac:27:6e:a4:3d:30"


@pytest.fixture
def onboarding(tmp_path, monkeypatch):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'onboard.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(web, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "fleet_root_secret", "test-only-fleet-root-with-enough-entropy")
    yield TestClient(web.app, raise_server_exceptions=False), sessions
    engine.dispose()


def onboard(client: TestClient, *, family: str, zone_id: str, nonce: str):
    body = json.dumps({
        "firmware_family": family, "hardware_id": MAC, "zone_id": zone_id, "zone_name": zone_id,
        "device_id": "1", "firmware_version": "3.1.1" if family == "hikvision" else "2.7.8",
    }).encode()
    timestamp = utc_now().isoformat().replace("+00:00", "Z")
    digest = body_sha256(body)
    signature = sign_request(
        token=derive_bootstrap_secret(MAC), method="POST", path="/device/v2/onboard",
        timestamp=timestamp, nonce=nonce, body_hash=digest,
    )
    return client.post("/device/v2/onboard", content=body, headers={
        "Content-Type": "application/json", "X-Zone-MAC": MAC.upper(),
        "X-ADD-Timestamp": timestamp, "X-ADD-Nonce": nonce,
        "X-ADD-Body-SHA256": digest, "X-ADD-Signature": signature,
    })


def test_the_other_family_is_a_conflict_and_changes_nothing(onboarding):
    client, sessions = onboarding
    first = onboard(client, family="zkt", zone_id="ZONE-TEST-01", nonce="nonce-zkt-0001")
    assert first.status_code == 200, first.text
    refused = onboard(client, family="hikvision", zone_id="ZONE-NEW-02", nonce="nonce-hik-0001")
    assert refused.status_code == 409
    assert refused.json()["detail"].startswith("FIRMWARE_FAMILY_MISMATCH:")
    # Rolled back as before: no nonce, no new site, the connector untouched.
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(OnboardingNonce)) == 1
        assert db.scalar(select(func.count()).select_from(Site).where(Site.site_id == "ZONE-NEW-02")) == 0
        connector = db.scalar(select(Connector).where(Connector.hardware_id == MAC))
        assert (connector.firmware_family, connector.zone_id, connector.onboarding_generation) == (
            "zkt", "ZONE-TEST-01", 1)
    # The same family still re-onboards.
    again = onboard(client, family="zkt", zone_id="ZONE-TEST-01", nonce="nonce-zkt-0002")
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False


def test_other_onboarding_errors_are_unchanged(onboarding, monkeypatch):
    client, _sessions = onboarding

    def broken(*_args, **_kwargs):
        raise ValueError("UNKNOWN_FIRMWARE_FAMILY")

    monkeypatch.setattr(web, "onboard_connector", broken)
    assert onboard(client, family="zkt", zone_id="ZONE-TEST-01", nonce="nonce-zkt-0003").status_code == 500
