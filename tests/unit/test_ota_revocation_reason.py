"""A valid revocation reason must commit on PostgreSQL, including its prefix."""
import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from zk_add.models import AuditEvent, Base
from zk_add.ota import FirmwareCampaign, FirmwareRelease
from zk_add.security import AdminContext


@pytest.fixture(params=["sqlite", "postgres"])
def database(request):
    admin = None
    if request.param == "postgres":
        url = os.environ.get("ADD_SAFE_REPAIR_TEST_DATABASE_URL") or (
            os.environ.get("ADD_DATABASE_URL") if os.environ.get("CI") else None
        )
        if not url or not url.startswith("postgresql"):
            pytest.skip("Set ADD_SAFE_REPAIR_TEST_DATABASE_URL for PostgreSQL qualification")
        schema = "ota_revoke_test_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as db:
            db.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={
            "options": f"-csearch_path={schema} -clock_timeout=5000 -cstatement_timeout=30000",
        })
    else:
        engine = create_engine("sqlite+pysqlite:///:memory:")

    def cleanup():
        engine.dispose()
        if admin is not None:
            with admin.begin() as db:
                db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()

    request.addfinalizer(cleanup)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.mark.parametrize("length", [10, 183, 190, 200])
def test_revocation_keeps_full_audit_reason_and_bounded_campaign_text(database, monkeypatch, length):
    from zk_add import web

    db = database
    release = FirmwareRelease(release_id="synthetic-revoke", version="2.6.17", git_sha="a" * 40,
        image_sha256="b" * 64, image_size=1024, signing_key_id="test-only",
        partition_layout="zone-lite-ota-v1", storage_name="synthetic.bin", state="HIL_ONLY",
        manifest={"application_sha256": "c" * 64}, manifest_signature="synthetic-not-signed")
    db.add(release)
    db.flush()
    campaigns = [FirmwareCampaign(campaign_id=f"synthetic-{status}", release_id=release.id,
        zone_id=f"SYNTHETIC-{status}", status=status, actor="tester", idempotency_key=status,
        reason="Synthetic revocation test", typed_confirmation=release.version)
        for status in ("ACTIVE", "PAUSED", "COMPLETED")]
    db.add_all(campaigns)
    db.commit()
    observed = []
    monkeypatch.setattr(web, "require_step_up", lambda *_: observed.append("step-up"))

    async def publish(topic, event):
        # Publishing must occur only after the database accepted the revoke.
        assert db.scalar(select(FirmwareRelease.state)) == "REVOKED"
        observed.append((topic, event))

    monkeypatch.setattr(web.browser_events, "publish", publish)
    reason = "R" * length
    result = asyncio.run(web.revoke_firmware_release(release.release_id,
        web._FirmwareControlIn(reason=reason, password="synthetic-only"),
        (db, AdminContext(1, "tester", "synthetic-csrf", None))))
    assert result == {"release_id": release.release_id, "state": "REVOKED"}
    assert release.revoked_by == "tester" and release.revoked_at is not None
    for campaign in campaigns[:2]:
        assert campaign.status == "PAUSED"
        assert campaign.pause_reason == ("Release revoked: " + reason)[:200]
    assert campaigns[2].status == "COMPLETED"
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "FIRMWARE_RELEASE_REVOKED"))
    assert audit and audit.after == {"reason": reason}
    assert observed[0] == "step-up" and observed[1][0] == "firmware"
