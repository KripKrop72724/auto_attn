from datetime import datetime, timezone

import pytest

from zk_zone_agent.device_validation import validate_device_connection
from zk_zone_agent.zk_client import ZKDeviceInfo


class _ValidationClient:
    def __init__(self, *, ip, port=4370, comm_key=0, timeout=5):
        self.comm_key = comm_key
        self.disconnected = False

    def connect(self):
        if self.comm_key != 1979:
            raise RuntimeError("bad key")

    def disconnect(self):
        self.disconnected = True

    def get_info(self):
        return ZKDeviceInfo("ADZV211860253", "ZLM60_TFT", "MB20/0")

    def get_time(self):
        return datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc)


def test_validate_device_connection_requires_comm_key():
    with pytest.raises(ValueError, match="Comm Key is required"):
        validate_device_connection(
            ip="192.168.110.137",
            port=4370,
            comm_key=" ",
            client_factory=_ValidationClient,
        )


def test_validate_device_connection_reads_info_and_clock():
    result = validate_device_connection(
        ip="192.168.110.137",
        port=4370,
        comm_key="1979",
        client_factory=_ValidationClient,
    )

    assert result.info.serial == "ADZV211860253"
    assert result.info.platform == "ZLM60_TFT"
    assert result.device_time == datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc)
