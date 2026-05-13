from __future__ import annotations

import threading
import ipaddress
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from zk_common.time_utils import utc_now
from zk_zone_agent.db import Device, DeviceDiscoveryResult, DiscoveryScanRun, SessionLocal
from zk_zone_agent.network_scanner import NetworkScanner, ScanCandidate, network_scanner
from zk_zone_agent.settings import settings


@dataclass(frozen=True)
class DiscoveryState:
    running: bool
    current_scan_id: int | None
    last_started_at: datetime | None
    last_finished_at: datetime | None


class DiscoveryService:
    def __init__(
        self,
        *,
        stop_event: threading.Event | None = None,
        session_factory: sessionmaker[Session] = SessionLocal,
        scanner: NetworkScanner = network_scanner,
    ) -> None:
        self.stop_event = stop_event or threading.Event()
        self.session_factory = session_factory
        self.scanner = scanner
        self._thread: threading.Thread | None = None
        self._scan_thread: threading.Thread | None = None
        self._scan_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = DiscoveryState(False, None, None, None)
        self._last_manual_scan_started_at: datetime | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="device-discovery-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def trigger_scan(self, *, source: str = "MANUAL", subnets: list[str] | None = None) -> DiscoveryState:
        if source == "MANUAL" and self._manual_scan_rate_limited():
            return self.status()
        if self._scan_thread and self._scan_thread.is_alive():
            return self.status()
        self._scan_thread = threading.Thread(
            target=self.run_scan,
            kwargs={"source": source, "subnets": subnets},
            name=f"device-discovery-{source.lower()}",
            daemon=True,
        )
        self._scan_thread.start()
        return self.status()

    def run_scan(self, *, source: str = "AUTO", subnets: list[str] | None = None) -> DiscoveryScanRun:
        with self._scan_lock:
            started_at = utc_now()
            with self.session_factory() as session:
                scan_run = DiscoveryScanRun(source=source, status="RUNNING", started_at=started_at)
                session.add(scan_run)
                session.commit()
                scan_id = scan_run.id
            self._set_state(running=True, current_scan_id=scan_id, last_started_at=started_at)
            try:
                candidates = self.scanner.scan(
                    subnets=subnets,
                    port=settings.scan_port,
                    timeout=settings.scan_timeout_seconds,
                    max_workers=settings.scan_concurrency,
                    max_hosts_per_subnet=settings.scan_max_hosts_per_subnet,
                )
                with self.session_factory() as session:
                    scan_run = session.get(DiscoveryScanRun, scan_id)
                    if scan_run is None:
                        raise RuntimeError(f"Discovery scan {scan_id} disappeared.")
                    scan_run.target_count = self._estimated_target_count(subnets)
                    scan_run.found_count = len(candidates)
                    scan_run.status = "COMPLETED"
                    scan_run.ended_at = utc_now()
                    self._upsert_candidates(session, candidates, source=source)
                    self._mark_unreachable(session, candidates, subnets=subnets)
                    session.commit()
                    ended_at = scan_run.ended_at
                    self._set_state(
                        running=False,
                        current_scan_id=None,
                        last_finished_at=ended_at,
                    )
                    return scan_run
            except Exception as exc:
                with self.session_factory() as session:
                    scan_run = session.get(DiscoveryScanRun, scan_id)
                    if scan_run is not None:
                        scan_run.status = "FAILED"
                        scan_run.error_count = 1
                        scan_run.message = str(exc)
                        scan_run.ended_at = utc_now()
                    session.commit()
                self._set_state(running=False, current_scan_id=None, last_finished_at=utc_now())
                raise

    def status(self) -> DiscoveryState:
        with self._state_lock:
            return self._state

    def _loop(self) -> None:
        if self.stop_event.wait(settings.auto_discovery_startup_delay_seconds):
            return
        while not self.stop_event.is_set():
            try:
                self.run_scan(source="AUTO")
            except Exception:
                pass
            if self.stop_event.wait(settings.auto_discovery_interval_seconds):
                return

    def _manual_scan_rate_limited(self) -> bool:
        now = utc_now()
        last = self._last_manual_scan_started_at
        if last and (now - last).total_seconds() < settings.manual_rescan_min_interval_seconds:
            return True
        self._last_manual_scan_started_at = now
        return False

    def _set_state(
        self,
        *,
        running: bool | None = None,
        current_scan_id: int | None = None,
        last_started_at: datetime | None = None,
        last_finished_at: datetime | None = None,
    ) -> None:
        with self._state_lock:
            self._state = DiscoveryState(
                running=self._state.running if running is None else running,
                current_scan_id=current_scan_id,
                last_started_at=last_started_at or self._state.last_started_at,
                last_finished_at=last_finished_at or self._state.last_finished_at,
            )

    def _estimated_target_count(self, subnets: list[str] | None) -> int:
        if subnets:
            return len(subnets) * settings.scan_max_hosts_per_subnet
        try:
            return len(self.scanner.discover_subnets()) * settings.scan_max_hosts_per_subnet
        except Exception:
            return 0

    def _upsert_candidates(
        self,
        session: Session,
        candidates: Iterable[ScanCandidate],
        *,
        source: str,
    ) -> None:
        now = utc_now()
        configured = {
            (device.ip, device.port): device.device_id
            for device in session.scalars(select(Device).order_by(Device.id.asc()))
        }
        for candidate in candidates:
            row = session.scalar(
                select(DeviceDiscoveryResult).where(
                    DeviceDiscoveryResult.ip == candidate.ip,
                    DeviceDiscoveryResult.port == candidate.port,
                )
            )
            if row is None:
                row = DeviceDiscoveryResult(
                    ip=candidate.ip,
                    port=candidate.port,
                    first_seen=now,
                )
                session.add(row)
            row.subnet = candidate.subnet or row.subnet
            row.interface_name = candidate.interface_name or row.interface_name
            row.source = source
            row.last_seen = now
            row.last_checked_at = now
            row.consecutive_failures = 0
            row.last_error = None
            row.updated_at = now
            configured_device_id = configured.get((candidate.ip, candidate.port))
            if configured_device_id:
                row.status = "CONFIGURED"
                row.configured_device_id = configured_device_id
            elif row.status != "IGNORED":
                row.status = "NEEDS_COMM_KEY"
                row.configured_device_id = None

    def _mark_unreachable(
        self,
        session: Session,
        candidates: Iterable[ScanCandidate],
        *,
        subnets: list[str] | None,
    ) -> None:
        found = {(candidate.ip, candidate.port) for candidate in candidates}
        now = utc_now()
        scanned_networks = []
        if subnets:
            scanned_networks = [ipaddress.ip_network(item, strict=False) for item in subnets]
        rows = session.scalars(
            select(DeviceDiscoveryResult).where(DeviceDiscoveryResult.port == settings.scan_port)
        ).all()
        for row in rows:
            if (row.ip, row.port) in found or row.status in {"CONFIGURED", "IGNORED"}:
                continue
            if scanned_networks:
                row_ip = ipaddress.ip_address(row.ip)
                if not any(row_ip in network for network in scanned_networks):
                    continue
            row.status = "UNREACHABLE"
            row.last_checked_at = now
            row.consecutive_failures += 1
            row.updated_at = now


discovery_service = DiscoveryService()
