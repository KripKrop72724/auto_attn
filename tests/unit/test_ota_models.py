from zk_add.models import Base
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
