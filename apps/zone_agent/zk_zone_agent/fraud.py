from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from zk_common.enums import ClockStatus, IncidentSeverity, IncidentType, SourceType, TrustStatus
from zk_common.time_utils import device_local_to_utc, ensure_utc


@dataclass(frozen=True)
class Classification:
    trust_status: TrustStatus
    fraud_score: int
    fraud_reason: str
    drift_seconds: float | None = None


@dataclass(frozen=True)
class ClockClassification:
    status: ClockStatus
    drift_seconds: float | None
    expected_device_time: datetime | None
    jump_seconds: float | None
    reason: str
    incident_type: IncidentType | None = None
    severity: IncidentSeverity | None = None


class FraudEngine:
    def __init__(
        self,
        *,
        drift_threshold_seconds: int = 120,
        jump_threshold_seconds: int = 15,
        critical_drift_seconds: int = 300,
    ) -> None:
        self.drift_threshold_seconds = drift_threshold_seconds
        self.jump_threshold_seconds = jump_threshold_seconds
        self.critical_drift_seconds = critical_drift_seconds

    def classify_live_attendance(
        self,
        *,
        device_event_time: datetime,
        zone_trusted_time: datetime,
        timezone_name: str,
        internet_online: bool,
        current_clock_status: ClockStatus | str | None,
        pc_clock_suspicious: bool = False,
    ) -> Classification:
        if pc_clock_suspicious:
            return Classification(
                TrustStatus.SUSPECT_PC_TIME,
                90,
                "Windows PC clock changed suspiciously near this punch.",
            )
        event_utc = device_local_to_utc(device_event_time, timezone_name)
        drift = (event_utc - ensure_utc(zone_trusted_time)).total_seconds()
        if abs(drift) > self.drift_threshold_seconds:
            return Classification(
                TrustStatus.SUSPECT_DEVICE_TIME,
                80,
                f"Device event time differs from trusted time by {drift:.0f} seconds.",
                drift,
            )
        if str(current_clock_status or ClockStatus.OK) == ClockStatus.SUSPICIOUS.value:
            return Classification(
                TrustStatus.SUSPECT_DEVICE_CLOCK_JUMP,
                80,
                "Nearby clock guard check detected suspicious device clock movement.",
                drift,
            )
        if internet_online:
            return Classification(TrustStatus.TRUSTED_LIVE, 0, "Live punch observed with trusted time.", drift)
        return Classification(
            TrustStatus.INTERNET_OFFLINE_TRUSTED_LOCAL,
            10,
            "Internet was offline but local device monitoring and monotonic trusted time continued.",
            drift,
        )

    def classify_backfill(
        self,
        *,
        source_type: SourceType,
        device_event_time: datetime,
        timezone_name: str,
        trusted_now: datetime,
        in_lan_outage: bool,
        reconnect_clock_ok: bool,
        service_was_down: bool = False,
    ) -> Classification:
        event_utc = device_local_to_utc(device_event_time, timezone_name)
        if event_utc > ensure_utc(trusted_now) + timedelta(minutes=5):
            return Classification(
                TrustStatus.BACKFILL_SUSPECT_TIME,
                100,
                "Dumped attendance timestamp is in the future.",
            )
        if service_was_down:
            return Classification(
                TrustStatus.BACKFILL_UNVERIFIED_AGENT_DOWN,
                100,
                "Attendance was found after the zone agent was stopped or crashed.",
            )
        if not in_lan_outage:
            return Classification(
                TrustStatus.DUMP_RECONCILE_NORMAL,
                30 if source_type != SourceType.LIVE else 0,
                "Dump reconciliation record outside a known LAN blind period.",
            )
        if reconnect_clock_ok:
            return Classification(
                TrustStatus.BACKFILL_ACCEPTED_CLOCK_OK,
                40,
                "Backfilled after LAN outage and reconnect clock check was consistent.",
            )
        return Classification(
            TrustStatus.BACKFILL_UNVERIFIED_BLIND_PERIOD,
            60,
            "Attendance occurred during a LAN blind period and cannot be fully verified.",
        )

    def classify_clock_check(
        self,
        *,
        device_time: datetime | None,
        trusted_time: datetime,
        timezone_name: str,
        previous_device_time: datetime | None,
        previous_trusted_time: datetime | None,
    ) -> ClockClassification:
        if device_time is None:
            return ClockClassification(ClockStatus.ERROR, None, None, None, "Device time read failed.")

        device_utc = device_local_to_utc(device_time, timezone_name)
        trusted_utc = ensure_utc(trusted_time)
        drift_seconds = (device_utc - trusted_utc).total_seconds()
        expected_device_time: datetime | None = None
        jump_seconds: float | None = None
        reasons: list[str] = []
        incident_type: IncidentType | None = None
        severity: IncidentSeverity | None = None

        if previous_device_time is not None and previous_trusted_time is not None:
            elapsed = ensure_utc(trusted_time) - ensure_utc(previous_trusted_time)
            expected_device_time = device_local_to_utc(previous_device_time, timezone_name) + elapsed
            jump_seconds = (device_utc - expected_device_time).total_seconds()
            if abs(jump_seconds) > self.jump_threshold_seconds:
                reasons.append(f"Device clock jumped by {jump_seconds:.0f} seconds.")
                incident_type = IncidentType.DEVICE_CLOCK_JUMP
                severity = IncidentSeverity.HIGH

        if abs(drift_seconds) > self.drift_threshold_seconds:
            reasons.append(f"Device clock drift is {drift_seconds:.0f} seconds.")
            incident_type = incident_type or IncidentType.DEVICE_CLOCK_DRIFT
            severity = (
                IncidentSeverity.CRITICAL
                if abs(drift_seconds) > self.critical_drift_seconds
                else IncidentSeverity.HIGH
            )

        if reasons:
            return ClockClassification(
                ClockStatus.SUSPICIOUS,
                drift_seconds,
                expected_device_time,
                jump_seconds,
                " ".join(reasons),
                incident_type,
                severity,
            )
        return ClockClassification(
            ClockStatus.OK,
            drift_seconds,
            expected_device_time,
            jump_seconds,
            "Device clock is within configured thresholds.",
        )


fraud_engine = FraudEngine()
