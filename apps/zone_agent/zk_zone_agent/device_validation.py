from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from zk_zone_agent.zk_client import PyZKClient, ZKClient, ZKDeviceInfo


ClientFactory = Callable[..., ZKClient]


@dataclass(frozen=True)
class DeviceValidation:
    info: ZKDeviceInfo
    device_time: datetime


def validate_device_connection(
    *,
    ip: str,
    port: int,
    comm_key: str,
    timeout: float = 5,
    client_factory: ClientFactory = PyZKClient,
) -> DeviceValidation:
    normalized_key = comm_key.strip()
    if normalized_key == "":
        raise ValueError("Comm Key is required. Enter the device Comm Key before saving.")
    try:
        comm_key_int = int(normalized_key)
    except ValueError as exc:
        raise ValueError("Comm Key must be a number.") from exc

    client = client_factory(ip=ip, port=port, comm_key=comm_key_int, timeout=timeout)
    try:
        client.connect()
        info = client.get_info()
        device_time = client.get_time()
        return DeviceValidation(info=info, device_time=device_time)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
