from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_zone_agent.crypto import protect_secret, unprotect_secret
from zk_zone_agent.db import Device


class DeviceRegistry:
    def enabled_devices(self, session: Session) -> list[Device]:
        return list(session.scalars(select(Device).where(Device.enabled == True).order_by(Device.label.asc())))  # noqa: E712

    def list_devices(self, session: Session) -> list[Device]:
        return list(session.scalars(select(Device).order_by(Device.label.asc())))

    def save_device(
        self,
        session: Session,
        *,
        device_id: str,
        label: str,
        ip: str,
        port: int,
        comm_key: str | int,
        serial: str | None = None,
        platform: str | None = None,
        device_name: str | None = None,
        enabled: bool = True,
    ) -> Device:
        row = session.scalar(select(Device).where(Device.device_id == device_id))
        if row is None:
            row = Device(
                device_id=device_id,
                label=label,
                ip=ip,
                port=port,
                comm_key_encrypted=protect_secret(str(comm_key)),
                serial=serial,
                platform=platform,
                device_name=device_name,
                enabled=enabled,
            )
            session.add(row)
        else:
            row.label = label
            row.ip = ip
            row.port = port
            row.comm_key_encrypted = protect_secret(str(comm_key))
            row.serial = serial or row.serial
            row.platform = platform or row.platform
            row.device_name = device_name or row.device_name
            row.enabled = enabled
        session.flush()
        return row

    def comm_key(self, device: Device) -> int:
        raw = unprotect_secret(device.comm_key_encrypted)
        return int(raw or 0)


device_registry = DeviceRegistry()
