from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session

from zk_common.time_utils import utc_now
from zk_zone_agent.crypto import protect_secret, unprotect_secret
from zk_zone_agent.db import ZoneConfig


@dataclass(frozen=True)
class ActiveZoneConfig:
    zone_id: str
    zone_name: str
    timezone: str
    head_office_url: str
    zone_token: str
    setup_completed: bool


class ConfigManager:
    def get(self, session: Session) -> ActiveZoneConfig | None:
        row = session.scalar(select(ZoneConfig).order_by(ZoneConfig.id.asc()))
        if row is None:
            return None
        return ActiveZoneConfig(
            zone_id=row.zone_id,
            zone_name=row.zone_name,
            timezone=row.timezone,
            head_office_url=row.head_office_url.rstrip("/"),
            zone_token=unprotect_secret(row.zone_token_encrypted),
            setup_completed=row.setup_completed,
        )

    def setup_completed(self, session: Session) -> bool:
        config = self.get(session)
        return bool(config and config.setup_completed)

    def save_setup(
        self,
        session: Session,
        *,
        zone_id: str,
        zone_name: str,
        timezone: str,
        head_office_url: str,
        zone_token: str,
    ) -> ZoneConfig:
        row = session.scalar(select(ZoneConfig).order_by(ZoneConfig.id.asc()))
        if row is None:
            row = ZoneConfig(
                id=1,
                zone_id=zone_id,
                zone_name=zone_name,
                timezone=timezone,
                head_office_url=head_office_url.rstrip("/"),
                zone_token_encrypted=protect_secret(zone_token),
                setup_completed=True,
            )
            session.add(row)
        else:
            row.zone_id = zone_id
            row.zone_name = zone_name
            row.timezone = timezone
            row.head_office_url = head_office_url.rstrip("/")
            row.zone_token_encrypted = protect_secret(zone_token)
            row.setup_completed = True
            row.updated_at = utc_now()
        session.flush()
        return row


config_manager = ConfigManager()
