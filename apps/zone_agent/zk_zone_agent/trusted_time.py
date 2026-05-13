from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from zk_common.enums import IncidentSeverity, IncidentType
from zk_common.time_utils import ensure_utc, utc_now
from zk_zone_agent.db import FraudIncident, HeadOfficeTimeSync


@dataclass(frozen=True)
class TrustedNow:
    value: datetime
    source: str


@dataclass(frozen=True)
class PcClockTamper:
    jump_seconds: float
    expected_wall_utc: datetime
    actual_wall_utc: datetime


class TrustedTimeService:
    def __init__(
        self,
        *,
        wall_clock: Callable[[], datetime] = utc_now,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        pc_jump_threshold_seconds: int = 15,
    ) -> None:
        self.wall_clock = wall_clock
        self.monotonic_ns = monotonic_ns
        self.pc_jump_threshold_seconds = pc_jump_threshold_seconds
        self.last_head_office_time_utc: datetime | None = None
        self.last_monotonic_ns: int | None = None
        self.previous_wall_utc: datetime | None = None
        self.previous_monotonic_ns: int | None = None
        self.last_pc_tamper_at: datetime | None = None

    def now(self) -> TrustedNow:
        if self.last_head_office_time_utc is None or self.last_monotonic_ns is None:
            return TrustedNow(ensure_utc(self.wall_clock()), "LOCAL_TIME_UNVERIFIED")
        elapsed_ns = self.monotonic_ns() - self.last_monotonic_ns
        return TrustedNow(
            self.last_head_office_time_utc + timedelta(seconds=elapsed_ns / 1_000_000_000),
            "HEAD_OFFICE_MONOTONIC",
        )

    def update_from_head_office(self, server_utc: datetime, session: Session | None = None) -> None:
        server_utc = ensure_utc(server_utc)
        local_wall = ensure_utc(self.wall_clock())
        monotonic = self.monotonic_ns()
        self.last_head_office_time_utc = server_utc
        self.last_monotonic_ns = monotonic
        if session is not None:
            session.add(
                HeadOfficeTimeSync(
                    server_utc=server_utc,
                    local_wall_utc=local_wall,
                    monotonic_ns=monotonic,
                    offset_seconds=(server_utc - local_wall).total_seconds(),
                )
            )

    def check_pc_clock_tamper(self) -> PcClockTamper | None:
        wall = ensure_utc(self.wall_clock())
        monotonic = self.monotonic_ns()
        if self.previous_wall_utc is None or self.previous_monotonic_ns is None:
            self.previous_wall_utc = wall
            self.previous_monotonic_ns = monotonic
            return None
        elapsed = (monotonic - self.previous_monotonic_ns) / 1_000_000_000
        expected_wall = self.previous_wall_utc + timedelta(seconds=elapsed)
        jump = (wall - expected_wall).total_seconds()
        self.previous_wall_utc = wall
        self.previous_monotonic_ns = monotonic
        if abs(jump) <= self.pc_jump_threshold_seconds:
            return None
        self.last_pc_tamper_at = wall
        return PcClockTamper(jump, expected_wall, wall)

    def record_pc_tamper(self, session: Session, zone_id: str, tamper: PcClockTamper) -> FraudIncident:
        incident = FraudIncident(
            zone_id=zone_id,
            device_id=None,
            incident_type=IncidentType.ZONE_PC_CLOCK_TAMPER.value,
            severity=IncidentSeverity.CRITICAL.value,
            description=(
                "Windows wall clock jumped by "
                f"{tamper.jump_seconds:.0f} seconds. Expected {tamper.expected_wall_utc.isoformat()}, "
                f"actual {tamper.actual_wall_utc.isoformat()}."
            ),
        )
        session.add(incident)
        session.flush()
        return incident


trusted_time_service = TrustedTimeService()
