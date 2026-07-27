from zk_add.models import Base
from zk_add.ota import _versions_match
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
