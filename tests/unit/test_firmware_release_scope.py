"""The Firmware page shows current releases by default and hides superseded ones.

Hiding is a read-only view: a release's state never changes, because a revoked
predecessor would block the OTA path of a device that still runs it. These
tests pin which releases stay current, the per-release device counts, and that
callers that do not ask for a scope still receive every release.
"""
from __future__ import annotations

import hashlib
import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from zk_add import ota
from zk_add.db import Base
from zk_add.models import Connector
from zk_add.ota import FirmwareCampaign, FirmwareRelease, release_page
from zk_add.security import ADMIN_COOKIE, create_admin_session, hash_admin_password
from zk_add.settings import settings
from zk_add.web import app, get_db


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(settings, "firmware_ota_enabled", False)
    monkeypatch.setattr(settings, "firmware_hil_enabled", False)
    monkeypatch.setattr(settings, "fleet_root_secret", secrets.token_hex(32))
    monkeypatch.setattr(settings, "admin_username", "StateHealthAdmin")
    monkeypatch.setattr(settings, "admin_password_hash", hash_admin_password("correct-password"))
    monkeypatch.setattr(settings, "admin_cookie_secure", False)
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        app.dependency_overrides.clear()
        engine.dispose()


def release(db, version, state="AVAILABLE", family="zkt") -> FirmwareRelease:
    manifest = {"application_sha256": "c" * 64}
    if family == "hikvision":
        manifest.update(firmware_family="hikvision", project_name="zone_lite_hikvision")
    identity = f"{family}-{version}"
    row = FirmwareRelease(
        release_id=f"zone-lite-{identity}", version=version, git_sha="a" * 40,
        image_sha256=hashlib.sha256(identity.encode()).hexdigest(), image_size=1024,
        signing_key_id="production-key", partition_layout=ota.OTA_LAYOUT,
        storage_name=f"{identity}/firmware.bin", manifest=manifest, manifest_signature="sig", state=state)
    db.add(row)
    db.flush()
    return row


def device(db, firmware_version, family="zkt", *, active=True) -> Connector:
    name = secrets.token_hex(4)
    octets = hashlib.sha256(name.encode()).hexdigest()[:6]
    row = Connector(
        connector_id=f"connector-{name}",
        hardware_id="02:00:00:" + ":".join(octets[index:index + 2] for index in (0, 2, 4)),
        zone_id=f"ZONE-{name.upper()}", zone_name=name, device_id="1", display_name=name,
        firmware_version=firmware_version, firmware_family=family, active=active)
    db.add(row)
    db.flush()
    return row


def campaign(db, rel, status) -> FirmwareCampaign:
    row = FirmwareCampaign(
        campaign_id=secrets.token_hex(16), release_id=rel.id, zone_id=f"ZONE-{secrets.token_hex(4)}",
        status=status, actor="StateHealthAdmin", idempotency_key=secrets.token_hex(8), reason="Synthetic",
        typed_confirmation=rel.version, eligible_count=1)
    db.add(row)
    db.flush()
    return row


def versions(page) -> list[str]:
    return [row["version"] for row in page["rows"]]


def row_for(page, version) -> dict:
    return next(row for row in page["rows"] if row["version"] == version)


def test_default_scope_still_lists_every_release_newest_first(db):
    for version, state in [("2.5.2", "AVAILABLE"), ("2.6.0", "AVAILABLE"), ("2.6.1", "REVOKED"),
                           ("2.7.3", "AVAILABLE"), ("2.7.8", "HIL_ONLY")]:
        release(db, version, state)
    db.commit()

    page = release_page(db)
    assert release_page(db, scope="all") == page
    assert versions(page) == ["2.7.8", "2.7.3", "2.6.1", "2.6.0", "2.5.2"]
    assert page["filtered_total"] == 5
    assert page["next_cursor"] is None
    assert page["totals"] == {"all": 5, "available": 3, "hil_only": 1, "revoked": 1, "current": 2, "hidden": 3}
    assert all(row["devices_running"] == 0 for row in page["rows"])
    assert {row["version"] for row in page["rows"] if row["current"]} == {"2.7.8", "2.7.3"}
    with pytest.raises(ValueError):
        release_page(db, scope="newest")


def test_current_scope_hides_an_old_unused_release(db):
    release(db, "2.5.2")
    release(db, "2.6.0")
    release(db, "2.7.3")
    db.commit()

    page = release_page(db, scope="current")
    assert versions(page) == ["2.7.3"]
    assert page["filtered_total"] == 1
    assert (page["totals"]["current"], page["totals"]["hidden"]) == (1, 2)


def test_current_scope_keeps_a_release_a_device_runs(db):
    release(db, "2.5.2")
    release(db, "2.6.0")
    release(db, "2.6.15", "HIL_ONLY")
    release(db, "2.7.3")
    device(db, "zone-lite-2.5.2")
    device(db, "2.5.2")
    device(db, "zone-lite-2.6.15")
    # A retired connector does not keep its old release on the page.
    device(db, "zone-lite-2.6.0", active=False)
    db.commit()

    page = release_page(db, scope="current")
    assert versions(page) == ["2.7.3", "2.6.15", "2.5.2"]
    assert row_for(page, "2.5.2")["devices_running"] == 2
    assert row_for(page, "2.6.15")["devices_running"] == 1
    assert row_for(release_page(db), "2.6.0")["devices_running"] == 0


def test_current_scope_keeps_the_newest_production_release_of_each_family(db):
    release(db, "2.7.2")
    release(db, "2.7.3")
    release(db, "3.0.2", family="hikvision")
    release(db, "3.0.3", family="hikvision")
    db.commit()

    page = release_page(db, scope="current")
    assert versions(page) == ["3.0.3", "2.7.3"]
    # Newest by semantic version, not text or publication order: 2.10.0 is
    # newer than 2.9.0 although 2.9.0 was published after it.
    release(db, "2.10.0")
    release(db, "2.9.0")
    db.commit()
    assert versions(release_page(db, scope="current")) == ["2.10.0", "3.0.3"]


def test_current_scope_keeps_a_newer_hil_candidate_but_not_an_older_one(db):
    release(db, "2.7.1", "HIL_ONLY")
    release(db, "2.7.3")
    release(db, "2.7.5", "HIL_ONLY")
    release(db, "2.7.8", "HIL_ONLY")
    db.commit()

    assert versions(release_page(db, scope="current")) == ["2.7.8", "2.7.3"]
    # Once production catches up, the candidate is no longer newer than it.
    db.scalar(select(FirmwareRelease).where(FirmwareRelease.version == "2.7.8")).state = "AVAILABLE"
    db.commit()
    assert versions(release_page(db, scope="current")) == ["2.7.8"]


def test_current_scope_keeps_a_release_with_a_live_campaign(db):
    active = release(db, "2.6.9", "HIL_ONLY")
    paused = release(db, "2.6.10", "HIL_ONLY")
    finished = release(db, "2.6.11", "HIL_ONLY")
    release(db, "2.7.3")
    campaign(db, active, "ACTIVE")
    campaign(db, paused, "PAUSED")
    campaign(db, finished, "COMPLETED")
    db.commit()

    assert versions(release_page(db, scope="current")) == ["2.7.3", "2.6.10", "2.6.9"]


def test_current_scope_never_includes_a_revoked_release(db):
    release(db, "2.7.3")
    revoked = release(db, "2.7.9", "REVOKED")
    device(db, "zone-lite-2.7.9")
    campaign(db, revoked, "PAUSED")
    db.commit()

    page = release_page(db, scope="current")
    assert versions(page) == ["2.7.3"]
    everything = release_page(db)
    assert row_for(everything, "2.7.9")["devices_running"] == 1
    assert row_for(everything, "2.7.9")["current"] is False
    assert versions(release_page(db, state="REVOKED", scope="current")) == []


def test_devices_running_counts_only_the_same_family(db):
    release(db, "2.7.8")
    release(db, "3.0.3", family="hikvision")
    release(db, "3.1.1", family="hikvision")
    device(db, "zone-lite-2.7.8")
    # A Hikvision terminal that reports a ZKT version number is not on the ZKT release.
    device(db, "zone-lite-2.7.8", family="hikvision")
    device(db, "zone-lite-3.0.3", family="hikvision")
    device(db, "zone-lite-3.0.3")
    # A field build suffix is not the signed 3.1.1 release (the OTA matcher agrees).
    device(db, "zone-lite-3.1.1-bld1fix7", family="hikvision")
    db.commit()

    page = release_page(db)
    assert {row["version"]: row["devices_running"] for row in page["rows"]} == {
        "2.7.8": 1, "3.0.3": 1, "3.1.1": 0}
    assert not ota._versions_match("zone-lite-3.1.1-bld1fix7", "3.1.1")


def test_current_scope_pages_only_current_releases(db):
    for version in ("2.5.2", "2.6.0", "2.6.15", "2.7.2", "2.7.3"):
        release(db, version)
    device(db, "zone-lite-2.5.2")
    device(db, "zone-lite-2.6.15")
    db.commit()

    first = release_page(db, scope="current", limit=2)
    assert versions(first) == ["2.7.3", "2.6.15"]
    assert first["filtered_total"] == 3
    assert first["next_cursor"] is not None
    second = release_page(db, scope="current", cursor=first["next_cursor"], limit=2)
    assert versions(second) == ["2.5.2"]
    assert second["next_cursor"] is None
    assert versions(release_page(db, query="2.6", scope="current")) == ["2.6.15"]


def test_hiding_never_changes_a_release(db):
    for version, state in [("2.5.2", "AVAILABLE"), ("2.6.0", "AVAILABLE"), ("2.7.1", "HIL_ONLY"),
                           ("2.7.3", "AVAILABLE")]:
        release(db, version, state)
    db.commit()
    before = {row.version: (row.state, row.revoked_at, row.revoked_by)
              for row in db.scalars(select(FirmwareRelease))}

    release_page(db, scope="current")
    db.commit()
    db.expire_all()
    assert {row.version: (row.state, row.revoked_at, row.revoked_by)
            for row in db.scalars(select(FirmwareRelease))} == before


def test_releases_route_defaults_to_every_release_and_accepts_current(db):
    release(db, "2.6.0")
    release(db, "2.7.3")
    device(db, "zone-lite-2.7.3")
    raw, _admin = create_admin_session(db, username="StateHealthAdmin", ip_address="127.0.0.1",
                                       user_agent="pytest")
    db.commit()

    def override():
        yield db

    app.dependency_overrides[get_db] = override
    client = TestClient(app)
    client.cookies.set(ADMIN_COOKIE, raw)

    everything = client.get("/api/v1/firmware/releases")
    assert everything.status_code == 200
    assert versions(everything.json()) == ["2.7.3", "2.6.0"]
    current = client.get("/api/v1/firmware/releases", params={"scope": "current"})
    assert current.status_code == 200
    body = current.json()
    assert versions(body) == ["2.7.3"]
    assert body["rows"][0]["devices_running"] == 1 and body["rows"][0]["current"] is True
    assert (body["filtered_total"], body["totals"]["current"], body["totals"]["hidden"]) == (1, 1, 1)
    assert client.get("/api/v1/firmware/releases", params={"scope": "newest"}).status_code == 422
