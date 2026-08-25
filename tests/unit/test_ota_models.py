import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zk_add.models import Base, Connector
from zk_add.ota import (
    FirmwareCampaign,
    FirmwareDeployment,
    FirmwareRelease,
    _versions_match,
    campaign_detail,
    campaign_page,
    record_progress,
    release_page,
    version_at_least,
)
from zk_add.settings import settings


def test_ota_tables_are_registered_in_metadata() -> None:
    expected = {
        "add_firmware_releases",
        "add_firmware_campaigns",
        "add_firmware_deployments",
        "add_firmware_events",
        "add_firmware_download_grants",
    }
    assert expected.issubset(Base.metadata.tables)


def test_ota_is_disabled_by_default() -> None:
    assert settings.firmware_ota_enabled is False


def test_ota_version_matching_accepts_connector_and_app_formats() -> None:
    assert _versions_match("2.2.7", "2.2.7")
    assert _versions_match("zone-lite-2.2.7", "2.2.7")
    assert _versions_match("2.2.7", "zone-lite-2.2.7")
    assert not _versions_match("2.2.6", "2.2.7")
    assert not _versions_match(None, "2.2.7")
    assert not _versions_match("2.2.7-rc1", "2.2.7")
    assert version_at_least("2.10.0", "2.2.0")
    assert not version_at_least("2.1.99", "2.2.0")


def test_ota_progress_requires_legal_monotonic_signed_boot_evidence() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        release = FirmwareRelease(
            release_id="release-2-5-0",
            version="2.5.0",
            git_sha="a" * 40,
            image_sha256="b" * 64,
            image_size=1024,
            signing_key_id="production-key",
            partition_layout="zone-lite-ota-v1",
            minimum_bootstrap_version="2.2.0",
            storage_name="2.5.0/firmware.bin",
            manifest={"application_sha256": "c" * 64},
            manifest_signature="test-signature",
            state="AVAILABLE",
        )
        connector = Connector(
            connector_id="connector-progress",
            hardware_id="00:11:22:33:44:66",
            zone_id="ZONE-PROGRESS",
            zone_name="Progress",
            device_id="1",
            display_name="Progress terminal",
            firmware_version="2.4.13",
        )
        session.add_all([release, connector])
        session.flush()
        campaign = FirmwareCampaign(
            campaign_id="campaign-progress",
            release_id=release.id,
            zone_id=connector.zone_id,
            status="ACTIVE",
            actor="StateHealthAdmin",
            idempotency_key="campaign-progress-key",
            reason="Verify signed progress transitions",
            typed_confirmation="2.5.0",
            eligible_count=1,
            legacy_skipped_count=0,
        )
        session.add(campaign)
        session.flush()
        deployment = FirmwareDeployment(
            deployment_id="deployment-progress",
            campaign_id=campaign.id,
            release_id=release.id,
            connector_id=connector.id,
            status="OFFERED",
            previous_version="2.4.13",
            target_version="2.5.0",
        )
        session.add(deployment)
        session.flush()

        with pytest.raises(ValueError, match="Illegal firmware transition"):
            record_progress(
                session,
                connector=connector,
                deployment_public_id=deployment.deployment_id,
                state="VERIFYING",
                bytes_written=1024,
            )
        record_progress(
            session,
            connector=connector,
            deployment_public_id=deployment.deployment_id,
            state="DOWNLOADING",
            bytes_written=512,
        )
        with pytest.raises(ValueError, match="artifact bounds"):
            record_progress(
                session,
                connector=connector,
                deployment_public_id=deployment.deployment_id,
                state="DOWNLOADING",
                bytes_written=511,
            )
        record_progress(
            session,
            connector=connector,
            deployment_public_id=deployment.deployment_id,
            state="VERIFYING",
            bytes_written=1024,
        )
        record_progress(
            session,
            connector=connector,
            deployment_public_id=deployment.deployment_id,
            state="READY_TO_BOOT",
            bytes_written=1024,
        )
        with pytest.raises(ValueError, match="digest"):
            record_progress(
                session,
                connector=connector,
                deployment_public_id=deployment.deployment_id,
                state="BOOTED_PENDING",
                bytes_written=1024,
                running_version="2.5.0",
                running_partition="ota_0",
                image_sha256="d" * 64,
            )
        record_progress(
            session,
            connector=connector,
            deployment_public_id=deployment.deployment_id,
            state="BOOTED_PENDING",
            bytes_written=1024,
            running_version="zone-lite-2.5.0",
            running_partition="ota_1",
            image_sha256="c" * 64,
        )
        assert deployment.status == "BOOTED_PENDING"


def test_firmware_catalog_pages_are_stable_filterable_and_summary_first(monkeypatch) -> None:
    monkeypatch.setattr(settings, "firmware_ota_enabled", False)
    monkeypatch.setattr(settings, "firmware_hil_enabled", False)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        releases = [
            FirmwareRelease(
                release_id=f"release-{version}",
                version=version,
                git_sha=git_sha,
                image_sha256=image_sha,
                image_size=1024,
                signing_key_id="production-key",
                partition_layout="zone-lite-ota-v1",
                minimum_bootstrap_version="2.2.0",
                storage_name=f"{version}/firmware.bin",
                manifest={"application_sha256": application_sha},
                manifest_signature="test-signature",
                state=state,
            )
            for version, git_sha, image_sha, application_sha, state in [
                ("2.2.30", "a" * 40, "b" * 64, "c" * 64, "AVAILABLE"),
                ("2.2.31", "d" * 40, "e" * 64, "f" * 64, "REVOKED"),
            ]
        ]
        session.add_all(releases)
        connector = Connector(
            connector_id="connector-one",
            hardware_id="00:11:22:33:44:55",
            zone_id="ZONE-ONE",
            zone_name="Zone One",
            device_id="1",
            display_name="Terminal One",
            firmware_version="2.2.29",
        )
        session.add(connector)
        session.flush()
        campaigns = [
            FirmwareCampaign(
                campaign_id=f"campaign-{index}",
                release_id=releases[index - 1].id,
                zone_id="ZONE-ONE" if index == 1 else "ZONE-TWO",
                status=status,
                actor="StateHealthAdmin",
                idempotency_key=f"campaign-key-{index}",
                reason="Audited rollout test",
                typed_confirmation=releases[index - 1].version,
                eligible_count=1,
                legacy_skipped_count=0,
            )
            for index, status in [(1, "ACTIVE"), (2, "PAUSED")]
        ]
        session.add_all(campaigns)
        session.flush()
        session.add(
            FirmwareDeployment(
                deployment_id="deployment-one",
                campaign_id=campaigns[0].id,
                release_id=releases[0].id,
                connector_id=connector.id,
                status="OFFERED",
                previous_version="2.2.29",
                target_version="2.2.30",
            )
        )
        session.commit()

        first = release_page(session, query="2.2", limit=1)
        assert first["filtered_total"] == 2
        assert first["totals"] == {
            "all": 2,
            "available": 1,
            "hil_only": 0,
            "revoked": 1,
        }
        assert first["next_cursor"] is not None
        second = release_page(session, query="2.2", cursor=first["next_cursor"], limit=1)
        assert {first["rows"][0]["release_id"], second["rows"][0]["release_id"]} == {
            "release-2.2.30",
            "release-2.2.31",
        }

        page = campaign_page(session, status="ACTIVE", limit=10)
        assert page["filtered_total"] == 1
        assert page["totals"]["campaigns"] == {"all": 2, "active": 1, "paused": 1}
        assert page["totals"]["deployments"] == {"all": 1, "offered": 1}
        assert page["rows"][0]["counts"] == {"OFFERED": 1}
        assert page["rows"][0]["deployments"] == []
        detail = campaign_detail(session, "campaign-1")
        assert detail is not None
        assert detail["deployments"][0]["display_name"] == "Terminal One"
