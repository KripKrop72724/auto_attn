from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from zk_common.time_utils import utc_now
from zk_zone_agent.crypto import protect_secret, unprotect_secret
from zk_zone_agent.db import AttendanceEvent, ClockCheck, FraudIncident, OutagePeriod, ZoneConfig
from zk_zone_agent.head_office_policy import normalize_head_office_url
from zk_zone_agent.settings import settings


UNREGISTERED_ZONE_ID = "LOCAL-UNREGISTERED"
UNREGISTERED_ZONE_NAME = "Unregistered Zone"


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

    def runtime_config(self, session: Session) -> ActiveZoneConfig:
        config = self.get(session)
        if config is not None:
            return config
        return ActiveZoneConfig(
            zone_id=UNREGISTERED_ZONE_ID,
            zone_name=UNREGISTERED_ZONE_NAME,
            timezone=settings.default_timezone,
            head_office_url="",
            zone_token="",
            setup_completed=False,
        )

    def save_pending_registration(
        self,
        session: Session,
        *,
        zone_id: str,
        zone_name: str,
        timezone: str,
        head_office_url: str,
    ) -> ZoneConfig:
        row = session.scalar(select(ZoneConfig).order_by(ZoneConfig.id.asc()))
        old_zone_id = UNREGISTERED_ZONE_ID if row is None else row.zone_id
        if row is None:
            row = ZoneConfig(
                id=1,
                zone_id=zone_id,
                zone_name=zone_name,
                timezone=timezone,
            head_office_url=normalize_head_office_url(head_office_url),
                zone_token_encrypted=protect_secret(""),
                setup_completed=False,
            )
            session.add(row)
        else:
            row.zone_id = zone_id
            row.zone_name = zone_name
            row.timezone = timezone
            row.head_office_url = normalize_head_office_url(head_office_url)
            row.setup_completed = False
            row.updated_at = utc_now()
        self.reassign_zone_records(session, old_zone_id=old_zone_id, new_zone_id=zone_id)
        session.flush()
        return row

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
        old_zone_id = UNREGISTERED_ZONE_ID if row is None else row.zone_id
        old_setup_completed = bool(row and row.setup_completed)
        if old_setup_completed:
            raise ValueError("Zone setup is already completed. Reset setup before entering a new token.")
        normalized_url = normalize_head_office_url(head_office_url)
        if row is None:
            row = ZoneConfig(
                id=1,
                zone_id=zone_id,
                zone_name=zone_name,
                timezone=timezone,
                head_office_url=normalized_url,
                zone_token_encrypted=protect_secret(zone_token),
                setup_completed=True,
            )
            session.add(row)
        else:
            row.zone_id = zone_id
            row.zone_name = zone_name
            row.timezone = timezone
            row.head_office_url = normalized_url
            row.zone_token_encrypted = protect_secret(zone_token)
            row.setup_completed = True
            row.updated_at = utc_now()
        if not old_setup_completed:
            self.reassign_zone_records(session, old_zone_id=old_zone_id, new_zone_id=zone_id)
        session.flush()
        return row

    def clear_setup(self, session: Session) -> None:
        row = session.scalar(select(ZoneConfig).order_by(ZoneConfig.id.asc()))
        if row is None:
            return
        row.zone_token_encrypted = protect_secret("")
        row.setup_completed = False
        row.updated_at = utc_now()

    def reassign_zone_records(self, session: Session, *, old_zone_id: str, new_zone_id: str) -> None:
        if old_zone_id == new_zone_id:
            return
        for model in (AttendanceEvent, ClockCheck, OutagePeriod, FraudIncident):
            session.execute(
                update(model)
                .where(model.zone_id == old_zone_id)
                .values(zone_id=new_zone_id)
            )


config_manager = ConfigManager()
